// ===========================================================================
//  MediWatt - INGEST SERVICE
// ---------------------------------------------------------------------------
//  Job in one sentence:
//     "I am the only service allowed to talk to the database. I take energy
//      meter readings in, I store them, and I hand them back out over REST."
//
//  This is the classic microservice rule: one service owns one database.
//  Nobody else gets to touch MongoDB - they have to ask me over HTTP.
//  That pattern is called "Database per Service".
// ===========================================================================

const express = require('express');
const { MongoClient } = require('mongodb');
const os = require('os');
const { ZONES, PROFILES, ZONE_PROFILE } = require('./zones');

const app = express();
app.use(express.json({ limit: '1mb' }));

const PORT = process.env.PORT || 8080;

// The pod name. Kubernetes injects this for us (see the YAML file).
// We echo it back in every response so the dashboard can prove that
// requests are being spread across several running copies of this service.
const POD = process.env.POD_NAME || os.hostname();

// --- Database connection -------------------------------------------------
// The username, password and host all come from environment variables.
// They are NEVER written in the source code. In Kubernetes they arrive
// from a Secret and a ConfigMap.
const MONGO_USER = process.env.MONGO_USER || 'mediwatt';
const MONGO_PASS = process.env.MONGO_PASSWORD || 'devpassword';
const MONGO_HOST = process.env.MONGO_HOST || 'localhost:27017';
const MONGO_DB   = process.env.MONGO_DB || 'mediwatt';

const MONGO_URI = `mongodb://${encodeURIComponent(MONGO_USER)}:${encodeURIComponent(MONGO_PASS)}@${MONGO_HOST}/?authSource=admin`;

let db = null;
let dbReady = false;

async function connectToMongo() {
  // Kubernetes may start this service before MongoDB is ready.
  // So we retry forever instead of crashing. This is the
  // "Retry with backoff" cloud pattern.
  let attempt = 0;
  while (true) {
    attempt++;
    try {
      const client = new MongoClient(MONGO_URI, {
        serverSelectionTimeoutMS: 3000,
        maxPoolSize: 20
      });
      await client.connect();
      db = client.db(MONGO_DB);
      await db.command({ ping: 1 });

      // An index makes "give me the last 24 hours for this zone" fast.
      await db.collection('readings').createIndex({ zoneId: 1, ts: -1 });
      await db.collection('readings').createIndex({ ts: -1 });

      dbReady = true;
      log('info', `Connected to MongoDB at ${MONGO_HOST} (attempt ${attempt})`);
      return;
    } catch (err) {
      dbReady = false;
      const wait = Math.min(attempt * 2, 15);
      log('warn', `MongoDB not ready (${err.message}). Retrying in ${wait}s...`);
      await new Promise(r => setTimeout(r, wait * 1000));
    }
  }
}

// --- Time handling --------------------------------------------------------
// The hospital is in Sweden, and the electricity price API also reports
// Swedish local time. So every hour bucket in this whole system means
// "hour of the day in Europe/Stockholm". We compute it explicitly rather
// than relying on the container's clock settings, so it is always correct.
const HOUR_FMT = new Intl.DateTimeFormat('en-GB', {
  timeZone: 'Europe/Stockholm',
  hour: '2-digit',
  hour12: false
});

function stockholmHour(date) {
  return parseInt(HOUR_FMT.format(date), 10) % 24;
}

// --- Structured logging ---------------------------------------------------
// We print one JSON object per line. This is what real cloud systems do,
// because a log collector can then parse it automatically.
function log(level, message, extra = {}) {
  console.log(JSON.stringify({
    ts: new Date().toISOString(),
    level,
    service: 'ingest',
    pod: POD,
    message,
    ...extra
  }));
}

// Log every incoming request, with how long it took.
app.use((req, res, next) => {
  const started = Date.now();
  res.on('finish', () => {
    log('info', 'request', {
      method: req.method,
      path: req.originalUrl,
      status: res.statusCode,
      ms: Date.now() - started
    });
  });
  next();
});

// ===========================================================================
//  HEALTH ENDPOINTS
//  Kubernetes calls these constantly.
//    /healthz  = "are you alive?"   If this fails, Kubernetes restarts the pod.
//    /readyz   = "can you work?"    If this fails, Kubernetes stops sending
//                                    traffic to the pod but leaves it running.
// ===========================================================================
app.get('/healthz', (req, res) => res.json({ status: 'alive', service: 'ingest', pod: POD }));

app.get('/readyz', (req, res) => {
  if (!dbReady) {
    return res.status(503).json({ status: 'not-ready', reason: 'database unavailable', pod: POD });
  }
  res.json({ status: 'ready', service: 'ingest', pod: POD });
});

// ===========================================================================
//  REST API
// ===========================================================================

// ---- List the hospital's metered zones ------------------------------------
// GET /api/zones
app.get('/api/zones', (req, res) => {
  res.json({ pod: POD, count: ZONES.length, zones: ZONES });
});

// ---- Store one meter reading ----------------------------------------------
// POST /api/readings   { "zoneId": "icu", "kwh": 148.2, "ts": "2026-08-21T09:00:00Z" }
//
// This is the endpoint a real smart meter (or an IoT gateway) would call.
app.post('/api/readings', async (req, res) => {
  if (!dbReady) return res.status(503).json({ error: 'database unavailable' });

  const { zoneId, kwh, ts } = req.body || {};

  // --- Input validation. Never trust what arrives over the network. -------
  const zone = ZONES.find(z => z.zoneId === zoneId);
  if (!zone) {
    return res.status(400).json({ error: 'unknown zoneId', validZones: ZONES.map(z => z.zoneId) });
  }
  const value = Number(kwh);
  if (!Number.isFinite(value) || value < 0 || value > 100000) {
    return res.status(400).json({ error: 'kwh must be a number between 0 and 100000' });
  }
  const when = ts ? new Date(ts) : new Date();
  if (isNaN(when.getTime())) {
    return res.status(400).json({ error: 'ts must be a valid ISO-8601 timestamp' });
  }

  const doc = {
    zoneId,
    kwh: value,
    ts: when,
    hour: stockholmHour(when),
    receivedBy: POD
  };

  await db.collection('readings').insertOne(doc);
  log('info', 'reading stored', { zoneId, kwh: value });

  res.status(201).json({ stored: true, pod: POD, reading: doc });
});

// ---- Read raw readings back -----------------------------------------------
// GET /api/readings?zone=icu&limit=50
app.get('/api/readings', async (req, res) => {
  if (!dbReady) return res.status(503).json({ error: 'database unavailable' });

  const filter = {};
  if (req.query.zone) filter.zoneId = String(req.query.zone);

  const limit = Math.min(Math.max(parseInt(req.query.limit, 10) || 100, 1), 1000);

  const rows = await db.collection('readings')
    .find(filter)
    .sort({ ts: -1 })
    .limit(limit)
    .toArray();

  res.json({ pod: POD, count: rows.length, readings: rows });
});

// ---- The important one: a 24-hour summary per zone -------------------------
// GET /api/summary
//
// The optimizer service calls this. It returns, for every zone, how many kWh
// were used in each of the last 24 hours.
app.get('/api/summary', async (req, res) => {
  if (!dbReady) return res.status(503).json({ error: 'database unavailable' });

  const since = new Date(Date.now() - 24 * 60 * 60 * 1000);

  // MongoDB aggregation pipeline: group readings by zone and by hour,
  // then add up the kWh in each bucket.
  const rows = await db.collection('readings').aggregate([
    { $match: { ts: { $gte: since } } },
    { $group: { _id: { zoneId: '$zoneId', hour: '$hour' }, kwh: { $sum: '$kwh' } } },
    { $sort: { '_id.hour': 1 } }
  ]).toArray();

  // Reshape into something friendly: one object per zone with a 24-slot array.
  const byZone = {};
  for (const z of ZONES) {
    byZone[z.zoneId] = {
      zoneId: z.zoneId,
      name: z.name,
      critical: z.critical,
      shiftable: z.shiftable,
      baselineKw: z.baselineKw,
      description: z.description,
      hourlyKwh: new Array(24).fill(0),
      totalKwh: 0
    };
  }
  for (const r of rows) {
    const z = byZone[r._id.zoneId];
    if (!z) continue;
    z.hourlyKwh[r._id.hour] = Math.round(r.kwh * 100) / 100;
  }
  for (const z of Object.values(byZone)) {
    z.totalKwh = Math.round(z.hourlyKwh.reduce((a, b) => a + b, 0) * 100) / 100;
  }

  const zones = Object.values(byZone);
  const totalKwh = Math.round(zones.reduce((a, z) => a + z.totalKwh, 0) * 100) / 100;

  res.json({
    pod: POD,
    windowHours: 24,
    generatedAt: new Date().toISOString(),
    totalKwh,
    zones
  });
});

// ---- Seed 24 hours of realistic demo data ----------------------------------
// POST /api/simulate
//
// A real deployment gets this data from physical meters. For the demo we
// generate a realistic day so there is something to optimize.
app.post('/api/simulate', async (req, res) => {
  if (!dbReady) return res.status(503).json({ error: 'database unavailable' });

  await db.collection('readings').deleteMany({});

  const now = new Date();
  const docs = [];

  for (const zone of ZONES) {
    const profile = PROFILES[ZONE_PROFILE[zone.zoneId]];
    for (let hoursAgo = 23; hoursAgo >= 0; hoursAgo--) {
      const t = new Date(now.getTime() - hoursAgo * 60 * 60 * 1000);
      const hour = stockholmHour(t);
      // baseline power x profile shape x a little random noise
      const noise = 0.94 + Math.random() * 0.12;
      const kwh = zone.baselineKw * profile[hour] * noise;
      docs.push({
        zoneId: zone.zoneId,
        kwh: Math.round(kwh * 100) / 100,
        ts: t,
        hour,
        receivedBy: POD,
        simulated: true
      });
    }
  }

  // --- Inject two realistic equipment faults -------------------------------
  // Without a fault in the data the anomaly detector has nothing to find, and
  // the maintenance feature cannot be demonstrated. These are the two faults
  // hospital estates teams actually see most often. Turn them off with
  // POST /api/simulate?faults=0
  const withFaults = req.query.faults !== '0';
  const faults = [];
  if (withFaults) {
    // A chiller that is short-cycling overnight: it should be idling, instead
    // it keeps restarting and draws close to full power at 03:00.
    const hvacNight = docs.find(d => d.zoneId === 'hvac' && d.hour === 3);
    if (hvacNight) {
      hvacNight.kwh = Math.round(hvacNight.kwh * 3.6 * 100) / 100;
      hvacNight.fault = 'chiller-short-cycling';
      faults.push({ zoneId: 'hvac', hour: 3, fault: 'chiller short-cycling' });
    }
    // A ward air-handling unit with a stuck damper, running flat out at 01:00.
    const wardNight = docs.find(d => d.zoneId === 'wards' && d.hour === 1);
    if (wardNight) {
      wardNight.kwh = Math.round(wardNight.kwh * 2.6 * 100) / 100;
      wardNight.fault = 'ahu-damper-stuck-open';
      faults.push({ zoneId: 'wards', hour: 1, fault: 'AHU damper stuck open' });
    }
  }

  await db.collection('readings').insertMany(docs);
  log('info', 'simulated 24h of meter data', { documents: docs.length, faultsInjected: faults.length });

  res.status(201).json({
    seeded: true, pod: POD, documents: docs.length, zones: ZONES.length, faults
  });
});

// ---- Anything else --------------------------------------------------------
app.use((req, res) => res.status(404).json({ error: 'not found', path: req.originalUrl }));

// Catch-all error handler so one bad request can never kill the process.
app.use((err, req, res, next) => {
  log('error', 'unhandled error', { error: err.message });
  res.status(500).json({ error: 'internal error' });
});

// ===========================================================================
//  START UP
// ===========================================================================
app.listen(PORT, () => {
  log('info', `Ingest service listening on port ${PORT}`);
  connectToMongo();
});

// Graceful shutdown: when Kubernetes wants to remove this pod (for example
// when scaling down) it sends SIGTERM. We finish what we are doing and exit
// cleanly instead of dropping requests on the floor.
process.on('SIGTERM', () => {
  log('info', 'SIGTERM received, shutting down gracefully');
  process.exit(0);
});
