# Video script: 8 minutes

The assignment asks for 5-10 minutes covering: what the project is, what each
microservice does, running it on Kubernetes, accessing it through a browser,
showing log output, and walking through the YAML.

This script covers all six, in that order, with timings.

**Before you press record:**

- Deploy the app (`.\scripts\2-deploy.ps1`) so it is already running.
- Open these windows and arrange them so you can switch quickly:
  1. Browser at `http://localhost:30080`
  2. PowerShell, in the `medimatrx` folder
  3. VS Code with the `medimatrx` folder open
- Close Slack, email, and anything with notifications.
- Do a 20-second test recording first and check your microphone actually works.

**Speak slowly.** Everyone rushes their first take. If you fumble a sentence,
pause for three seconds and say it again. You can cut it later, or just leave it,
because examiners do not care about a stumble.

---

## 0:00-0:50: What the problem is

> "Hi, I'm Abhinav. This is MediMatrx, a microservice application for optimising
> energy use in hospitals.
>
> Here's the problem. A large Swedish hospital spends somewhere between eight and
> fifteen million kronor a year on electricity, and it runs twenty-four hours a
> day. Meanwhile, electricity in Sweden is priced hourly on a day-ahead market,
> and the cheapest hour of the day typically costs about a third of what the most
> expensive hour costs.
>
> Now, a lot of what a hospital does with electricity is completely
> non-negotiable, intensive care, operating theatres, imaging. You do not
> reschedule those. But some of it *is* negotiable. The laundry, the sterilisation
> department, bulk cooking, and pre-cooling the building with the HVAC plant. That work has to happen every day, but it does not matter what hour it happens in.
>
> MediMatrx finds that flexible load and moves it into the cheap hours. And it has
> one hard rule built into the code: it never, ever touches clinical load."

---

## 0:50-2:30: The dashboard (browser on screen)

Switch to the browser. Point at things as you talk.

> "This is the dashboard. It's served from inside the Kubernetes cluster and I'm
> reaching it in a normal browser at localhost port 30080.
>
> **[point at the top tiles]** These are the results for today. About one and a
> half million kronor a year, which is roughly a twelve percent cut in the energy
> bill, plus a reduction in the grid demand charge, that's a separate bill the
> hospital pays based on its single highest hour of the month, which is why
> flattening the peak matters on its own.
>
> **[point at the chart]** The orange bars are what the hospital actually used in
> each hour today. The blue bars are the plan. You can see load being taken out of
> the expensive afternoon and pushed into the night.
>
> **[point at the price panel underneath]** And this is why. That's the real
> electricity price for today, hour by hour, fetched live from a public Swedish
> API called elprisetjustnu.se. Cheapest hour is around three in the morning,
> most expensive around five in the afternoon.
>
> **[hover over a bar]** If I hover, I get the exact numbers for that hour and
> what the change is worth.
>
> **[scroll to recommendations]** These are the actual recommendations, move this
> much laundry load into this window, save this much. And note the last one:
> *no action taken in four clinical zones*. That's the safety rule, and it's
> enforced on the server, not in the interface, so it can't be bypassed.
>
> **[point at maintenance warnings]** This panel is a bonus feature. It compares
> every hour against the hours either side of it. A chiller that's short-cycling
> overnight shows up here weeks before it actually breaks. I've deliberately
> injected two faults into the demo data so you can see it working."

---

## 2:30-4:00: The architecture and the four microservices

Switch to VS Code, open `docs/01-REPORT.md` and scroll to the architecture
diagram, or just talk over the dashboard.

> "Architecturally this is four microservices plus a database, and they're
> deliberately split along the lines of *how they scale*, not how the code is
> organised.
>
> **The gateway** is Node.js and Express. It's the only service exposed outside
> the cluster. It serves the dashboard and routes every API call. It's also where
> I do security once instead of four times, the security headers, the rate
> limiting, the upstream timeouts.
>
> **The ingest service** is also Node.js. It's the only service allowed to touch
> MongoDB, that's the Database-per-Service pattern. Meters POST readings to it,
> and it serves a twenty-four hour summary back out using a MongoDB aggregation
> pipeline.
>
> **The price service** is Python and FastAPI. It's an HTTP client of somebody
> else's REST API and an HTTP server of its own at the same time. It caches for
> fifteen minutes so we're not hammering a free public API, and, this bit
> matters, if that API is down, it serves a stale cache, and if it has no cache
> it serves a modelled price curve, clearly labelled. The hospital never sees a
> blank dashboard because of somebody else's outage.
>
> **The optimizer** is also Python. It's the brain. It calls ingest and price
> *concurrently*, works out the plan, and returns it. It owns no database and
> holds no state at all, which is exactly why it's the easiest thing here to
> scale.
>
> And **MongoDB** runs as a StatefulSet with a persistent volume.
>
> Why split them at all? Because these three workloads grow at completely
> different rates. Ingest grows with the number of meters. The optimizer grows
> with the number of hospitals. And the price service doesn't grow at all,
> because the price is the same for everybody in a bidding area. In a monolith
> I'd have to scale all three to relieve any one of them."

---

## 4:00-5:15: The Kubernetes YAML (VS Code on screen)

Open `k8s/03-ingest.yaml`.

> "Here's the Kubernetes side. There are thirty objects across nine files.
>
> **[scroll through 03-ingest.yaml]** This is a typical service. A ClusterIP
> Service, which gives it a stable DNS name and load balances across whatever
> replicas exist. Note it's ClusterIP, not NodePort, so this is unreachable from
> outside the cluster entirely.
>
> Then the Deployment. Two replicas to start. A rolling update with
> `maxUnavailable: 0`, so releases are zero-downtime.
>
> **[point at env]** Configuration comes from a ConfigMap and the database
> password from a Secret, nothing is hard-coded, so the same image runs in every
> environment.
>
> **[point at probes]** Three probes. Startup, liveness and readiness. Liveness
> failing means restart me. Readiness failing means stop sending me traffic but
> leave me alone to recover. Mine returns 503 while MongoDB is unreachable.
>
> **[point at securityContext]** And the hardening: non-root user, no privilege
> escalation, all Linux capabilities dropped, read-only root filesystem."

Open `k8s/02-mongodb.yaml`.

> "MongoDB is different, it's a StatefulSet, not a Deployment, because a database
> needs a stable identity and needs the *same disk* back every time. That's this
> `volumeClaimTemplates` block at the bottom: a two-gigabyte PersistentVolumeClaim
> that is not deleted when the pod is."

Open `k8s/08-network-policy.yaml`.

> "And this file is the one I'm most pleased with. By default in Kubernetes every
> pod can talk to every other pod. This starts with a default-deny rule and then
> opens only the ten conversations the system actually needs. The important one is
> here, only pods labelled `app: ingest` may open a connection to MongoDB. Even
> if an attacker compromised the optimizer and stole the database password, the
> network would refuse the connection.
>
> One honest caveat: Docker Desktop's default networking doesn't enforce
> NetworkPolicies, so locally these objects exist but don't block anything. On a
> cluster running Calico or Cilium, the same YAML is enforced."

---

## 5:15-6:45: Running it live

Switch to PowerShell.

```powershell
kubectl get pods -n medimatrx
```

> "There's everything running. Two gateways, two ingest, two price, two optimizer,
> and one MongoDB."

```powershell
kubectl logs -l app=optimizer -n medimatrx --tail=15
```

> "These are the logs. Structured JSON, one object per line, tagged with the
> service and the pod name so a log collector can parse them. You can see each
> optimisation being computed and what it found."

```powershell
kubectl logs -l app=price -n medimatrx --tail=15
```

> "And here's the price service fetching from the live API. You can see it
> reporting how many records it pulled and reducing them to twenty-four hours."

Now the scaling demo:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\3-demo-scaling.ps1
```

> "Now the scaling requirement. I'm scaling only the optimizer, from two replicas
> to six.
>
> **[after it scales]** And look, the optimizer went from two to six, and the
> gateway, ingest and price deployments did not move at all. Each service has its
> own HorizontalPodAutoscaler with its own metric and its own ceiling. They're
> genuinely independent.
>
> **[during the 20 requests]** And now I'm sending twenty requests through the
> gateway, and printing which optimizer pod computed each one. You can see
> Kubernetes spreading them across the replicas, and I didn't change a single
> line of application code to make that happen."

---

## 6:45-7:30: Persistent storage

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\4-demo-persistence.ps1
```

> "Last requirement: storage that survives a restart of the infrastructure.
>
> **[as it runs]** It's recording how much data is stored, and now it deletes the
> entire MongoDB pod. Gone.
>
> **[as it recreates]** Kubernetes recreates it immediately, that's the
> StatefulSet doing its job. And critically, the PersistentVolumeClaim was not
> deleted, so the new pod attaches to the same disk.
>
> **[at the result]** Same number. The data survived the pod being destroyed."

---

## 7:30-8:00: Wrap up

> "So to summarise: four independently scalable microservices in two languages,
> each with its own REST API, a MongoDB database on persistent storage, all
> deployed on Kubernetes, all four images on Docker Hub, and accessible from a
> normal web browser.
>
> Two honest limitations. There's no authentication yet, anyone who can reach
> the port can see the dashboard, and all internal traffic is plain HTTP. Both
> are covered in the security section of my report, along with the fixes: OIDC
> at the ingress and a service mesh for mutual TLS.
>
> And on scale, for one hospital this honestly doesn't need to scale. The
> scaling story is real when you run it as a SaaS product for a hospital group:
> sixty sites, hundreds of meters each, re-planning every fifteen minutes. That's
> the scenario the architecture is designed for.
>
> The code and all the Kubernetes YAML are on GitHub, linked in my submission.
> Thanks for watching."

---

## Checklist: did you show everything the assignment asks for?

- [ ] What the project is about
- [ ] What each separate microservice does
- [ ] The project running on Kubernetes (`kubectl get pods`)
- [ ] Accessed through a web browser
- [ ] Log output (`kubectl logs`)
- [ ] A walk through the Kubernetes YAML
- [ ] Bonus: independent scaling demonstrated live
- [ ] Bonus: persistent storage proven by destroying the database pod
- [ ] Bonus: security discussed honestly, including what is *not* done
