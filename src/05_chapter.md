# Chapter 5: The Simulator

## What the Simulator Does

The simulator is the engine that gives life to the system. It loads the road network from Aura into memory, places ten vehicles at their home intersections and then moves each one along the road graph, writing a GPS position to Lakebase every two seconds.

The simulator has four responsibilities:

1. Load the road graph from Aura once at startup into an in-memory adjacency structure
2. Compute an initial route for each vehicle using Breadth-First Search (BFS)
3. Move each vehicle one intersection at a time along its route, writing a position record to Lakebase at each tick
4. Assign a new destination when a vehicle reaches the end of its current route

It runs as a background subprocess, started from the Jupyter notebook and continues independently while you use the Streamlit dashboard.

## Running as a Background Process

The simulator uses the `%%writefile` pattern -- the Jupyter cell writes the simulator source code to `simulator.py` on disk and then a second cell launches it as a subprocess:

```python
proc = subprocess.Popen(
    [sys.executable, "-u", "simulator.py"],
    stdout=subprocess.PIPE,
    stderr=subprocess.STDOUT,
    text=True,
    env={**os.environ, "PYTHONUNBUFFERED": "1"}
)
```

Two details are important here:

1. `sys.executable` uses the virtual environment's Python interpreter; using the bare `python` command would pick up the system Python, which doesn't have `neo4j` or `psycopg2` installed
2. `-u` (unbuffered) combined with `PYTHONUNBUFFERED=1` forces Python to flush output immediately rather than buffering it, which means we can see the simulator's startup messages in the notebook

After launch, a sentinel loop reads lines from the subprocess until it sees "Simulator running":

```python
for line in proc.stdout:
    print(line, end="")
    if "Simulator running" in line:
        break
print("Simulator is running in the background.")
```

Once the sentinel fires, the loop exits and the simulator continues independently in the background. The notebook cell completes, leaving the simulator process alive.

## Loading the Road Graph

At startup, the simulator connects to Aura and loads the entire road graph into memory:

```python
with driver.session() as session:
    result = session.run("""
        MATCH (a:Intersection)-[r:ROAD]->(b:Intersection)
        RETURN a.node_id AS u, b.node_id AS v, r.length_m AS length_m
    """)
    for rec in result:
        graph[rec["u"]].append((rec["v"], rec["length_m"]))
```

The result is a plain Python dictionary: `{node_id: [(neighbour_id, length_m), ...]}`. With Merton's edges, this takes a second or two and uses negligible memory. All routing from this point runs entirely in this in-memory structure and no further Aura queries happen during the simulation loop.

Loading the full graph into memory rather than querying Aura per tick is a deliberate design choice. An Aura query per tick would add latency and load to Aura unnecessarily. The road network doesn't change between ticks, so there's no reason to re-read it.

## BFS Routing

Each vehicle follows a route computed by breadth-first search. BFS finds the shortest path in terms of number of hops (intersections traversed), not physical distance. For a vehicle tracking demo, hop count is close enough to physical distance to produce realistic-looking movement.

The routing function enforces a minimum path length of 20 hops. Shorter routes would cause vehicles to reach their destination almost immediately and spend most of their time idle, which makes for a less interesting demo. If BFS can't find a path of 20 or more hops in the given direction, it falls back to the longest path it found and if it can't find any path at all (which can happen in graphs with isolated components), it uses a very short local fallback:

```python
def bfs_route(graph, start, min_hops=20):
    queue = deque([(start, [start])])
    best = None
    while queue:
        node, path = queue.popleft()
        if len(path) >= min_hops:
            return path
        if best is None or len(path) > len(best):
            best = path
        for neighbour, _ in graph.get(node, []):
            if neighbour not in path:
                queue.append((neighbour, path + [neighbour]))
    return best or [start]
```

## Zone Bias

Vehicles don't route randomly across the entire borough. They have a home zone and 70% of the time their next destination is chosen from intersections in their home zone or an adjacent zone. The remaining 30% of the time, the destination is any intersection in the borough. This creates realistic clustering, as vehicles tend to work their home area, with occasional longer cross-borough runs.

The zone adjacency from the config drives this. The `zone_adjacency()` helper function from `config_validator.py` returns a dictionary mapping each zone name to a list of that zone and its neighbors:

```python
ZONE_ADJACENCY = zone_adjacency(cfg)
# {'Wimbledon': ['Wimbledon', 'Raynes Park', 'Colliers Wood'], ...}
```

When assigning a destination, the simulator picks a zone with 70/30 weighting, then chooses a random intersection from that zone's node list.

## The Main Loop

The simulator runs a simple tick loop. Each tick:

1. Checks for a stop signal (discussed below)
2. Reconnects to Postgres if the connection dropped
3. Advances each vehicle one step along its route
4. Writes a position record to Lakebase
5. Assigns a new destination if the vehicle has reached the end of its route
6. Sleeps for approximately two seconds, with a small random jitter

The tick jitter (`±0.3` seconds, randomized per tick) prevents all ten vehicles from writing simultaneously, which would create brief spikes in Lakebase write load. Staggered writes produce a smoother stream of position records.

```python
tick_sleep = TICK_SECONDS + random.uniform(-TICK_JITTER, TICK_JITTER)
time.sleep(tick_sleep)
```

## Writing Positions to Lakebase

Each position write is a single SQL INSERT:

```python
cursor.execute("""
    INSERT INTO vehicle_positions
        (vehicle_id, lat, lon, speed_kmh, current_zone)
    VALUES (%s, %s, %s, %s, %s)
""", (vehicle_id, lat, lon, speed_kmh, current_zone))
```

The `current_zone` is determined from the in-memory zone lookup and not an Aura query. At startup, the simulator builds a dictionary mapping each `node_id` to its zone name, loaded from Aura:

```python
result = session.run("""
    MATCH (i:Intersection)-[:IN_ZONE]->(z:Zone)
    RETURN i.node_id AS node_id, z.name AS zone
""")
node_zone = {rec["node_id"]: rec["zone"] for rec in result}
```

The `current_zone` value is then just `node_zone.get(current_node_id, "Unknown")` on every tick.

## Postgres Reconnection

The simulator handles Postgres disconnections gracefully. The OAuth token expires after one hour and the connection will drop when it does. Rather than crashing, the simulator reconnects at the start of each tick:

```python
def pg_reconnect(conn):
    try:
        conn.cursor().execute("SELECT 1")
        return conn
    except Exception:
        return psycopg2.connect(...)
```

If the reconnection itself fails (for example, because the token has expired and hasn't been refreshed), the simulator logs the error and retries on the next tick. Vehicles continue to route in memory; only the Lakebase writes are paused until connectivity is restored.

## Stopping the Simulator

The simulator checks for a stop signal at the top of each tick. The Streamlit analytics dashboard's "Stop Simulator" button writes a small flag file called `simulator.stop` to disk. The simulator detects it, deletes it and exits cleanly:

```python
if os.path.exists("simulator.stop"):
    os.remove("simulator.stop")
    print("\nStop signal received -- shutting down.")
    raise KeyboardInterrupt
```

This is a simple inter-process communication pattern -- no sockets, no message queues, just a file on the shared filesystem. It works because both the Streamlit application and the simulator run in the same directory. The `KeyboardInterrupt` triggers the existing clean shutdown code, which resets all vehicle statuses to `idle` in Lakebase before the process exits.

Alternatively, the simulator can be stopped from the Jupyter notebook by interrupting the kernel or running:

```python
proc.terminate()
```

## Dead-End Detection

Some intersections in the road graph are dead ends as they have no outgoing edges or all their outgoing edges lead back to nodes already visited in the current route. When BFS gets stuck at a dead end, it returns the shortest path it found rather than an empty list. The simulator detects a one-node path (just the current intersection, with no next step) and assigns a new destination immediately:

```python
if len(state["path"]) == 1:
    # Dead end -- assign new destination
    state["path"] = assign_destination(vehicle_id, current_node)
```

Dead ends are more common in the San Francisco and Singapore graphs, where some streets connect to motorway on-ramps or service roads with no exit back into the local network.

## Startup Output

When the simulator starts, it prints a summary of the zone distribution and the initial route assignment for each vehicle:

```
Simulator started (PID 87163)
Using Python: /Users/veryfatboy/myenv/bin/python3.12
Connecting to Neo4j...
Loaded 3,203 nodes and 7,276 directed edges
Zone distribution: {'Wimbledon': 839, 'Raynes Park': 761, ...}
Neo4j connection closed after graph load.

Connecting to Postgres...

Placing vehicles and computing initial routes...
  V001: route 45 hops -> Colliers Wood
  V002: route 23 hops -> Wimbledon
  ...

Simulator running. Tick every ~2s. Press Ctrl+C to stop.
```

The hop count shows how long each vehicle's initial route is. Routes of 20-80 hops are typical for Merton. Occasionally a vehicle gets a very long route (100+ hops) if BFS finds a winding path through a dense part of the network, which is normal and produces interesting movement on the map.

## Verifying the Simulator

A cell at the end of the notebook connects directly to Lakebase and checks that position records are being written:

```
Total position records : 30

Latest position per vehicle:
  Vehicle    Lat        Lon       Zone           Recorded at
  V001    51.42145  -0.20482  Wimbledon      2026-08-24 ...
  V002    51.40891  -0.19134  Raynes Park    2026-08-24 ...
  ...
```

Thirty records after a few seconds of running means ten vehicles have each written three positions, which is exactly right for a 2-second tick interval.

## Gotchas

**sys.executable vs python.** Always use `sys.executable` in the `Popen` call. The bare `python` or `python3` command uses the system Python, which won't have the project's dependencies installed.

**Output buffering.** Without `-u` and `PYTHONUNBUFFERED=1`, the subprocess buffers its output and nothing appears in the notebook. Both flags are required.

**readline() deadlock.** If you loop on `proc.stdout.readline()` without a sentinel, the loop blocks indefinitely once the simulator is running (because the simulator is producing output, not closing stdout). Always break on a sentinel string.

**Stop flag file location.** The `simulator.stop` flag file must be created in the same directory from which the simulator is running. If the Streamlit analytics application and the simulator are started from different directories, the button won't work. Run everything from the project root.

**Fallback routes.** A "fallback route (short path)" message at startup means BFS couldn't find a 20-hop path from that vehicle's starting position. This happens most often in sparse zones or zones with many dead ends. The vehicle will still move; it just won't travel as far before needing a new destination.
