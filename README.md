# MediWatt: Hospital Energy Optimisation Platform

A microservice application that cuts a hospital's electricity bill by moving
deferrable work into cheap-electricity hours, **without ever touching clinical
load**.

Built for the Cloud Computing assignment at Blekinge Institute of Technology.
Uses **live Swedish electricity spot prices** from the public
[elprisetjustnu.se](https://www.elprisetjustnu.se) API.

---

## What it does

Swedish electricity is priced hourly on a day-ahead market, and the cheapest hour
of the day typically costs about a third of the most expensive hour. Hospitals
run 24/7 and spend 8-15 MSEK a year on power, but only part of that load is
negotiable.

MediWatt separates the two:

- **Clinical load**: ICU, operating theatres, imaging, wards. Never touched.
  This is enforced server-side, in the optimizer, where a user interface cannot
  bypass it.
- **Deferrable load**: laundry, sterilisation, catering, HVAC pre-cooling. The
  work still happens today; *when* it happens is a choice.

It then reports the money saved, the reduction in peak demand (a separate bill),
the carbon avoided, and any equipment behaving like it is about to fail.

On the demo dataset: **~12% off the energy bill plus a peak-demand reduction, in the order of 1.3-1.6 MSEK per year for a single hospital.**

---

## Architecture

```
Browser ──► API Gateway (Node.js) ──┬──► Ingest Service (Node.js) ──► MongoDB
             NodePort :30080        │         owns the data          StatefulSet
             the only way in        │                                    + PVC
                                    ├──► Price Service (Python) ──► elprisetjustnu.se
                                    │         caches 15 min            (public API)
                                    │
                                    └──► Optimizer Service (Python)
                                              stateless brain
                                              calls ingest + price
```

| Service | Language | Replicas | Owns state | Role |
|---|---|---|---|---|
| **gateway** | Node.js / Express | 2-10 | no | Single public entry point; serves the dashboard; routing, rate limiting, security headers |
| **ingest** | Node.js / Express | 2-12 | **MongoDB** | Receives and stores meter readings; serves 24-hour aggregates |
| **price** | Python / FastAPI | 2-4 | cache only | Fetches live spot prices; caches; degrades gracefully when upstream fails |
| **optimizer** | Python / FastAPI | 2-15 | no | Computes the load-shifting plan, savings and anomalies |
| **mongodb** | MongoDB 7.0 | 1 | **yes** | Persistent storage on a PersistentVolumeClaim |

**Patterns used:** API Gateway · Database per Service · Backend for Frontend ·
Service Discovery · Cache-Aside · Graceful Degradation · Retry with Backoff ·
Health/Readiness Separation · Bulkhead & Fail-Fast · Stateless Compute ·
Externalised Configuration.

Full design rationale, benefits, challenges and the security analysis are in
**[`docs/01-REPORT.md`](docs/01-REPORT.md)**.

---

## Quick start

**Prerequisites:** Docker Desktop with Kubernetes enabled, and a free Docker Hub
account.

```powershell
# 1. Build the four images and push them to your Docker Hub account
powershell -ExecutionPolicy Bypass -File .\scripts\1-build-and-push.ps1

# 2. Deploy all 30 Kubernetes objects and load a demo day
powershell -ExecutionPolicy Bypass -File .\scripts\2-deploy.ps1
```

Then open **http://localhost:30080**

Step-by-step instructions written for a complete beginner, including every way it
can go wrong: **[`docs/02-RUN-GUIDE.md`](docs/02-RUN-GUIDE.md)**

### Without Kubernetes

To run the whole system locally in about 30 seconds, useful for telling code
problems apart from cluster problems:

```powershell
docker compose up --build
```
Then open http://localhost:8080

---

## Demonstrations

```powershell
# Independent horizontal scaling: scale ONE service, show the others unchanged,
# then watch 20 requests spread across the new replicas
powershell -ExecutionPolicy Bypass -File .\scripts\3-demo-scaling.ps1

# Persistent storage: destroy the database pod and show the data survives
powershell -ExecutionPolicy Bypass -File .\scripts\4-demo-persistence.ps1
```

---

## Repository layout

```
mediwatt/
├── services/
│   ├── gateway/        Node.js  - API gateway + the dashboard (public/index.html)
│   ├── ingest/         Node.js  - meter data, MongoDB owner
│   ├── price/          Python   - live electricity prices
│   └── optimizer/      Python   - the optimisation brain
├── k8s/
│   ├── 00-namespace.yaml
│   ├── 01-config-and-secrets.yaml
│   ├── 02-mongodb.yaml            StatefulSet + PersistentVolumeClaim
│   ├── 03-ingest.yaml
│   ├── 04-price.yaml
│   ├── 05-optimizer.yaml
│   ├── 06-gateway.yaml            NodePort (+ commented Ingress)
│   ├── 07-autoscaling.yaml        4 × HPA, 3 × PodDisruptionBudget
│   └── 08-network-policy.yaml     default-deny + 9 explicit allows
├── scripts/            numbered PowerShell scripts for Windows
├── docs/
│   ├── 01-REPORT.md    the assignment report
│   ├── 02-RUN-GUIDE.md step-by-step instructions
│   ├── 03-VIDEO-SCRIPT.md
│   └── 04-QA-PREP.md   likely examiner questions and answers
└── docker-compose.yml  run everything without Kubernetes
```

---

## REST API

Everything is reachable through the gateway at `http://localhost:30080`.

### Consumption (ingest service)

```
GET  /api/zones                      the nine metered zones
POST /api/readings                   store a reading  {"zoneId":"icu","kwh":148.2}
GET  /api/readings?zone=&limit=      raw readings, newest first
GET  /api/summary                    per-zone, per-hour totals for the last 24h
POST /api/simulate                   load a realistic demo day (?faults=0 to skip)
```

### Prices (price service)

```
GET  /api/prices?area=SE4            today's 24 hourly prices, live
GET  /api/prices/cheapest-window?hours=3
GET  /api/stats                      cache and upstream counters
```

### Optimisation (optimizer service)

```
GET  /api/optimize?area=SE4          the plan, the savings, the recommendations
GET  /api/anomalies                  equipment faults detected
```

### Operational

```
GET  /healthz                        liveness  - on every service
GET  /readyz                         readiness - on every service
GET  /api/topology                   which pod is serving each service
```

---

## Requirements checklist

| Requirement | Where it is met |
|---|---|
| Deployable using Kubernetes | `k8s/`, 30 objects across 9 files |
| At least two types of microservice + a database | Four services in two languages + MongoDB |
| Each microservice implements a REST API | See the API section above |
| Accessible from outside Kubernetes | NodePort 30080 in a web browser |
| All microservices independently horizontally scalable | 4 separate HPAs in `07-autoscaling.yaml` |
| Images pushed to Docker Hub | `scripts/1-build-and-push.ps1` |
| Database as a separate microservice | MongoDB StatefulSet |
| Storage persistent across restarts | `volumeClaimTemplates`, proven by `scripts/4-demo-persistence.ps1` |
| Programmatically connect to and use a REST API | Price service → elprisetjustnu.se; optimizer → ingest + price |
| Acknowledge if too small to warrant scaling | `docs/01-REPORT.md` §1, "Scale, honestly" |

---

## Known limitations

Stated openly, with the fix, in `docs/01-REPORT.md` §5 and §6:

- **No authentication**: anyone who can reach port 30080 can use it. Fix: OIDC at
  the ingress with role-based access.
- **Plain HTTP inside the cluster**. Fix: a service mesh with mutual TLS.
- **Kubernetes Secrets are base64, not encrypted**. Fix: an external vault with
  rotating credentials.
- **NetworkPolicies are not enforced on Docker Desktop**: the objects are correct
  but its default CNI ignores them. Enforced on Calico or Cilium.
- **Rate limiting is per-pod**, so the real limit is (limit × replicas). Fix:
  Redis, or rate limit at the ingress.
- **Single MongoDB pod**: a single point of failure, accepted because the
  assignment states the database need not be scalable. Fix: a three-member replica
  set with tested backups.

---

## Licence

MIT licence. Coursework project.
