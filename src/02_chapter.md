# Chapter 2: The Road Network

## What We Need From the Road Network

The road network is the foundation of the entire system. Without it, we can't route vehicles, compute shortest paths between zones or tell a vehicle where to go next. Everything the simulator and the Streamlit application do depends on having an accurate, connected graph of the roads in a city.

We specifically need:

- Every road intersection as a node, with its GPS coordinates
- Every drivable road segment as a directed edge, with its name, road type, speed limit and length
- A spatial index on intersection coordinates so we can quickly find the nearest intersection to any GPS position
- A connected graph, with no isolated subgraphs, so every vehicle can reach every other zone via some sequence of road segments

OpenStreetMap gives us the raw data. OSMnx gives us the tools to download it, clean it and convert it to a graph we can load into Neo4j.

## The Neo4j Graph Model

The road network maps naturally to Neo4j's property graph model. Here's what we're storing:

```text
(:Intersection {node_id, lat, lon, street_count, location})
    -[:ROAD {osmid, name, highway, maxspeed, oneway, length_m}]->
(:Intersection)
```

Each `Intersection` node represents a road junction, which is a point where two or more roads meet. The `location` property stores a Neo4j `point()` object, which enables native spatial queries (nearest-neighbor lookup, distance calculation) without any additional libraries.

Each `ROAD` relationship represents a directed road segment between two intersections. A one-way street from A to B is stored as a single relationship A -> B. A two-way street is stored as two relationships: A -> B and B -> A. The `highway` property holds the OSM road classification (`primary`, `residential`, `motorway` and so on) and `maxspeed` holds the posted speed limit where OSM data are available.

## Downloading the Road Network

For Merton, we use OSMnx's `graph_from_place()` function, which geocodes the place name via Nominatim, finds the boundary polygon in OpenStreetMap and downloads all drivable road segments within it:

```python
G = ox.graph_from_place("London Borough of Merton, UK", network_type="drive")
```

The `network_type="drive"` filter keeps only roads that cars can use, excluding footpaths, cycleways and pedestrian areas.

For alternate cities, such as San Francisco and Singapore, where neighborhood polygon boundaries are either unavailable or produce disconnected subgraphs, we use a different approach: download a regional OSM data file from Geofabrik and clip it to a bounding box using `pyrosm`. This is covered in notebook `00_prepare_osm`, which runs once before the road network notebook to prepare the clipped file.

The city config file (`config.yaml`) tells notebook 2 which approach to use:

```yaml
city:
  osmnx_place: "London Borough of Merton, UK"  # Merton -- direct download
  # osm_file: "singapore-central.osm.pbf"      # Singapore -- local file
```

If `osm_file` is present, the notebook loads from the local clipped file. Otherwise it uses `graph_from_place()`.

## Cleaning the Data

OSMnx returns a `MultiDiGraph`, which is a directed graph where edges can have multiple attributes, some of which are lists rather than scalars. In the OSM data model, a single road segment can carry multiple OSM tag values when the underlying data are ambiguous or inconsistent.

Before loading into Neo4j, we need to clean three things:

1. **List-valued properties.** The `osmid`, `name`, `highway` and `maxspeed` fields can be Python lists rather than single values. We take the first element in each case:

```python
def flatten(val):
    if isinstance(val, list):
        return val[0]
    return val
```

2. **Speed limit strings.** OSM stores `maxspeed` as a string (`"50 mph"` or just `"50"`) rather than a number. We parse it to an integer, stripping the unit suffix:

```python
def parse_maxspeed(val):
    try:
        return int(str(val).replace(" mph", "").strip())
    except (ValueError, AttributeError):
        return None
```

3. **NaN values.** Pandas uses `float('nan')` for missing values, but the Neo4j Python driver doesn't accept NaN. We convert all NaN values to `None` before loading:

```python
rows = df.where(pd.notnull(df), None).to_dict("records")
```

## Deduplicating Edges

OSMnx assigns the same `osmid` to multiple edges in two cases:

1. Bidirectional roads, where the same OSM way appears as both A -> B and B -> A
2. Segmented ways, where a single named road is split into multiple segments sharing one OSM ID

The composite key `osmid + u + v` (where `u` and `v` are the source and target node IDs) is usually unique, but not always, as some split ways produce multiple segments with the same `u`, `v` and `osmid` but different lengths. We deduplicate by keeping the longest segment for each `(osmid, u, v)` triple:

```python
edges_clean = (
    edges_clean
    .sort_values("length_m", ascending=False)
    .drop_duplicates(subset=["osmid", "u", "v"], keep="first")
)
```

This reduces Merton's 7,317 raw edges to 7,276 clean edges with 41 duplicates removed.

## Loading Into Neo4j

We load nodes and edges in separate passes, using `MERGE` to make the operation idempotent. Running the notebook twice won't create duplicate nodes.

**Nodes.** Each intersection becomes a Neo4j node with a uniqueness constraint on `node_id`:

```cypher
UNWIND $rows AS row
MERGE (i:Intersection {node_id: row.node_id})
SET i.lat          = row.lat,
    i.lon          = row.lon,
    i.street_count = row.street_count,
    i.location     = point({latitude: row.lat, longitude: row.lon})
```

The `location` property stores a native Neo4j spatial point, which enables the `point.distance()` function used later for nearest-driver queries.

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

## Clearing Before Reload

When switching cities, we need to clear the existing graph before loading a new one. The clear step runs before the constraint and index setup:

```cypher
MATCH (n)
CALL (n) { DETACH DELETE n } IN TRANSACTIONS OF 1000 ROWS
```

## The Spatial Index

The POINT INDEX on `location` is created before loading nodes:

```cypher
CREATE POINT INDEX intersection_location IF NOT EXISTS
FOR (i:Intersection) ON (i.location)
```

Creating it before the data load means the index is populated incrementally as nodes are inserted, which is faster than building it from scratch after loading.

## Verifying the Result

After loading, we verify the counts match expectations and inspect the longest named roads:

```
Intersections : 3,203
Roads         : 7,276

Longest named roads:
  Home Park Road    residential    20 mph    1013 m
  Croydon Road      primary        30 mph     982 m
```

For Merton, we expect 3,203 intersections and 7,276 roads after deduplication. Home Park Road, at over a kilometre, is a residential street. Croydon Road, at 30 mph, is a local connector across parts of the borough.

## Alternate Cities

For San Francisco and Singapore, the road network is loaded from a pre-clipped OSM file rather than using the Overpass API. The `00_prepare_osm` notebook handles downloading the regional file from Geofabrik and clipping it to the bounding box defined in the city config. You can regenerate these files at any time by running that notebook with the appropriate config.

The clipped files are included in the GitHub repo, so you won't need to run the prepare notebook.

San Francisco's graph is larger than Merton's because the area covered is larger and SF's road network is denser. Singapore's graph is similar in size to Merton. Both are fully connected, with a single strongly connected component, which means every vehicle can reach every other zone via directed road segments.

## Gotchas

**OSMnx version compatibility.** OSMnx 2.1.1 requires Shapely 2.1.x. Earlier Shapely versions (2.0.x) were built against NumPy 1.x and fail with `TypeError` when OSMnx calls `union_all()` internally. The fix is to pin Shapely to 2.1.2 or later.

**List-valued edge properties.** OSMnx can return `osmid`, `name`, `highway` and `maxspeed` as Python lists rather than scalars for some edges. Always flatten before loading. The `flatten()` helper takes the first element of any list and returns non-list values unchanged.

**NaN vs None.** The Neo4j Python driver doesn't accept `float('nan')`. Call `df.where(pd.notnull(df), None)` before converting to records.

**Edge deduplication.** The composite key `osmid + u + v` is not always unique. Sort by length descending and drop duplicates before loading.

**Cypher 25 syntax.** Use `CALL (r) { DELETE r } IN TRANSACTIONS` not the older `CALL { WITH r DELETE r }` form, which is deprecated and produces warnings.

**Speed limits in alternate cities.** OSM stores speed limits as numbers without units. For UK cities the values are in mph. For other countries they're in km/h, but the field name is the same.

**pbf format.** Geofabrik distributes OSM data in Protocol Buffer format (`.osm.pbf`). This is a binary format that OSMnx's `graph_from_xml()` function can't read directly, as it expects plain OSM XML. Use `pyrosm` to read `.pbf` files.

**pyrosm `--no-deps`.** Install pyrosm with `--no-deps` to suppress protobuf version conflict warnings from other packages in the environment.

**nx.compose() produces disconnected subgraphs.** Composing multiple neighborhood polygon graphs with `nx.compose()` can produce isolated subgraphs if the constituent polygons don't share road edges at their boundaries. This causes `shortestPath()` to return no result between nodes in different subgraphs. Use `pyrosm` with a bounding box to get a single connected graph instead.

**Not all place names have OSM polygon boundaries.** `ox.graph_from_place()` requires Nominatim to return a polygon boundary for the place name. Neighborhood names in particular often return only a point. Test with `ox.geocode_to_gdf(place)` and check that `geom_type` is `Polygon` or `MultiPolygon` before using a place name in your config.

**nx.compose() node count is misleading.** `len(G.nodes)` on the first graph in a compose chain returns only that graph's node count. After composing multiple graphs, Neo4j receives the full union of all node IDs. The final Neo4j intersection count is correct; the intermediate `len(G.nodes)` is not.
