"""
The assistant's vocabulary.

Everything the assistant understands about the hospital lives here, separate
from the logic that uses it. Adding a new zone or a new phrasing means
editing this file only.
"""

# ---------------------------------------------------------------------------
# How a human might refer to each metered zone.
# ---------------------------------------------------------------------------
ZONE_SYNONYMS = {
    "icu": ["icu", "intensive care", "intensive", "critical care", "iva"],
    # "or" and "op" are deliberately absent: as whole words they collide with
    # ordinary English and would tag half the questions as theatre questions.
    "theatres": ["theatre", "theatres", "operating", "surgery", "surgical",
                 "operating room", "operating theatre"],
    "imaging": ["imaging", "mri", "ct", "x-ray", "xray", "radiology",
                "scanner", "diagnostic"],
    "wards": ["ward", "wards", "inpatient", "patient room", "bed"],
    "hvac": ["hvac", "chiller", "chillers", "air handling", "ahu",
             "ventilation", "cooling", "heating", "climate", "air con",
             "air conditioning", "plant"],
    "sterilisation": ["steril", "sterilisation", "sterilization", "cssd",
                      "autoclave", "autoclaves", "disinfect"],
    "laundry": ["laundry", "linen", "washing", "washer", "dryer", "dryers"],
    "catering": ["catering", "kitchen", "cooking", "food", "dishwash",
                 "canteen", "meal"],
    "admin": ["admin", "administration", "office", "offices", "it room"],
}

# Zones the optimiser is allowed to shift, and why each has the limit it has.
FLEX_RATIONALE = {
    "hvac": "thermal mass lets you pre-cool the building, but only so far "
            "before comfort and infection-control airflow targets are affected",
    "sterilisation": "autoclave batches can run overnight as long as the trays "
                     "are sterile and ready for the first theatre list",
    "laundry": "industrial washing is almost fully deferrable, it only has to "
               "be finished before the morning linen round",
    "catering": "cold storage has to run constantly, but bulk cooking and "
                "dishwashing can move",
}

CLINICAL_NOTE = (
    "Clinical zones (ICU, operating theatres, imaging and wards) are excluded "
    "from optimisation by a hard rule in the optimizer service. They are never "
    "shifted, reduced or delayed, whatever the electricity price does."
)

# ---------------------------------------------------------------------------
# Intents. Each keyword carries a weight; the highest-scoring intent wins.
# Weight 3 = unambiguous, 2 = strong, 1 = supporting evidence.
# ---------------------------------------------------------------------------
INTENTS = {
    "whatif": [
        ("what if", 3), ("what would happen", 3), ("suppose", 3),
        ("scenario", 3), ("if we made", 3), ("if the", 1), ("instead of", 2),
        ("more flexible", 3), ("less flexible", 3), ("increase", 1),
        ("could we", 2), ("how much more", 2),
    ],
    "explain": [
        ("why", 3), ("explain", 3), ("reason", 2), ("justify", 3),
        ("how did you", 3), ("how do you decide", 3), ("what makes", 2),
        ("logic", 2), ("working", 1), ("basis", 2),
    ],
    "savings": [
        ("saving", 3), ("save", 3), ("money", 2), ("cost cut", 3),
        ("how much", 2), ("roi", 3), ("return", 1), ("worth", 2),
        ("kronor", 1), ("sek", 1), ("bill", 2),
    ],
    "anomaly": [
        ("anomal", 3), ("fault", 3), ("broken", 3), ("wrong", 2),
        ("not right", 3), ("misbehav", 3), ("check on", 2),
        ("maintenance", 3), ("problem", 2), ("issue", 2), ("alert", 2),
        ("warning", 2), ("failing", 3), ("odd", 2), ("strange", 2),
        ("unusual", 3), ("short-cycling", 3), ("damper", 3),
    ],
    "price": [
        ("price", 3), ("spot", 3), ("cheapest", 3), ("expensive", 3),
        ("tariff", 3), ("per kwh", 3), ("electricity cost", 3),
        ("market", 2), ("when is it cheap", 3), ("dearest", 3),
    ],
    "consumption": [
        ("consumption", 3), ("consume", 3), ("using", 2), ("used", 2),
        ("use", 1), ("draw", 2), ("drew", 2), ("kwh", 2), ("demand", 1),
        ("load", 1), ("how much power", 3), ("energy use", 3),
    ],
    "peak": [
        ("peak", 3), ("demand charge", 3), ("maximum", 2), ("highest hour", 3),
        ("capacity charge", 3), ("grid charge", 3), ("busiest", 2),
    ],
    # Weight 6: these words are decisive. They must outrank "save" (3) plus
    # "how much" (2), otherwise "how much CO2 do we save" is handed to the
    # savings intent.
    "carbon": [
        ("carbon", 6), ("co2", 6), ("emission", 6), ("green", 2),
        ("sustainab", 6), ("climate", 2), ("footprint", 6), ("environment", 2),
    ],
    # People ask "what should I turn off" far more often than "what should I
    # shift". The premise is wrong (MediMatrx never switches anything off, it
    # moves work in time) so this intent exists to answer the question they
    # actually meant AND correct the premise.
    "opportunity": [
        ("which zone", 5), ("which part", 5), ("which area", 5),
        ("which department", 5), ("what part", 5), ("where should", 5),
        ("biggest", 4), ("focus on", 4), ("priority", 3), ("start with", 4),
        ("most saving", 5), ("best opportunity", 5), ("worst offender", 5),
        ("turn off", 5), ("shut down", 5), ("shutdown", 5), ("switch off", 5),
        ("stop using", 4), ("cut back", 4), ("biggest saving", 5),
        ("where is the money", 5), ("low hanging", 5),
    ],
    "recommendations": [
        ("recommend", 3), ("what should", 3), ("action", 2), ("advice", 3),
        ("what can i do", 3), ("how do i save", 3), ("help me save", 3),
        ("suggest", 3), ("plan", 2), ("what do i do", 3), ("next step", 2),
        ("to do", 1),
    ],
    "zones": [
        ("zones", 3), ("departments", 3), ("what do you monitor", 3),
        ("what do you measure", 3), ("meters", 2), ("areas", 2),
        ("list", 1), ("coverage", 2),
    ],
    "safety": [
        ("safe", 3), ("safety", 3), ("clinical", 2), ("patient", 3),
        ("dangerous", 3), ("risk", 2), ("touch the icu", 3),
        ("affect care", 3), ("harm", 3),
    ],
    "status": [
        ("status", 3), ("healthy", 3), ("running", 2), ("pods", 3),
        ("up and", 2), ("working", 1), ("online", 2), ("infrastructure", 3),
    ],
    "help": [
        ("help", 3), ("what can you", 3), ("who are you", 3),
        ("what are you", 3), ("hello", 3), ("hi ", 3), ("hey", 3),
        ("capabilit", 3), ("commands", 2),
    ],
}

SUGGESTIONS = [
    "How much can we save?",
    "Which zone should I focus on?",
    "Why move the laundry to the night?",
    "What did the ICU use last night?",
    "Is anything broken?",
    "What if the laundry were 100% flexible?",
    "When is electricity cheapest today?",
    "Do you ever touch the ICU?",
]
