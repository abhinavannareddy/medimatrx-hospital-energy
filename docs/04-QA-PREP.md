# Question and answer preparation

The assignment says you must be prepared to answer questions about six things.
This document is one section per thing, with the questions an examiner is most
likely to ask and an answer you can give in your own words.

**Read this until you can answer without looking.** An examiner can tell the
difference between someone who understands their system and someone reading a
script, and the questions are where the difference shows.

---

## Part 0: Understand your own code first

Before the questions, make sure you can explain these five things. They are the
whole system.

### 1. What happens when the page loads

The browser asks the **gateway** for the dashboard. The dashboard's JavaScript
then calls `GET /api/optimize` on the gateway. The gateway forwards that to the
**optimizer**. The optimizer makes two calls at the same time, one to **ingest**
asking "what did the hospital use in each of the last 24 hours?" and one to
**price** asking "what does electricity cost in each hour today?". The optimizer
does the maths and sends the answer back up the chain.

### 2. How the optimisation actually works

Four steps, and you should be able to say them in order:

1. **Work out today's cost.** For every zone and every hour, multiply the kWh
   used by the price in that hour, and add it all up.
2. **Decide which hours to relieve.** An hour needs relieving if *either* the
   price is above the daily average, *or* the whole site is near its daily peak.
   Two reasons, because the hospital pays two different bills: an energy bill
   and a demand charge based on its single highest hour.
3. **Move the flexible load.** For each deferrable zone, take a fixed percentage
   of its load out of the relief hours (85% for laundry, 65% for sterilisation,
   45% for catering, only 30% for HVAC because thermal mass is limited), and put
   it back into the cheapest remaining hours, capped per hour so we never create
   a brand-new spike.
4. **Add it up again** and report the difference.

Clinical zones are skipped entirely in step 3.

### 3. How the anomaly detector works

For each hour, it compares that hour against the average of the hour before and
the hour after. If an hour is 1.6× higher (or less than half) of its neighbours,
and the gap is worth something in absolute terms, it is flagged.

**Be ready for the follow-up "why not standard deviation?"**: because a
hospital's load is *supposed* to swing during the day. The HVAC plant genuinely
runs at four times its night load every afternoon, so measured against the whole
day that is not unusual. What is never legitimate is a step that the neighbouring
hours do not share.

### 4. What each Kubernetes file does

Nine files. You should be able to say each one in a sentence: namespace,
config+secrets, MongoDB, then one per service, then autoscaling, then network
policy.

### 5. Where the two "REST API" requirements are met

- *"programmatically connect to and use a REST API"* → the **price service**
  calling `elprisetjustnu.se` with `httpx`, and the **optimizer** calling ingest
  and price.
- *"program a microservice that provides a REST API"* → all four services
  provide one.

---

## Part 1: Your software application idea

**"Why hospitals?"**

> Three reasons. They're enormous energy users that run 24/7, so the absolute
> savings are large. They have a genuinely clear split between load you may never
> touch and load you may. Most industries don't have that line drawn so sharply.
> And in Sweden public-sector organisations have hard carbon-reduction targets,
> so the environmental result is a purchasing driver, not just a nice-to-have.

**"Is the data real?"**

> The electricity prices are completely real and live, from the public
> elprisetjustnu.se API. The meter readings are simulated from realistic 24-hour
> load profiles for each department type, with noise and two deliberately
> injected equipment faults. In a real deployment the same POST endpoint would be
> called by the hospital's building management system. Nothing else in the
> architecture would change.

**"Would a hospital actually buy this?"**

> The demo dataset shows around 1.3 to 1.6 million kronor a year for one hospital
> against a cloud hosting cost of a few hundred kronor a month. But the honest
> answer is that the technology is the easy part. The hard part is trust: you'd
> start in advisory mode, where MediWatt only recommends and the estates team
> executes, and you'd need clinical safety sign-off before any automatic control.

**"What would you build next?"**

> Three things, in order. First, actual control: right now it recommends, it
> doesn't act; connecting to the building management system over BACnet or Modbus
> is where the value multiplies. Second, weather forecasts, because HVAC load is
> mostly a function of outside temperature. Third, learning each zone's real
> flexibility from history instead of using my fixed percentages.

---

## Part 2: Architecture design decisions

**"Why four microservices and not one application?"**

> Because the three workloads scale with completely different things. Ingest
> scales with the number of meters. The optimizer scales with the number of sites
> being re-planned. The price service doesn't scale at all, because the price is
> identical for everyone in a bidding area. One cache serves a thousand
> hospitals. In a monolith I'd have to scale all three to relieve any one of them,
> and I'd be paying for it.
>
> The second reason is deployment risk. The optimisation algorithm is where the
> product's value is, so it's what changes most often. Because it's separate and
> stateless, I can redeploy it several times a day without touching the service
> that must never lose a meter reading.

**"Isn't this over-engineered for a student project?"**

> For one hospital, yes, and I say so in the report. The application is honestly
> too small to need scaling. The architecture is designed for the SaaS case:
> sixty hospitals, hundreds of meters each, re-planned every fifteen minutes. I'd
> rather demonstrate that I know *why* you'd split a system than split it for the
> sake of the assignment.

**"Why is the optimizer stateless? Why does that matter?"**

> Stateless means it holds nothing between requests. Every answer is computed
> from scratch from data it fetches over REST. That's the precondition for
> horizontal scaling: if any replica can answer any request, Kubernetes can run
> one copy or fifty and the load balancer doesn't need to care which one you get.
> The moment a service holds session state you need sticky routing or a shared
> session store, and scaling gets much harder.

**"Why MongoDB and not PostgreSQL?"**

> Meter readings are schemaless time-series documents, written far more often
> than they're updated, and read back with aggregations. A document store fits
> that without a schema migration every time a new meter type appears. Honestly,
> at very large scale TimescaleDB (Postgres with time-series extensions) would
> probably be the better choice, because it has proper time-series compression
> and continuous aggregates.

**"Why two programming languages? Isn't that a maintenance burden?"**

> It is a real cost, and I'd think twice in a two-person team. The reason it's
> justified here is that the two kinds of work are genuinely different. Node.js
> is non-blocking, which suits high-volume writes and proxying. Python is where
> the numerical and machine-learning ecosystem lives, and the optimizer is where
> ML will eventually go. The reason it's *possible* at all is that the contract
> between services is HTTP and JSON, not a shared runtime.

**"Why REST and not gRPC or a message queue?"**

> REST because it's readable in a browser and with curl, which makes debugging a
> distributed system enormously easier, and because the assignment asks for it.
> gRPC would be measurably faster for the internal calls. A message queue between
> the meters and the ingest service would be the right answer at real scale, it
> decouples the write rate from the database's ability to absorb it and gives you
> a buffer during a database outage. That's on my list.

---

## Part 3: Business implications

**"What does this architecture cost to run?"**

> On a managed Kubernetes service, this fits in a small two-node cluster, roughly 700 to 1500 SEK a month including storage. Against savings measured in
> millions per hospital, hosting is a rounding error. The real cost is
> engineering time: microservices need CI/CD, monitoring and someone who
> understands Kubernetes, and that's a salary, not a server.

**"How does the architecture affect the business model?"**

> It makes multi-tenant SaaS practical. The marginal cost of the next hospital is
> a few more optimizer pods, not a new deployment. And because the price service
> is shared across all customers in a bidding area, the per-customer cost actually
> *falls* as customers are added.
>
> It also makes the pricing model easy to defend: you can charge a percentage of
> verified savings, because the system measures both the baseline and the
> optimised figure.

**"What's the risk of this architecture to the business?"**

> Operational complexity is a real risk for a small company. Thirty Kubernetes
> objects and four images need a build pipeline and someone on call. If MediWatt
> were a startup with two engineers, I'd probably start as a modular monolith and
> split out the optimizer first, when the scaling pressure actually appeared. The
> architecture I've built is the *destination*, and I'd be honest with an
> investor that arriving there on day one costs velocity.

**"Who would you sell to, and what would stop them buying?"**

> The buyer is the estates or facilities director; the blocker is the clinical
> safety officer. That's why the "clinical zones are never touched" rule is
> enforced server-side and stated explicitly in every API response, it's not a
> feature, it's the thing that gets you through the door.

---

## Part 4: Interaction between microservices

**"Walk me through what happens when I press Refresh."**

Use the flow from Part 0, question 1. Then add:

> The important detail is that the optimizer makes its two calls
> **concurrently**, with `asyncio.gather`. Two 200-millisecond calls in parallel
> cost 200 milliseconds, not 400. And every response carries a `servedBy` block
> naming the pods that took part, which is how the dashboard proves load
> balancing is happening.

**"How does one service find another?"**

> Kubernetes DNS. The optimizer calls `http://ingest-service:8080`. That's a
> Service name, not an IP address. Kubernetes resolves it to whichever ingest pods
> are currently healthy and load balances across them. There is no IP address
> anywhere in my code or configuration, which is why pods can restart, move to a
> different node, or multiply, and nothing breaks.

**"What happens if the price service is down?"**

> The optimizer's call fails, it catches that, and it returns a 503 naming which
> dependency failed. The dashboard shows a clear error instead of hanging.
>
> But more interestingly: the price service is designed so that this almost never
> happens. If its *upstream*, the public price API, is down, the price service
> doesn't fail. It serves its stale cache, and if it has no cache it serves a
> modelled price curve, marked as `modelled-fallback` in the response and labelled
> in the UI. That's graceful degradation: one number gets less accurate instead of
> the whole dashboard going blank.

**"What happens if MongoDB is down?"**

> The ingest service's readiness probe starts failing, so Kubernetes takes those
> pods out of the load balancer. They stay running, they just stop receiving
> traffic. The ingest service retries the connection forever with backoff rather
> than crash-looping. The price service and the dashboard keep working. When
> MongoDB comes back, ingest reconnects on its own and readiness recovers.
>
> That's the difference between liveness and readiness, and getting it wrong is
> how you turn a database blip into a restart storm.

**"Could a service talk to another service's database?"**

> Technically no, and I enforce it in two places. Architecturally, only ingest
> has the MongoDB connection code. And at the network layer, the `mongodb-ingress`
> NetworkPolicy means only pods labelled `app: ingest` can even open a TCP
> connection to port 27017. An attacker who stole the password from a compromised
> optimizer pod would find the network refusing them.

---

## Part 5: Deployment details

**"How is the application accessible from outside the cluster?"**

> The gateway's Service is type NodePort on port 30080. On Docker Desktop the
> node is my laptop, so it's at localhost:30080. The other three services are
> ClusterIP, they have no externally reachable address at all. In production I'd
> use an Ingress with TLS instead, and I've included that definition, commented
> out, in `06-gateway.yaml`.

**"How is horizontal scaling achieved, and how do you know it's independent?"**

> Every service has its own HorizontalPodAutoscaler object, with its own metric,
> its own target and its own min and max. Nothing is shared between them. The
> optimizer can go to fifteen pods while the price service stays at two.
>
> The price service is deliberately capped at four, by the way, because each
> replica keeps its own cache. More pods would mean more calls to somebody else's
> free public API. That's a design decision, not a limitation.
>
> I demonstrate it by scaling only the optimizer and showing the other three
> deployments unchanged.

**"How is the storage persistent?"**

> MongoDB is a StatefulSet with a `volumeClaimTemplates` block, which creates a
> PersistentVolumeClaim that is *not* deleted when the pod is. I prove it by
> deleting the pod outright and showing the data is still there afterwards.
>
> I deliberately left `storageClassName` out so the cluster's default is used, hostpath on Docker Desktop, an EBS volume on AWS. The same YAML deploys
> everywhere.

**"Why a StatefulSet and not a Deployment?"**

> A Deployment treats pods as interchangeable. It will happily destroy one and
> create another with a new name and potentially a different volume. A database
> needs a stable identity and needs the *same* disk every time. A StatefulSet
> guarantees both: the pod is always called `mongodb-0` and always re-attaches to
> the same PersistentVolumeClaim.

**"What are the three probes for?"**

> Startup: "have you finished booting?" It gives a slow start more time without
> loosening the liveness check. Liveness: "are you alive?" Failing it restarts
> the container. Readiness: "can you serve traffic?" Failing it removes the pod
> from load balancing but leaves it running to recover. My ingest service returns
> 503 on readiness while MongoDB is unreachable, which is exactly the case
> readiness exists for.

**"What happens during a deployment of a new version?"**

> A rolling update with `maxUnavailable: 0` and `maxSurge: 1`. Kubernetes brings
> a new pod up and waits for it to pass readiness *before* removing an old one, so
> capacity never drops. Combined with the PodDisruptionBudget, which stops an
> administrator draining a node from taking the last replica, releases are
> zero-downtime.

---

## Part 6: Security

**"What security measures have you implemented?"**

Give four, in order of importance:

> First, attack surface: only the gateway is exposed. The other three services
> are ClusterIP and cannot be reached from outside the cluster at all.
>
> Second, a default-deny NetworkPolicy. Kubernetes by default lets every pod talk
> to every other pod; I block everything and then open only the ten conversations
> the system needs. The critical one is that only ingest may reach MongoDB.
>
> Third, least-privilege containers: non-root user, no privilege escalation, all
> Linux capabilities dropped, read-only root filesystem, and resource limits on
> everything so one container can't starve the node.
>
> Fourth, at the application layer: input validation on every endpoint, HTML
> escaping on everything rendered, a Content-Security-Policy plus four other
> security headers, rate limiting, and no credentials anywhere in source control.

**"What's wrong with your security?"** *(Ask yourself this. It's the question that separates a good answer from a great one.)*

> Two things stand out and I'd fix them before anything else.
>
> There's no authentication. Anyone who can reach port 30080 can see the
> dashboard and seed data. The fix is OIDC against the hospital's identity
> provider, enforced at the ingress, with role-based access so estates staff read
> and only service accounts write.
>
> And all internal traffic is plain HTTP. The fix is a service mesh, Istio or
> Linkerd, giving automatic mutual TLS between every pod, plus TLS termination
> at the ingress with a real certificate.
>
> Beyond those: Kubernetes Secrets are only base64-encoded, not encrypted, so I'd
> move to an external vault with rotating credentials. My rate limiter counts in
> one pod's memory, so with four replicas the real limit is four times what I
> configured, it belongs in Redis or at the ingress. And there's no image
> scanning in a pipeline yet.

**"Is your NetworkPolicy actually working?"**

> On Docker Desktop, no. Its default CNI doesn't enforce NetworkPolicies, so the
> objects are created and look correct but nothing is blocked. On a cluster with
> Calico or Cilium the identical YAML is enforced. I'd rather say that than claim
> protection I don't have.

**"Is SQL injection possible?"**

> There's no SQL. It's MongoDB. NoSQL injection is the equivalent risk, and it
> isn't reachable here because I only ever address MongoDB through the driver with
> parameterised documents, never by building a query out of concatenated strings.
> On top of that, `zoneId` is checked against a fixed list of nine values before
> it goes anywhere near the database.

**"What about GDPR?"**

> Energy consumption isn't personal data at zone level, which is why I aggregate
> to zone and never to room. But occupancy patterns inferred from a small ward
> could become personal data, so before any per-room metering you'd need a
> retention policy and a data protection impact assessment.

---

## The three questions people fail on

**"Explain this line of code."**: Pick any five random lines from
`optimizer/main.py` and `ingest/server.js` and practise explaining them out loud.

**"What would you do differently?"**: Have a real answer ready. Mine: start as a
modular monolith and split out the optimizer first, when scaling pressure actually
appears; add OpenTelemetry tracing from day one, because debugging four services
without it is genuinely painful; and put a message queue between the meters and
ingest.

**"What's the weakest part of your system?"**: Say the single MongoDB pod. It's
a single point of failure, I accepted it knowingly because the assignment says
the database needn't be scalable, and the production answer is a three-member
replica set across availability zones with tested backups.
