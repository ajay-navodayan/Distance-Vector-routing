param(
  [string]$Action = "up"
)

$ErrorActionPreference = "Stop"
$IMAGE = "my-router"

function Up {
  Write-Host "==> Cleaning up old containers (if any)"
  try {
    docker rm -f router_a router_b router_c 2>$null | Out-Null
  } catch {
    # Ignore "No such container" errors
  }

  Write-Host "==> Creating virtual networks"
  # Recreate networks to guarantee correct subnets
  $netContainers = @()
  $netContainers += docker ps -aq --filter "network=net_ab"
  $netContainers += docker ps -aq --filter "network=net_bc"
  $netContainers += docker ps -aq --filter "network=net_ac"
  $netContainers = $netContainers | Sort-Object -Unique | Where-Object { $_ -and $_.Trim() -ne "" }
  if ($netContainers.Count -gt 0) {
    Write-Host "==> Removing containers attached to old networks"
    docker rm -f $netContainers 2>$null | Out-Null
  }
  try {
    docker network rm net_ab net_bc net_ac 2>$null | Out-Null
  } catch {
    # Ignore "network not found" errors
  }
  docker network create --subnet=10.0.1.0/24 --gateway=10.0.1.254 net_ab | Out-Null
  docker network create --subnet=10.0.2.0/24 --gateway=10.0.2.254 net_bc | Out-Null
  docker network create --subnet=10.0.3.0/24 --gateway=10.0.3.254 net_ac | Out-Null

  Write-Host "==> Building router image"
  docker build -t $IMAGE .

  Write-Host "==> Starting Router A (10.0.1.1 / 10.0.3.1)"
  docker run -d --name router_a --privileged `
    --network net_ab --ip 10.0.1.1 `
    -e MY_IP=10.0.1.1 `
    -e MY_SUBNETS=10.0.1.0/24,10.0.3.0/24 `
    -e NEIGHBORS=10.0.1.2,10.0.3.2 `
    $IMAGE | Out-Null
  docker network connect --ip 10.0.3.1 net_ac router_a

  Write-Host "==> Starting Router B (10.0.1.2 / 10.0.2.1)"
  docker run -d --name router_b --privileged `
    --network net_ab --ip 10.0.1.2 `
    -e MY_IP=10.0.1.2 `
    -e MY_SUBNETS=10.0.1.0/24,10.0.2.0/24 `
    -e NEIGHBORS=10.0.1.1,10.0.2.2 `
    $IMAGE | Out-Null
  docker network connect --ip 10.0.2.1 net_bc router_b

  Write-Host "==> Starting Router C (10.0.2.2 / 10.0.3.2)"
  docker run -d --name router_c --privileged `
    --network net_bc --ip 10.0.2.2 `
    -e MY_IP=10.0.2.2 `
    -e MY_SUBNETS=10.0.2.0/24,10.0.3.0/24 `
    -e NEIGHBORS=10.0.2.1,10.0.3.1 `
    $IMAGE | Out-Null
  docker network connect --ip 10.0.3.2 net_ac router_c

  Write-Host ""
  Write-Host "Topology is UP. Wait ~10 s for convergence, then run:"
  Write-Host "  .\\deploy.ps1 tables"
}

function Down {
  Write-Host "==> Removing containers"
  try {
    docker rm -f router_a router_b router_c 2>$null | Out-Null
  } catch {
    # Ignore "No such container" errors
  }
  Write-Host "==> Removing networks"
  docker network rm net_ab net_bc net_ac 2>$null | Out-Null
  Write-Host "Done."
}

function Logs {
  Write-Host "Streaming logs (Ctrl+C to stop)..."
  docker logs -f router_a
  docker logs -f router_b
  docker logs -f router_c
}

function Tables {
  foreach ($r in @("router_a","router_b","router_c")) {
    Write-Host ""
    Write-Host "========== $r =========="
    docker exec $r ip route show
  }
}

switch ($Action) {
  "up"     { Up }
  "down"   { Down }
  "logs"   { Logs }
  "tables" { Tables }
  default  { Write-Host "Usage: .\\deploy.ps1 {up|down|logs|tables}" }
}
