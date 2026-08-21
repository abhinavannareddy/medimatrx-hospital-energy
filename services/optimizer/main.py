"""
===========================================================================
 MediMatrx - OPTIMIZER SERVICE
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
from pydantic import BaseModel, Field
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


# --------------------------------------------------------------------------
#  Number formatting
#  Swedish convention groups thousands with a space, not a comma, and the
#  currency is written "kr". Python only offers comma grouping, so we format
#  with commas and swap them. Every figure a human reads goes through these
#  two helpers, which is why the dashboard, the recommendation text and the
#  assistant's answers all agree with each other. Getting this wrong looks
#  small but it is exactly the kind of detail a hospital finance officer
#  notices first.
# --------------------------------------------------------------------------
def grouped(v: float) -> str:
    """1786.4 -> '1 786'"""
    return f"{v:,.0f}".replace(",", " ")


def lowest_feasible_ceiling(
    after_removal: list[float],
    receiving: list[int],
    need: float,
    prefer_below: float,
) -> float:
    """The lowest site-wide kWh ceiling that can still absorb `need` kWh.

    Given the site profile after load has been removed, and the set of hours
    we are willing to push load into, find the smallest ceiling C such that

        sum over receiving hours of max(C - profile[h], 0)  >=  need

    This is the standard "water filling" formulation of peak minimisation:
    imagine pouring `need` litres of water into the valleys of the profile
    and asking how high the water rises. Binary search converges fast and,
    unlike a greedy cheapest-hour-first fill, it cannot build a new spike.

    `prefer_below` is the baseline site peak. We start the search assuming we
    can stay under it, and only go above it if the day genuinely has nowhere
    else to put the energy, which is reported honestly rather than hidden.
    """
    if need <= 0 or not receiving:
        return max(after_removal) if after_removal else 0.0

    def headroom(c: float) -> float:
        return sum(max(c - after_removal[h], 0.0) for h in receiving)

    lo = max(after_removal[h] for h in receiving)
    hi = max(lo, prefer_below)

    # Only if even the baseline peak leaves too little room do we raise the
    # ceiling above it. Bounded so a pathological input cannot spin forever.
    guard = 0
    while headroom(hi) < need and guard < 200:
        hi = hi * 1.25 + 1.0
        guard += 1

    for _ in range(60):                      # ~1e-18 relative precision
        mid = (lo + hi) / 2
        if headroom(mid) >= need:
            hi = mid
        else:
            lo = mid
    return hi


app = FastAPI(
    title="MediMatrx Optimizer Service",
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


def optimise(summary: dict, prices: dict, flex_overrides: dict | None = None) -> dict:
    """
    flex_overrides lets a caller ask "what if the laundry were 90% flexible
    instead of 85%?" without changing any configuration. It is what powers
    the assistant's what-if answers, and it is also how an estates manager
    would model a change before committing to it.
    """
    capacity_table = dict(SHIFT_CAPACITY)
    if flex_overrides:
        capacity_table.update(flex_overrides)

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

    # Preferred hours first, then everything else cheapest-first as a spill
    # tier. On most days the spill tier is never touched: the cheap hours
    # have plenty of room. It matters on days where the price curve leaves
    # only two or three hours below the average, because then ALL the
    # deferrable load in the hospital wants the same two hours, and a plan
    # that stacks it there trades an energy saving for a much larger demand
    # charge.
    #
    # Including every hour also gives the algorithm a useful guarantee.
    # Today's actual profile is itself a valid arrangement that fits under
    # today's peak, so a ceiling equal to the baseline peak is always
    # feasible. The search below therefore can never return a ceiling above
    # the baseline peak, which means this optimiser cannot raise the site
    # peak. That is a property of the method, not something we test for.
    placement_hours = receiving_hours + [
        h for h in sorted(range(24), key=lambda h: hourly_price[h])
        if h not in receiving_hours
    ]

    # ---- 3. Move deferrable load out of the hours we want to relieve -----
    #
    # This is done in TWO passes, and the split is the whole point.
    #
    # Pass 1 takes movable load out of every zone. Pass 2 decides where it
    # lands. These cannot be merged, because "where it lands" is a decision
    # about the WHOLE SITE, not about one zone. If each zone independently
    # picks the cheapest hour, every zone picks the SAME hour, and together
    # they build a new peak taller than the one we just removed. That is not
    # a saving, it is a bigger demand charge with extra steps.
    #
    # An earlier version of this function did exactly that and reported a
    # peak reduction of zero while quietly making the peak worse.
    recommendations = []
    protected_zones = []
    total_shifted_kwh = 0.0
    co2_saved_g = 0.0

    plans = {}                # zoneId -> the 24-hour plan we are building
    movable_by_zone = {}      # zoneId -> kWh picked up in pass 1

    for z in zones:
        plan = list(z["hourlyKwh"])          # start from what actually happened
        movable = 0.0

        if z["critical"] or not z["shiftable"]:
            # ---- SAFETY RULE -------------------------------------------
            # Clinical load and office load are left exactly as they are.
            # We collect the protected zones and report them as a single
            # line at the end, rather than repeating the same sentence for
            # every ward.
            if z["critical"]:
                protected_zones.append(z["name"])
        else:
            capacity = capacity_table.get(z["zoneId"], 0.0)
            if capacity > 0:
                for h in relief_hours:
                    take = plan[h] * capacity
                    plan[h] -= take
                    movable += take

        plans[z["zoneId"]] = plan
        movable_by_zone[z["zoneId"]] = movable

    # What the site looks like after the removals, before anything goes back.
    after_removal_total = [
        sum(plans[z["zoneId"]][h] for z in zones) for h in range(24)
    ]
    total_movable = sum(movable_by_zone.values())

    # ---- 3b. Pick the flattest day that still fits the load --------------
    #
    # Water filling. We look for the LOWEST site ceiling whose spare space,
    # across the receiving hours only, is big enough to hold everything we
    # picked up. Then we fill up to that ceiling and no higher.
    #
    # Filling to a ceiling instead of "cheapest hour first" is what makes the
    # peak fall rather than merely move, and the peak is what the grid
    # demand charge is billed on: one number, the highest hour of the month.
    # A plan that halves the energy bill while adding 900 kW to the peak can
    # easily cost the hospital more than doing nothing.
    ceiling = lowest_feasible_ceiling(
        after_removal_total, placement_hours, total_movable, site_peak
    )

    site_now = list(after_removal_total)

    for z in zones:
        zone_id = z["zoneId"]
        movable = movable_by_zone[zone_id]
        if movable <= 0.01:
            continue

        plan = plans[zone_id]
        # A zone cannot absorb unlimited power in one hour either. The
        # laundry has a fixed number of machines; 1.6x its normal draw is a
        # generous but finite ceiling.
        cap_per_hour = z["baselineKw"] * 1.6
        remaining = movable
        receiving = []

        for h in placement_hours:
            if remaining <= 0.01:
                break
            site_room = max(ceiling - site_now[h], 0.0)
            zone_room = max(cap_per_hour - plan[h], 0.0)
            add = min(site_room, zone_room, remaining)
            if add > 0.5:                      # ignore trivial dribbles
                plan[h] += add
                site_now[h] += add
                remaining -= add
                receiving.append(h)

        # If a zone's own power limit stopped us placing everything, the
        # remainder goes into whichever receiving hour is currently LOWEST,
        # so even the last resort flattens the day instead of spiking it.
        if remaining > 0.01:
            h = min(placement_hours, key=lambda x: site_now[x])
            plan[h] += remaining
            site_now[h] += remaining
            if h not in receiving:
                receiving.append(h)

        before = sum(z["hourlyKwh"][h] * hourly_price[h] for h in range(24))
        after = sum(plan[h] * hourly_price[h] for h in range(24))
        saving = before - after

        total_shifted_kwh += movable
        co2_saved_g += movable * (CO2_PEAK_HOUR - CO2_CHEAP_HOUR)

        window_label = format_hours(sorted(receiving))

        recommendations.append({
            "zoneId": zone_id,
            "zone": z["name"],
            "action": "shift",
            "priority": "high" if saving > 400 else "medium" if saving > 120 else "low",
            "shiftedKwh": round(movable, 1),
            "targetWindow": window_label,
            "savingSek": round(saving, 2),
            "text": (
                f"Move {grouped(movable)} kWh of {z['name']} load into "
                f"{window_label}, when power is cheapest. Saves "
                f"{grouped(saving)} kr today "
                f"({grouped(saving * 365)} kr/year)."
            ),
        })

    optimised_hourly_total = [0.0] * 24
    optimised_cost = 0.0
    for z in zones:
        plan = plans[z["zoneId"]]
        for h in range(24):
            optimised_hourly_total[h] += plan[h]
            optimised_cost += plan[h] * hourly_price[h]

    # ---- 4. Peak demand: the second, hidden saving -----------------------
    baseline_peak = max(baseline_hourly_total)
    optimised_peak = max(optimised_hourly_total)

    # Signed on purpose. A previous version clamped this at zero, which meant
    # a plan that RAISED the site peak displayed as "0 kW saved" instead of
    # as the problem it was. If this number is ever negative the plan is
    # costing the hospital money on the demand charge, and the dashboard says
    # so. Never round a regression down to zero.
    peak_delta_kw = baseline_peak - optimised_peak
    peak_reduction_kw = max(peak_delta_kw, 0.0)
    demand_saving_month = peak_delta_kw * DEMAND_CHARGE_SEK_PER_KW_MONTH

    if peak_delta_kw < -1:
        log.warning(
            "plan increases site peak by %.0f kW - demand charge would rise",
            -peak_delta_kw,
        )
        recommendations.append({
            "zoneId": "site",
            "zone": "Whole site",
            "action": "peak-warning",
            "priority": "high",
            "savingSek": 0.0,
            "text": (
                f"Warning: this plan would raise the site peak by "
                f"{grouped(-peak_delta_kw)} kW, which costs about "
                f"{grouped(-demand_saving_month)} kr a month in grid demand "
                f"charges. The energy saving above does not cover it. Do not "
                f"apply this plan."
            ),
        })

    if peak_reduction_kw > 1:
        peak_text = (
            f"Site peak demand falls by {grouped(peak_reduction_kw)} kW. At a grid "
            f"demand charge of {DEMAND_CHARGE_SEK_PER_KW_MONTH:.0f} kr/kW/month "
            f"that is a further {grouped(demand_saving_month)} kr every month."
        )

        recommendations.append({
            "zoneId": "site",
            "zone": "Whole site",
            "action": "peak-shaving",
            "priority": "high",
            "savingSek": round(demand_saving_month / 30, 2),
            "text": peak_text,
        })

    # ---- 5. Is this plan actually worth applying? ------------------------
    #
    # Two things are being traded against each other: the energy bill and the
    # demand charge. Usually they pull the same way, because expensive hours
    # and busy hours are the same hours. On an inverted day, they do not.
    #
    # If cheap power happens to arrive exactly when the hospital is busiest
    # (a windy weekday afternoon), then flattening the peak means moving load
    # OUT of the cheapest hours, and the energy bill goes up by more than the
    # demand charge goes down. The arithmetic is correct; the plan is still a
    # bad idea.
    #
    # A tool that says "saves you money" on a day when it does not is worse
    # than no tool, because someone acts on it. So when the net is negative
    # we say so, in the first line, and tell them to do nothing today.
    energy_saving_check = baseline_cost - optimised_cost
    annual_total_check = energy_saving_check * 365 + demand_saving_month * 12

    if annual_total_check <= 0:
        log.warning(
            "plan is net negative (%.0f kr/year) - advising no action",
            annual_total_check,
        )
        recommendations.insert(0, {
            "zoneId": "site",
            "zone": "Whole site",
            "action": "no-action",
            "priority": "high",
            "savingSek": 0.0,
            "text": (
                f"Recommendation today: change nothing. Electricity is cheapest "
                f"during this site's busiest hours, so shifting load would cut "
                f"the peak but raise the energy bill by more than it saves. "
                f"The net effect of the plan below would be "
                f"{grouped(-annual_total_check)} kr a year WORSE than doing "
                f"nothing. The individual moves are shown for transparency, "
                f"but they should not be applied today."
            ),
        })
        plan_worth_applying = False
    else:
        plan_worth_applying = True

    # Sort by value, but never let the no-action warning leave the top.
    head = [r for r in recommendations if r["action"] == "no-action"]
    tail = [r for r in recommendations if r["action"] != "no-action"]
    tail.sort(key=lambda r: -r["savingSek"])
    recommendations = head + tail

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
            # Signed. Negative means the plan raises the peak, which the
            # dashboard must show rather than clamp away.
            "peakDeltaKw": round(peak_delta_kw, 1),
            # False when the energy bill would rise by more than the demand
            # charge falls. The dashboard must not show a savings headline
            # when this is False.
            "planWorthApplying": plan_worth_applying,
            "shiftedKwh": round(total_shifted_kwh, 1),
            "co2SavedKgPerDay": round(co2_saved_g / 1000, 1),
            "co2SavedTonnesPerYear": round(co2_saved_g / 1000 * 365 / 1000, 1),
        },
        "consumption": {
            "totalKwh": round(total_kwh, 1),
            "avgPriceSekPerKwh": round(day_avg_price, 4),
        },
        # ---- Why the plan looks the way it does ---------------------------
        # Exposing the working, not just the answer. The assistant service
        # uses this to explain a recommendation instead of paraphrasing it,
        # and an estates manager can audit the logic without reading code.
        "reasoning": {
            "reliefHours": sorted(relief_hours),
            "reliefHoursLabel": format_hours(sorted(relief_hours)),
            "receivingHours": receiving_hours,
            "receivingHoursLabel": format_hours(receiving_hours),
            "dayAvgPriceSekPerKwh": round(day_avg_price, 4),
            "cheapestHour": hourly_price.index(min(hourly_price)),
            "cheapestPrice": round(min(hourly_price), 4),
            "dearestHour": hourly_price.index(max(hourly_price)),
            "dearestPrice": round(max(hourly_price), 4),
            "priceSpreadRatio": round(max(hourly_price) / min(hourly_price), 2) if min(hourly_price) else 0.0,
            "sitePeakHour": baseline_hourly_total.index(max(baseline_hourly_total)),
            "flexibilityUsed": capacity_table,
            "demandChargeSekPerKwMonth": DEMAND_CHARGE_SEK_PER_KW_MONTH,
            "protectedZones": protected_zones,
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
                        f"{z['name']} drew {grouped(v)} kWh at {h:02d}:00, but the hours "
                        f"either side averaged only {grouped(neighbours)} kWh "
                        f"({ratio:.1f}x expected). That is {grouped(abs(excess_kwh))} kWh "
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
def parse_flex(flex: str | None) -> dict:
    """
    Parse a what-if flexibility override: "laundry:0.9,catering:0.55".

    Validated hard, because this is the one input that changes the answer:
    only known deferrable zones, only fractions between 0 and 1, and never
    a clinical zone. A caller cannot use this to make the optimiser touch
    the ICU.
    """
    if not flex:
        return {}
    overrides = {}
    for part in flex.split(","):
        if ":" not in part:
            raise ValueError(f"expected zone:fraction, got '{part}'")
        zone, _, value = part.partition(":")
        zone = zone.strip().lower()
        if zone not in SHIFT_CAPACITY:
            raise ValueError(
                f"'{zone}' is not a deferrable zone. Valid: {', '.join(SHIFT_CAPACITY)}")
        try:
            fraction = float(value)
        except ValueError:
            raise ValueError(f"'{value}' is not a number")
        if not 0.0 <= fraction <= 1.0:
            raise ValueError("flexibility must be between 0 and 1")
        overrides[zone] = fraction
    return overrides


@app.get("/api/optimize")
async def optimize(
    area: str = Query(default="SE4", pattern="^SE[1-4]$"),
    flex: str | None = Query(default=None, max_length=200,
                             description="What-if override, e.g. laundry:0.9,catering:0.55"),
):
    """
    The headline endpoint. Returns the full optimisation plan.

    GET /api/optimize?area=SE4
    GET /api/optimize?area=SE4&flex=laundry:0.9   <- what-if scenario
    """
    try:
        flex_overrides = parse_flex(flex)
    except ValueError as exc:
        return JSONResponse(status_code=400,
                            content={"error": str(exc), "pod": POD})

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

    result = optimise(summary, prices, flex_overrides)
    result.update({
        "pod": POD,
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "area": area,
        "scenario": bool(flex_overrides),
        "flexOverrides": flex_overrides,
        "priceSource": prices.get("source"),
        "priceStale": prices.get("stale", False),
        "servedBy": {"ingestPod": summary.get("pod"), "pricePod": prices.get("pod"), "optimizerPod": POD},
        "prices": prices["hourly"],
    })
    log.info(f"optimisation computed: {result['savings']['dailySek']} SEK/day, "
             f"{result['savings']['dailyPct']}%")
    return result


class ScenarioZone(BaseModel):
    zoneId: str
    name: str = ""
    critical: bool = False
    shiftable: bool = False
    baselineKw: float = Field(default=100.0, gt=0, le=100000)
    description: str = ""
    hourlyKwh: list[float] = Field(min_length=24, max_length=24)


class ScenarioRequest(BaseModel):
    """A load matrix and a price curve that did not come from our database."""
    zones: list[ScenarioZone] = Field(min_length=1, max_length=100)
    prices: list[float] = Field(min_length=24, max_length=24)
    label: str = Field(default="scenario", max_length=80)


@app.post("/api/optimize/scenario")
async def optimize_scenario(req: ScenarioRequest):
    """
    Run the optimiser over a load matrix supplied by the caller instead of
    over yesterday's measured data.

    This is what lets the forecast service plan TOMORROW: it predicts the
    load, fetches tomorrow's prices, and posts both here. The point is that
    it reuses this exact engine rather than reimplementing it, so today's
    report and tomorrow's plan can never drift apart in their logic.

    Still read-only: nothing is stored, the answer is computed and returned.
    """
    for z in req.zones:
        for v in z.hourlyKwh:
            if v < 0 or v > 100000:
                return JSONResponse(status_code=400, content={
                    "error": f"hourlyKwh out of range in zone {z.zoneId}", "pod": POD})
    for v in req.prices:
        if v < -10 or v > 100:
            return JSONResponse(status_code=400, content={
                "error": "price out of plausible range", "pod": POD})

    summary = {"zones": [z.model_dump() for z in req.zones],
               "totalKwh": sum(sum(z.hourlyKwh) for z in req.zones)}
    prices = {"hourly": req.prices}

    result = optimise(summary, prices)
    result.update({
        "pod": POD,
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "label": req.label,
        "scenario": True,
        "prices": req.prices,
    })
    log.info(f"scenario optimisation '{req.label}': "
             f"{result['savings']['dailySek']} SEK/day")
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
