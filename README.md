# Distance-Vector Router (DV-JSON)

Python routing daemon implementing a simplified RIP-style Distance-Vector protocol.
Runs inside Docker containers connected in a triangle topology and installs real
kernel routes via `ip route`.

**Features:** Bellman-Ford · Split Horizon · Triggered Updates · Neighbor Timeout

---

## Files

| File | Purpose |
|------|---------|
| `router.py` | Complete routing daemon (all logic in one file) |
| `Dockerfile` | Alpine image with python3 + iproute2 + iputils |
| `docker-compose.yml` | Defines the 3-router triangle topology |
| `test.ps1` | Automated convergence + failure/recovery test |

---

## Topology

```
        Router A  (10.0.1.1 / 10.0.3.1)
           /  \
      net_ab   net_ac
   10.0.1.0   10.0.3.0
       /            \
  Router B        Router C
 (10.0.1.2/      (10.0.2.2/
  10.0.2.1)       10.0.3.2)
       \            /
        ---net_bc---
          10.0.2.0
```

---

## Quick Start

### 1. Start all three routers

```powershell
docker compose up -d --build
```

### 2. Watch live convergence logs

```powershell
docker logs -f router_a
docker logs -f router_b
docker logs -f router_c
```

Or stream all containers at once:

```powershell
docker compose logs -f
```

### 3. Inspect kernel routing tables

```powershell
docker exec router_a ip route show
docker exec router_b ip route show
docker exec router_c ip route show
```

### 4. Run the automated test (convergence + failure + recovery)

```powershell
.\test.ps1 -SleepSeconds 20
```

Note: In PowerShell, you must use `.\` to run a script from the current folder.

### 5. Tear down

```powershell
docker compose down
```

---

## Environment Variables (set in docker-compose.yml)

| Variable | Example | Description |
|----------|---------|-------------|
| `MY_IP` | `10.0.1.1` | This router's primary IP (advertised as router_id) |
| `ROUTER_NAME` | `Router A` | Display name in routing table output |
| `MY_SUBNETS` | `10.0.1.0/24,10.0.3.0/24` | Directly-connected subnets (distance 0) |
| `NEIGHBORS` | `10.0.1.2,10.0.3.2` | Neighbour IPs to exchange updates with |
| `PING_TARGETS` | `10.0.1.2,10.0.3.2` | IPs to ping for reachability verification |

---

## Protocol

- **Format:** DV-JSON over UDP port 5000
- **Update interval:** 5 seconds
- **Neighbour timeout:** 15 seconds (3 missed updates)
- **Infinity metric:** 16 (RIP convention)
- **Loop prevention:** Split Horizon + Triggered Poisoned Updates
