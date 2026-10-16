
import os
import time
import random
import psycopg2

from collections import deque
from config_validator import load_config, ConfigError, zone_adjacency
from neo4j import GraphDatabase

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

NEO4J_URI      = os.environ["NEO4J_URI"]
NEO4J_USERNAME = os.environ["NEO4J_USERNAME"]
NEO4J_PASSWORD = os.environ["NEO4J_PASSWORD"]

LAKEBASE_HOST   = os.environ["LAKEBASE_HOST"]
LAKEBASE_USER   = os.environ["LAKEBASE_USER"]
LAKEBASE_TOKEN  = os.environ["LAKEBASE_TOKEN"]
LAKEBASE_DBNAME = os.environ["LAKEBASE_DBNAME"]

TICK_SECONDS   = 2     # base tick interval
TICK_JITTER    = 0.3   # +/- random jitter in seconds
MIN_HOPS       = 20    # minimum route length
ZONE_BIAS      = 0.70  # probability of staying in home/adjacent zone
FALLBACK_LIMIT = 50    # BFS candidates to try before fallback

try:
    cfg = load_config("config.yaml")
except (FileNotFoundError, ConfigError) as e:
    raise SystemExit(f"Config error: {e}")

VEHICLES = [
    {"vehicle_id": v["id"], "zone": v["zone"]}
    for v in cfg["vehicles"]
]

ZONE_ADJACENCY = zone_adjacency(cfg)

# ---------------------------------------------------------------------------
# Load road graph from Neo4j Aura
# ---------------------------------------------------------------------------

def load_graph(driver):
    """Load intersection nodes and adjacency from Neo4j into memory."""
    nodes = {}
    edges = {}

    with driver.session() as session:
        result = session.run("""
            MATCH (i:Intersection)-[:IN_ZONE]->(z:Zone)
            RETURN i.node_id AS node_id, i.lat AS lat, i.lon AS lon,
                   z.name AS zone, i.street_count AS street_count
        """)
        for rec in result:
            nodes[rec["node_id"]] = {
                "lat":         rec["lat"],
                "lon":         rec["lon"],
                "zone":        rec["zone"],
                "street_count": rec["street_count"] or 1,
            }
            edges[rec["node_id"]] = []

        result = session.run("""
            MATCH (a:Intersection)-[:ROAD]->(b:Intersection)
            RETURN a.node_id AS u, b.node_id AS v
        """)
        for rec in result:
            u, v = rec["u"], rec["v"]
            if u in edges:
                edges[u].append(v)

    # Pre-build zone node lists for fast lookup
    zone_nodes = {}
    for nid, n in nodes.items():
        zone_nodes.setdefault(n["zone"], []).append(nid)

    total_edges = sum(len(v) for v in edges.values())

    print(f"Loaded {len(nodes):,} nodes and {total_edges:,} directed edges")
    print(f"Zone distribution: { {z: len(v) for z, v in zone_nodes.items()} }")
    return nodes, edges, zone_nodes

# ---------------------------------------------------------------------------
# BFS shortest path
# ---------------------------------------------------------------------------

def bfs_path(edges, start, goal):
    """Return list of node IDs from start to goal using BFS.
    Returns None if no path exists."""
    if start == goal:
        return [start]
    visited = {start}
    queue   = deque([[start]])
    while queue:
        path = queue.popleft()
        node = path[-1]
        for neighbour in edges.get(node, []):
            if neighbour == goal:
                return path + [neighbour]
            if neighbour not in visited:
                visited.add(neighbour)
                queue.append(path + [neighbour])
    return None

# ---------------------------------------------------------------------------
# Lakebase helpers
# ---------------------------------------------------------------------------

def pg_connect():
    conn = psycopg2.connect(
        host     = LAKEBASE_HOST,
        user     = LAKEBASE_USER,
        password = LAKEBASE_TOKEN,
        dbname   = LAKEBASE_DBNAME,
        sslmode  = "require",
        port     = 5432
    )
    conn.autocommit = True
    return conn

def pg_reconnect(conn):
    """Attempt to reconnect if the connection is closed."""
    try:
        if conn and conn.closed == 0:
            return conn
    except Exception:
        pass
    print("Lakebase connection lost -- reconnecting...")
    try:
        conn = pg_connect()
        print("Lakebase reconnected.")
        return conn
    except Exception as e:
        print(f"Lakebase reconnect failed: {e}")
        return None

def write_position(cursor, vehicle_id, lat, lon, zone):
    cursor.execute("""
        INSERT INTO vehicle_positions (vehicle_id, lat, lon, current_zone)
        VALUES (%s, %s, %s, %s)
    """, (vehicle_id, lat, lon, zone))

def update_vehicle_status(cursor, vehicle_id, status):
    cursor.execute("""
        UPDATE vehicles SET status = %s WHERE vehicle_id = %s
    """, (status, vehicle_id))

# ---------------------------------------------------------------------------
# Destination assignment
# ---------------------------------------------------------------------------

def candidate_nodes(home_zone, zone_nodes, nodes):
    """Return candidate destination nodes with zone bias applied."""
    if random.random() < ZONE_BIAS:
        # Stay in home zone or adjacent zones
        preferred_zones = ZONE_ADJACENCY.get(home_zone, [home_zone])
        candidates = []
        for z in preferred_zones:
            candidates.extend(zone_nodes.get(z, []))
        if candidates:
            return candidates
    # Fallback: any node in the graph
    return list(nodes.keys())

def assign_destination(vid, home_zone, current, nodes, edges, zone_nodes):
    """Pick a destination at least MIN_HOPS away and compute the BFS path."""
    candidates = candidate_nodes(home_zone, zone_nodes, nodes)

    for _ in range(FALLBACK_LIMIT):
        destination = random.choice(candidates)
        if destination == current:
            continue
        path = bfs_path(edges, current, destination)
        if path and len(path) >= MIN_HOPS:
            dest_zone = nodes[destination]["zone"]
            print(f"  {vid}: route {len(path)} hops -> {dest_zone}")
            return path

    # Fallback -- any reachable destination
    print(f"  {vid}: fallback route (short path)")
    for _ in range(20):
        destination = random.choice(list(nodes.keys()))
        path = bfs_path(edges, current, destination)
        if path:
            return path
    return [current]

# ---------------------------------------------------------------------------
# Vehicle placement
# ---------------------------------------------------------------------------

def place_vehicles(vehicles, nodes, edges, zone_nodes):
    """Place each vehicle in its home zone and assign initial route."""
    state = {}
    for v in vehicles:
        home_zone  = v["zone"]
        zone_pool  = zone_nodes.get(home_zone, list(nodes.keys()))
        current    = random.choice(zone_pool)
        path       = assign_destination(
            v["vehicle_id"], home_zone, current, nodes, edges, zone_nodes
        )
        state[v["vehicle_id"]] = {
            "current":   current,
            "home_zone": home_zone,
            "path":      path,
            "step":      0,
        }
    return state

# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def main():
    print("Connecting to Neo4j Aura...")
    driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USERNAME, NEO4J_PASSWORD))
    nodes, edges, zone_nodes = load_graph(driver)
    driver.close()
    print("Neo4j Aura connection closed after graph load.")

    print("\nConnecting to Lakebase...")
    conn   = pg_connect()
    cursor = conn.cursor()

    print("\nPlacing vehicles and computing initial routes...")
    state = place_vehicles(VEHICLES, nodes, edges, zone_nodes)

    # Set all vehicles to en_route
    for vid in state:
        try:
            update_vehicle_status(cursor, vid, "en_route")
        except Exception:
            pass

    print(f"\nSimulator running. Tick every ~{TICK_SECONDS}s.\n")

    tick          = 0
    reroutes      = 0
    total_written = 0

    try:
        while True:
            tick += 1

            # Check for stop signal from analytics dashboard
            if os.path.exists("simulator.stop"):
                os.remove("simulator.stop")
                print("\nStop signal received -- shutting down.")
                raise KeyboardInterrupt

            # Reconnect Lakebase if needed
            conn = pg_reconnect(conn)
            if conn is None:
                print("No Lakebase connection -- skipping tick")
                time.sleep(TICK_SECONDS)
                continue
            cursor = conn.cursor()

            for vid, s in state.items():
                s["step"] += 1

                # Reroute when destination reached or path is a dead end
                if s["step"] >= len(s["path"]) or len(s["path"]) == 1:
                    current  = s["path"][-1]
                    new_path = assign_destination(
                        vid, s["home_zone"], current, nodes, edges, zone_nodes
                    )
                    s["path"] = new_path
                    s["step"] = 0
                    reroutes += 1

                current_node = s["path"][s["step"]]
                s["current"] = current_node
                n = nodes[current_node]

                try:
                    write_position(cursor, vid, n["lat"], n["lon"], n["zone"])
                    total_written += 1
                except Exception as e:
                    print(f"  Write failed for {vid}: {e}")

            if tick % 10 == 0:
                print(f"Tick {tick:,} | {len(state)} vehicles | "
                      f"{total_written:,} positions written | "
                      f"{reroutes} reroutes")

            # Tick with jitter
            time.sleep(TICK_SECONDS + random.uniform(-TICK_JITTER, TICK_JITTER))

    except KeyboardInterrupt:
        print(f"\nSimulator stopped.")
        print(f"  Ticks completed : {tick:,}")
        print(f"  Positions written: {total_written:,}")
        print(f"  Reroutes         : {reroutes}")

        # Reset vehicle statuses
        try:
            for vid in state:
                update_vehicle_status(cursor, vid, "idle")
            print("Vehicle statuses reset to idle.")
        except Exception as e:
            print(f"Status reset failed: {e}")

        cursor.close()
        conn.close()
        print("Lakebase connection closed.")

if __name__ == "__main__":
    main()
