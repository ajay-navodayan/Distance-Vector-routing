"""
Distance-Vector Router (DV-JSON Protocol)
Assignment 4 - Custom Routing Daemon
Implements: Bellman-Ford, Split Horizon, Triggered Updates, UDP updates on port 5000
"""

import socket
import json
import threading
import time
import os
import logging
import subprocess

# ─── Configuration ────────────────────────────────────────────────────────────
MY_IP        = os.getenv("MY_IP", "127.0.0.1")
ROUTER_NAME  = os.getenv("ROUTER_NAME", MY_IP)
NEIGHBORS    = [n for n in os.getenv("NEIGHBORS", "").split(",") if n]
MY_SUBNETS   = [s for s in os.getenv("MY_SUBNETS", "").split(",") if s]
PING_TARGETS = [t for t in os.getenv("PING_TARGETS", "").split(",") if t]
PORT             = 5000
INFINITY         = 16    # RIP-style infinity (max hop count)
UPDATE_INTERVAL  = 5     # seconds between periodic broadcasts
TIMEOUT_INTERVAL = 15    # seconds before a silent neighbor is declared dead
PING_INTERVAL    = 5     # seconds between reachability pings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("router")

# ─── Shared State ─────────────────────────────────────────────────────────────
# routing_table: { subnet_cidr: {"distance": int, "next_hop": str} }
routing_table = {}
table_lock = threading.Lock()

# neighbor_last_seen: { neighbor_ip: float(timestamp) }
neighbor_last_seen = {}
seen_lock = threading.Lock()


# ─── Initialization ───────────────────────────────────────────────────────────

def init_routing_table():
    """Seed directly-connected subnets at distance 0."""
    with table_lock:
        for subnet in MY_SUBNETS:
            routing_table[subnet] = {"distance": 0, "next_hop": "0.0.0.0"}
    log.info("Local subnets seeded: %s", MY_SUBNETS)


# ─── Packet Handling ──────────────────────────────────────────────────────────

def build_packet(exclude_neighbor=None):
    """
    Construct a DV-JSON update packet.
    Split Horizon: omit any route whose next_hop == exclude_neighbor so we never
    advertise a route back to the neighbor we learned it from.
    """
    with table_lock:
        routes = [
            {"subnet": subnet, "distance": info["distance"]}
            for subnet, info in routing_table.items()
            if info["next_hop"] != exclude_neighbor
        ]
    packet = {"router_id": MY_IP, "version": 1.0, "routes": routes}
    return json.dumps(packet).encode()


def build_poison_packet():
    """
    Build a packet that poisons ALL currently-known routes by advertising them
    at INFINITY. Sent to remaining neighbours immediately after a failure is
    detected to speed up convergence (Triggered Update with Poisoned Reverse).
    """
    with table_lock:
        routes = [
            {"subnet": subnet, "distance": INFINITY}
            for subnet, info in routing_table.items()
            if info["distance"] >= INFINITY          # only already-dead routes
        ]
    packet = {"router_id": MY_IP, "version": 1.0, "routes": routes}
    return json.dumps(packet).encode()


def parse_packet(data):
    """Parse and validate a received DV-JSON packet."""
    try:
        pkt = json.loads(data.decode())
        if pkt.get("version") != 1.0:
            return None, None
        return pkt["router_id"], pkt["routes"]
    except (json.JSONDecodeError, KeyError):
        return None, None


# ─── Broadcast ────────────────────────────────────────────────────────────────

def broadcast_updates():
    """Periodic update sender — runs every UPDATE_INTERVAL seconds."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

    while True:
        for neighbor in NEIGHBORS:
            try:
                payload = build_packet(exclude_neighbor=neighbor)
                routes  = json.loads(payload)["routes"]
                sock.sendto(payload, (neighbor, PORT))
                log.info("TX → %s  (%d routes)", neighbor, len(routes))
            except Exception as exc:
                log.warning("Send failed to %s: %s", neighbor, exc)
        time.sleep(UPDATE_INTERVAL)


def send_triggered_update():
    """
    Immediately push poisoned-route information to all neighbours.
    Called right after a neighbour failure is detected so remaining
    routers can re-converge without waiting for the next periodic cycle.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    payload = build_poison_packet()
    if not json.loads(payload)["routes"]:
        sock.close()
        return  # nothing to poison
    for neighbor in NEIGHBORS:
        try:
            sock.sendto(payload, (neighbor, PORT))
            log.info("TRIGGERED UPDATE (poison) → %s", neighbor)
        except Exception as exc:
            log.warning("Triggered update failed to %s: %s", neighbor, exc)
    sock.close()


# ─── Listener ─────────────────────────────────────────────────────────────────

def listen_for_updates():
    """Main receive loop — blocks on recvfrom, processes every incoming update."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("0.0.0.0", PORT))
    log.info("Listening on UDP port %d", PORT)

    while True:
        try:
            data, addr = sock.recvfrom(65535)
            neighbor_ip = addr[0]

            # Heartbeat — record last-seen time for this neighbour
            with seen_lock:
                neighbor_last_seen[neighbor_ip] = time.time()

            router_id, routes = parse_packet(data)
            if router_id is None:
                log.warning("Malformed packet from %s — dropped", neighbor_ip)
                continue

            log.info("RX ← %s  (%d routes)", neighbor_ip, len(routes))
            update_logic(neighbor_ip, routes)

        except Exception as exc:
            log.error("Listener error: %s", exc)


# ─── Bellman-Ford Update Logic ────────────────────────────────────────────────

def update_logic(neighbor_ip, routes_from_neighbor):
    """
    Bellman-Ford: for each advertised route,
        new_cost = their_cost + 1   (all links have equal cost 1)
    Update our table if the new cost is strictly better, or refresh a
    same-hop route whose metric changed.
    """
    changed       = False
    # Collect kernel operations to perform AFTER releasing table_lock
    to_install    = []   # list of (subnet, via)
    to_remove     = []   # list of subnet

    with table_lock:
        for route in routes_from_neighbor:
            subnet  = route["subnet"]
            their_d = route["distance"]

            # ── Poisoned route received: neighbour is telling us this is dead ──
            if their_d >= INFINITY:
                existing = routing_table.get(subnet)
                if existing and existing["next_hop"] == neighbor_ip:
                    routing_table[subnet] = {"distance": INFINITY, "next_hop": neighbor_ip}
                    to_remove.append(subnet)
                    changed = True
                continue

            new_distance = their_d + 1

            # Don't install routes that already exceed infinity
            if new_distance >= INFINITY:
                continue

            existing = routing_table.get(subnet)

            if existing is None or new_distance < existing["distance"]:
                # New subnet or shorter path found
                routing_table[subnet] = {"distance": new_distance, "next_hop": neighbor_ip}
                to_install.append((subnet, neighbor_ip))
                changed = True

            elif existing["next_hop"] == neighbor_ip and new_distance != existing["distance"]:
                # Same next-hop but metric changed — refresh
                routing_table[subnet] = {"distance": new_distance, "next_hop": neighbor_ip}
                to_install.append((subnet, neighbor_ip))
                changed = True

    # ── Apply kernel changes OUTSIDE the lock (os.system is slow) ─────────────
    for subnet in to_remove:
        remove_kernel_route(subnet)
    for subnet, via in to_install:
        install_kernel_route(subnet, via)

    if changed:
        print_routing_table()


# ─── Kernel Route Management ──────────────────────────────────────────────────

def install_kernel_route(subnet, via):
    """Install or update a kernel route using 'ip route replace'."""
    ret = os.system(f"ip route replace {subnet} via {via}")
    if ret != 0:
        log.warning("ip route replace FAILED: %s via %s", subnet, via)
    else:
        log.info("KERNEL: %s via %s installed", subnet, via)


def remove_kernel_route(subnet):
    """Remove a kernel route silently (ignore 'not found' errors)."""
    ret = os.system(f"ip route del {subnet} 2>/dev/null")
    if ret == 0:
        log.info("KERNEL: %s removed", subnet)


# ─── Failure Detection ────────────────────────────────────────────────────────

def check_dead_neighbors():
    """
    Background thread: every UPDATE_INTERVAL seconds, check whether any
    neighbour has been silent for longer than TIMEOUT_INTERVAL.
    If so, invalidate all routes learned through it and send a triggered update.
    """
    while True:
        time.sleep(UPDATE_INTERVAL)
        now  = time.time()
        dead = []

        with seen_lock:
            for nbr, last in list(neighbor_last_seen.items()):
                if now - last > TIMEOUT_INTERVAL:
                    dead.append(nbr)

        for nbr in dead:
            log.warning("Neighbour %s timed out — invalidating routes", nbr)
            invalidate_neighbor_routes(nbr)
            with seen_lock:
                neighbor_last_seen.pop(nbr, None)

        if dead:
            # Push poisoned routes immediately to speed up convergence
            send_triggered_update()


def invalidate_neighbor_routes(neighbor_ip):
    """
    Mark all routes through a dead neighbour as INFINITY and remove
    their kernel entries.  Lock is held only during the table scan;
    kernel calls happen after the lock is released.
    """
    to_remove = []

    with table_lock:
        for subnet, info in routing_table.items():
            if info["next_hop"] == neighbor_ip and info["distance"] < INFINITY:
                routing_table[subnet] = {"distance": INFINITY, "next_hop": neighbor_ip}
                to_remove.append(subnet)

    # Kernel calls outside the lock
    for subnet in to_remove:
        remove_kernel_route(subnet)

    if to_remove:
        print_routing_table()


# ─── Display ──────────────────────────────────────────────────────────────────

def print_routing_table():
    """Print the current routing table to stdout (convergence log)."""
    with table_lock:
        snapshot = list(routing_table.items())

    header = ROUTER_NAME
    if not header.startswith("Router "):
        header = "Router " + header
    lines = [f"\n{header}"]
    for subnet, info in sorted(snapshot):
        dist = "INF" if info["distance"] >= INFINITY else str(info["distance"])
        lines.append(f"{subnet}   distance {dist}   next hop {info['next_hop']}")
    print("\n".join(lines), flush=True)


# ─── Optional: Connectivity Ping ─────────────────────────────────────────────

def ping_targets():
    """
    Optional background thread: periodically ping configured PING_TARGETS
    to verify end-to-end reachability through the installed kernel routes.
    """
    if not PING_TARGETS:
        return
    while True:
        for target in PING_TARGETS:
            try:
                result = subprocess.run(
                    ["ping", "-c", "1", "-W", "1", target],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                )
                status = "OK" if result.returncode == 0 else "FAIL"
                log.info("PING %-18s %s", target, status)
            except Exception as exc:
                log.warning("PING %s error: %s", target, exc)
        time.sleep(PING_INTERVAL)


# ─── Entry Point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    log.info("="*52)
    log.info(" Router starting: %s  (%s)", ROUTER_NAME, MY_IP)
    log.info(" Neighbours : %s", NEIGHBORS)
    log.info(" Own subnets: %s", MY_SUBNETS)
    log.info("="*52)

    init_routing_table()
    print_routing_table()

    threading.Thread(target=broadcast_updates,  daemon=True).start()
    threading.Thread(target=check_dead_neighbors, daemon=True).start()
    threading.Thread(target=ping_targets,        daemon=True).start()
    listen_for_updates()  # blocks — runs in main thread
