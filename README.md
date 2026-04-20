# Distance-Vector Router (DV-JSON)

Simple Python distance-vector routing daemon for a 3-router Docker lab. Each router exchanges DV-JSON updates over UDP and installs routes via `ip route` inside the container.

## Files
- `router.py` - routing daemon (all routing logic)
- `Dockerfile` - container image
- `deploy.ps1` - create networks and run routers
- `test.ps1` - basic convergence/failure tests

## Prerequisites
- Docker Desktop
- PowerShell

## Run
1. Build and start the topology:
   ```powershell
   .\deploy.ps1 up
   ```
2. Wait ~10-20 seconds for convergence, then check routes:
   ```powershell
   .\deploy.ps1 tables
   ```
3. Run the test script:
   ```powershell
   .\test.ps1 -SleepSeconds 20
   ```

## Stop
```powershell
.\deploy.ps1 down
```

## Notes
- UDP port: 5000
- Networks: net_ab (10.0.1.0/24), net_bc (10.0.2.0/24), net_ac (10.0.3.0/24)
