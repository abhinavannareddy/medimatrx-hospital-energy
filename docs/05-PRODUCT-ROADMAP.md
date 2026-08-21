# MediMatrx: from demo to product

What stands between the current system and something a hospital would sign a
contract for. Ordered by business value, not by how interesting it is to build.

---

## Where we are

Shipped and working:

- Five independently scalable microservices, MongoDB on persistent storage
- Live Swedish spot prices with graceful degradation when the upstream fails
- Load-shifting optimiser with a hard clinical-safety rule enforced server-side
- Neighbour-comparison anomaly detection for equipment faults
- What-if scenario modelling
- A read-only natural-language assistant grounded in the platform's own APIs

Honest status: this is a **credible demonstrator on simulated meter data**.
The prices are real. The consumption is modelled.

---

## Tier 1: without these it is a dashboard, not a product

### 1. Measurement and Verification

**The single biggest unlock.** Right now the platform *claims* savings. It
cannot *prove* them.

M&V means establishing a weather-and-occupancy-normalised baseline of what the
hospital would have used, then reporting actual against that baseline. The
international standard for this is IPMVP.

Why it matters commercially: with credible M&V you can sell on **shared
savings**. The hospital pays nothing upfront and gives you a share of verified
savings. That removes procurement as an obstacle, and for a public hospital
procurement is the real obstacle, not price.

Without M&V, every number in the dashboard is marketing.

### 2. Day-ahead planning

The system is currently retrospective: *"you could have saved this yesterday."*

Nordic day-ahead prices for tomorrow are published each afternoon. Turning the
output into *"here is tomorrow's plan, approve it"* changes the product from a
report card into an operational tool. This is a moderate amount of work and it
transforms how the product is perceived.

### 3. Multi-tenancy

Organisations, sites, zones. Without it, customer number two needs a second
deployment. This is the difference between a project and a SaaS business.

### 4. Authentication, authorisation and TLS

Already documented as gaps in the report. OIDC against the hospital's identity
provider, role-based access so estates staff read and only service accounts
write, mutual TLS for meter endpoints. Nothing gets past hospital IT without
these.

### 5. Audit logging

Who saw what, who approved which plan, what the system did and when. Required
in healthcare procurement and needed for M&V credibility.

---

## Tier 2: the differentiators

### Grid services revenue (FCR / aFRR)

**The idea most likely to make this a business rather than a cost-saving tool.**

Hospitals already own backup generators and UPS systems that sit idle almost
all the time. Svenska kraftnät procures frequency reserves, and its own
[balancing market outlook to 2030](https://www.svk.se/49db02/siteassets/aktorsportalen/bidra-med-reserver/behov-av-reserver-idag/framtidsrapport-om-balansmarknaderna/balancing-market-outlook-2030-2026-update.pdf)
projects growing reserve need.

This flips energy from a cost centre into a **revenue line**, which is a very
different sales conversation. Nobody is telling hospitals this.

Caveats to resolve before promising anything: the technical requirements for
prequalification are strict (response time, availability, telemetry), and a
hospital's backup capacity exists for patient safety first. That constraint is
non-negotiable and must be designed around, not around-argued.

### Weather integration

SMHI publishes free open APIs. HVAC load is largely a function of outdoor
temperature. Without weather, both the forecasts and the M&V baseline are weak.
Cheap to add, high leverage, and a prerequisite for Tier 1 item 1.

### Live carbon intensity

The CO2 figure currently uses two fixed constants. Replacing them with real
hourly grid intensity makes the sustainability number defensible in reporting
rather than merely indicative. The assistant already says this out loud when
asked, which is the right behaviour but not a permanent answer.

### Control integration

BACnet or Modbus into the building management system, so recommendations
execute rather than being retyped by a human. This is where value multiplies,
and also where clinical safety sign-off becomes mandatory.

Sensible intermediate step: export the plan as a schedule or work order that
the BMS team imports. Lower risk, most of the benefit, no safety case needed.

### Peak-demand alarm

Real-time warning *before* the site sets a new monthly peak. Demand charges are
billed on a single hour, so catching it live is worth real money. Needs no
machine learning, just a threshold and a notification channel.

### Anomaly to work order

Push detected faults into the existing maintenance system so an alert becomes a
closed loop with an owner and a due date.

---

## Tier 3: enterprise polish

Auto-generated monthly board report · SSO · mobile and wall-display views ·
partner API · battery and thermal storage optimisation · per-zone flexibility
learned from history instead of the current fixed percentages.

---

## What matters more than any feature

**Get one pilot, not ten features.** One real hospital with real meter data
will teach more than six months of building. The simulated data is carefully
constructed, but it is a *model* of a hospital and reality will differ in ways
that cannot be guessed. BTH's links to Region Blekinge are a genuine advantage.

**The technology is not the moat.** A competent team could rebuild the
optimiser in a fortnight. The moat is verified savings history, BMS
integrations, and clinical safety approval. Build toward those.

**The safety rule is the best sales asset.** "Clinical zones are never touched,
enforced server-side, stated in every API response, and not even simulatable in
what-if" is the sentence that gets past a clinical safety officer. Most energy
vendors cannot say it, because they never designed for it.

---

## Deliberately not on this list

**CSRD sustainability reporting.** An earlier version of this roadmap proposed
it as a compliance wedge. On checking, the Omnibus I package
[significantly narrowed CSRD's scope](https://accountancyeurope.eu/publications/omnibus-explained-key-changes-to-the-csrd-and-csddd/),
and Swedish regional hospitals are public bodies largely outside it in any
case. Verify before building anything against a regulation.

---

## Sources

- [Svenska kraftnät, Balancing market outlook 2030 (2026 update)](https://www.svk.se/49db02/siteassets/aktorsportalen/bidra-med-reserver/behov-av-reserver-idag/framtidsrapport-om-balansmarknaderna/balancing-market-outlook-2030-2026-update.pdf)
- [Svenska kraftnät, Information on different ancillary services](https://www.svk.se/en/stakeholders-portal/electricity-market/provision-of-ancillary-services/information-on-different-ancillary-services/)
- [Accountancy Europe, Omnibus explained: key changes to the CSRD and CSDDD](https://accountancyeurope.eu/publications/omnibus-explained-key-changes-to-the-csrd-and-csddd/)
