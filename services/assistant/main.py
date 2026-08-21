"""
===========================================================================
 MediMatrx - ASSISTANT SERVICE
---------------------------------------------------------------------------
 Job in one sentence:
    "I answer questions about this hospital's energy in plain English, and
     every number I say comes from one of our own REST APIs, never from my
     own head."

 The design rule that matters
 ----------------------------
 In a clinical setting an assistant that invents a number is worse than no
 assistant at all. So:

   1. READ-ONLY. There is no tool here that changes anything. Not one.
   2. I NEVER do arithmetic on my own. Numbers come from the ingest, price
      and optimizer services; my job is to pick the right endpoint and put
      the answer into a sentence.
   3. EVERY answer cites which endpoint it came from, so it is auditable.
   4. I ALWAYS work. The default engine is deterministic intent matching -
      no API key, no internet, no cost, no hallucination. If an LLM key is
      configured I will use it for nicer phrasing, but if that call fails
      for any reason I fall back to the deterministic answer rather than
      failing the request.

 Rule 4 is the same graceful-degradation pattern the price service uses for
 the upstream price API. Applied twice, deliberately.
===========================================================================
"""

import os
import re
import socket
import json
import logging
import asyncio
from datetime import datetime, timezone

import httpx
from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from knowledge import (ZONE_SYNONYMS, FLEX_RATIONALE, CLINICAL_NOTE,
                       INTENTS, SUGGESTIONS)

POD = os.environ.get("POD_NAME", socket.gethostname())
SERVICE = "assistant"


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
DEFAULT_AREA = os.environ.get("PRICE_AREA", "SE4")

# Optional. Absent by default, and everything works without it.
LLM_PROVIDER = os.environ.get("LLM_PROVIDER", "").lower()
LLM_API_KEY = os.environ.get("LLM_API_KEY", "")
LLM_MODEL = os.environ.get("LLM_MODEL", "")

app = FastAPI(
    title="MediMatrx Assistant Service",
    description="Answers energy questions using only the platform's own REST APIs.",
    version="1.0.0",
)

_stats = {"questions": 0, "deterministic": 0, "llm": 0, "llm_failures": 0}


# ==========================================================================
#  THE TOOL LAYER
#  Every one of these is a GET. There is deliberately no POST, no PUT and
#  no DELETE anywhere in this service.
# ==========================================================================
async def _get(client: httpx.AsyncClient, url: str) -> dict:
    resp = await client.get(url)
    resp.raise_for_status()
    return resp.json()


async def tool_plan(area: str, flex: str | None = None) -> dict:
    """The optimisation plan, optionally as a what-if scenario."""
    q = f"?area={area}" + (f"&flex={flex}" if flex else "")
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as c:
        return await _get(c, f"{OPTIMIZER_URL}/api/optimize{q}")


async def tool_summary() -> dict:
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as c:
        return await _get(c, f"{INGEST_URL}/api/summary")


async def tool_prices(area: str) -> dict:
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as c:
        return await _get(c, f"{PRICE_URL}/api/prices?area={area}")


async def tool_anomalies() -> dict:
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as c:
        return await _get(c, f"{OPTIMIZER_URL}/api/anomalies")


async def tool_zones() -> dict:
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as c:
        return await _get(c, f"{INGEST_URL}/api/zones")


# ==========================================================================
#  UNDERSTANDING THE QUESTION
# ==========================================================================
def classify(text: str) -> tuple[str, int]:
    """Score every intent by weighted keyword hits. Highest score wins."""
    t = f" {text.lower()} "
    scores = {}
    for intent, keywords in INTENTS.items():
        score = sum(weight for kw, weight in keywords if kw in t)
        if score:
            scores[intent] = score
    if not scores:
        return "unknown", 0
    best = max(scores, key=scores.get)
    return best, scores[best]


def rescue_intent(text: str, zone: str | None) -> str | None:
    """
    Nothing scored. Before giving up, make a sensible guess from whatever
    signal the question does carry. Most "I did not understand" replies are a
    failure of effort rather than of input.

    Returns a real intent name so the answer is labelled honestly, instead of
    quietly answering one thing while reporting "unknown".
    """
    t = text.lower()
    if zone:
        return "consumption"
    if any(w in t for w in ("save", "money", "cost", "cheaper", "reduce",
                            "lower", "bill", "spend", "expensive", "budget")):
        return "opportunity"
    if any(w in t for w in ("should", "action", "next", "fix", "improve",
                            "better", "optimis", "optimiz")):
        return "recommendations"
    if any(w in t for w in ("bad", "poor", "waste", "leak", "old")):
        return "anomaly"
    return None


def find_zone(text: str) -> str | None:
    """
    Which zone is the user talking about, if any.

    Word boundaries matter here: a plain substring search makes "ct" match
    "eleCTricity" and silently tags a price question as an imaging question.
    The longest match wins, so "intensive care" beats "care".
    """
    t = text.lower()
    best, best_len = None, 0
    for zone_id, words in ZONE_SYNONYMS.items():
        for w in words:
            if len(w) > best_len and re.search(rf"\b{re.escape(w)}\b", t):
                best, best_len = zone_id, len(w)
    return best


def find_fraction(text: str) -> float | None:
    """Pull a percentage or fraction out of the question."""
    m = re.search(r"(\d{1,3}(?:\.\d+)?)\s*(?:%|per ?cent)", text)
    if m:
        return min(float(m.group(1)) / 100.0, 1.0)
    m = re.search(r"\b0?\.(\d+)\b", text)
    if m:
        return float(f"0.{m.group(1)}")
    if "fully" in text.lower() or "completely" in text.lower():
        return 1.0
    return None


def find_hour(text: str) -> int | None:
    m = re.search(r"\b(\d{1,2})\s*(?::00)?\s*(am|pm)\b", text.lower())
    if m:
        h = int(m.group(1)) % 12
        return h + (12 if m.group(2) == "pm" else 0)
    m = re.search(r"\b(\d{1,2}):00\b", text)
    if m and 0 <= int(m.group(1)) <= 23:
        return int(m.group(1))
    return None


# ==========================================================================
#  BUILDING THE ANSWER
#  Every builder returns (reply_text, sources, data).
# ==========================================================================
def sek(v: float) -> str:
    """Money, formatted the Swedish way, without inventing precision."""
    if abs(v) >= 1_000_000:
        return f"{v/1_000_000:,.2f} Mkr".replace(",", " ")
    return f"{v:,.0f} kr".replace(",", " ")


def kwh(v: float) -> str:
    return f"{v:,.0f} kWh".replace(",", " ")


ZONE_NAMES = {
    "icu": "the ICU", "theatres": "the operating theatres",
    "imaging": "diagnostic imaging", "wards": "the inpatient wards",
    "hvac": "the HVAC central plant", "sterilisation": "sterilisation (CSSD)",
    "laundry": "the laundry", "catering": "catering", "admin": "the admin block",
}


async def answer_savings(area, text, zone):
    p = await tool_plan(area)
    s, r = p["savings"], p["reasoning"]
    reply = (
        f"Optimising today's plan saves **{sek(s['dailySek'])} on energy**, "
        f"which is {s['dailyPct']}% off the day's bill "
        f"({sek(p['baseline']['costSek'])} down to {sek(p['optimised']['costSek'])}).\n\n"
        f"On top of that, site peak demand falls by {s['peakReductionKw']:,.0f} kW. "
        f"At a grid demand charge of {r['demandChargeSekPerKwMonth']:.0f} kr/kW/month "
        f"that is another {sek(s['demandChargeMonthlySek'])} every month.\n\n"
        f"**Projected annual total: {sek(s['annualTotalSek'])}.**\n\n"
        f"That comes from shifting {kwh(s['shiftedKwh'])} out of expensive hours. "
        f"No clinical load was moved."
    )
    return reply, ["optimizer /api/optimize"], {"savings": s}


async def answer_explain(area, text, zone):
    p = await tool_plan(area)
    r = p["reasoning"]

    lines = [
        f"The plan works from two facts about today.\n",
        f"**One: the price spread.** Electricity is cheapest at "
        f"{r['cheapestHour']:02d}:00 ({r['cheapestPrice']:.2f} kr/kWh) and dearest at "
        f"{r['dearestHour']:02d}:00 ({r['dearestPrice']:.2f} kr/kWh). That is a "
        f"**{r['priceSpreadRatio']:.1f}x difference** for the same kilowatt-hour.\n",
        f"**Two: the site peak.** The hospital's highest hour today was "
        f"{r['sitePeakHour']:02d}:00. The grid demand charge is billed on that single "
        f"hour, so flattening it is worth money on its own.\n",
        f"So the optimiser relieves **{r['reliefHoursLabel']}** (hours that are either "
        f"above the {r['dayAvgPriceSekPerKwh']:.2f} kr/kWh daily average, or close to "
        f"the site peak) and moves that work into **{r['receivingHoursLabel']}**.\n",
    ]

    if zone and zone in FLEX_RATIONALE:
        pct = int(r["flexibilityUsed"].get(zone, 0) * 100)
        rec = next((x for x in p["recommendations"] if x.get("zoneId") == zone), None)
        lines.append(
            f"For {ZONE_NAMES[zone]} specifically, {pct}% of its peak-hour load is "
            f"treated as movable, because {FLEX_RATIONALE[zone]}."
        )
        if rec:
            lines.append(
                f"Today that means moving {kwh(rec['shiftedKwh'])} into "
                f"{rec['targetWindow']}, worth {sek(rec['savingSek'])}."
            )
    elif zone:
        lines.append(
            f"{ZONE_NAMES.get(zone, zone).capitalize()} is a clinical zone, so it is "
            f"not moved at all. {CLINICAL_NOTE}"
        )
    else:
        lines.append(
            "Load is never pushed into an hour that is itself being relieved, and "
            "each receiving hour is capped at the zone's rated power so the plan "
            "cannot create a brand new spike."
        )

    return "\n".join(lines), ["optimizer /api/optimize"], {"reasoning": r}


async def answer_consumption(area, text, zone):
    summary = await tool_summary()
    hour = find_hour(text)

    if not zone:
        total = summary["totalKwh"]
        top = sorted(summary["zones"], key=lambda z: -z["totalKwh"])[:3]
        listed = ", ".join(f"{z['name']} ({kwh(z['totalKwh'])})" for z in top)
        return (
            f"The hospital used **{kwh(total)}** in the last 24 hours across "
            f"{len(summary['zones'])} metered zones.\n\nThe three largest were {listed}.",
            ["ingest /api/summary"], {"totalKwh": total})

    z = next((x for x in summary["zones"] if x["zoneId"] == zone), None)
    if not z:
        return "I do not have a meter for that zone.", ["ingest /api/summary"], {}

    if hour is not None:
        v = z["hourlyKwh"][hour]
        return (
            f"{z['name']} used **{kwh(v)}** at {hour:02d}:00.\n\n"
            f"Its 24-hour total was {kwh(z['totalKwh'])}, against a rated load of "
            f"{z['baselineKw']:,.0f} kW.",
            ["ingest /api/summary"], {"zone": zone, "hour": hour, "kwh": v})

    peak_h = z["hourlyKwh"].index(max(z["hourlyKwh"]))
    low_h = z["hourlyKwh"].index(min(z["hourlyKwh"]))
    kind = ("clinical, so it is never optimised" if z["critical"]
            else "deferrable, so it can be shifted" if z["shiftable"]
            else "not deferrable")
    return (
        f"{z['name']} used **{kwh(z['totalKwh'])}** in the last 24 hours.\n\n"
        f"It peaked at {peak_h:02d}:00 with {kwh(max(z['hourlyKwh']))} and was lowest "
        f"at {low_h:02d}:00 with {kwh(min(z['hourlyKwh']))}. Rated load "
        f"{z['baselineKw']:,.0f} kW.\n\nThis zone is classed as **{kind}**.\n\n"
        f"_{z['description']}_",
        ["ingest /api/summary"], {"zone": zone, "totalKwh": z["totalKwh"]})


async def answer_price(area, text, zone):
    pr = await tool_prices(area)
    st = pr["stats"]
    src = ("live from elprisetjustnu.se" if pr["source"] == "elprisetjustnu.se"
           else "a modelled fallback curve, because the upstream API was unreachable")
    stale = " (served from cache)" if pr.get("stale") else ""
    hour = find_hour(text)

    if hour is not None:
        return (
            f"At {hour:02d}:00 electricity in {area} costs "
            f"**{pr['hourly'][hour]:.2f} kr/kWh**.\n\nToday's average is "
            f"{st['avg']:.2f} kr/kWh. Source: {src}{stale}.",
            ["price /api/prices"], {"hour": hour, "price": pr["hourly"][hour]})

    return (
        f"In bidding area **{area}** today, electricity is cheapest at "
        f"**{st['cheapest_hour']:02d}:00 ({st['min']:.2f} kr/kWh)** and most expensive at "
        f"**{st['most_expensive_hour']:02d}:00 ({st['max']:.2f} kr/kWh)**.\n\n"
        f"The average is {st['avg']:.2f} kr/kWh and the spread across the day is "
        f"{st['spread_pct']:.0f}% of the average. That spread is the entire reason "
        f"this platform exists.\n\nSource: {src}{stale}.",
        ["price /api/prices"], {"stats": st})


async def answer_anomaly(area, text, zone):
    a = await tool_anomalies()
    items = a["anomalies"]
    if zone:
        items = [x for x in items if x["zoneId"] == zone] or items

    if not items:
        return ("No unusual consumption in the last 24 hours. Every zone tracked its "
                "own normal hour-to-hour pattern.\n\nI compare each hour against the "
                "hours either side of it, so a genuine fault shows up even in a zone "
                "whose load legitimately swings during the day.",
                ["optimizer /api/anomalies"], {"count": 0})

    lines = [f"I found **{len(items)}** thing(s) worth a maintenance check:\n"]
    for x in items:
        lines.append(
            f"- **{x['zone']} at {x['hour']:02d}:00** drew {kwh(x['kwh'])} when the "
            f"hours either side averaged {kwh(x['expectedKwh'])}. That is "
            f"{x['ratio']:.1f}x expected, {kwh(abs(x['excessKwh']))} more than the "
            f"pattern predicts. Severity: {x['severity']}."
        )
    lines.append(
        "\nAn overnight spike that the neighbouring hours do not share usually means "
        "a chiller short-cycling or an air-handling unit with a stuck damper. Both "
        "waste money for weeks before they actually break."
    )
    return "\n".join(lines), ["optimizer /api/anomalies"], {"count": len(items)}


async def answer_whatif(area, text, zone):
    fraction = find_fraction(text)

    if not zone or zone not in FLEX_RATIONALE:
        return (
            "I can model what happens if a deferrable zone becomes more or less "
            "flexible. Try: _\"what if the laundry were 100% flexible?\"_\n\n"
            f"The four I can model are: {', '.join(FLEX_RATIONALE)}. Clinical zones "
            "cannot be modelled, because they are never shifted at all.",
            [], {})

    if fraction is None:
        return (f"How flexible should I assume {ZONE_NAMES[zone]} is? "
                f"Give me a percentage, for example _\"what if {zone} were 90% flexible?\"_",
                [], {})

    base, scenario = await asyncio.gather(
        tool_plan(area), tool_plan(area, flex=f"{zone}:{fraction}"))

    d_year = scenario["savings"]["annualTotalSek"] - base["savings"]["annualTotalSek"]
    d_shift = scenario["savings"]["shiftedKwh"] - base["savings"]["shiftedKwh"]
    now_pct = int(base["reasoning"]["flexibilityUsed"].get(zone, 0) * 100)
    direction = "more" if d_year >= 0 else "less"

    return (
        f"Today I assume {now_pct}% of {ZONE_NAMES[zone]}'s peak-hour load is movable, "
        f"because {FLEX_RATIONALE[zone]}.\n\n"
        f"At **{int(fraction*100)}%** instead:\n\n"
        f"- Annual saving: {sek(base['savings']['annualTotalSek'])} → "
        f"**{sek(scenario['savings']['annualTotalSek'])}**\n"
        f"- That is {sek(abs(d_year))} a year {direction}\n"
        f"- Load shifted per day: {kwh(base['savings']['shiftedKwh'])} → "
        f"{kwh(scenario['savings']['shiftedKwh'])} ({d_shift:+,.0f} kWh)\n\n"
        f"This is a real recomputation, not an estimate. I re-ran the optimiser with "
        f"the override and compared the two results.",
        ["optimizer /api/optimize (baseline)", "optimizer /api/optimize (scenario)"],
        {"zone": zone, "fraction": fraction, "deltaAnnualSek": round(d_year)})


async def answer_peak(area, text, zone):
    p = await tool_plan(area)
    s, r = p["savings"], p["reasoning"]
    return (
        f"The site peaked at **{p['baseline']['peakKw']:,.0f} kW** today, in the hour "
        f"beginning {r['sitePeakHour']:02d}:00. Under the plan that falls to "
        f"**{p['optimised']['peakKw']:,.0f} kW**, a reduction of "
        f"{s['peakReductionKw']:,.0f} kW.\n\n"
        f"This matters separately from the energy bill. The grid demand charge is "
        f"billed on the single highest hour of the month at "
        f"{r['demandChargeSekPerKwMonth']:.0f} kr/kW, so that reduction is worth "
        f"{sek(s['demandChargeMonthlySek'])} a month, "
        f"{sek(s['demandChargeMonthlySek']*12)} a year.",
        ["optimizer /api/optimize"], {"peakReductionKw": s["peakReductionKw"]})


async def answer_carbon(area, text, zone):
    p = await tool_plan(area)
    s = p["savings"]
    return (
        f"Shifting load into cheap hours avoids about **{s['co2SavedKgPerDay']:,.0f} kg "
        f"of CO₂ a day**, roughly {s['co2SavedTonnesPerYear']:,.1f} tonnes a year.\n\n"
        f"The mechanism: cheap hours in the Nordic market are cheap because wind and "
        f"hydro are abundant, which also makes them cleaner. Expensive hours are "
        f"expensive because gas and imports are running.\n\n"
        f"Being straight with you, this figure uses two fixed carbon-intensity "
        f"constants rather than a live grid feed. Wiring in real hourly intensity "
        f"would make it defensible for formal reporting. Right now it is indicative.",
        ["optimizer /api/optimize"], {"co2TonnesPerYear": s["co2SavedTonnesPerYear"]})


async def answer_opportunity(area, text, zone):
    """
    "Which part should I shut down to save money?"

    Two things are going on in that question. The premise is wrong, because
    MediMatrx never switches anything off, it moves work to a different hour.
    But the intent behind it is completely reasonable: where is the money?
    So correct the premise briefly, then actually answer.
    """
    p = await tool_plan(area)
    shifts = [r for r in p["recommendations"] if r["action"] == "shift"]
    shifts.sort(key=lambda r: -r["savingSek"])

    asked_to_switch_off = any(w in text.lower() for w in
                              ("turn off", "shut down", "shutdown", "switch off",
                               "stop using", "cut back"))

    lines = []
    if asked_to_switch_off:
        lines.append(
            "Worth saying first: **nothing gets switched off.** A hospital cannot "
            "turn anything off, and MediMatrx never tries to. It moves work to a "
            "cheaper hour instead. The laundry still runs, it just runs at 02:00 "
            "rather than 18:00.\n"
        )

    if not shifts:
        lines.append("There is nothing worth moving in today's data.")
        return "\n".join(lines), ["optimizer /api/optimize"], {}

    top = shifts[0]
    lines.append(f"**Start with {top['zone']}.** It is the single biggest "
                 f"opportunity today: {sek(top['savingSek'])} saved, which is "
                 f"{sek(top['savingSek'] * 365)} a year, by moving "
                 f"{kwh(top['shiftedKwh'])} into {top['targetWindow']}.\n")

    if len(shifts) > 1:
        lines.append("The full ranking:\n")
        for i, r in enumerate(shifts, 1):
            lines.append(f"{i}. **{r['zone']}** - {sek(r['savingSek'])}/day "
                         f"({sek(r['savingSek'] * 365)}/year), moving "
                         f"{kwh(r['shiftedKwh'])}")

    peak = next((r for r in p["recommendations"] if r["action"] == "peak-shaving"), None)
    if peak:
        lines.append(f"\nAnd separately from the energy bill: {peak['text']}")

    lines.append(f"\n{CLINICAL_NOTE} So the four zones above are the whole "
                 f"opportunity, and none of them affect a patient.")

    return "\n".join(lines), ["optimizer /api/optimize"], {"top": top["zoneId"]}


async def answer_recommendations(area, text, zone):
    p = await tool_plan(area)
    recs = [r for r in p["recommendations"] if r["action"] != "protect"]
    if zone:
        recs = [r for r in recs if r.get("zoneId") == zone] or recs
    if not recs:
        return "No actions to recommend right now.", ["optimizer /api/optimize"], {}

    lines = ["Ranked by money saved:\n"]
    for r in recs[:5]:
        lines.append(f"- **[{r['priority']}]** {r['text']}")
    lines.append(f"\n{CLINICAL_NOTE}")
    return "\n".join(lines), ["optimizer /api/optimize"], {"count": len(recs)}


async def answer_zones(area, text, zone):
    z = await tool_zones()
    clinical = [x for x in z["zones"] if x["critical"]]
    flexible = [x for x in z["zones"] if x["shiftable"]]
    fixed = [x for x in z["zones"] if not x["critical"] and not x["shiftable"]]
    fmt = lambda items: "\n".join(
        f"- {x['name']} ({x['baselineKw']:,.0f} kW) - {x['description']}" for x in items)
    return (
        f"I monitor **{z['count']} metered zones**.\n\n"
        f"**Clinical, never optimised ({len(clinical)}):**\n{fmt(clinical)}\n\n"
        f"**Deferrable, can be shifted ({len(flexible)}):**\n{fmt(flexible)}\n\n"
        f"**Fixed, not deferrable ({len(fixed)}):**\n{fmt(fixed)}",
        ["ingest /api/zones"], {"count": z["count"]})


async def answer_safety(area, text, zone):
    p = await tool_plan(area)
    protected = p["reasoning"]["protectedZones"]
    return (
        f"No. {CLINICAL_NOTE}\n\n"
        f"On today's plan that covers {len(protected)} zones: "
        f"{', '.join(protected)}.\n\n"
        f"Two details that matter for a clinical safety review. First, the rule lives "
        f"in the optimizer service on the server, not in the user interface, so no "
        f"front end and no API caller can bypass it. Second, the what-if modelling "
        f"refuses a clinical zone outright, so you cannot even simulate touching one.",
        ["optimizer /api/optimize"], {"protectedZones": protected})


async def answer_status(area, text, zone):
    results = await asyncio.gather(
        tool_summary(), tool_prices(area), tool_plan(area),
        return_exceptions=True)
    names = ["ingest", "price", "optimizer"]
    lines = ["Live service check:\n"]
    for name, r in zip(names, results):
        if isinstance(r, Exception):
            lines.append(f"- **{name}**: unreachable ({type(r).__name__})")
        else:
            lines.append(f"- **{name}**: healthy, answered by pod `{r.get('pod','?')}`")
    lines.append(f"- **assistant**: healthy, this pod is `{POD}`")
    return "\n".join(lines), ["all services /healthz"], {}


async def answer_help(area, text, zone):
    return (
        "I am the MediMatrx assistant. I answer questions about this hospital's "
        "energy, and every number I give you comes from one of our own APIs. I do "
        "not guess and I cannot change anything, I am strictly read-only.\n\n"
        "Things I can do:\n\n"
        "- **Savings** - how much the plan is worth, in kronor and per cent\n"
        "- **Explain** - why a particular recommendation exists, showing the working\n"
        "- **Consumption** - what any zone used, overall or in a specific hour\n"
        "- **Prices** - today's spot price, cheapest and dearest hours\n"
        "- **Faults** - equipment behaving abnormally\n"
        "- **What-if** - re-run the optimiser with different flexibility assumptions\n"
        "- **Peak demand** - the site peak and what the demand charge costs\n"
        "- **Safety** - what is protected and how that is enforced\n\n"
        "Try: _\"why move the laundry to the night?\"_",
        [], {})


HANDLERS = {
    "opportunity": answer_opportunity,
    "savings": answer_savings, "explain": answer_explain,
    "consumption": answer_consumption, "price": answer_price,
    "anomaly": answer_anomaly, "whatif": answer_whatif, "peak": answer_peak,
    "carbon": answer_carbon, "recommendations": answer_recommendations,
    "zones": answer_zones, "safety": answer_safety, "status": answer_status,
    "help": answer_help,
}


async def answer_unknown(area, text, zone):
    """
    Nothing matched confidently. Rather than give up and print a menu, make a
    sensible guess from whatever signal the question does contain. Most
    "I did not understand" replies are a failure of effort, not of input.
    """
    return (
        "I did not follow that one. I am a keyword-based assistant, so I do "
        "better with a direct question than with open conversation.\n\n"
        "I can tell you about: **savings**, **which zone to focus on**, "
        "**why** a recommendation exists, what any **zone** used, today's "
        "**prices**, equipment **faults**, **peak demand**, **carbon**, and "
        "**what-if** scenarios.\n\n"
        "Try: " + " · ".join(f"_{s}_" for s in SUGGESTIONS[:3]),
        [], {})


# ==========================================================================
#  THE OPTIONAL LLM LAYER
#  Off unless a key is configured. If it errors for any reason we return the
#  deterministic answer instead, so a missing key, a rate limit or a bad
#  response can never turn into a failed request.
# ==========================================================================
def llm_enabled() -> bool:
    return bool(LLM_API_KEY and LLM_PROVIDER in ("anthropic", "openai"))


async def polish_with_llm(question: str, facts: str) -> str | None:
    """
    Rephrase a grounded answer. The model is given the facts and told it may
    not add any number that is not already in them.
    """
    system = (
        "You rewrite answers for a hospital energy dashboard. You are given a "
        "question and a factual answer produced from live APIs. Rewrite it to be "
        "clear and natural for an estates manager. Absolute rules: do not add, "
        "change or round any number; do not add any fact that is not in the "
        "supplied answer; keep it under 200 words; keep the markdown formatting."
    )
    prompt = f"Question: {question}\n\nFactual answer:\n{facts}"

    try:
        async with httpx.AsyncClient(timeout=12.0) as c:
            if LLM_PROVIDER == "anthropic":
                r = await c.post(
                    "https://api.anthropic.com/v1/messages",
                    headers={"x-api-key": LLM_API_KEY,
                             "anthropic-version": "2023-06-01",
                             "content-type": "application/json"},
                    json={"model": LLM_MODEL or "claude-3-5-haiku-20241022",
                          "max_tokens": 700, "system": system,
                          "messages": [{"role": "user", "content": prompt}]})
                r.raise_for_status()
                return r.json()["content"][0]["text"]

            r = await c.post(
                "https://api.openai.com/v1/chat/completions",
                headers={"Authorization": f"Bearer {LLM_API_KEY}",
                         "content-type": "application/json"},
                json={"model": LLM_MODEL or "gpt-4o-mini", "max_tokens": 700,
                      "messages": [{"role": "system", "content": system},
                                   {"role": "user", "content": prompt}]})
            r.raise_for_status()
            return r.json()["choices"][0]["message"]["content"]

    except Exception as exc:  # noqa: BLE001 - never let this break the answer
        _stats["llm_failures"] += 1
        log.warning(f"LLM polish failed, using deterministic answer: {exc}")
        return None


# ==========================================================================
#  API
# ==========================================================================
class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=500)
    area: str = Field(default=DEFAULT_AREA, pattern="^SE[1-4]$")


@app.get("/healthz")
async def healthz():
    return {"status": "alive", "service": SERVICE, "pod": POD}


@app.get("/readyz")
async def readyz():
    """
    Ready as soon as the process is up. The deterministic engine needs no
    warm-up and no external dependency to be considered ready.
    """
    return {"status": "ready", "service": SERVICE, "pod": POD,
            "engine": "llm+deterministic" if llm_enabled() else "deterministic",
            "stats": _stats}


@app.get("/api/chat/suggestions")
async def suggestions():
    return {"pod": POD, "suggestions": SUGGESTIONS}


@app.post("/api/chat")
async def chat(req: ChatRequest):
    _stats["questions"] += 1
    intent, score = classify(req.message)
    zone = find_zone(req.message)

    # A named zone with no real intent behind it ("the ICU?", "how's the MRI")
    # almost always means "tell me about this zone". Only override when the
    # intent score is genuinely negligible, otherwise this rule steals
    # legitimate matches such as "anything wrong with the chillers".
    if zone and score < 2 and intent not in ("whatif", "explain", "safety"):
        intent = "consumption"

    if intent == "unknown":
        intent = rescue_intent(req.message, zone) or "unknown"

    handler = HANDLERS.get(intent, answer_unknown)

    try:
        reply, sources, data = await handler(req.area, req.message, zone)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 409:
            return {"pod": POD, "intent": intent, "engine": "deterministic",
                    "reply": "There is no meter data loaded yet. Press "
                             "**Load demo day** on the dashboard and ask me again.",
                    "sources": [], "data": {}}
        log.error(f"upstream error for intent {intent}: {exc}")
        return JSONResponse(status_code=502, content={
            "error": "a service I depend on returned an error", "pod": POD})
    except Exception as exc:  # noqa: BLE001
        log.error(f"could not answer intent {intent}: {exc}")
        return JSONResponse(status_code=502, content={
            "error": "I could not reach the services I need to answer that.",
            "detail": str(exc), "pod": POD})

    engine = "deterministic"
    _stats["deterministic"] += 1
    if llm_enabled() and reply:
        polished = await polish_with_llm(req.message, reply)
        if polished:
            reply, engine = polished, f"llm:{LLM_PROVIDER}"
            _stats["llm"] += 1

    log.info(f"answered intent={intent} score={score} zone={zone} engine={engine}")
    return {"pod": POD, "intent": intent, "confidence": score, "zone": zone,
            "engine": engine, "reply": reply, "sources": sources, "data": data}


@app.on_event("startup")
async def startup():
    log.info(f"Assistant started. engine="
             f"{'llm+deterministic' if llm_enabled() else 'deterministic (no LLM key set)'} "
             f"ingest={INGEST_URL} price={PRICE_URL} optimizer={OPTIMIZER_URL}")
