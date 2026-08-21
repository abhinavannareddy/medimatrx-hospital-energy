# Deploying MediMatrx: the simple version

One action per step. Do them in order. Do not skip.

If a step does not look like the picture I describe, **stop and ask** rather
than carrying on.

---

## Before you start

You need Docker Desktop open, and the Kubernetes light green.

---

## Step 1: Open the black window

1. Open the folder `medimatrx` on your Desktop.
2. At the top of the window there is a white bar showing the folder path.
   Click once inside it. The text turns blue.
3. Type this word over it: `powershell`
4. Press **Enter**.

A dark window opens.

**Check:** the last line should end with `Desktop\medimatrx>`

If it says anything else, close the window and do Step 1 again.

---

## Step 2: Check Kubernetes is awake

Type this and press Enter:

```
kubectl get nodes
```

**You want to see:** a line containing the word `Ready`.

If you see `Unable to connect`, Kubernetes is still starting. Wait two minutes
and type it again.

---

## Step 3: Build the six programs (5 to 10 minutes)

Type this and press Enter:

```
powershell -ExecutionPolicy Bypass -File .\scripts\1-build-and-push.ps1
```

It will ask you two things:

1. **Your Docker Hub username.** Type it in small letters. Press Enter.
2. **Your Docker Hub password.** Type it. Press Enter.

> Nothing appears on screen while you type the password. No dots, no stars.
> This is normal. Just type it and press Enter.

Now a lot of text scrolls past. This is normal. Sometimes it pauses for a
minute with nothing happening. That is also normal. **Do not touch anything.**

**You are done when you see:**

```
DONE. All 6 images are on Docker Hub.
```

(That number is printed from the list the script actually built, so it will
always match how many images really went up.)

**If you see red text:** stop here. Copy the red text and ask.

---

## Step 4: Start everything (2 to 4 minutes)

Type this and press Enter:

```
powershell -ExecutionPolicy Bypass -File .\scripts\2-deploy.ps1
```

It will print a long list of things being created. Then it says:

```
Waiting for MongoDB to be ready (this is the slow one) ...
```

**This line can sit there for three minutes doing nothing.** That is normal.
MongoDB is the database and it is slow to wake up. Do not press anything.

Then it checks each of the six services in turn.

**You are done when you see:**

```
MediMatrx is running.
Open:  http://localhost:30080
```

Your web browser opens on its own.

---

## Step 5: Look at it

In the browser you should see:

- The word **MediMatrx** at the top left
- Five boxes with numbers, the first one saying about **1.4 Mkr**
- A chart with **orange and blue bars**
- A **green line** underneath the bars

Scroll down to the box called **Ask MediMatrx**.

Click one of the grey buttons, for example *"Why move the laundry to the night?"*

An answer appears in a few seconds.

**That is it. You are finished.**

---

## Step 6: Check the pods (optional)

Go back to the black window and type:

```
kubectl get pods -n medimatrx
```

You should count **11 lines** of pods, all saying `Running`.

---

# If something goes wrong

## The browser page is blank or will not load

First check the pods are running (Step 6). If they are, type this:

```
kubectl port-forward -n medimatrx svc/gateway-service 8080:80
```

Leave that window open, and use **http://localhost:8080** instead.

## A pod says ImagePullBackOff

Kubernetes cannot download your programs.

Go to hub.docker.com, sign in, and check all six repositories are there and
say **Public**. If one says Private, click it, then Settings, then make it
Public.

## A pod says CrashLoopBackOff

Something inside that program is failing. Find out what:

```
kubectl logs -l app=assistant -n medimatrx --tail=30
```

Change `assistant` to whichever pod is unhappy.

## The dashboard says "no meter data yet"

Press the **Load demo day** button on the dashboard. That is all it needs.

## The dashboard says "price feed: modelled fallback"

The price service could not reach the internet. Press **Refresh** once.

If it stays, that is fine. It is a designed safety feature, not a fault. The
system falls back to a modelled price curve so the dashboard never goes blank.

## I want to start completely over

```
kubectl delete namespace medimatrx
```

Wait for it to finish, then do Step 4 again. You do not need Step 3 again.

---

# Words explained

| Word | What it means |
|---|---|
| **Pod** | One running copy of one of your programs |
| **Namespace** | A labelled box holding all your things |
| **Image** | Your program, packed up ready to run |
| **Docker Hub** | The website where your packed programs are stored |
| **Deploy** | Tell Kubernetes to start your programs |
| **NodePort** | The door into your app, at localhost:30080 |
