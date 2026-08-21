# ===========================================================================
#  Remove MediMatrx from the cluster.
#
#  Deleting the namespace removes every deployment, service, pod, config,
#  secret, autoscaler and network policy in one command - that is one of the
#  reasons everything was put in its own namespace.
#
#  NOTE: this also deletes the PersistentVolumeClaim, so the stored meter
#  data is gone for good. That is deliberate for a coursework demo. In
#  production you would set the PersistentVolume's reclaim policy to Retain
#  so that deleting the application cannot delete the data.
# ===========================================================================

Write-Host ""
Write-Host "This will delete the entire 'medimatrx' namespace and all its data." -ForegroundColor Yellow
$confirm = Read-Host "Type YES to continue"

if ($confirm -ne "YES") {
    Write-Host "Cancelled - nothing was deleted." -ForegroundColor Cyan
    exit 0
}

kubectl delete namespace medimatrx
Write-Host ""
Write-Host "MediMatrx removed." -ForegroundColor Green
Write-Host "Your container images are still on Docker Hub." -ForegroundColor DarkGray
Write-Host ""
