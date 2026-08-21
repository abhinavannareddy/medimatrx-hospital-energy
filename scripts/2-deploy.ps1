# ===========================================================================
#  STEP 2 - Deploy MediMatrx to Kubernetes.
#
#  Run:
#     powershell -ExecutionPolicy Bypass -File .\scripts\2-deploy.ps1
# ===========================================================================

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

Write-Host ""
Write-Host "======================================================" -ForegroundColor Cyan
Write-Host "  MediMatrx - Step 2: deploy to Kubernetes" -ForegroundColor Cyan
Write-Host "======================================================" -ForegroundColor Cyan
Write-Host ""

# --- Is Kubernetes switched on? ------------------------------------------
kubectl cluster-info *> $null
if ($LASTEXITCODE -ne 0) {
    Write-Host "ERROR: cannot reach a Kubernetes cluster." -ForegroundColor Red
    Write-Host ""
    Write-Host "In Docker Desktop: Settings -> Kubernetes -> tick" -ForegroundColor Yellow
    Write-Host "'Enable Kubernetes' -> Apply & Restart. Wait for the" -ForegroundColor Yellow
    Write-Host "Kubernetes indicator to turn green, then run this again." -ForegroundColor Yellow
    exit 1
}
Write-Host "[ok] Kubernetes is reachable." -ForegroundColor Green

# --- Did step 1 run? ------------------------------------------------------
$ingestYaml = Get-Content ".\k8s\03-ingest.yaml" -Raw
if ($ingestYaml -match "DOCKERHUB_USER/") {
    Write-Host "ERROR: the Kubernetes files still say DOCKERHUB_USER." -ForegroundColor Red
    Write-Host "Run .\scripts\1-build-and-push.ps1 first." -ForegroundColor Yellow
    exit 1
}
Write-Host "[ok] Image names are set." -ForegroundColor Green
Write-Host ""

# --- Apply everything, in order ------------------------------------------
# The numbering of the files is the order they must be created in:
# the namespace has to exist before anything can go inside it, and the
# config and secrets have to exist before a pod can mount them.
Write-Host "Applying the manifests ..." -ForegroundColor Cyan
kubectl apply -f .\k8s\
if ($LASTEXITCODE -ne 0) {
    Write-Host "ERROR: kubectl apply failed. Read the message above." -ForegroundColor Red
    exit 1
}
Write-Host ""

# --- Wait for the database, then the services ----------------------------
Write-Host "Waiting for MongoDB to be ready (this is the slow one) ..." -ForegroundColor Cyan
kubectl wait --for=condition=ready pod -l app=mongodb -n medimatrx --timeout=240s
if ($LASTEXITCODE -ne 0) {
    Write-Host "MongoDB did not become ready in time." -ForegroundColor Red
    Write-Host "Look at what happened with:" -ForegroundColor Yellow
    Write-Host "   kubectl describe pod mongodb-0 -n medimatrx" -ForegroundColor Yellow
    exit 1
}
Write-Host "[ok] MongoDB is ready." -ForegroundColor Green

foreach ($d in @("ingest", "price", "optimizer", "assistant", "forecast", "gateway")) {
    Write-Host "Waiting for $d ..." -ForegroundColor Cyan
    kubectl rollout status deployment/$d -n medimatrx --timeout=240s
    if ($LASTEXITCODE -ne 0) {
        Write-Host "Deployment $d did not come up." -ForegroundColor Red
        Write-Host "   kubectl logs -l app=$d -n medimatrx --tail=50" -ForegroundColor Yellow
        exit 1
    }
}
Write-Host ""

# --- Load the demo day ----------------------------------------------------
Write-Host "Loading 24 hours of demo meter data ..." -ForegroundColor Cyan
Start-Sleep -Seconds 4
try {
    $r = Invoke-RestMethod -Uri "http://localhost:30080/api/simulate" -Method Post -TimeoutSec 30
    Write-Host "[ok] $($r.documents) readings written across $($r.zones) zones." -ForegroundColor Green
} catch {
    Write-Host "Could not seed automatically - no problem." -ForegroundColor Yellow
    Write-Host "Just press the 'Load demo day' button on the dashboard." -ForegroundColor Yellow
}

Write-Host ""
kubectl get pods -n medimatrx
Write-Host ""
Write-Host "======================================================" -ForegroundColor Green
Write-Host "  MediMatrx is running." -ForegroundColor Green
Write-Host "  Open:  http://localhost:30080" -ForegroundColor Green
Write-Host "======================================================" -ForegroundColor Green
Write-Host ""

Start-Process "http://localhost:30080"
