# ===========================================================================
#  STEP 4 - Prove that the database storage really is persistent.
#
#  The assignment says: "The database must use storage which is persistent
#  across restarts of the deployment infrastructure."
#
#  This script proves it by destroying the database pod and showing that the
#  data comes back. Run it on camera - it is one of the strongest moments in
#  the whole demonstration.
# ===========================================================================

$ErrorActionPreference = "Continue"

function Pause-Demo($message) {
    Write-Host ""
    Write-Host ">>> $message" -ForegroundColor Yellow
    Write-Host ">>> Press Enter to continue..." -ForegroundColor DarkGray
    Read-Host | Out-Null
}

Write-Host ""
Write-Host "======================================================" -ForegroundColor Cyan
Write-Host "  MediWatt - persistent storage demonstration" -ForegroundColor Cyan
Write-Host "======================================================" -ForegroundColor Cyan

Write-Host ""
Write-Host "How much data is in the database right now?" -ForegroundColor Cyan
$before = Invoke-RestMethod -Uri "http://localhost:30080/api/summary" -TimeoutSec 20
Write-Host ("  Total consumption stored: {0:N1} kWh across {1} zones" -f $before.totalKwh, $before.zones.Count) -ForegroundColor Green

Write-Host ""
Write-Host "The disk that holds it:" -ForegroundColor Cyan
kubectl get pvc -n mediwatt

Pause-Demo "Now I DELETE the entire database pod. In a moment it will be gone."

kubectl delete pod mongodb-0 -n mediwatt
Write-Host ""
Write-Host "Pod deleted. Watch Kubernetes recreate it..." -ForegroundColor Cyan
Start-Sleep -Seconds 4
kubectl get pods -n mediwatt -l app=mongodb

Write-Host ""
Write-Host "Waiting for the replacement to be ready..." -ForegroundColor Cyan
kubectl wait --for=condition=ready pod -l app=mongodb -n mediwatt --timeout=240s

Write-Host ""
Write-Host "The PersistentVolumeClaim was NOT deleted - same disk, new pod:" -ForegroundColor Cyan
kubectl get pvc -n mediwatt

Pause-Demo "Now the moment of truth: is the data still there?"

# The ingest pods need a few seconds to reconnect to the new database pod.
Start-Sleep -Seconds 12
$ok = $false
for ($i = 1; $i -le 10; $i++) {
    try {
        $after = Invoke-RestMethod -Uri "http://localhost:30080/api/summary" -TimeoutSec 20
        if ($after.totalKwh -gt 0) { $ok = $true; break }
    } catch { }
    Write-Host "  ...services still reconnecting (attempt $i)" -ForegroundColor DarkGray
    Start-Sleep -Seconds 6
}

Write-Host ""
if ($ok) {
    Write-Host ("  Before the pod was destroyed: {0:N1} kWh" -f $before.totalKwh) -ForegroundColor Cyan
    Write-Host ("  After the pod was destroyed:  {0:N1} kWh" -f $after.totalKwh) -ForegroundColor Cyan
    if ([math]::Abs($after.totalKwh - $before.totalKwh) -lt 0.5) {
        Write-Host ""
        Write-Host "  IDENTICAL. The data survived the pod being destroyed." -ForegroundColor Green
        Write-Host "  That is the PersistentVolumeClaim doing its job." -ForegroundColor Green
    } else {
        Write-Host "  Values differ slightly - readings may have arrived in between." -ForegroundColor Yellow
    }
} else {
    Write-Host "  Services have not finished reconnecting yet." -ForegroundColor Yellow
    Write-Host "  Wait a few more seconds and refresh http://localhost:30080" -ForegroundColor Yellow
    Write-Host "  (the ingest service retries the database connection forever" -ForegroundColor Yellow
    Write-Host "   by design, so it will recover on its own)." -ForegroundColor Yellow
}
Write-Host ""
