"""
===========================================================================
 MediMatrx - FORECAST SERVICE
---------------------------------------------------------------------------
 Job in one sentence:
    "Everything else in this platform tells you about yesterday. I tell you
     what to do tomorrow."

 Why this service exists
 -----------------------
 A report card is not a product. An estates manager cannot act on
 "you could have saved 3,400 kr yesterday". They can act on "here is
 tomorrow's plan, approve it".

 To plan tomorrow you need two things nobody has yet:

   1. TOMORROW'S PRICES. Nordic day-ahead prices are published in the early
      afternoon for the following day. Before publication they genuinely do
      not exist, and this service says so rather than inventing them.

   2. TOMORROW'S LOAD. Which is mostly a question of weather. A hospital's
      HVAC plant is the single biggest deferrable load, and how hard it works
      is largely a function of outdoor temperature. So we fetch the forecast
      and adjust.

 The weather model is deliberately simple and explainable
 -------------------------------------------------------
 Degree-hours, not machine learning. For each hour we compute how far the
 temperature sits outside the comfort band, then scale tomorrow's HVAC load
 against today's by the ratio of those numbers.

 An estates engineer can check this on paper in about a minute. That matters
 more than accuracy here: a hospital will not act on a number it cannot
 interrogate, and every competitor in this market ships a black box.

 The plan itself is NOT computed here. We post the predicted load and the
 predicted prices to the optimizer's scenario endpoint, so tomorrow's plan
 and today's report come out of the exact same engine and can never drift
 apart in their logic.
===========================================================================
"""

import os
import socket
import json
import logging
import asyncio
import time
from datetime import datetime, timedelta, timezone

import httpx
from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse

POD = os.environ.get("POD_NAME", socket.gethostname())
SERVICE = "forecast"


class JsonFormatter(logging.Formatter):
    def format(self, record):
        return json.dumps({
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname.lower(),
            "service": SERVICE, "pod": POD,
            "message": record.getMessage(),
        })


handler = logging.StreamHandler()
handler.setFormatter(JsonFormatter())
log = logging.getLogger(SERVICE)
log.setLevel(logging.INFO)
log.handlers = [handler]
log.propagate = False

INGEST_URL = os.environ.get("INGEST_URL", "http://localhost:8081")
PRICE_URL = os.environ.get("PRICE_URL", "http://localhost:8082")
OPTIMIZER_URL = os.environ.get("OPTIMIZER_URL", "http://localhost:8083")
HTTP_TIMEOUT = float(os.environ.get("HTTP_TIMEOUT", "8.0"))

# Open-Meteo needs no API key at all, which is why it is the default.
WEATHER_API = os.environ.get(
    "WEATHER_API", "https://api.open-meteo.com/v1/forecast")
SITE_LAT = float(os.environ.get("SITE_LAT", "56.1612"))    # Karlskrona
SITE_LON = float(os.environ.get("SITE_LON", "15.5869"))
SITE_NAME = os.environ.get("SITE_NAME", "Blekinge Regional Hospital")
WEATHER_TTL = int(os.environ.get("WEATHER_TTL_SECONDS", "3600"))

# ---------------------------------------------------------------------------
# The comfort band. Outside it, the HVAC plant has to work.
#   above COOLING_BASE  -> chillers run
#   below HEATING_BASE  -> heating runs
# The sensitivities say how much extra HVAC load one degree-hour causes,
# as a fraction of the zone's normal load. Cooling costs more than heating
# per degree because chillers are the expensive part of the plant.
# ---------------------------------------------------------------------------
COOLING_BASE_C = float(os.environ.get("COOLING_BASE_C", "18.0"))
HEATING_BASE_C = float(os.environ.get("HEATING_BASE_C", "15.0"))
COOLING_SENSITIVITY = float(os.environ.get("COOLING_SENSITIVITY", "0.075"))
HEATING_SENSITIVITY = float(os.environ.get("HEATING_SENSITIVITY", "0.045"))

# Zones whose load actually responds to outdoor temperature. Everything else
# is driven by the clinical timetable, not the weather.
WEATHER_SENSITIVE = {"hvac": 1.0, "wards": 0.25, "theatres": 0.20,
                     "imaging": 0.15, "admin": 0.30}

app = FastAPI(
    title="MediMatrx Forecast Service",
    description="Predicts tomorrow's load and produces tomorrow's operating plan.",
    version="1.0.0",
)

_weather_cache: tuple[float, dict] | None = None
_stats = {"weather_calls": 0, "weather_cache_hits": 0,
          "weather_failures": 0, "plans": 0}


# ==========================================================================
#  WEATHER
# ==========================================================================
def seasonal_fallback(day_offset: int) -> list[float]:
    """
    If Open-Meteo cannot be reached we still have to produce a plan. This is
    a climatological day for southern Sweden: a sine curve between a seasonal
    minimum and maximum, coldest around 05:00 and warmest around 15:00.

    It is clearly labelled as modelled wherever it is used. An approximate
    plan beats a blank screen, but only if nobody is misled about which they
    are looking at.
    """
    import math
    day = (datetime.now(timezone.utc) + timedelta(days=day_offset)).timetuple().tm_yday
    # Seasonal mean for Blekinge: about 1 C in January, 18 C in July.
    seasonal_mean = 9.5 - 8.5 * math.cos(2 * math.pi * (day - 15) / 365)
    swing = 4.0
    # +cos peaks where the argument is zero, so this is warmest at 15:00 and
    # coldest around 03:00. Getting this sign wrong inverts the whole day.
    return [round(seasonal_mean + swing * math.cos(2 * math.pi * (h - 15) / 24), 2)
            for h in range(24)]


async def fetch_weather() -> dict:
    """
    Two days of hourly temperature: today (the reference) and tomorrow (the
    thing we are planning). Cached for an hour, because a forecast does not
    change faster than that and hammering a free service would be rude.
    """
    global _weather_cache
    now = time.time()
    if _weather_cache and (now - _weather_cache[0]) < WEATHER_TTL:
        _stats["weather_cache_hits"] += 1
        out = dict(_weather_cache[1]); out["cached"] = True
        return out

    params = {
        "latitude": SITE_LAT, "longitude": SITE_LON,
        "hourly": "temperature_2m", "forecast_days": 2,
        "timezone": "Europe/Stockholm",
    }
    try:
        _stats["weather_calls"] += 1
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as c:
            r = await c.get(WEATHER_API, params=params,
                            headers={"User-Agent": "MediMatrx/1.1"})
            r.raise_for_status()
            raw = r.json()

        temps = raw["hourly"]["temperature_2m"]
        if len(temps) < 48:
            raise ValueError(f"expected 48 hourly values, got {len(temps)}")

        payload = {
            "source": "open-meteo.com", "modelled": False,
            "today": [float(v) for v in temps[:24]],
            "tomorrow": [float(v) for v in temps[24:48]],
            "site": SITE_NAME, "lat": SITE_LAT, "lon": SITE_LON,
            "fetched_at": datetime.now(timezone.utc).isoformat(),
        }
        _weather_cache = (now, payload)
        log.info(f"weather fetched for {SITE_NAME}: tomorrow "
                 f"{min(payload['tomorrow']):.1f} to {max(payload['tomorrow']):.1f} C")
        out = dict(payload); out["cached"] = False
        return out

    except Exception as exc:  # noqa: BLE001
        _stats["weather_failures"] += 1
        log.warning(f"weather API unreachable, using seasonal model: {exc}")
        if _weather_cache:
            out = dict(_weather_cache[1])
            out.update(cached=True, stale=True, degraded_reason=str(exc))
            return out
        return {
            "source": "seasonal-model", "modelled": True,
            "today": seasonal_fallback(0), "tomorrow": seasonal_fallback(1),
            "site": SITE_NAME, "lat": SITE_LAT, "lon": SITE_LON,
            "degraded_reason": str(exc), "cached": False,
            "fetched_at": datetime.now(timezone.utc).isoformat(),
        }


def degree_hours(temps: list[float]) -> dict:
    cooling = [max(0.0, t - COOLING_BASE_C) for t in temps]
    heating = [max(0.0, HEATING_BASE_C - t) for t in temps]
    return {"cooling": cooling, "heating": heating,
            "coolingTotal": round(sum(cooling), 1),
            "heatingTotal": round(sum(heating), 1)}


def weather_factor(dh: dict, hour: int) -> float:
    """How much harder than baseline the plant works in this hour."""
    return (1.0
            + COOLING_SENSITIVITY * dh["cooling"][hour]
            + HEATING_SENSITIVITY * dh["heating"][hour])


# ==========================================================================
#  PREDICTING TOMORROW'S LOAD
# ==========================================================================
def predict(summary: dict, weather: dict) -> tuple[list[dict], dict]:
    dh_today = degree_hours(weather["today"])
    dh_tomorrow = degree_hours(weather["tomorrow"])

    zones, notes = [], []
    for z in summary["zones"]:
        sensitivity = WEATHER_SENSITIVE.get(z["zoneId"], 0.0)
        predicted, ratios = [], []

        for h in range(24):
            actual = z["hourlyKwh"][h]
            if sensitivity == 0.0 or actual <= 0:
                predicted.append(round(actual, 2))
                continue
            ratio = weather_factor(dh_tomorrow, h) / weather_factor(dh_today, h)
            # A weather model should nudge a forecast, never dominate it.
            ratio = max(0.6, min(1.7, ratio))
            scaled = 1.0 + (ratio - 1.0) * sensitivity
            predicted.append(round(actual * scaled, 2))
            ratios.append(scaled)

        change = (sum(predicted) / sum(z["hourlyKwh"]) - 1) * 100 if sum(z["hourlyKwh"]) else 0
        if abs(change) >= 1.0:
            notes.append({"zoneId": z["zoneId"], "zone": z["name"],
                          "changePct": round(change, 1)})

        zones.append({
            "zoneId": z["zoneId"], "name": z["name"],
            "critical": z["critical"], "shiftable": z["shiftable"],
            "baselineKw": z["baselineKw"], "description": z.get("description", ""),
            "hourlyKwh": predicted,
        })

    total_today = sum(sum(z["hourlyKwh"]) for z in summary["zones"])
    total_tomorrow = sum(sum(z["hourlyKwh"]) for z in zones)

    explanation = {
        "method": "degree-hour scaling against today's measured load",
        "coolingBaseC": COOLING_BASE_C, "heatingBaseC": HEATING_BASE_C,
        "todayDegreeHours": {"cooling": dh_today["coolingTotal"],
                             "heating": dh_today["heatingTotal"]},
        "tomorrowDegreeHours": {"cooling": dh_tomorrow["coolingTotal"],
                                "heating": dh_tomorrow["heatingTotal"]},
        "todayTempRange": [min(weather["today"]), max(weather["today"])],
        "tomorrowTempRange": [min(weather["tomorrow"]), max(weather["tomorrow"])],
        "zonesAdjusted": notes,
        "totalTodayKwh": round(total_today, 1),
        "totalPredictedKwh": round(total_tomorrow, 1),
        "predictedChangePct": round(
            (total_tomorrow / total_today - 1) * 100, 1) if total_today else 0.0,
    }
    return zones, explanation


# ==========================================================================
#  HEALTH
# ==========================================================================
@app.get("/healthz")
async def healthz():
    return {"status": "alive", "service": SERVICE, "pod": POD}


@app.get("/readyz")
async def readyz():
    return {"status": "ready", "service": SERVICE, "pod": POD,
            "site": SITE_NAME, "stats": _stats}


# ==========================================================================
#  REST API
# ==========================================================================
@app.get("/api/forecast/weather")
async def weather():
    """Tomorrow's hourly temperature, and today's for comparison."""
    w = await fetch_weather()
    w["degreeHours"] = {"today": degree_hours(w["today"]),
                        "tomorrow": degree_hours(w["tomorrow"])}
    w["pod"] = POD
    return w


@app.get("/api/forecast/plan")
async def plan(area: str = Query(default="SE4", pattern="^SE[1-4]$")):
    """
    Tomorrow's operating plan.

    GET /api/forecast/plan?area=SE4
    """
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as c:
        summary_task = c.get(f"{INGEST_URL}/api/summary")
        price_task = c.get(f"{PRICE_URL}/api/prices?area={area}&day=tomorrow")
        weather_task = fetch_weather()
        summary_r, price_r, wx = await asyncio.gather(
            summary_task, price_task, weather_task, return_exceptions=True)

        if isinstance(summary_r, Exception) or summary_r.status_code != 200:
            return JSONResponse(status_code=503, content={
                "error": "consumption data unavailable", "pod": POD,
                "dependency": "ingest"})
        summary = summary_r.json()
        if summary.get("totalKwh", 0) <= 0:
            return JSONResponse(status_code=409, content={
                "error": "no meter data yet",
                "hint": "press Load demo day on the dashboard", "pod": POD})

        if isinstance(price_r, Exception) or price_r.status_code != 200:
            return JSONResponse(status_code=503, content={
                "error": "price data unavailable", "pod": POD,
                "dependency": "price"})
        prices = price_r.json()

        if isinstance(wx, Exception):
            return JSONResponse(status_code=503, content={
                "error": "weather unavailable", "pod": POD})

        zones, explanation = predict(summary, wx)

        # Reuse the optimizer rather than reimplement it.
        plan_r = await c.post(f"{OPTIMIZER_URL}/api/optimize/scenario",
                              json={"zones": zones, "prices": prices["hourly"],
                                    "label": "tomorrow"})
        if plan_r.status_code != 200:
            log.error(f"optimizer scenario failed: {plan_r.status_code} {plan_r.text[:200]}")
            return JSONResponse(status_code=502, content={
                "error": "could not compute the plan", "pod": POD,
                "dependency": "optimizer"})
        result = plan_r.json()

    _stats["plans"] += 1
    tomorrow = (datetime.now(timezone(timedelta(hours=2))) + timedelta(days=1))

    # Be explicit about how much of this is measured and how much is modelled.
    confidence = "high"
    caveats = []
    if wx.get("modelled"):
        confidence = "low"
        caveats.append("Weather is a seasonal model, not a live forecast.")
    elif wx.get("stale"):
        confidence = "medium"
        caveats.append("Weather forecast is from cache.")
    if prices.get("source") != "elprisetjustnu.se":
        confidence = "low"
        caveats.append("Tomorrow's prices are modelled. Day-ahead prices are "
                       "published in the early afternoon; before then they do "
                       "not exist yet.")
    elif prices.get("stale"):
        caveats.append("Prices served from cache.")

    result.update({
        "forecastPod": POD,
        "forDate": tomorrow.strftime("%Y-%m-%d"),
        "site": SITE_NAME,
        "weather": {
            "source": wx["source"], "modelled": wx.get("modelled", False),
            "tomorrow": wx["tomorrow"],
            "tomorrowMin": min(wx["tomorrow"]), "tomorrowMax": max(wx["tomorrow"]),
            "todayMin": min(wx["today"]), "todayMax": max(wx["today"]),
        },
        "priceSource": prices.get("source"),
        "prediction": explanation,
        "confidence": confidence,
        "caveats": caveats,
    })
    log.info(f"plan for {result['forDate']}: "
             f"{result['savings']['dailySek']} SEK, confidence={confidence}")
    return result


@app.on_event("startup")
async def startup():
    log.info(f"Forecast service started. site={SITE_NAME} "
             f"({SITE_LAT},{SITE_LON}) weather={WEATHER_API}")
