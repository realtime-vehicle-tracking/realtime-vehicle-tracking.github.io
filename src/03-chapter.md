# Chapter 3: Zones and graph queries

## Why zones?

The road network we loaded in Chapter 2 is a graph of thousands of intersections connected by thousands of directed road segments. That's a detailed and accurate model of Merton's road network, but it's not enough for a vehicle tracking system on its own.

A dispatcher needs to think in terms of areas, not individual intersections. "Send the nearest vehicle in Colliers Wood to the pickup in Mitcham" is a meaningful instruction. "Send the vehicle at node 65304090 to node 229523558" is not. We need to group the road network into named zones that the simulator and the dashboard can both reason about.

We also need to know which zones border which other zones. The simulator uses this to decide where to send a vehicle next, with a 70% chance of choosing a destination in the vehicle's home zone or an adjacent zone and a 30% chance of choosing one anywhere in the borough. Without an explicit zone adjacency graph, the simulator would have to compute this at runtime from intersection coordinates, which is slower and more error-prone.

## The zone model

We define five logical zones for Merton, each as a simple bounding box:

| Zone | Latitude range | Longitude range |
|---|---|---|
| Wimbledon | 51.4100 -- 51.4411 | -0.2495 -- -0.1900 |
| Raynes Park | 51.4100 -- 51.4411 | -0.1900 -- -0.1245 |
| Colliers Wood | 51.3950 -- 51.4100 | -0.2495 -- -0.1800 |
| Mitcham | 51.3950 -- 51.4100 | -0.1800 -- -0.1245 |
| Morden | 51.3809 -- 51.3950 | -0.2495 -- -0.1245 |

These are logical simplified zones for the demo, not official neighborhood boundary polygons. Merton's Local Plan recognizes a richer set of neighborhoods including South Wimbledon, Wimbledon Park, Merton Park and Lower Morden, none of which are represented here. The bounding boxes are a deliberate simplification that keeps the configuration transparent and easy to adjust.

Intersections that fall outside all five boxes are assigned to an `Unknown` zone automatically. In practice this is just one intersection for Merton.

All zone definitions live in `config.yaml`. Switching to a different city means changing the config file; the notebook code itself is city-agnostic.

Each zone also has a center point in the config. The centers are reference points on the road network. The apps use them to find the nearest intersection and to measure distances to a zone, and they don't have to sit inside the bounding box. For Merton, the Raynes Park, Colliers Wood and Morden centers fall outside their own boxes, because the boxes are simplified and the centers sit close to the real locations of those neighborhoods.

## Adding zones to Aura

We extend the graph model from Chapter 2 with two new elements:

```text
(:Zone {name, lat_min, lat_max, lon_min, lon_max})
(:Intersection)-[:IN_ZONE]->(:Zone)
(:Zone)-[:ADJACENT_TO]->(:Zone)
```

Each `Zone` node stores its bounding box coordinates. The `IN_ZONE` relationship connects each intersection to its zone. The `ADJACENT_TO` relationship connects neighboring zones and is stored bidirectionally. We define the pairs by hand in the config so that they form a single connected chain, which means every zone can be reached from every other zone. The chain doesn't list every pair of boxes that touch.

### Assigning intersections to zones

The zone assignment query runs entirely in Cypher. For each intersection, it finds the zone whose bounding box contains the intersection's coordinates and creates an `IN_ZONE` relationship:

```cypher
MATCH (i:Intersection)
MATCH (z:Zone)
WHERE i.lat >= z.lat_min AND i.lat < z.lat_max
  AND i.lon >= z.lon_min AND i.lon < z.lon_max
MERGE (i)-[:IN_ZONE]->(z)
RETURN count(*) AS assigned
```

Any intersection not matched by this query goes to the Unknown zone. We create that zone first and then assign the leftover intersections to it:

```cypher
MERGE (z:Zone {name: "Unknown"})
```

```cypher
MATCH (i:Intersection)
WHERE NOT (i)-[:IN_ZONE]->()
MATCH (z:Zone {name: "Unknown"})
MERGE (i)-[:IN_ZONE]->(z)
```

For Merton, this assigns nearly all intersections to named zones and 1 to Unknown.

### Zone adjacency

The adjacency relationships define which zones neighbor which others. For Merton, the five zones form a single chain, running Raynes Park -> Wimbledon -> Colliers Wood -> Mitcham -> Morden:

```
Raynes Park -- Wimbledon -- Colliers Wood -- Mitcham -- Morden
```

We store adjacency bidirectionally and a single pair `[Wimbledon, Colliers Wood]` in the config generates two `ADJACENT_TO` relationships in Aura:

```cypher
UNWIND $pairs AS pair
MATCH (a:Zone {name: pair.a})
MATCH (b:Zone {name: pair.b})
MERGE (a)-[:ADJACENT_TO]->(b)
MERGE (b)-[:ADJACENT_TO]->(a)
```

## Verifying the zone graph

After loading, we verify both the intersection counts per zone and the adjacency structure:

```
Intersections per zone:
  Wimbledon       839
  Raynes Park     761
  Mitcham         727
  Colliers Wood   544
  Morden          340
  Unknown         1
  Total           3,212

Zone adjacency:
  Colliers Wood   -> Mitcham, Wimbledon
  Mitcham         -> Colliers Wood, Morden
  Morden          -> Mitcham
  Raynes Park     -> Wimbledon
  Wimbledon       -> Colliers Wood, Raynes Park
```

Wimbledon has the most intersections. Box size isn't a reliable guide here, since Morden's box is nearly as large as Wimbledon's but holds the fewest. Even so, Morden has more than enough intersections for the simulator to route vehicles meaningfully.

## Graph queries on the zone topology

With zones and adjacency loaded, we can ask questions that would be challenging in a relational database.

**Which zones border Wimbledon?**

```cypher
MATCH (z:Zone {name: 'Wimbledon'})-[:ADJACENT_TO]->(neighbor:Zone)
RETURN neighbor.name AS zone
ORDER BY zone
```

Result: Colliers Wood, Raynes Park.

**Which zones can a vehicle reach within two hops from Morden?**

```cypher
MATCH (z:Zone {name: 'Morden'})-[:ADJACENT_TO*1..2]->(reachable:Zone)
WHERE reachable.name <> 'Morden'
RETURN DISTINCT reachable.name AS zone
ORDER BY zone
```

Result: Colliers Wood, Mitcham. Wimbledon and Raynes Park are three and four hops away. The adjacency pairs form one connected chain, so a vehicle starting anywhere can still reach every zone.

**How many complex intersections are in Wimbledon?**

```cypher
MATCH (i:Intersection)-[:IN_ZONE]->(z:Zone {name: 'Wimbledon'})
WHERE i.street_count >= 3
RETURN count(i) AS complex_intersections
```

Result: 635. About 76% of Wimbledon's 839 intersections connect three or more roads, which reflects the dense residential street grid in this part of London.

## Shortest path between zones

The most powerful query the zone graph enables is shortest path between two named zones. The Streamlit application uses this for the "Find shortest path" feature -- the user selects two zones, the app finds the nearest intersection to each zone's center point and then asks Aura for the shortest directed path between them along `ROAD` relationships:

```cypher
MATCH (start:Intersection {node_id: $start_id})
MATCH (end:Intersection   {node_id: $end_id})
MATCH path = shortestPath((start)-[:ROAD*..300]->(end))
RETURN [n IN nodes(path) | [n.lon, n.lat]] AS coords,
       length(path) AS hops,
       round(reduce(total = 0.0, r IN relationships(path) |
           total + coalesce(r.length_m, 0.0)) / 1000.0, 2) AS dist_km
```

The `*..300` hop limit prevents the query from running indefinitely on disconnected graphs. For Merton's well-connected network, shortest paths are typically tens of hops. The result is a sequence of coordinates, in longitude and latitude order, that the Streamlit application draws as a line on the map, together with the hop count and the total length in kilometers.

Finding the nearest intersection to a zone center uses the `location` point stored on each intersection:

```cypher
MATCH (i:Intersection)
RETURN i.node_id AS node_id
ORDER BY point.distance(
    i.location,
    point({latitude: $lat, longitude: $lon})
) ASC
LIMIT 1
```

This runs in milliseconds for a network of this size. As written, the query ranks every intersection by distance, so for a much larger network we'd add a bounding box or distance filter that the POINT INDEX from Chapter 2 can use.

## Why store zones in Aura?

We could store zone assignments in Postgres alongside the vehicle positions. The simulator could look up each vehicle's zone by querying the Lakebase `vehicles` table. Why put zones in Aura instead?

Two reasons. First, the adjacency queries are natural graph traversals. "Zones reachable within two hops" is a recursive query that SQL handles with CTEs, which can be verbose and slow for deeper traversals. In Cypher it's a single line with a variable-length path pattern.

Second, the shortest path query crosses zone boundaries. The zone context, which intersections belong to which zone, enriches the result without requiring a join to a separate database. Everything the path query needs is in the same graph.

## Zone-awareness in the simulator

The simulator uses zone membership in two ways. At startup, it reads zone membership counts from Aura to confirm the graph loaded correctly:

```
Zone distribution: {'Wimbledon': 839, 'Raynes Park': 761, ... }
```

At runtime, it determines the `current_zone` of each vehicle by checking which zone the vehicle's current intersection belongs to. This is achieved with an in-memory dictionary built from Aura data at startup, not a live Aura query per tick, so it adds no latency to the simulation loop.

The zone adjacency graph from the config drives the routing bias: 70% of the time a vehicle's next destination is chosen from its home zone or an adjacent zone and 30% of the time from anywhere in the borough. This creates realistic clustering and vehicles tend to stay in their home area but occasionally make longer cross-borough runs.

## Gotchas

**Bounding box zones are simplified.** The zone bounding boxes are logical demo zones, not official neighborhood boundaries. For Merton, the Local Plan recognizes a richer set of neighborhoods. For Singapore, the URA planning areas have irregular polygon boundaries. For San Francisco, the city publishes official neighborhood boundary maps. The YAML zones are a deliberate simplification for the demo.

**Switching cities requires a full Aura wipe.** ROAD relationships, Intersection nodes and Zone nodes all need to be cleared before loading a new city. Clearing only ROAD relationships leaves orphaned Intersection nodes from the previous city, which then get mixed into the zone assignment for the new city.
