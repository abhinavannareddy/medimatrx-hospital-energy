// ===========================================================================
//  MediWatt - API GATEWAY
// ---------------------------------------------------------------------------
//  Job in one sentence:
//     "I am the only door into the system. The web browser talks to me and
//      nobody else. I serve the dashboard, and I forward each API call to
//      whichever internal microservice owns that job."
//
//  This is the "API Gateway" cloud pattern. Why bother?
//    1. The browser needs ONE address, not four.
//    2. The internal services stay private - they are never exposed to the
//       internet, so their attack surface is zero from outside.
//    3. Security work (rate limiting, headers, authentication) is done once,
//       here, instead of being copy-pasted into every service.
//    4. Internal services can be renamed, rewritten or split up without the
//       browser ever noticing.
// ===========================================================================

const express = require('express');
const path = require('path');
const os = require('os');

const app = express();
app.use(express.json({ limit: '256kb' }));
app.disable('x-powered-by');   // do not advertise what we are running

const PORT = process.env.PORT || 8080;
const POD = process.env.POD_NAME || os.hostname();

// Where the internal services live. In Kubernetes these are DNS names that
// the cluster resolves to a load-balanced set of pods.
const INGEST_URL = process.env.INGEST_URL || 'http://localhost:8081';
const PRICE_URL = process.env.PRICE_URL || 'http://localhost:8082';
const OPTIMIZER_URL = process.env.OPTIMIZER_URL || 'http://localhost:8083';
const UPSTREAM_TIMEOUT_MS = parseInt(process.env.UPSTREAM_TIMEOUT_MS || '8000', 10);

function log(level, message, extra = {}) {
  console.log(JSON.stringify({
    ts: new Date().toISOString(), level, service: 'gateway', pod: POD, message, ...extra
  }));
}

// ---------------------------------------------------------------------------
//  SECURITY LAYER 1 - response headers
//  These tell the browser to lock things down. They cost nothing and they
//  block whole families of attack (clickjacking, MIME sniffing, XSS).
// ---------------------------------------------------------------------------
app.use((req, res, next) => {
  res.setHeader('X-Content-Type-Options', 'nosniff');
  res.setHeader('X-Frame-Options', 'DENY');
  res.setHeader('Referrer-Policy', 'no-referrer');
  res.setHeader('Permissions-Policy', 'geolocation=(), microphone=(), camera=()');
  res.setHeader('Content-Security-Policy',
    "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'");
  next();
});

// ---------------------------------------------------------------------------
//  SECURITY LAYER 2 - rate limiting
//  A simple fixed-window counter per client IP. It stops one noisy or
//  malicious client from flooding the hospital's optimizer.
//
//  Honest limitation: this counter lives in the memory of ONE pod, so with
//  several gateway replicas the real limit is (limit x replicas). In
//  production you move this to Redis or to an ingress-level rate limiter.
//  It is documented in the report as a known trade-off.
// ---------------------------------------------------------------------------
const RATE_LIMIT = parseInt(process.env.RATE_LIMIT || '120', 10);  // requests
const RATE_WINDOW_MS = 60 * 1000;                                   // per minute
const hits = new Map();

setInterval(() => hits.clear(), RATE_WINDOW_MS).unref();

app.use('/api', (req, res, next) => {
  const ip = req.ip || 'unknown';
  const count = (hits.get(ip) || 0) + 1;
  hits.set(ip, count);
  res.setHeader('X-RateLimit-Limit', RATE_LIMIT);
  res.setHeader('X-RateLimit-Remaining', Math.max(RATE_LIMIT - count, 0));
  if (count > RATE_LIMIT) {
    log('warn', 'rate limit exceeded', { ip, count });
    return res.status(429).json({ error: 'too many requests', retryAfterSeconds: 60 });
  }
  next();
});

// Request logging
app.use((req, res, next) => {
  const started = Date.now();
  res.on('finish', () => {
    log('info', 'request', {
      method: req.method, path: req.originalUrl,
      status: res.statusCode, ms: Date.now() - started
    });
  });
  next();
});

// ---------------------------------------------------------------------------
//  The proxy helper
//  Forwards a call to an internal service, with a hard timeout so a slow
//  service can never hang the browser. Timeout + clear error = "fail fast",
//  which is what keeps a distributed system usable when one part is sick.
// ---------------------------------------------------------------------------
async function proxy(res, targetUrl, options = {}) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), UPSTREAM_TIMEOUT_MS);
  try {
    const upstream = await fetch(targetUrl, { ...options, signal: controller.signal });
    const body = await upstream.text();
    res.status(upstream.status);
    res.setHeader('Content-Type', upstream.headers.get('content-type') || 'application/json');
    res.setHeader('X-Gateway-Pod', POD);
    res.send(body);
  } catch (err) {
    const timedOut = err.name === 'AbortError';
    log('error', 'upstream call failed', { targetUrl, error: err.message, timedOut });
    res.status(502).json({
      error: timedOut ? 'upstream service timed out' : 'upstream service unavailable',
      target: targetUrl.replace(/\/\/.*@/, '//'),   // never leak credentials
      gatewayPod: POD
    });
  } finally {
    clearTimeout(timer);
  }
}

// ===========================================================================
//  HEALTH
// ===========================================================================
app.get('/healthz', (req, res) => res.json({ status: 'alive', service: 'gateway', pod: POD }));
app.get('/readyz', (req, res) => res.json({ status: 'ready', service: 'gateway', pod: POD }));

// ===========================================================================
//  ROUTES -> INGEST SERVICE
// ===========================================================================
app.get('/api/zones', (req, res) => proxy(res, `${INGEST_URL}/api/zones`));
app.get('/api/summary', (req, res) => proxy(res, `${INGEST_URL}/api/summary`));

app.get('/api/readings', (req, res) => {
  const zone = req.query.zone ? `zone=${encodeURIComponent(req.query.zone)}&` : '';
  const limit = encodeURIComponent(req.query.limit || 50);
  return proxy(res, `${INGEST_URL}/api/readings?${zone}limit=${limit}`);
});

app.post('/api/readings', (req, res) => proxy(res, `${INGEST_URL}/api/readings`, {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify(req.body || {})
}));

app.post('/api/simulate', (req, res) => proxy(res, `${INGEST_URL}/api/simulate`, { method: 'POST' }));

// ===========================================================================
//  ROUTES -> PRICE SERVICE
// ===========================================================================
app.get('/api/prices', (req, res) => {
  const area = encodeURIComponent(req.query.area || 'SE4');
  return proxy(res, `${PRICE_URL}/api/prices?area=${area}`);
});

app.get('/api/prices/cheapest-window', (req, res) => {
  const area = encodeURIComponent(req.query.area || 'SE4');
  const hours = encodeURIComponent(req.query.hours || 3);
  return proxy(res, `${PRICE_URL}/api/prices/cheapest-window?area=${area}&hours=${hours}`);
});

// ===========================================================================
//  ROUTES -> OPTIMIZER SERVICE
// ===========================================================================
app.get('/api/optimize', (req, res) => {
  const area = encodeURIComponent(req.query.area || 'SE4');
  return proxy(res, `${OPTIMIZER_URL}/api/optimize?area=${area}`);
});

app.get('/api/anomalies', (req, res) => proxy(res, `${OPTIMIZER_URL}/api/anomalies`));

// ===========================================================================
//  TOPOLOGY - "who is actually running right now?"
//  Calls every service's health endpoint and reports which pod answered.
//  This is what makes horizontal scaling visible in the demo: scale a
//  service up, refresh, and watch different pod names come back.
// ===========================================================================
app.get('/api/topology', async (req, res) => {
  const targets = [
    { name: 'ingest', url: `${INGEST_URL}/healthz` },
    { name: 'price', url: `${PRICE_URL}/healthz` },
    { name: 'optimizer', url: `${OPTIMIZER_URL}/healthz` }
  ];

  const results = await Promise.all(targets.map(async (t) => {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 3000);
    try {
      const r = await fetch(t.url, { signal: controller.signal });
      const body = await r.json();
      return { service: t.name, up: r.ok, pod: body.pod };
    } catch (err) {
      return { service: t.name, up: false, pod: null, error: err.message };
    } finally {
      clearTimeout(timer);
    }
  }));

  res.json({
    gatewayPod: POD,
    checkedAt: new Date().toISOString(),
    services: [{ service: 'gateway', up: true, pod: POD }, ...results]
  });
});

// ===========================================================================
//  THE DASHBOARD (static files)
// ===========================================================================
app.use(express.static(path.join(__dirname, 'public'), { maxAge: '5m' }));

app.use((req, res) => res.status(404).json({ error: 'not found', path: req.originalUrl }));

app.listen(PORT, () => {
  log('info', `Gateway listening on port ${PORT}`);
  log('info', 'upstreams configured', { INGEST_URL, PRICE_URL, OPTIMIZER_URL });
});

process.on('SIGTERM', () => {
  log('info', 'SIGTERM received, shutting down gracefully');
  process.exit(0);
});
