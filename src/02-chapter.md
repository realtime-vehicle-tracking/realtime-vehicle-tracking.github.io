# Chapter 2: The road network

## What we need from the road network

The road network is the foundation of the entire system. Without it, we can't route vehicles, compute shortest paths between zones or tell a vehicle where to go next. Everything the simulator and the Streamlit application do depends on having an accurate, connected graph of the roads in a city.

We specifically need:

- Every road intersection as a node, with its GPS coordinates.
- Every drivable road segment as a directed edge, with its name, road type, speed limit and length.
- A spatial index on intersection coordinates to support location lookups, such as finding the nearest intersection to a GPS position.
- A connected graph, with no isolated subgraphs, so every vehicle can reach every other zone via some sequence of road segments.

OpenStreetMap gives us the raw data. OSMnx gives us the tools to download it, clean it and convert it to a graph we can load into Aura.

## The Aura graph model

The road network maps naturally to Aura's property graph model. Here's what we're storing:

```text
(:Intersection {node_id, lat, lon, street_count, location})
    -[:ROAD {osmid, u, v, name, highway, maxspeed, oneway, length_m}]->
(:Intersection)
```

Each `Intersection` node represents a road junction, which is a point where two or more roads meet. The `location` property stores an Aura `point()` object, which enables native spatial queries (nearest-neighbor lookup, distance calculation) without any additional libraries.

Each `ROAD` relationship represents a directed road segment between two intersections. A one-way street from A to B is stored as a single relationship A -> B. A two-way street is stored as two relationships: A -> B and B -> A. The `highway` property holds the OSM road classification (`primary`, `residential`, `motorway` and so on) and `maxspeed` holds the posted speed limit where OSM data are available. The `osmid` property is stored as a string.

## Downloading the road network

For Merton, we use OSMnx's `graph_from_place()` function, which geocodes the place name via Nominatim, finds the boundary polygon in OpenStreetMap and downloads all drivable road segments within it:

```python
G = ox.graph_from_place("London Borough of Merton, UK", network_type="drive")
```

The `network_type="drive"` filter keeps only roads that cars can use, excluding footpaths, cycleways and pedestrian areas. By default OSMnx also drops road segments that aren't part of the largest connected component, which helps keep the graph connected.

For alternate cities, such as San Francisco and Singapore, where neighborhood polygon boundaries are either unavailable or produce disconnected subgraphs, we use a different approach: download a regional OSM data file from Geofabrik and clip it to a bounding box using `pyrosm`. This is covered in notebook `01_prepare_osm.ipynb`, which you only need to run if you want to regenerate the clipped file or prepare your own city.

The city config file (`config.yaml`) tells `02_road_network.ipynb` which approach to use:

```yaml
city:
  osmnx_place: "London Borough of Merton, UK"  # Merton -- direct download
  # osm_file: "singapore-central.osm.pbf"      # Singapore -- local file
```

If `osm_file` is present, the notebook loads from the local clipped file. Otherwise it uses `graph_from_place()`. The notebook also accepts `osmnx_place` as a list of place names and composes the individual graphs with `nx.compose()`, but none of the example configs use this.

## Cleaning the data

OSMnx returns a `MultiDiGraph`, which is a directed graph where edges can have multiple attributes, some of which are lists rather than scalars. In the OSM data model, a single road segment can carry multiple OSM tag values when the underlying data are ambiguous or inconsistent.

Before loading into Aura, we need to clean three things:

1. **List-valued properties.** The `osmid`, `name`, `highway` and `maxspeed` fields can be Python lists rather than single values. We take the first element in each case:

```python
def flatten(val):
    if isinstance(val, list):
        return val[0]
    return val
```

2. **Speed limit strings.** OSM stores `maxspeed` as a string (`"50 mph"` or just `"50"`) rather than a number. We parse it to a number, stripping the unit suffix. Missing values become `None`, so the column is stored as floating point, which is why speed limits show as 20.0 rather than 20:

```python
def parse_maxspeed(val):
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return None
    val = flatten(val)
    try:
        return int(str(val).replace(" mph", "").strip())
    except (ValueError, AttributeError):
        return None
```

3. **NaN values.** Pandas uses `float('nan')` for missing values, but the Neo4j Python driver doesn't accept NaN. We convert all NaN values to `None` before loading:

```python
rows = df.where(pd.notnull(df), None).to_dict("records")
```

## Deduplicating edges

OSMnx assigns the same `osmid` to multiple edges in two cases:

1. Bidirectional roads, where the same OSM way appears as both A -> B and B -> A. These differ in `u` and `v`, so we keep both as separate directed relationships.
2. Segmented ways, where a single named road is split into multiple segments sharing one OSM ID.

Only the second case needs deduplicating. The composite key `osmid + u + v` (where `u` and `v` are the source and target node IDs) is usually unique, but not always, as some split ways produce multiple segments with the same `u`, `v` and `osmid` but different lengths. We deduplicate by keeping the longest segment for each `(osmid, u, v)` triple:

```python
edges_clean = (
    edges_clean
    .sort_values("length_m", ascending=False)
    .drop_duplicates(subset=["osmid", "u", "v"], keep="first")
)
```

This reduces Merton's 7,335 raw edges to 7,296 clean edges with duplicates removed.

## Loading into Aura

We load nodes and edges in separate passes, using `MERGE` to make the operation idempotent. Running the notebook twice won't create duplicate nodes.

**Nodes.** Each intersection becomes an Aura node, with a uniqueness constraint on `node_id` created beforehand:

```cypher
CREATE CONSTRAINT intersection_node_id IF NOT EXISTS
FOR (i:Intersection) REQUIRE i.node_id IS UNIQUE
```

The node load itself looks like this:

```cypher
UNWIND $rows AS row
MERGE (i:Intersection {node_id: row.node_id})
SET i.lat          = row.lat,
    i.lon          = row.lon,
    i.street_count = row.street_count,
    i.location     = point({latitude: row.lat, longitude: row.lon})
```

The `location` property stores a native Aura spatial point, which enables the `point.distance()` function used later for nearest-driver queries.

**Edges.** Each road segment becomes a `ROAD` relationship. The composite `MERGE` key uses `osmid`, `u` and `v` together to uniquely identify each directed edge:

```cypher
UNWIND $rows AS row
MATCH (a:Intersection {node_id: row.u})
MATCH (b:Intersection {node_id: row.v})
MERGE (a)-[r:ROAD {osmid: row.osmid, u: row.u, v: row.v}]->(b)
SET r.name     = row.name,
    r.highway  = row.highway,
    r.maxspeed = row.maxspeed,
    r.oneway   = row.oneway,
    r.length_m = row.length_m
```

We load in batches of 500 rows at a time, using a progress bar to track progress. For Merton this load is quite fast. For San Francisco and Singapore it takes longer.

## Clearing before reload

**Warning:** The reset deletes everything in the database, including data, constraints and indexes that don't belong to this book. Run it only against an instance you're happy to wipe, such as a fresh Aura Free instance created for this project.

When switching cities, we need to reset the database before loading a new one. The reset drops all constraints, drops all non-lookup indexes and then deletes every node and relationship in batches of 1,000. We skip the lookup indexes because Neo4j manages those itself. The reset runs before the constraint and index setup, so the notebook always starts from a clean slate:

```python
def clear_database(session):
    # Drop all constraints
    constraints = session.run("SHOW CONSTRAINTS YIELD name RETURN name").data()
    for c in constraints:
        session.run(f"DROP CONSTRAINT {c['name']} IF EXISTS")

    # Drop all non-lookup indexes
    indexes = session.run("""
        SHOW INDEXES YIELD name, type
        WHERE type <> 'LOOKUP'
        RETURN name
    """).data()
    for idx in indexes:
        session.run(f"DROP INDEX {idx['name']} IF EXISTS")

    # Delete all nodes and relationships in batches
    while True:
        result = session.run("""
            MATCH (n) WITH n LIMIT 1000
            DETACH DELETE n
            RETURN count(*) AS deleted
        """)
        if result.single()["deleted"] == 0:
            break

    print("Database cleared.")

with driver.session() as session:
    clear_database(session)
```

## The spatial index

The POINT INDEX on `location` is created before loading nodes:

```cypher
CREATE POINT INDEX intersection_location IF NOT EXISTS
FOR (i:Intersection) ON (i.location)
```

Creating it before the data load means the index is populated incrementally as nodes are inserted.

## Verifying the result

After loading, we verify the counts match expectations and inspect the longest named roads:

```
Intersections : 3,212
Roads         : 7,296

Longest named roads:
  Home Park Road                 residential  20.0       1013.41 m
  Home Park Road                 residential  20.0       1013.41 m
  Croydon Road                   primary      30.0       960.11 m
  Croydon Road                   primary      30.0       960.11 m
  Lynmouth Avenue                residential  20.0       828.92 m
```

The duplicate entries reflect different directed edges sharing the same display attributes. For example, two segments of a road traveling in opposite directions. The graph data in Aura is correct.

For Merton, we expect 3,212 intersections and 7,296 roads after deduplication. Home Park Road, at over a kilometer, is a residential street. Croydon Road is a primary road with a 30 mph limit. OpenStreetMap is edited continuously, so your counts may differ slightly from the ones shown here.

## Alternate cities

For San Francisco and Singapore, the road network is loaded from a pre-clipped OSM file rather than using the Overpass API. The `01_prepare_osm.ipynb` notebook handles downloading the regional file from Geofabrik and clipping it to the bounding box defined in the city config. You can regenerate these files at any time by running that notebook with the appropriate config.

The clipped files are included in the GitHub repo, so you won't need to run the prepare notebook.

San Francisco's graph is larger than Merton's because the area covered is larger and its road network is denser. Singapore's graph is of a broadly similar size. Both are clipped to a single bounding box, which should give one connected graph. The verification step in `01_prepare_osm.ipynb` reports the number of strongly connected components, so you can confirm this for yourself.

## Gotchas

**OSMnx version compatibility.** OSMnx and Shapely versions need to be compatible with each other. Older Shapely releases were built against an earlier major version of NumPy and can fail with a `TypeError` when OSMnx calls `union_all()` internally. Pin Shapely to the version in the notebook's install cell, which is known to work.

**List-valued edge properties.** OSMnx can return `osmid`, `name`, `highway` and `maxspeed` as Python lists rather than scalars for some edges. Always flatten before loading. The `flatten()` helper takes the first element of any list and returns non-list values unchanged.

**NaN vs None.** The Neo4j Python driver doesn't accept `float('nan')`. Call `df.where(pd.notnull(df), None)` before converting to records.

**Edge deduplication.** The composite key `osmid + u + v` is not always unique. Sort by length descending and drop duplicates before loading.

**Speed limits in alternate cities.** OSM stores speed limits as numbers, sometimes with a unit suffix, and the unit depends on the country. The UK and the US use mph, whereas most other countries, including Singapore, use km/h. The field name is the same, so the notebook stores the number as given and doesn't convert it.

**pbf format.** Geofabrik distributes OSM data in Protocol Buffer format (`.osm.pbf`). This is a binary format that OSMnx's `graph_from_xml()` function can't read directly, as it expects plain OSM XML. Use `pyrosm` to read `.pbf` files.

**nx.compose() produces disconnected subgraphs.** Composing multiple neighborhood polygon graphs with `nx.compose()` can produce isolated subgraphs if the constituent polygons don't share road edges at their boundaries. This causes `shortestPath()` to return no result between nodes in different subgraphs. Use `pyrosm` with a bounding box to get a single connected graph instead.

**Not all place names have OSM polygon boundaries.** `ox.graph_from_place()` requires Nominatim to return a polygon boundary for the place name. Neighborhood names in particular often return only a point. Test with `ox.geocode_to_gdf(place)` and check that `geom_type` is `Polygon` or `MultiPolygon` before using a place name in your config.

**nx.compose() node count is misleading.** `len(G.nodes)` on the first graph in a compose chain returns only that graph's node count. After composing multiple graphs, Aura receives the full union of all node IDs. The final Aura intersection count is correct; the intermediate `len(G.nodes)` is not.
