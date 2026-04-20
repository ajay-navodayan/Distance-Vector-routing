param(
  [int]$SleepSeconds = 20
)

$ErrorActionPreference = "Stop"

function Pass($msg)    { Write-Host "  [PASS] $msg" -ForegroundColor Green }
function Fail($msg)    { Write-Host "  [FAIL] $msg" -ForegroundColor Red   }
function Info($msg)    { Write-Host "  [INFO] $msg" -ForegroundColor Cyan  }
function Section($msg) {
    Write-Host ""
    Write-Host "================================================"
    Write-Host "TEST: $msg"
    Write-Host "================================================"
}

function RouteExists($container, $subnet) {
    $state = docker inspect -f "{{.State.Running}}" $container 2>$null
    if ($state -ne "true") {
        return $false
    }
    $out = docker exec $container ip route show 2>$null
    return ($out -match [regex]::Escape($subnet))
}

function ShowTables {
    foreach ($r in @("router_a","router_b","router_c")) {
        Write-Host ""
        Write-Host "  [$r] kernel routes:"
        $state = docker inspect -f "{{.State.Running}}" $r 2>$null
        if ($state -ne "true") {
            Write-Host "    (container not running)"
            continue
        }
        docker exec $r ip route show 2>$null | ForEach-Object { "    $_" }
    }
}

# --- Test 1: Initial Convergence ---------------------------------------------
Section "Initial Convergence -- waiting ${SleepSeconds}s for routers to exchange updates"
Start-Sleep -Seconds $SleepSeconds

$routers = @("router_a", "router_b", "router_c")
$subnets = @("10.0.1.0/24", "10.0.2.0/24", "10.0.3.0/24")
$allPass = $true

foreach ($r in $routers) {
    foreach ($s in $subnets) {
        if (RouteExists $r $s) {
            Pass "$r knows $s"
        } else {
            Fail "$r MISSING $s"
            $allPass = $false
        }
    }
}

if ($allPass) { Info "All routers converged with all 3 subnets -- OK" }

# --- Test 2: Direct Path Preference ------------------------------------------
Section "Router A prefers direct path to 10.0.3.0/24 (via its own NIC, not via B)"
$routeA = docker exec router_a ip route show 10.0.3.0/24 2>$null
# Direct path: appears without going through net_ab (10.0.1.x)
if ($routeA -match "10\.0\.3\." -and $routeA -notmatch "via 10\.0\.1\.") {
    Pass "Router A routes 10.0.3.0/24 directly (not via net_ab)"
} else {
    Fail "Router A route for 10.0.3.0/24: '$routeA'"
}

# --- Test 3: Router B knows 10.0.3.0/24 -------------------------------------
Section "Router B can reach 10.0.3.0/24 (learned path via A or C)"
$routeB3 = docker exec router_b ip route show 10.0.3.0/24 2>$null
if ($routeB3) { Pass "Router B has route for 10.0.3.0/24: $routeB3" }
else          { Fail "Router B has NO route for 10.0.3.0/24" }

# --- Test 4: Link Failure -- stop Router C -----------------------------------
Section "Link failure: stopping Router C and waiting ${SleepSeconds}s for reconvergence"
docker stop router_c | Out-Null
Info "Router C stopped."
Start-Sleep -Seconds $SleepSeconds

# Router A must NOT still be using Router C's IPs as next-hop
$routeAAll = docker exec router_a ip route show 2>$null
if ($routeAAll -notmatch "via 10\.0\.3\.2" -and $routeAAll -notmatch "via 10\.0\.2\.2") {
    Pass "Router A no longer routes through dead Router C"
} else {
    Fail "Router A still has stale route via Router C: '$routeAAll'"
}

# Router B must NOT still be using Router C's IPs
$routeBAll = docker exec router_b ip route show 2>$null
if ($routeBAll -notmatch "via 10\.0\.2\.2" -and $routeBAll -notmatch "via 10\.0\.3\.2") {
    Pass "Router B no longer routes through dead Router C"
} else {
    Fail "Router B still has stale route via Router C: '$routeBAll'"
}

Info "Kernel routes after Router C failure:"
ShowTables

# --- Test 5: Recovery -- restart Router C ------------------------------------
Section "Recovery: restarting Router C and waiting ${SleepSeconds}s for reconvergence"
docker start router_c | Out-Null
Info "Router C restarted."
Start-Sleep -Seconds $SleepSeconds

foreach ($s in @("10.0.2.0/24", "10.0.3.0/24")) {
    if (RouteExists "router_a" $s) { Pass "Router A recovered $s" }
    else                           { Fail "Router A missing $s after recovery" }
}
foreach ($s in @("10.0.1.0/24", "10.0.3.0/24")) {
    if (RouteExists "router_b" $s) { Pass "Router B recovered $s" }
    else                           { Fail "Router B missing $s after recovery" }
}

# --- Final Tables ------------------------------------------------------------
Section "Final Routing Tables (all routers)"
ShowTables

Write-Host ""
Write-Host "================================================"
Write-Host "  Tests complete."
Write-Host "================================================"
