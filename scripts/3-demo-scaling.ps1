# ===========================================================================
#  STEP 3 - The scaling demonstration for your video.
#
#  This script proves the assignment requirement that every microservice is
#  independently horizontally scalable. It scales ONE service up, shows that
#  the others did not change, generates traffic so you can see the load
#  spread across replicas, and then scales back down.
#
#  Run it while you are recording:
#     powershell -ExecutionPolicy Bypass -File .\scripts\3-demo-scaling.ps1
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
Write-Host "  MediMatrx - independent horizontal scaling demo" -ForegroundColor Cyan
Write-Host "======================================================" -ForegroundColor Cyan

Write-Host ""
Write-Host "This is what we have right now:" -ForegroundColor Cyan
kubectl get deployments -n medimatrx

Pause-Demo "Now I scale ONLY the optimizer to 6 replicas."

kubectl scale deployment optimizer --replicas=6 -n medimatrx
Start-Sleep -Seconds 3
kubectl rollout status deployment/optimizer -n medimatrx --timeout=120s

Write-Host ""
Write-Host "Look at the replica counts now:" -ForegroundColor Cyan
kubectl get deployments -n medimatrx
Write-Host ""
Write-Host "The optimizer went 2 -> 6. Gateway, ingest and price did NOT move." -ForegroundColor Green
Write-Host "Each service scales on its own. That is the point of microservices." -ForegroundColor Green

Write-Host ""
Write-Host "The six optimizer pods:" -ForegroundColor Cyan
kubectl get pods -n medimatrx -l app=optimizer -o wide

Pause-Demo "Now I send 20 requests and show which pod answers each one."

Write-Host ""
Write-Host "Pod that computed each optimisation:" -ForegroundColor Cyan
$seen = @{}
for ($i = 1; $i -le 20; $i++) {
    try {
        $r = Invoke-RestMethod -Uri "http://localhost:30080/api/optimize?area=SE4" -TimeoutSec 20
        $pod = $r.servedBy.optimizerPod
        $seen[$pod] = $true
        Write-Host ("  request {0,2}  ->  {1}" -f $i, $pod)
    } catch {
        Write-Host ("  request {0,2}  ->  failed: {1}" -f $i, $_.Exception.Message) -ForegroundColor Red
    }
}
Write-Host ""
Write-Host "Distinct optimizer pods that served traffic: $($seen.Count)" -ForegroundColor Green
Write-Host "Kubernetes load-balanced across them with no code changes." -ForegroundColor Green

Pause-Demo "Now the autoscalers. These scale the services automatically on CPU."

kubectl get hpa -n medimatrx
Write-Host ""
Write-Host "If the TARGETS column says <unknown>, metrics-server is not installed." -ForegroundColor DarkGray
Write-Host "That does not affect manual scaling. Install it with:" -ForegroundColor DarkGray
Write-Host "  kubectl apply -f https://github.com/kubernetes-sigs/metrics-server/releases/latest/download/components.yaml" -ForegroundColor DarkGray

Pause-Demo "Finally, self-healing: I delete a pod and Kubernetes replaces it."

$victim = (kubectl get pods -n medimatrx -l app=optimizer -o jsonpath="{.items[0].metadata.name}")
Write-Host "Deleting pod: $victim" -ForegroundColor Cyan
kubectl delete pod $victim -n medimatrx
Start-Sleep -Seconds 5
Write-Host ""
Write-Host "Kubernetes immediately created a replacement:" -ForegroundColor Cyan
kubectl get pods -n medimatrx -l app=optimizer

Pause-Demo "Scaling the optimizer back down to 2."

kubectl scale deployment optimizer --replicas=2 -n medimatrx
Start-Sleep -Seconds 3
kubectl get deployments -n medimatrx

Write-Host ""
Write-Host "======================================================" -ForegroundColor Green
Write-Host "  Demo complete." -ForegroundColor Green
Write-Host "======================================================" -ForegroundColor Green
