"""
Distance-Vector Router (DV-JSON Protocol)
Implements: Bellman-Ford, Poison Reverse, Triggered Updates, UDP port 5000
"""

import ipaddress
import json
import os
import socket
import subprocess
import threading
import time
from typing import Any

# ─── Configuration ────────────────────────────────────────────────────────────
PROTOCOL_VERSION   = 1.0
PORT               = int(os.getenv("PORT", "5000"))
INFINITY           = int(os.getenv("INFINITY", "16"))
BROADCAST_INTERVAL = float(os.getenv("BROADCAST_INTERVAL", "2"))
DEAD_INTERVAL      = float(os.getenv("NEIGHBOR_DEAD_INTERVAL", "9"))

MY_IP        = os.getenv("MY_IP", "127.0.0.1")
ROUTER_NAME  = os.getenv("ROUTER_NAME", MY_IP)
NEIGHBORS    = [n.strip() for n in os.getenv("NEIGHBORS", "").split(",") if n.strip()]
# Support both MY_SUBNETS (our env) and DIRECT_SUBNETS (reference env)
_SUBNETS_ENV = os.getenv("MY_SUBNETS", "") or os.getenv("DIRECT_SUBNETS", "")
DIRECT_SUBNETS_ENV = [s.strip() for s in _SUBNETS_ENV.split(",") if s.strip()]

DIRECT_SOURCE   = "direct"
NEIGHBOR_SOURCE = "neighbor"
DIRECT_NEXT_HOP = "0.0.0.0"

RouteEntry   = dict[str, Any]
RoutingTable = dict[str, RouteEntry]
NeighborState = dict[str, Any]

routing_table:   RoutingTable              = {}
neighbor_tables: dict[str, NeighborState] = {}
state_lock = threading.Lock()


# ─── Logging ──────────────────────────────────────────────────────────────────

def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


# ─── Kernel Route Helpers ─────────────────────────────────────────────────────

def run_ip_route(args: list[str]) -> None:
    result = subprocess.run(
        ["ip", "route"] + args,
        capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        err = result.stderr.strip() or result.stdout.strip() or "unknown error"
        log(f"ip route {' '.join(args)} failed: {err}")


# ─── Subnet Helpers ───────────────────────────────────────────────────────────

def normalize_subnet(value: str) -> str | None:
    try:
        return str(ipaddress.ip_network(value, strict=False))
    except ValueError:
        return None


def make_route(distance: int, next_hop: str, source: str) -> RouteEntry:
    return {"distance": distance, "next_hop": next_hop, "source": source}


def route_learned_from_neighbor(entry: RouteEntry | None) -> bool:
    return bool(entry and entry["source"] == NEIGHBOR_SOURCE)


# ─── Direct Subnet Discovery ──────────────────────────────────────────────────

def discover_direct_subnets() -> set[str]:
    """Collect directly-connected IPv4 subnets from interfaces + env var."""
    discovered: set[str] = set()

    try:
        output = subprocess.check_output(
            ["ip", "-4", "-o", "addr", "show", "scope", "global"], text=True,
        )
        for line in output.splitlines():
            parts = line.split()
            if "inet" not in parts:
                continue
            cidr = parts[parts.index("inet") + 1]
            network = ipaddress.ip_interface(cidr).network
            discovered.add(str(network))
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        log(f"Could not auto-discover subnets from interfaces: {exc}")

    for subnet in DIRECT_SUBNETS_ENV:
        normalized = normalize_subnet(subnet)
        if normalized:
            discovered.add(normalized)

    return discovered


def direct_route_entries() -> RoutingTable:
    return {
        subnet: make_route(0, DIRECT_NEXT_HOP, DIRECT_SOURCE)
        for subnet in sorted(discover_direct_subnets())
    }


# ─── Initialization ───────────────────────────────────────────────────────────

def init_routing_table() -> None:
    direct_entries = direct_route_entries()
    if not direct_entries:
        log("No direct subnets discovered. Set MY_SUBNETS env var if needed.")
    with state_lock:
        routing_table.clear()
        routing_table.update(direct_entries)
    log(f"Router started: MY_IP={MY_IP}, neighbors={NEIGHBORS}")
    log(f"Direct subnets: {sorted(direct_entries)}")


# ─── Packet Handling ──────────────────────────────────────────────────────────

def validate_packet(packet: dict[str, Any]) -> bool:
    return (
        isinstance(packet, dict)
        and packet.get("version") == PROTOCOL_VERSION
        and isinstance(packet.get("routes"), list)
    )


def parse_routes(routes: list[dict[str, Any]]) -> dict[str, int]:
    """Parse raw route list into {subnet: distance}, dropping invalid entries."""
    cleaned: dict[str, int] = {}
    for entry in routes:
        if not isinstance(entry, dict):
            continue
        subnet = entry.get("subnet")
        distance = entry.get("distance")
        if not isinstance(subnet, str):
            continue
        subnet = normalize_subnet(subnet)
        if subnet is None:
            continue
        try:
            distance = int(distance)
        except (ValueError, TypeError):
            continue
        cleaned[subnet] = max(0, min(distance, INFINITY))
    return cleaned


def build_packet(for_neighbor: str | None = None) -> bytes:
    """Build DV-JSON update with poison reverse.
    Routes learned from a neighbor are advertised back at INFINITY.
    """
    with state_lock:
        packet_routes = []
        for subnet, entry in sorted(routing_table.items()):
            dist = entry["distance"]
            if (
                for_neighbor
                and entry["source"] == NEIGHBOR_SOURCE
                and entry["next_hop"] == for_neighbor
            ):
                dist = INFINITY
            packet_routes.append({
                "subnet": subnet,
                "distance": int(min(dist, INFINITY)),
            })

    return json.dumps({
        "router_id": MY_IP,
        "version": PROTOCOL_VERSION,
        "routes": packet_routes,
    }).encode("utf-8")


# ─── Route Recomputation ──────────────────────────────────────────────────────

def apply_kernel_route_changes(old_table: RoutingTable, new_table: RoutingTable) -> None:
    """Sync kernel routes to match transition from old_table → new_table."""
    for subnet in sorted(set(old_table) | set(new_table)):
        old = old_table.get(subnet)
        new = new_table.get(subnet)

        old_dynamic = route_learned_from_neighbor(old)
        new_dynamic = route_learned_from_neighbor(new)

        if old_dynamic and not new_dynamic:
            run_ip_route(["del", subnet])
            log(f"Route removed: {subnet}")
            continue

        if new_dynamic:
            if (
                not old_dynamic
                or old["next_hop"] != new["next_hop"]
                or old["distance"] != new["distance"]
            ):
                run_ip_route(["replace", subnet, "via", new["next_hop"]])
                log(f"Route {subnet} via {new['next_hop']} (dist {new['distance']})")


def recompute_routes_locked() -> None:
    """Rebuild best routes from scratch using current neighbor tables + direct subnets.
    Must be called with state_lock held."""
    old_table = dict(routing_table)

    # Always re-scan interfaces — picks up late-attached / re-attached networks
    new_table = direct_route_entries()

    now = time.time()
    for nbr_ip in NEIGHBORS:
        state = neighbor_tables.get(nbr_ip)
        if not state:
            continue
        if now - state["last_seen"] > DEAD_INTERVAL:
            continue  # neighbour timed out

        for subnet, nbr_dist in state["routes"].items():
            if subnet in new_table:
                continue  # direct route always wins

            candidate = min(INFINITY, nbr_dist + 1)
            if candidate >= INFINITY:
                continue

            current = new_table.get(subnet)
            if not current:
                new_table[subnet] = make_route(candidate, nbr_ip, NEIGHBOR_SOURCE)
                continue

            better = candidate < current["distance"]
            tie_break = (candidate == current["distance"] and nbr_ip < current["next_hop"])
            if better or tie_break:
                new_table[subnet] = make_route(candidate, nbr_ip, NEIGHBOR_SOURCE)

    apply_kernel_route_changes(old_table, new_table)
    routing_table.clear()
    routing_table.update(new_table)


# ─── Broadcast ────────────────────────────────────────────────────────────────

def broadcast_updates() -> None:
    """Send distance vector to each neighbor every BROADCAST_INTERVAL seconds."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    while True:
        for nbr in NEIGHBORS:
            try:
                sock.sendto(build_packet(for_neighbor=nbr), (nbr, PORT))
            except OSError as exc:
                log(f"Failed sending update to {nbr}: {exc}")
        time.sleep(BROADCAST_INTERVAL)


# ─── Listener ─────────────────────────────────────────────────────────────────

def listen_for_updates() -> None:
    """Listen for neighbor advertisements and fold them into local state."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("0.0.0.0", PORT))
    log(f"Listening for updates on UDP {PORT}")

    while True:
        data, addr = sock.recvfrom(65535)
        nbr_ip = addr[0]

        if NEIGHBORS and nbr_ip not in NEIGHBORS:
            continue

        try:
            packet = json.loads(data.decode("utf-8"))
        except json.JSONDecodeError:
            continue

        if not validate_packet(packet):
            continue

        routes = parse_routes(packet["routes"])

        with state_lock:
            neighbor_tables[nbr_ip] = {
                "last_seen": time.time(),
                "routes": routes,
            }
            recompute_routes_locked()


# ─── Maintenance Loop ─────────────────────────────────────────────────────────

def maintenance_loop() -> None:
    """Recompute routes every second so timeouts and interface changes are caught."""
    while True:
        with state_lock:
            recompute_routes_locked()
        time.sleep(1)


# ─── Display ──────────────────────────────────────────────────────────────────

def format_routing_table() -> str:
    rows = []
    for subnet, entry in sorted(routing_table.items()):
        rows.append(
            f"{subnet:<20} dist={entry['distance']:<2} "
            f"next_hop={entry['next_hop']:<15} source={entry['source']}"
        )
    return "Routing table:\n  " + "\n  ".join(rows) if rows else "Routing table: (empty)"


def print_table_loop() -> None:
    while True:
        with state_lock:
            snapshot = format_routing_table()
        log(snapshot)
        time.sleep(5)


# ─── Entry Point ──────────────────────────────────────────────────────────────

def main() -> None:
    init_routing_table()

    threading.Thread(target=broadcast_updates, daemon=True).start()
    threading.Thread(target=maintenance_loop,  daemon=True).start()
    threading.Thread(target=print_table_loop,  daemon=True).start()

    listen_for_updates()


if __name__ == "__main__":
    main()
