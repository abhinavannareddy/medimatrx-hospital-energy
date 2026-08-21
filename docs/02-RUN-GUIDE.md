# How to run MediWatt: step by step

Written for someone who has never done this before. Follow it in order and do
not skip a step. If something goes wrong, the fixes are at the bottom.

**Total time: about 45 minutes**, most of which is waiting for downloads.

---

## Before you start: what you need

| # | Thing | How to check you have it |
|---|---|---|
| 1 | **Docker Desktop**, installed and running | Look at the bottom-right of your screen for the whale icon. Open the app and it should say "Engine running". |
| 2 | **A Docker Hub account** (free) | Go to https://hub.docker.com and sign up. **Write down your username in lowercase.** You will type it several times. |
| 3 | **A GitHub account** (free) | https://github.com. You need it for the repository link you must submit. |
| 4 | **The MediWatt folder** on your Desktop | It should be called `mediwatt`. |

---

## Step 1: Turn on Kubernetes inside Docker Desktop

Kubernetes is not switched on by default. This is the step people most often
miss.

1. Open **Docker Desktop**.
2. Click the **gear icon** (⚙️ Settings) in the top-right.
3. In the left menu, click **Kubernetes**.
4. Tick the box **Enable Kubernetes**.
5. Click **Apply & Restart**.
6. **Wait.** The first time, this downloads a lot and can take 5-10 minutes.
   You are finished when the Kubernetes indicator at the bottom-left of Docker
   Desktop turns **green** and says "Kubernetes running".

☕ Go and make a coffee. Do not continue until it is green.

---

## Step 2: Open PowerShell in the project folder

1. Open the `mediwatt` folder on your Desktop in File Explorer.
2. Click once in the **address bar** at the top (where it shows the folder path).
3. Type `powershell` and press **Enter**.

A blue window opens, already sitting in the right folder. Everything from here on
gets typed into this window.

**Check it works.** Type this and press Enter:

```powershell
kubectl get nodes
```

You should see something like:

```
NAME             STATUS   ROLES           AGE   VERSION
docker-desktop   Ready    control-plane   5m    v1.30.2
```

If it says `Ready`, you are good. If it says it cannot connect, go back to Step 1. Kubernetes is not actually running yet.

---

## Step 3: Build the images and put them on Docker Hub

This turns your four services into container images and publishes them so that
Kubernetes can download them.

Type this exactly:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\1-build-and-push.ps1
```

> **Why the `-ExecutionPolicy Bypass` part?** Windows blocks scripts by default
> as a safety measure. This tells it to allow this one script, this one time.

The script will:

1. Check Docker is running.
2. **Ask you for your Docker Hub username.** Type it in lowercase and press Enter.
3. **Ask you to log in.** Type your Docker Hub password. **You will not see the
   characters appear as you type. That is normal.** Press Enter.
4. Build and push all four images. This takes **5-10 minutes** the first time
   because it downloads the Node.js and Python base images.
5. Automatically update the Kubernetes files to point at *your* images.

You are finished when you see **"DONE. All four images are on Docker Hub."**

**Check it worked:** go to https://hub.docker.com and log in. You should see four
new repositories: `mediwatt-ingest`, `mediwatt-price`, `mediwatt-optimizer`,
`mediwatt-gateway`.

---

## Step 4: Deploy to Kubernetes

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\2-deploy.ps1
```

This one creates all 30 Kubernetes objects, waits for the database to start,
waits for each service to be healthy, loads a day of demo meter data, and then
opens your browser automatically.

It takes **2-4 minutes**. MongoDB is the slow part.

You are finished when your browser opens at **http://localhost:30080** and you
see the MediWatt dashboard with numbers in it.

🎉 **That is it. Your application is running on Kubernetes.**

---

## Step 5: Look at what you built

Type each of these and look at what comes back. You will need to show some of
these in your video.

**All the running pods:**
```powershell
kubectl get pods -n mediwatt
```

**Everything at once (deployments, services, pods, autoscalers):**
```powershell
kubectl get all -n mediwatt
```

**The persistent disk:**
```powershell
kubectl get pvc -n mediwatt
```

**Live logs from the optimizer** (press `Ctrl+C` to stop):
```powershell
kubectl logs -l app=optimizer -n mediwatt --tail=30 -f
```

**Live logs from the price service**: this shows it calling the real
electricity price API:
```powershell
kubectl logs -l app=price -n mediwatt --tail=30
```

**Call the API directly, without the browser:**
```powershell
curl.exe http://localhost:30080/api/prices?area=SE4
```

---

## Step 6: Run the two demonstrations

These are what you record for your video.

**Independent horizontal scaling:**
```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\3-demo-scaling.ps1
```

It pauses and waits for you to press Enter between sections, so you have time to
talk over it.

**Persistent storage:**
```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\4-demo-persistence.ps1
```

This deletes the whole database pod on camera and shows the data coming back.

---

## Step 7: Put the code on GitHub

You must submit a link to a repository.

1. Go to https://github.com/new
2. Repository name: `mediwatt-hospital-energy`
3. Choose **Public** (your examiner has to be able to see it).
4. **Do not** tick "Add a README file". You already have one.
5. Click **Create repository**.

Then, back in PowerShell in your `mediwatt` folder, type these one at a time:

```powershell
git init
git add .
git commit -m "MediWatt: hospital energy optimisation on Kubernetes"
git branch -M main
git remote add origin https://github.com/YOUR-GITHUB-USERNAME/mediwatt-hospital-energy.git
git push -u origin main
```

Replace `YOUR-GITHUB-USERNAME` with your actual GitHub username.

If it asks you to log in, a browser window will open. Sign in there.

**Check it worked:** open your repository page in a browser. You should see the
`services`, `k8s`, `scripts` and `docs` folders.

---

## Step 8: Record the video

Use the script in `docs/03-VIDEO-SCRIPT.md`. It is written out shot by shot with
what to say and what to have on screen.

To record on Windows: press **Windows key + G** to open Xbox Game Bar, or use
OBS Studio (free), or Zoom with "share screen and record".

---

## When you are finished

To remove everything from the cluster:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\9-cleanup.ps1
```

Your images stay on Docker Hub, so you can redeploy any time with Step 4.

---

# If something goes wrong

## "kubectl is not recognized"

Kubernetes is not enabled in Docker Desktop. Go back to **Step 1**.

## "cannot connect to the Docker daemon"

Docker Desktop is not running. Open it from the Start menu and wait for
"Engine running".

## The deploy script says `ImagePullBackOff` or `ErrImagePull`

Kubernetes cannot download your images. Two usual causes:

1. Step 3 did not finish. Check https://hub.docker.com for your four repositories.
2. Your Docker Hub repositories are **private**. Open each one on Docker Hub →
   Settings → make it **Public**.

See exactly what is wrong with:
```powershell
kubectl describe pod -l app=gateway -n mediwatt
```
Read the `Events` section at the bottom.

## A pod says `CrashLoopBackOff`

Something inside the container is failing. Look at what it says:
```powershell
kubectl logs -l app=ingest -n mediwatt --tail=50
```

## MongoDB never becomes ready

Give it more time. The first start can take 3 minutes. If it still fails:
```powershell
kubectl describe pod mongodb-0 -n mediwatt
```
If the message mentions the PersistentVolumeClaim being unbound, your cluster has
no default storage class. On Docker Desktop this should not happen; if it does,
run `kubectl get storageclass` and check one is marked `(default)`.

## The dashboard says "no meter data yet"

Press the **Load demo day** button on the dashboard. That is all it needs.

## The dashboard says "price feed: modelled fallback"

The price service could not reach the internet from inside the cluster. The
application still works. It falls back to a modelled price curve on purpose.
Check your internet connection and press **Refresh**.

**This is actually a good thing to point out in your video**: it is a designed
resilience feature, not a failure.

## `kubectl get hpa` shows `<unknown>` in the TARGETS column

`metrics-server` is not installed. Docker Desktop does not include it. Install it:

```powershell
kubectl apply -f https://github.com/kubernetes-sigs/metrics-server/releases/latest/download/components.yaml
kubectl patch deployment metrics-server -n kube-system --type=json -p '[{\"op\":\"add\",\"path\":\"/spec/template/spec/containers/0/args/-\",\"value\":\"--kubelet-insecure-tls\"}]'
```

Wait a minute, then `kubectl get hpa -n mediwatt` again.

Manual scaling (`kubectl scale`) works perfectly well without it, so this is not
required for the demo.

## Everything is broken and I want to start over

```powershell
kubectl delete namespace mediwatt
```
Wait for it to finish, then run `.\scripts\2-deploy.ps1` again.

## I want to check whether it is my code or my cluster

Run the whole system without Kubernetes at all:

```powershell
docker compose up --build
```

Then open http://localhost:8080. If it works here but not in Kubernetes, the
problem is in the YAML. If it fails here too, the problem is in the code or in
Docker. Stop it with `Ctrl+C` then `docker compose down`.
