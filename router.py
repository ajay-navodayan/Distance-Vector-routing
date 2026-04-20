"""
Distance-Vector Router (DV-JSON Protocol)
Assignment 4 - Custom Routing Daemon (simple version)
Implements: Bellman-Ford, Split Horizon, UDP updates on port 5000
"""

import socket
import json
import threading
import time
import os
import logging

# ─── Configuration ────────────────────────────────────────────────────────────
MY_IP = os.getenv("MY_IP", "127.0.0.1")
NEIGHBORS = [n for n in os.getenv("NEIGHBORS", "").split(",") if n]
MY_SUBNETS = [s for s in os.getenv("MY_SUBNETS", "").split(",") if s]
PORT = 5000
INFINITY = 16            # RIP-style infinity
UPDATE_INTERVAL = 5      # seconds
TIMEOUT_INTERVAL = 15    # seconds

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("router")

# routing_table: { subnet: {"distance": int, "next_hop": str} }
routing_table = {}
table_lock = threading.Lock()

neighbor_last_seen = {}
seen_lock = threading.Lock()


def init_routing_table():
    """Seed directly-connected subnets (distance 0)."""
    with table_lock:
        for subnet in MY_SUBNETS:
            routing_table[subnet] = {"distance": 0, "next_hop": "0.0.0.0"}
    log.info("Local subnets: %s", MY_SUBNETS)


def build_packet(exclude_neighbor=None):
    """Create DV-JSON packet; Split Horizon by omitting routes via exclude_neighbor."""
    with table_lock:
        routes = [
            {"subnet": subnet, "distance": info["distance"]}
            for subnet, info in routing_table.items()
            if info["next_hop"] != exclude_neighbor
        ]
    packet = {"router_id": MY_IP, "version": 1.0, "routes": routes}
    return json.dumps(packet).encode()


def parse_packet(data):
    try:
        pkt = json.loads(data.decode())
        if pkt.get("version") != 1.0:
            return None, None
        return pkt["router_id"], pkt["routes"]
    except (json.JSONDecodeError, KeyError):
        return None, None


def broadcast_updates():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

    while True:
        for neighbor in NEIGHBORS:
            try:
                payload = build_packet(exclude_neighbor=neighbor)
                sock.sendto(payload, (neighbor, PORT))
            except Exception as exc:
                log.warning("Send failed to %s: %s", neighbor, exc)
        time.sleep(UPDATE_INTERVAL)


def listen_for_updates():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("0.0.0.0", PORT))
    log.info("Listening on UDP %d", PORT)

    while True:
        try:
            data, addr = sock.recvfrom(65535)
            neighbor_ip = addr[0]

            with seen_lock:
                neighbor_last_seen[neighbor_ip] = time.time()

            router_id, routes = parse_packet(data)
            if router_id is None:
                log.warning("Bad packet from %s", neighbor_ip)
                continue

            # Use the sender's IP as next hop to ensure a reachable gateway
            update_logic(neighbor_ip, routes)
        except Exception as exc:
            log.error("Listener error: %s", exc)


def update_logic(neighbor_ip, routes_from_neighbor):
    """Bellman-Ford update: new_distance = neighbor_distance + 1."""
    changed = False

    with table_lock:
        for route in routes_from_neighbor:
            subnet = route["subnet"]
            their_d = route["distance"]

            if their_d >= INFINITY:
                existing = routing_table.get(subnet)
                if existing and existing["next_hop"] == neighbor_ip:
                    routing_table[subnet] = {"distance": INFINITY, "next_hop": neighbor_ip}
                    remove_kernel_route(subnet)
                    changed = True
                continue

            new_distance = their_d + 1
            if new_distance >= INFINITY:
                continue

            existing = routing_table.get(subnet)

            if existing is None or new_distance < existing["distance"]:
                routing_table[subnet] = {"distance": new_distance, "next_hop": neighbor_ip}
                install_kernel_route(subnet, neighbor_ip)
                changed = True
            elif existing["next_hop"] == neighbor_ip and new_distance != existing["distance"]:
                routing_table[subnet] = {"distance": new_distance, "next_hop": neighbor_ip}
                install_kernel_route(subnet, neighbor_ip)
                changed = True

    if changed:
        print_routing_table()


def install_kernel_route(subnet, via):
    cmd = f"ip route replace {subnet} via {via}"
    ret = os.system(cmd)
    if ret != 0:
        log.warning("ip route replace failed for %s via %s", subnet, via)


def remove_kernel_route(subnet):
    os.system(f"ip route del {subnet} 2>/dev/null || true")


def check_dead_neighbors():
    """Expire silent neighbors and invalidate their routes."""
    while True:
        time.sleep(UPDATE_INTERVAL)
        now = time.time()
        dead = []

        with seen_lock:
            for nbr, last in list(neighbor_last_seen.items()):
                if now - last > TIMEOUT_INTERVAL:
                    dead.append(nbr)

        for nbr in dead:
            invalidate_neighbor_routes(nbr)
            with seen_lock:
                neighbor_last_seen.pop(nbr, None)


def invalidate_neighbor_routes(neighbor_ip):
    with table_lock:
        for subnet, info in routing_table.items():
            if info["next_hop"] == neighbor_ip and info["distance"] < INFINITY:
                routing_table[subnet] = {"distance": INFINITY, "next_hop": neighbor_ip}
                remove_kernel_route(subnet)
    print_routing_table()


def print_routing_table():
    with table_lock:
        lines = ["\nRouting Table (" + MY_IP + ")"]
        lines.append("Subnet                Distance  Next Hop")
        lines.append("------------------------------------------------")
        for subnet, info in sorted(routing_table.items()):
            dist = "INF" if info["distance"] >= INFINITY else str(info["distance"])
            lines.append(f"{subnet:<20}  {dist:<8}  {info['next_hop']}")
    print("\n".join(lines), flush=True)


if __name__ == "__main__":
    log.info("Starting router MY_IP=%s NEIGHBORS=%s SUBNETS=%s", MY_IP, NEIGHBORS, MY_SUBNETS)
    init_routing_table()
    print_routing_table()

    threading.Thread(target=broadcast_updates, daemon=True).start()
    threading.Thread(target=check_dead_neighbors, daemon=True).start()
    listen_for_updates()
