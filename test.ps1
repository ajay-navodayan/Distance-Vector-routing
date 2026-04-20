param(
  [int]$SleepSeconds = 12
)

$ErrorActionPreference = "Stop"

function Pass($msg) { Write-Host "  [PASS] $msg" -ForegroundColor Green }
function Fail($msg) { Write-Host "  [FAIL] $msg" -ForegroundColor Red }
function Section($msg) {
  Write-Host ""
  Write-Host "----------------------------------------"
  Write-Host "TEST: $msg"
}

function RouteExists($container, $subnet) {
  $out = docker exec $container ip route show 2>$null
  return ($out -match [regex]::Escape($subnet))
}

Section "Initial Convergence (wait ${SleepSeconds}s)"
Start-Sleep -Seconds $SleepSeconds

$routers = @("router_a", "router_b", "router_c")
$subnets = @("10.0.1.0/24", "10.0.2.0/24", "10.0.3.0/24")

foreach ($r in $routers) {
  foreach ($s in $subnets) {
    if (RouteExists $r $s) { Pass "$r knows $s" } else { Fail "$r missing $s" }
  }
}

Section "Router A prefers direct path to 10.0.3.0/24"
$routeA = docker exec router_a ip route show 10.0.3.0/24 2>$null
if ($routeA -match "10.0.3\.") { Pass "Router A routes 10.0.3.0/24 directly" } else { Fail "Router A route: $routeA" }

Section "Link failure: stopping Router C"
docker stop router_c | Out-Null
Write-Host "Waiting ${SleepSeconds}s for reconvergence..."
Start-Sleep -Seconds $SleepSeconds

$routeAAll = docker exec router_a ip route show 2>$null
if ($routeAAll -notmatch "via 10.0.3.2") { Pass "Router A no longer uses dead Router C (10.0.3.2)" } else { Fail "Router A still has route via Router C" }

Section "Recovery: restarting Router C"
docker start router_c | Out-Null
Write-Host "Waiting ${SleepSeconds}s for reconvergence..."
Start-Sleep -Seconds $SleepSeconds

foreach ($s in @("10.0.2.0/24", "10.0.3.0/24")) {
  if (RouteExists "router_a" $s) { Pass "Router A recovered $s" } else { Fail "Router A missing $s after recovery" }
}

Section "Final Routing Tables"
foreach ($r in $routers) {
  Write-Host ""
  Write-Host "[$r]"
  docker exec $r ip route show 2>$null | ForEach-Object { "  $_" }
}

Write-Host ""
Write-Host "Tests complete."
