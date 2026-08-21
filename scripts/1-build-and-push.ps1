# ===========================================================================
#  STEP 1 - Build the four container images and push them to Docker Hub.
#
#  Run this from the project folder in PowerShell:
#     powershell -ExecutionPolicy Bypass -File .\scripts\1-build-and-push.ps1
#
#  What it does, in order:
#     1. asks for your Docker Hub username
#     2. logs you in to Docker Hub
#     3. builds each of the four services into an image
#     4. pushes each image to your Docker Hub account
#     5. writes your username into the Kubernetes YAML files, so they
#        point at YOUR images instead of the DOCKERHUB_USER placeholder
# ===========================================================================

$ErrorActionPreference = "Stop"

# Always work from the project root, no matter where the script is run from.
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

Write-Host ""
Write-Host "======================================================" -ForegroundColor Cyan
Write-Host "  MediMatrx - Step 1: build and push container images" -ForegroundColor Cyan
Write-Host "======================================================" -ForegroundColor Cyan
Write-Host ""

# --- Check Docker is actually running ------------------------------------
try {
    docker info *> $null
    if ($LASTEXITCODE -ne 0) { throw "not running" }
} catch {
    Write-Host "ERROR: Docker Desktop is not running." -ForegroundColor Red
    Write-Host "Start Docker Desktop, wait until the whale icon stops animating," -ForegroundColor Yellow
    Write-Host "then run this script again." -ForegroundColor Yellow
    exit 1
}
Write-Host "[ok] Docker is running." -ForegroundColor Green

# --- Get the Docker Hub username -----------------------------------------
$user = Read-Host "Enter your Docker Hub username (all lowercase)"
if ([string]::IsNullOrWhiteSpace($user)) {
    Write-Host "ERROR: you must enter a username." -ForegroundColor Red
    exit 1
}
$user = $user.Trim().ToLower()
Write-Host ""
Write-Host "Images will be published as:" -ForegroundColor Cyan
Write-Host "   $user/medimatrx-ingest:1.0.0"
Write-Host "   $user/medimatrx-price:1.0.0"
Write-Host "   $user/medimatrx-optimizer:1.0.0"
Write-Host "   $user/medimatrx-gateway:1.0.0"
Write-Host ""

# --- Log in ---------------------------------------------------------------
Write-Host "Logging in to Docker Hub. Enter your Docker Hub password" -ForegroundColor Cyan
Write-Host "(or an access token, which is safer)." -ForegroundColor Cyan
docker login -u $user
if ($LASTEXITCODE -ne 0) {
    Write-Host "ERROR: Docker Hub login failed. Check your username and password." -ForegroundColor Red
    exit 1
}
Write-Host "[ok] Logged in." -ForegroundColor Green
Write-Host ""

# --- Build and push each service -----------------------------------------
$services = @("ingest", "price", "optimizer", "gateway")

foreach ($svc in $services) {
    $image = "$user/medimatrx-$svc" + ":1.0.0"

    Write-Host "------------------------------------------------------" -ForegroundColor DarkGray
    Write-Host "Building $svc ..." -ForegroundColor Cyan
    docker build -t $image ".\services\$svc"
    if ($LASTEXITCODE -ne 0) {
        Write-Host "ERROR: build failed for $svc." -ForegroundColor Red
        exit 1
    }

    Write-Host "Pushing $image ..." -ForegroundColor Cyan
    docker push $image
    if ($LASTEXITCODE -ne 0) {
        Write-Host "ERROR: push failed for $svc." -ForegroundColor Red
        Write-Host "Make sure the repository name matches your Docker Hub account." -ForegroundColor Yellow
        exit 1
    }
    Write-Host "[ok] $svc published." -ForegroundColor Green
    Write-Host ""
}

# --- Point the Kubernetes YAML at these images ---------------------------
Write-Host "Updating the Kubernetes files to use your images ..." -ForegroundColor Cyan
$changed = 0
Get-ChildItem ".\k8s\*.yaml" | ForEach-Object {
    $text = Get-Content $_.FullName -Raw
    if ($text -match "DOCKERHUB_USER/") {
        $text = $text -replace "DOCKERHUB_USER/", "$user/"
        Set-Content -Path $_.FullName -Value $text -NoNewline
        Write-Host "   updated $($_.Name)"
        $changed++
    }
}
if ($changed -eq 0) {
    Write-Host "   (already updated - nothing to change)" -ForegroundColor DarkGray
}

Write-Host ""
Write-Host "======================================================" -ForegroundColor Green
Write-Host "  DONE. All four images are on Docker Hub." -ForegroundColor Green
Write-Host "  Next:  .\scripts\2-deploy.ps1" -ForegroundColor Green
Write-Host "======================================================" -ForegroundColor Green
Write-Host ""
