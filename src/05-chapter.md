# Chapter 5: The Simulator

## What the simulator does

The simulator is the engine that gives life to the system. It loads the road network from Aura into memory, places ten vehicles at random intersections in their home zones and then moves each one along the road graph, writing a GPS position to Lakebase every two seconds.

The simulator has four responsibilities:

1. Load the road graph from Aura once at startup into an in-memory adjacency structure.
2. Compute an initial route for each vehicle using Breadth-First Search (BFS).
3. Move each vehicle one intersection at a time along its route, writing a position record to Lakebase at each tick.
4. Assign a new destination when a vehicle reaches the end of its current route.

It runs as a background subprocess, started from the Jupyter notebook and continues independently while you use the Streamlit dashboard.

## Running as a background process

The simulator uses the `%%writefile` pattern -- the Jupyter cell writes the simulator source code to `simulator.py` on disk and then a second cell launches it as a subprocess:

```python
env = os.environ.copy()
env["PYTHONUNBUFFERED"] = "1"

proc = subprocess.Popen(
    [sys.executable, "-u", "simulator.py"],
    env    = env,
    stdout = subprocess.PIPE,
    stderr = subprocess.STDOUT,
    text   = True
)
```

Two details are important here:

1. `sys.executable` uses the virtual environment's Python interpreter; using the bare `python` command would pick up the system Python, which doesn't have `neo4j` or `psycopg2` installed.
2. `-u` (unbuffered) combined with `PYTHONUNBUFFERED=1` forces Python to flush output immediately rather than buffering it, which means we can see the simulator's startup messages in the notebook.

After launch, a sentinel loop reads lines from the subprocess until it sees "Simulator running" (or until it has read 30 lines, which is more than the startup messages need):

```python
for _ in range(30):
    line = proc.stdout.readline()
    if line:
        print(line, end="")
    if "Simulator running" in line:
        break
print("\nSimulator is running in the background.")
```

Once the sentinel fires, the loop exits and the simulator continues independently in the background. The notebook cell completes, leaving the simulator process alive.

## Loading the road graph

At startup, the simulator connects to Aura and loads the road graph into memory with two queries. The first loads every intersection together with its coordinates and zone. The second loads the directed `ROAD` relationships:

```python
result = session.run("""
    MATCH (i:Intersection)-[:IN_ZONE]->(z:Zone)
    RETURN i.node_id AS node_id, i.lat AS lat, i.lon AS lon,
           z.name AS zone, i.street_count AS street_count
""")
for rec in result:
    nodes[rec["node_id"]] = {
        "lat":          rec["lat"],
        "lon":          rec["lon"],
        "zone":         rec["zone"],
        "street_count": rec["street_count"] or 1,
    }
    edges[rec["node_id"]] = []

result = session.run("""
    MATCH (a:Intersection)-[:ROAD]->(b:Intersection)
    RETURN a.node_id AS u, b.node_id AS v
""")
for rec in result:
    if rec["u"] in edges:
        edges[rec["u"]].append(rec["v"])
```

The result is two plain Python dictionaries: `nodes` maps each `node_id` to its coordinates and zone and `edges` maps each `node_id` to the list of intersections reachable in one step. A third dictionary, `zone_nodes`, groups the node IDs by zone name. The simulator doesn't load road lengths, because BFS counts intersections and not distance. All routing from this point runs entirely in these in-memory structures and no further Aura queries happen during the simulation loop.

Loading the full graph into memory rather than querying Aura per tick is a deliberate design choice. An Aura query per tick would add latency and load to Aura unnecessarily. The road network doesn't change between ticks, so there's no reason to re-read it.

## BFS routing

Each vehicle follows a route computed by breadth-first search. BFS finds the shortest path in terms of number of hops (intersections traversed), not physical distance. For a vehicle tracking demo, hop count is close enough to physical distance to produce realistic-looking movement. The route lengths the simulator prints are the number of intersections in the path.

The search itself is a standard BFS from the vehicle's current intersection to a specific goal:

```python
def bfs_path(edges, start, goal):
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
```

The `assign_destination()` function picks the goal. It enforces a minimum route length of 20 intersections, because shorter routes would cause vehicles to reach their destination almost immediately, which makes for a less interesting demo. It picks a random candidate destination, runs BFS and keeps the route only if it's long enough. It tries up to 50 candidates. If none of them gives a long enough route, it prints a "fallback route" message and accepts the first reachable destination it finds among 20 random tries, however short. If even that fails (which is rare, because Chapter 2 keeps only the largest connected part of the road network), the vehicle's route is just its current intersection and it's assigned a new destination on the next tick.

## Zone bias

Vehicles don't route randomly across the entire borough. They have a home zone and 70% of the time their next destination is chosen from the intersections in their home zone or an adjacent zone. The remaining 30% of the time, the destination is chosen from any intersection in the borough. This creates realistic clustering, as vehicles tend to work their home area, with occasional longer cross-borough runs.

The zone adjacency from the config drives this. The `zone_adjacency()` helper function from `config_validator.py` returns a dictionary mapping each zone name to a list of that zone and its neighbors:

```python
ZONE_ADJACENCY = zone_adjacency(cfg)
# {'Wimbledon': ['Wimbledon', 'Raynes Park', 'Colliers Wood'], ...}
```

For the 70% case, the simulator pools all the intersections of the home zone and its neighbors and picks one at random from the pool. This means that larger zones are chosen more often than smaller ones. Because the 30% case can also land in the home area, a vehicle's destination is in its home zone or an adjacent zone at least 70% of the time.

## The main loop

The simulator runs a simple tick loop. Each tick:

1. Checks for a stop signal (discussed below).
2. Reconnects to Lakebase if the connection dropped.
3. Advances each vehicle one step along its route.
4. Assigns a new destination if the vehicle has reached the end of its route.
5. Writes a position record to Lakebase for each vehicle.
6. Prints a summary line every ten ticks.
7. Sleeps for approximately two seconds, with a small random jitter.

The tick jitter (`±0.3` seconds, randomized per tick) varies the interval between ticks, so the stream of position records isn't perfectly regular. The ten inserts within a tick still run one after another, a few tens of milliseconds apart.

```python
time.sleep(TICK_SECONDS + random.uniform(-TICK_JITTER, TICK_JITTER))
```

## Writing positions to Lakebase

Each position write is a single SQL INSERT:

```python
cursor.execute("""
    INSERT INTO vehicle_positions (vehicle_id, lat, lon, current_zone)
    VALUES (%s, %s, %s, %s)
""", (vehicle_id, lat, lon, zone))
```

The simulator doesn't populate the `speed_kmh` column, so it's NULL in every row.

The `current_zone` comes from the in-memory graph and not from an Aura query. The zone of each intersection was loaded with its coordinates at startup, so the simulator reads it from the `nodes` dictionary on every tick:

```python
n = nodes[current_node]
write_position(cursor, vid, n["lat"], n["lon"], n["zone"])
```

## Lakebase reconnection

The simulator checks its Lakebase connection at the start of each tick and tries to reconnect if the connection has closed:

```python
def pg_reconnect(conn):
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
```

If the reconnection fails, the simulator logs the error, skips that tick (so the vehicles don't advance either) and tries again after a short pause. There's an important limit to this. The simulator reads `LAKEBASE_TOKEN` from the environment once, when it starts, and every reconnect reuses that same token. The OAuth token lasts an hour, so once it has expired a reconnect can't succeed. To keep the simulator running longer, generate a fresh token, update `LAKEBASE_TOKEN` and start the simulator again.

## Stopping the simulator

The simulator checks for a stop signal at the top of each tick. The Streamlit analytics dashboard's "Stop Simulator" button writes a small flag file called `simulator.stop` to disk. The simulator detects it, deletes it and exits cleanly:

```python
if os.path.exists("simulator.stop"):
    os.remove("simulator.stop")
    print("\nStop signal received -- shutting down.")
    raise KeyboardInterrupt
```

This is a simple inter-process communication pattern -- no sockets, no message queues, just a file on the shared filesystem. It works because both the Streamlit application and the simulator run in the same directory. The `KeyboardInterrupt` triggers the existing clean shutdown code, which resets all vehicle statuses to `idle` in Lakebase before the process exits.

Alternatively, the simulator can be stopped from the Jupyter notebook by running:

```python
proc.terminate()
```

This stops the process immediately and skips the clean shutdown, so the vehicles stay marked `en_route` in Lakebase. Use the stop flag when you want the statuses reset.

## Dead-end detection

Some intersections in the road graph are dead ends, as they have no outgoing edges. BFS can't find a path to a destination that's unreachable from the vehicle's current position and in that case `bfs_path()` returns `None`, so `assign_destination()` simply tries another candidate. If it runs out of candidates, the vehicle ends up with a one-node route (just its current intersection). The main loop detects this and assigns a new destination straight away:

```python
if s["step"] >= len(s["path"]) or len(s["path"]) == 1:
    current  = s["path"][-1]
    new_path = assign_destination(
        vid, s["home_zone"], current, nodes, edges, zone_nodes
    )
    s["path"] = new_path
    s["step"] = 0
```

The same check also handles the normal case of a vehicle that has reached the end of its route.

## Startup output

When the simulator starts, it prints a summary of the graph, the zone distribution and the initial route assignment for each vehicle:

```
Simulator started (PID 61772)
Using Python: /Users/akmalchaudhri/venv/bin/python3.12
Connecting to Neo4j Aura...
Loaded 3,212 nodes and 7,296 directed edges
Zone distribution: {'Wimbledon': 839, 'Raynes Park': 761, 'Colliers Wood': 544, 'Mitcham': 727, 'Morden': 340, 'Unknown': 1}
Neo4j Aura connection closed after graph load.

Connecting to Lakebase...

Placing vehicles and computing initial routes...
  V001: route 51 hops -> Morden
  V002: route 34 hops -> Raynes Park
  V003: route 28 hops -> Wimbledon
  V004: route 90 hops -> Wimbledon
  V005: route 29 hops -> Wimbledon
  V006: route 69 hops -> Mitcham
  V007: route 86 hops -> Colliers Wood
  V008: route 94 hops -> Colliers Wood
  V009: route 40 hops -> Mitcham
  V010: route 48 hops -> Wimbledon

Simulator running. Tick every ~2s.

Simulator is running in the background.
```

The hop count shows how long each vehicle's initial route is. In this run the routes ranged from about 30 to 95 hops. Longer routes appear when the destination is on the far side of the borough, which is normal and produces interesting movement on the map. The route and zone details differ from run to run, because the starting points and destinations are random.

## Verifying the simulator

A cell at the end of the notebook connects directly to Lakebase and checks that position records are being written:

```
Total position records : 30

Latest position per vehicle:
  Vehicle           Lat        Lon  Zone            Recorded at
  V001         51.42424   -0.21572  Wimbledon       2026-10-16 ...
  V002         51.42401   -0.20344  Wimbledon       2026-10-16 ...
  V003         51.41894   -0.17736  Raynes Park     2026-10-16 ...
  V004         51.40900   -0.15290  Mitcham         2026-10-16 ...
  V005         51.40693   -0.20492  Colliers Wood   2026-10-16 ...
  V006         51.40386   -0.21403  Colliers Wood   2026-10-16 ...
  V007         51.40319   -0.13600  Mitcham         2026-10-16 ...
  V008         51.40012   -0.13340  Mitcham         2026-10-16 ...
  V009         51.38789   -0.16093  Morden          2026-10-16 ...
  V010         51.38958   -0.18673  Morden          2026-10-16 ...
```

The cell waits six seconds after the simulator starts, so the count is small. It grows continuously as the simulator runs -- ten vehicles writing every two seconds, or a little under 300 records a minute. The latest position per vehicle confirms each is active and reporting from a zone. Vehicles leave their home zones as they follow their routes, which is why V004, a Raynes Park vehicle, is already in Mitcham.

## Gotchas

**sys.executable vs python.** Always use `sys.executable` in the `Popen` call. The bare `python` or `python3` command uses the system Python, which won't have the project's dependencies installed.

**Output buffering.** Without `-u` and `PYTHONUNBUFFERED=1`, the subprocess buffers its output and nothing appears in the notebook. Both flags are required.

**readline() blocking.** If you loop on `proc.stdout.readline()` with no exit condition, the loop blocks indefinitely once the simulator is running (because the simulator is producing output, not closing `stdout`). Always break on a sentinel string. The notebook also reads at most 30 lines.

**Keep the notebook kernel running.** The notebook reads the simulator's output through a pipe and stops reading once the sentinel fires. If you restart or close the kernel, the pipe closes and the simulator will probably fail the next time it prints something and exit. Keep the kernel alive while the simulator runs, or stop the simulator first.

**Stop flag file location.** The `simulator.stop` flag file must be created in the same directory from which the simulator is running. If the Streamlit analytics application and the simulator are started from different directories, the button won't work. Run everything from the project root.

**Fallback routes.** A "fallback route (short path)" message means that none of 50 randomly chosen destinations gave a route of 20 or more intersections. It can appear at startup or later, when a vehicle is assigned a new destination. The vehicle will still move; the route just isn't guaranteed to be long.

**Expired token.** The simulator uses the token it was started with. After the token expires, restart the simulator with a fresh `LAKEBASE_TOKEN`.
