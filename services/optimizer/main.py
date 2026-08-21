"""
===========================================================================
 MediWatt - OPTIMIZER SERVICE
---------------------------------------------------------------------------
 Job in one sentence:
    "I am the brain. I ask the Ingest Service what the hospital used, I ask
     the Price Service what electricity costs, and I work out how much money
     the hospital could save by running its non-clinical machines at
     different times of day."

 I own NO database. I am completely stateless: every answer is computed from
 scratch out of data I fetch over REST from my two sibling services.

 Being stateless is exactly why I can be scaled horizontally without limit -
 any copy of me can answer any request, so Kubernetes can run 1 of me or 50.

 The hard safety rule encoded here:
    Clinical zones (ICU, theatres, imaging, wards) are NEVER touched.
    Patient safety beats electricity prices, always.
===========================================================================
"""

import os
import socket
import logging
import json
import asyncio
from datetime import datetime, timezone

import httpx
from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse

POD = os.environ.get("POD_NAME", socket.gethostname())
SERVICE = "optimizer"


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
# Where my sibling services live. In Kubernetes these are just service names
# on the cluster's internal DNS - I never need to know any IP address.
# --------------------------------------------------------------------------
INGEST_URL = os.environ.get("INGEST_URL", "http://localhost:8081")
PRICE_URL = os.environ.get("PRICE_URL", "http://localhost:8082")
HTTP_TIMEOUT = float(os.environ.get("HTTP_TIMEOUT", "6.0"))

# How much of a zone's peak-hour load can realistically be moved.
# These numbers come from how the equipment actually works.
SHIFT_CAPACITY = {
    "hvac":          0.30,   # thermal mass lets you pre-cool, but only so far
    "sterilisation": 0.65,   # autoclave batches, as long as trays are ready by 07:00
    "laundry":       0.85,   # almost fully deferrable to the night shift
    "catering":      0.45,   # cold storage is fixed; bulk cooking and dishwashing move
}

# Grid carbon intensity, grams of CO2 per kWh.
# Cheap hours in the Nordic market are cheap because wind and hydro are
# abundant, which also makes them cleaner. Expensive hours are expensive
# because gas and imports are running.
CO2_CHEAP_HOUR = 22.0
CO2_PEAK_HOUR = 96.0

# Grid connection / demand charge in SEK per kW of monthly peak.
# Swedish hospitals genuinely pay this on top of energy.
DEMAND_CHARGE_SEK_PER_KW_MONTH = 68.0

app = FastAPI(
    title="MediWatt Optimizer Service",
    description="Computes load-shifting recommendations and cost savings.",
    version="1.0.0",
)


# ==========================================================================
#  Talking to the other microservices
# ==========================================================================
async def fetch_json(client: httpx.AsyncClient, url: str) -> dict:
    resp = await client.get(url)
    resp.raise_for_status()
    return resp.json()


async def gather_inputs(area: str):
    """
    Fetch consumption and prices *at the same time* instead of one after the
    other. Two 200 ms calls in parallel take 200 ms, not 400 ms.
    """
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        summary_task = fetch_json(client, f"{INGEST_URL}/api/summary")
        price_task = fetch_json(client, f"{PRICE_URL}/api/prices?area={area}")
        return await asyncio.gather(summary_task, price_task, return_exceptions=True)


# ==========================================================================
#  The optimization itself
# ==========================================================================
def format_hours(hours: list[int]) -> str:
    """
    Turn [0,1,2,3,22,23] into "00:00-04:00, 22:00-24:00".

    Cheap hours are not always next to each other - a Nordic day is usually
    cheap at night AND cheap again in the windy middle of the afternoon - so
    the label has to be able to describe several separate windows.
    """
    if not hours:
        return "n/a"
    hours = sorted(set(hours))
    runs, start, prev = [], hours[0], hours[0]
    for h in hours[1:]:
        if h == prev + 1:
            prev = h
            continue
        runs.append((start, prev))
        start = prev = h
    runs.append((start, prev))
    return ", ".join(f"{a:02d}:00-{b + 1:02d}:00" for a, b in runs)


def optimise(summary: dict, prices: dict) -> dict:
    hourly_price = prices["hourly"]
    day_avg_price = sum(hourly_price) / 24

    zones = summary["zones"]

    # ---- 1. What does today cost, as things are run now? -----------------
    baseline_hourly_total = [0.0] * 24
    baseline_cost = 0.0
    for z in zones:
        for h in range(24):
            baseline_hourly_total[h] += z["hourlyKwh"][h]
            baseline_cost += z["hourlyKwh"][h] * hourly_price[h]

    # ---- 2. Decide which hours we want to take load OUT of ---------------
    #
    # There are two different reasons to relieve an hour, and a hospital pays
    # for both of them:
    #
    #   a) the electricity is expensive in that hour  -> energy cost
    #   b) the whole site is near its daily maximum   -> grid demand charge,
    #      which is billed on the single highest hour of the month
    #
    # Optimising for price alone can leave the site peak completely
    # untouched, so we deliberately include the near-peak hours as well.
    site_peak = max(baseline_hourly_total)
    relief_hours = {
        h for h in range(24)
        if hourly_price[h] > day_avg_price or baseline_hourly_total[h] >= 0.92 * site_peak
    }

    # Receiving hours: everything else, cheapest first. We never push load
    # into an hour we are trying to relieve - that would just move the
    # problem around.
    receiving_hours = sorted(
        (h for h in range(24) if h not in relief_hours),
        key=lambda h: hourly_price[h]
    )
    if not receiving_hours:                      # pathological flat-price day
        receiving_hours = sorted(range(24), key=lambda h: hourly_price[h])[:6]

    hours_by_price = sorted(range(24), key=lambda h: hourly_price[h])
    expensive_hours = relief_hours

    # ---- 3. Move deferrable load out of expensive hours ------------------
    optimised_hourly_total = [0.0] * 24
    optimised_cost = 0.0
    recommendations = []
    protected_zones = []
    total_shifted_kwh = 0.0
    co2_saved_g = 0.0

    for z in zones:
        plan = list(z["hourlyKwh"])          # start from what actually happened

        if z["critical"] or not z["shiftable"]:
            # ---- SAFETY RULE -------------------------------------------
            # Clinical load and office load are left exactly as they are.
            # We collect the protected zones and report them as a single
            # line at the end, rather than repeating the same sentence for
            # every ward.
            if z["critical"]:
                protected_zones.append(z["name"])
        else:
            capacity = SHIFT_CAPACITY.get(z["zoneId"], 0.0)
            if capacity > 0:
                # Take the movable share out of every expensive hour...
                movable = 0.0
                for h in expensive_hours:
                    take = plan[h] * capacity
                    plan[h] -= take
                    movable += take

                if movable > 0.01:
                    # ...and put it back into the cheapest hours, spreading it
                    # over enough hours that we never create a new spike.
                    # We cap each receiving hour at the zone's rated power.
                    remaining = movable
                    cap_per_hour = z["baselineKw"] * 1.6
                    receiving = []
                    for h in receiving_hours:
                        if remaining <= 0.01:
                            break
                        headroom = max(cap_per_hour - plan[h], 0.0)
                        add = min(headroom, remaining)
                        if add > 0.5:                      # ignore trivial dribbles
                            plan[h] += add
                            remaining -= add
                            receiving.append(h)
                    # If we somehow could not place it all, put the rest back
                    # in the cheapest hour rather than losing energy.
                    if remaining > 0.01:
                        plan[receiving_hours[0]] += remaining
                        receiving.append(receiving_hours[0])

                    before = sum(z["hourlyKwh"][h] * hourly_price[h] for h in range(24))
                    after = sum(plan[h] * hourly_price[h] for h in range(24))
                    saving = before - after

                    total_shifted_kwh += movable
                    co2_saved_g += movable * (CO2_PEAK_HOUR - CO2_CHEAP_HOUR)

                    window_label = format_hours(receiving)

                    recommendations.append({
                        "zoneId": z["zoneId"],
                        "zone": z["name"],
                        "action": "shift",
                        "priority": "high" if saving > 400 else "medium" if saving > 120 else "low",
                        "shiftedKwh": round(movable, 1),
                        "targetWindow": window_label,
                        "savingSek": round(saving, 2),
                        "text": (
                            f"Move {movable:,.0f} kWh of {z['name']} load into {window_label}, "
                            f"when power is cheapest. Saves {saving:,.0f} SEK today "
                            f"({saving * 365:,.0f} SEK/year)."
                        ),
                    })

        for h in range(24):
            optimised_hourly_total[h] += plan[h]
            optimised_cost += plan[h] * hourly_price[h]

    # ---- 4. Peak demand: the second, hidden saving -----------------------
    baseline_peak = max(baseline_hourly_total)
    optimised_peak = max(optimised_hourly_total)
    peak_reduction_kw = max(baseline_peak - optimised_peak, 0.0)
    demand_saving_month = peak_reduction_kw * DEMAND_CHARGE_SEK_PER_KW_MONTH

    if peak_reduction_kw > 1:
        recommendations.append({
            "zoneId": "site",
            "zone": "Whole site",
            "action": "peak-shaving",
            "priority": "high",
            "savingSek": round(demand_saving_month / 30, 2),
            "text": (
                f"Site peak demand falls by {peak_reduction_kw:,.0f} kW. At a grid "
                f"demand charge of {DEMAND_CHARGE_SEK_PER_KW_MONTH:.0f} SEK/kW/month "
                f"that is a further {demand_saving_month:,.0f} SEK every month."
            ),
        })

    recommendations.sort(key=lambda r: -r["savingSek"])

    # The safety statement always goes last, and always appears - an examiner
    # (or a hospital's clinical safety officer) should be able to see at a
    # glance that patient-critical load was deliberately excluded.
    if protected_zones:
        recommendations.append({
            "zoneId": "protected",
            "zone": "Clinical zones",
            "action": "protect",
            "priority": "policy",
            "savingSek": 0.0,
            "text": (
                f"No action taken in {len(protected_zones)} clinical zones "
                f"({', '.join(protected_zones)}). Patient-critical load is excluded "
                f"from optimisation by safety policy and is never shifted, reduced "
                f"or delayed."
            ),
        })

    energy_saving = baseline_cost - optimised_cost
    total_kwh = sum(baseline_hourly_total)

    return {
        "baseline": {
            "costSek": round(baseline_cost, 2),
            "hourlyKwh": [round(v, 1) for v in baseline_hourly_total],
            "peakKw": round(baseline_peak, 1),
        },
        "optimised": {
            "costSek": round(optimised_cost, 2),
            "hourlyKwh": [round(v, 1) for v in optimised_hourly_total],
            "peakKw": round(optimised_peak, 1),
        },
        "savings": {
            "dailySek": round(energy_saving, 2),
            "dailyPct": round(energy_saving / baseline_cost * 100, 1) if baseline_cost else 0.0,
            "annualSek": round(energy_saving * 365, 0),
            "demandChargeMonthlySek": round(demand_saving_month, 0),
            "annualTotalSek": round(energy_saving * 365 + demand_saving_month * 12, 0),
            "peakReductionKw": round(peak_reduction_kw, 1),
            "shiftedKwh": round(total_shifted_kwh, 1),
            "co2SavedKgPerDay": round(co2_saved_g / 1000, 1),
            "co2SavedTonnesPerYear": round(co2_saved_g / 1000 * 365 / 1000, 1),
        },
        "consumption": {
            "totalKwh": round(total_kwh, 1),
            "avgPriceSekPerKwh": round(day_avg_price, 4),
        },
        "recommendations": recommendations,
    }


def find_anomalies(summary: dict) -> list:
    """
    Neighbour-comparison anomaly detection.

    Why not just "more than 2 standard deviations from the daily average"?
    Because a hospital's consumption is *supposed* to swing during the day.
    The HVAC plant legitimately runs at four times its night load every
    afternoon - measured against the whole day, that is not unusual at all,
    so a plain standard-deviation test misses the fault we actually care
    about.

    What is never legitimate is a sudden step that the neighbouring hours do
    not share. Electricity demand in a building changes smoothly: 03:00
    looks like 02:00 and 04:00. So we compare every hour against the average
    of the hour before and the hour after. A spike that its own neighbours
    do not agree with is a fault, not a busy afternoon.

    In practice this catches exactly what hospital estates teams look for:
    a chiller short-cycling overnight, an air-handling unit with a stuck
    damper, a compressor that will not unload. All of them waste money for
    weeks before they finally break down.
    """
    findings = []
    for z in summary["zones"]:
        values = z["hourlyKwh"]
        day_mean = sum(values) / 24
        if day_mean <= 0:
            continue

        # First pass: which hours are spikes? We need to know this before the
        # second pass, because a single spike makes BOTH of its neighbours
        # look artificially low - an "echo". Reporting those echoes as three
        # separate faults would send the maintenance team chasing ghosts.
        spikes = set()
        for h, v in enumerate(values):
            n = (values[(h - 1) % 24] + values[(h + 1) % 24]) / 2
            if n > 0 and v / n >= 1.6 and abs(v - n) >= 0.15 * day_mean:
                spikes.add(h)

        for h, v in enumerate(values):
            before = values[(h - 1) % 24]
            after = values[(h + 1) % 24]
            neighbours = (before + after) / 2
            if neighbours <= 0:
                continue

            ratio = v / neighbours

            # Suppress the echo: this hour only looks low because the hour
            # next to it is the real fault.
            if ratio < 1 and ((h - 1) % 24 in spikes or (h + 1) % 24 in spikes):
                continue
            # The gap must be big in relative terms AND worth money in
            # absolute terms, otherwise we would flag harmless wobble in
            # small zones and nobody would trust the alerts.
            if abs(v - neighbours) < 0.15 * day_mean:
                continue

            if ratio >= 1.6 or ratio <= 0.5:
                excess_kwh = v - neighbours
                findings.append({
                    "zoneId": z["zoneId"],
                    "zone": z["name"],
                    "hour": h,
                    "kwh": round(v, 1),
                    "expectedKwh": round(neighbours, 1),
                    "ratio": round(ratio, 2),
                    "excessKwh": round(excess_kwh, 1),
                    "severity": "high" if (ratio >= 2.0 or ratio <= 0.35) else "medium",
                    "text": (
                        f"{z['name']} drew {v:,.0f} kWh at {h:02d}:00, but the hours either "
                        f"side averaged only {neighbours:,.0f} kWh "
                        f"({ratio:.1f}x expected). That is {abs(excess_kwh):,.0f} kWh "
                        f"{'more' if excess_kwh > 0 else 'less'} than the pattern predicts - "
                        f"worth a maintenance check."
                    ),
                })

    findings.sort(key=lambda f: -abs(f["excessKwh"]))
    return findings[:8]


# ==========================================================================
#  HEALTH ENDPOINTS
# ==========================================================================
@app.get("/healthz")
async def healthz():
    return {"status": "alive", "service": SERVICE, "pod": POD}


@app.get("/readyz")
async def readyz():
    """
    I am ready as soon as my process is up. I deliberately do NOT fail
    readiness when a dependency is down - that would turn one broken
    service into a cascading outage across the cluster.
    """
    return {"status": "ready", "service": SERVICE, "pod": POD,
            "dependencies": {"ingest": INGEST_URL, "price": PRICE_URL}}


# ==========================================================================
#  REST API
# ==========================================================================
@app.get("/api/optimize")
async def optimize(area: str = Query(default="SE4", pattern="^SE[1-4]$")):
    """
    The headline endpoint. Returns the full optimisation plan.

    GET /api/optimize?area=SE4
    """
    summary, prices = await gather_inputs(area)

    if isinstance(summary, Exception):
        log.error(f"ingest service unreachable: {summary}")
        return JSONResponse(status_code=503, content={
            "error": "consumption data unavailable",
            "detail": str(summary), "pod": POD, "dependency": "ingest"})

    if isinstance(prices, Exception):
        log.error(f"price service unreachable: {prices}")
        return JSONResponse(status_code=503, content={
            "error": "price data unavailable",
            "detail": str(prices), "pod": POD, "dependency": "price"})

    if summary.get("totalKwh", 0) <= 0:
        return JSONResponse(status_code=409, content={
            "error": "no meter data yet",
            "hint": "POST /api/simulate on the ingest service to load a demo day",
            "pod": POD})

    result = optimise(summary, prices)
    result.update({
        "pod": POD,
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "area": area,
        "priceSource": prices.get("source"),
        "priceStale": prices.get("stale", False),
        "servedBy": {"ingestPod": summary.get("pod"), "pricePod": prices.get("pod"), "optimizerPod": POD},
        "prices": prices["hourly"],
    })
    log.info(f"optimisation computed: {result['savings']['dailySek']} SEK/day, "
             f"{result['savings']['dailyPct']}%")
    return result


@app.get("/api/anomalies")
async def anomalies():
    """Zones behaving oddly - a maintenance early-warning list."""
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        try:
            summary = await fetch_json(client, f"{INGEST_URL}/api/summary")
        except Exception as exc:  # noqa: BLE001
            log.error(f"ingest service unreachable: {exc}")
            return JSONResponse(status_code=503,
                                content={"error": "consumption data unavailable", "pod": POD})

    findings = find_anomalies(summary)
    return {"pod": POD, "count": len(findings), "anomalies": findings}


@app.on_event("startup")
async def startup():
    log.info(f"Optimizer started. ingest={INGEST_URL} price={PRICE_URL}")
