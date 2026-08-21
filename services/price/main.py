"""
===========================================================================
 MediWatt - PRICE SERVICE
---------------------------------------------------------------------------
 Job in one sentence:
    "I go out to the public internet, fetch today's real Swedish electricity
     spot prices, tidy them up into 24 hourly numbers, and serve them over
     my own REST API."

 This service is the assignment's "programmatically connect to and use a
 REST API" requirement. It is an HTTP *client* of somebody else's API
 (elprisetjustnu.se) and an HTTP *server* of its own API at the same time.

 Two cloud patterns live in here:
   * Cache-Aside      - we keep the answer in memory for 15 minutes instead
                        of hammering the upstream API on every request.
   * Circuit Breaker / Graceful Degradation
                      - if the upstream API is slow or down, we do not crash
                        and we do not block the whole hospital dashboard.
                        We serve a stale cache, or a modelled fallback curve,
                        and we clearly label which one we used.
===========================================================================
"""

import os
import socket
import logging
import json
import math
import time
from datetime import datetime, timedelta, timezone

import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse

# --------------------------------------------------------------------------
# Logging: one JSON object per line, same style as the Node services.
# --------------------------------------------------------------------------
POD = os.environ.get("POD_NAME", socket.gethostname())
SERVICE = "price"


class JsonFormatter(logging.Formatter):
    def format(self, record):
        return json.dumps({
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname.lower(),
            "service": SERVICE,
            "pod": POD,
            "message": record.getMessage(),
        })


handler = logging.StreamHandler()
handler.setFormatter(JsonFormatter())
log = logging.getLogger(SERVICE)
log.setLevel(logging.INFO)
log.handlers = [handler]
log.propagate = False

# --------------------------------------------------------------------------
# Configuration - all from environment variables, never hard-coded.
# --------------------------------------------------------------------------
UPSTREAM_BASE = os.environ.get("PRICE_API_BASE", "https://www.elprisetjustnu.se/api/v1/prices")
DEFAULT_AREA = os.environ.get("PRICE_AREA", "SE4")          # SE4 = southern Sweden (Blekinge)
CACHE_TTL_SECONDS = int(os.environ.get("CACHE_TTL_SECONDS", "900"))   # 15 minutes
UPSTREAM_TIMEOUT = float(os.environ.get("UPSTREAM_TIMEOUT", "6.0"))

STOCKHOLM = timezone(timedelta(hours=2))  # replaced per-record by the API's own offset

app = FastAPI(
    title="MediWatt Price Service",
    description="Serves hourly electricity spot prices for a Swedish bidding area.",
    version="1.0.0",
)

# The cache. Key = bidding area, value = (stored_at_epoch, payload dict).
_cache: dict[str, tuple[float, dict]] = {}

# Simple counters so we can prove in the demo that caching is working.
_stats = {"upstream_calls": 0, "cache_hits": 0, "fallbacks": 0, "upstream_failures": 0}


# ==========================================================================
#  Fallback price curve
# ==========================================================================
def modelled_curve(area: str) -> list[float]:
    """
    If the real API is unreachable we still have to give the optimizer
    *something* to work with, otherwise the whole dashboard goes blank.

    This is a modelled Nordic day-ahead shape: cheap at night, two peaks
    (morning and early evening), expensive around 07-09 and 17-20.
    Values are SEK per kWh.
    """
    shape = [0.42, 0.36, 0.33, 0.32, 0.35, 0.48, 0.79, 1.28,
             1.44, 1.21, 0.98, 0.86, 0.81, 0.83, 0.88, 1.02,
             1.31, 1.62, 1.55, 1.24, 0.95, 0.74, 0.58, 0.47]
    # Give each area a slightly different level so the demo looks alive.
    level = {"SE1": 0.55, "SE2": 0.60, "SE3": 0.92, "SE4": 1.00}.get(area, 1.0)
    return [round(v * level, 5) for v in shape]


# ==========================================================================
#  Upstream fetch
# ==========================================================================
async def fetch_upstream(area: str) -> dict:
    """
    Call the public elprisetjustnu.se REST API and reduce whatever it gives
    us to exactly 24 hourly averages.

    Important detail: that API changed from 60-minute to 15-minute
    resolution, so a day can contain 24 OR 96 entries. We handle both by
    averaging every record into the hour bucket it belongs to. Writing the
    client this way means an upstream format change does not break us.
    """
    today = datetime.now(timezone(timedelta(hours=2)))
    url = f"{UPSTREAM_BASE}/{today.year}/{today.month:02d}-{today.day:02d}_{area}.json"

    _stats["upstream_calls"] += 1
    async with httpx.AsyncClient(timeout=UPSTREAM_TIMEOUT) as client:
        resp = await client.get(url, headers={"User-Agent": "MediWatt/1.0 (BTH cloud computing coursework)"})
        resp.raise_for_status()
        raw = resp.json()

    if not isinstance(raw, list) or not raw:
        raise ValueError("upstream returned an unexpected shape")

    buckets: dict[int, list[float]] = {h: [] for h in range(24)}
    for record in raw:
        start = datetime.fromisoformat(record["time_start"])
        buckets[start.hour].append(float(record["SEK_per_kWh"]))

    hourly = []
    last_known = None
    for h in range(24):
        if buckets[h]:
            value = sum(buckets[h]) / len(buckets[h])
            last_known = value
        else:
            # Rare gap (e.g. a daylight-saving jump). Carry the previous hour.
            value = last_known if last_known is not None else 0.0
        hourly.append(round(value, 5))

    return {
        "area": area,
        "date": today.strftime("%Y-%m-%d"),
        "currency": "SEK/kWh",
        "source": "elprisetjustnu.se",
        "resolution_records": len(raw),
        "hourly": hourly,
    }


def build_payload(area: str, hourly: list[float], source: str, stale: bool = False) -> dict:
    lowest = min(hourly)
    highest = max(hourly)
    average = sum(hourly) / len(hourly)
    return {
        "pod": POD,
        "area": area,
        "currency": "SEK/kWh",
        "source": source,
        "stale": stale,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "hourly": hourly,
        "stats": {
            "min": round(lowest, 5),
            "max": round(highest, 5),
            "avg": round(average, 5),
            "spread_pct": round((highest - lowest) / average * 100, 1) if average else 0.0,
            "cheapest_hour": hourly.index(lowest),
            "most_expensive_hour": hourly.index(highest),
        },
    }


async def get_prices(area: str) -> dict:
    """Cache-aside: look in the cache first, only then go to the network."""
    now = time.time()
    cached = _cache.get(area)

    if cached and (now - cached[0]) < CACHE_TTL_SECONDS:
        _stats["cache_hits"] += 1
        payload = dict(cached[1])
        payload["pod"] = POD
        payload["cached"] = True
        return payload

    try:
        upstream = await fetch_upstream(area)
        payload = build_payload(area, upstream["hourly"], upstream["source"])
        payload["resolution_records"] = upstream["resolution_records"]
        payload["cached"] = False
        _cache[area] = (now, payload)
        log.info(f"fetched live prices for {area}: {upstream['resolution_records']} records -> 24 hours")
        return payload

    except Exception as exc:  # noqa: BLE001 - we deliberately catch everything
        _stats["upstream_failures"] += 1
        log.warning(f"upstream price API failed for {area}: {exc}")

        # Degrade gracefully, best option first.
        if cached:
            payload = dict(cached[1])
            payload["pod"] = POD
            payload["stale"] = True
            payload["cached"] = True
            payload["degraded_reason"] = str(exc)
            return payload

        _stats["fallbacks"] += 1
        payload = build_payload(area, modelled_curve(area), "modelled-fallback")
        payload["cached"] = False
        payload["degraded_reason"] = str(exc)
        return payload


# ==========================================================================
#  HEALTH ENDPOINTS
# ==========================================================================
@app.get("/healthz")
async def healthz():
    """Am I alive? If this fails, Kubernetes restarts the pod."""
    return {"status": "alive", "service": SERVICE, "pod": POD}


@app.get("/readyz")
async def readyz():
    """
    Can I do useful work? Yes - always. Even with no internet I can serve
    the modelled fallback curve, so this service is never truly 'unready'.
    That is a deliberate design decision, not an oversight.
    """
    return {"status": "ready", "service": SERVICE, "pod": POD, "stats": _stats}


# ==========================================================================
#  REST API
# ==========================================================================
@app.get("/api/prices")
async def prices(area: str = Query(default=DEFAULT_AREA, pattern="^SE[1-4]$")):
    """
    Today's electricity price, one number per hour, 00:00 to 23:00.

    Example:  GET /api/prices?area=SE4
    """
    return await get_prices(area)


@app.get("/api/prices/cheapest-window")
async def cheapest_window(
    hours: int = Query(default=3, ge=1, le=12),
    area: str = Query(default=DEFAULT_AREA, pattern="^SE[1-4]$"),
):
    """
    Find the cheapest run of N consecutive hours in the day.

    This is what the optimizer uses to decide when to run the laundry.
    """
    payload = await get_prices(area)
    hourly = payload["hourly"]

    best_start, best_cost = 0, math.inf
    for start in range(0, 24 - hours + 1):
        window_cost = sum(hourly[start:start + hours])
        if window_cost < best_cost:
            best_cost, best_start = window_cost, start

    average_price = best_cost / hours
    day_average = payload["stats"]["avg"]

    return {
        "pod": POD,
        "area": area,
        "source": payload["source"],
        "window_hours": hours,
        "start_hour": best_start,
        "end_hour": best_start + hours,
        "label": f"{best_start:02d}:00-{best_start + hours:02d}:00",
        "avg_price": round(average_price, 5),
        "day_avg_price": day_average,
        "cheaper_than_average_pct": round((day_average - average_price) / day_average * 100, 1) if day_average else 0.0,
    }


@app.get("/api/stats")
async def stats():
    """Cache and upstream counters - handy to show during the demo."""
    return {"pod": POD, "cache_ttl_seconds": CACHE_TTL_SECONDS, "cached_areas": list(_cache.keys()), **_stats}


@app.exception_handler(Exception)
async def unhandled(request, exc):
    log.error(f"unhandled error on {request.url.path}: {exc}")
    return JSONResponse(status_code=500, content={"error": "internal error", "pod": POD})


@app.on_event("startup")
async def startup():
    log.info(f"Price service started. Upstream={UPSTREAM_BASE} area={DEFAULT_AREA} ttl={CACHE_TTL_SECONDS}s")
