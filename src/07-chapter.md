# Chapter 7: Bringing it all together

## The full system

With all six chapters complete, the system looks like this:

- **Neo4j Aura.** Holds the road network for Merton -- 3,212 intersections and 7,296 road segments, loaded once from OpenStreetMap and unchanged for the life of the demo. It answers graph questions: shortest path between zones, zone adjacency, nearest intersection to a GPS coordinate.

- **Databricks Lakebase.** Holds the live operational data with vehicle positions written every two seconds by the simulator and the vehicle statuses, plus a `trips` table that's ready for trip records but isn't used in the demo. It answers OLTP questions: where is vehicle V003 right now, which vehicles are currently idle, how many positions have been written in the last 10 minutes.

- **Databricks Lakehouse.** Holds the analytical history with position data synced from Lakebase into a Delta table, aggregated by zone and road segment. It answers analytical questions: which zones have been busiest in the last hour, which roads carry the most vehicle traffic.

None of these systems knows about the others. Aura knows nothing about Lakebase positions. Lakebase knows nothing about road names. Lakehouse knows nothing about zone topology. The intelligence sits in the application layer -- the simulator, the Streamlit applications and the analytics notebook -- which orchestrates queries across all three and combines the results.

## Running the system

The order of operations matters. Work through the notebooks in sequence:

1. `01_prepare_osm.ipynb` -- For new cities only or if you wish to rerun the data generation for San Francisco or Singapore. Skip for Merton.
2. `02_road_network.ipynb` -- Loads the road network into Aura.
3. `03_zones.ipynb` -- Assigns intersections to zones and builds the adjacency graph.
4. `04_lakebase.ipynb` -- Creates the Lakebase tables and seeds the vehicles.
5. `05_simulator.ipynb` -- Starts the simulator in the background. Keep the notebook kernel running for as long as the simulator runs.
6. `06_analytics.ipynb` -- Optional. A one-time snapshot of four analytics. Don't run it while `analytics_app.py` is running, because both use the same Delta table.
7. In a terminal, from the project root: `streamlit run app.py`.
8. In a second terminal, from the project root: `streamlit run analytics_app.py --server.port 8502`.

Running everything from the project root matters because the analytics dashboard's stop button and the simulator find each other through a flag file in the working directory.

**Tokens.** The Lakebase OAuth token lasts an hour. After that, generate a fresh token, update `LAKEBASE_TOKEN` and restart the simulator and both Streamlit applications.

**Switching cities.** First stop the simulator and both Streamlit applications, because an old simulator would keep writing into tables that are about to be rebuilt. Then copy `config_sf.yaml` or `config_sg.yaml` to `config.yaml` and re-run notebooks 2 through 5. Notebook 2 clears the entire Aura database before loading the new city. Restart both Streamlit applications so that they pick up the new city.

## Adding a new city

The YAML config system makes it straightforward to adapt the project to any city covered by OpenStreetMap. The process for adding a new city is:

**Choose an area.** Pick a part of a city that's large enough to have interesting routing (at least a few thousand intersections) but small enough to fit within Neo4j Aura's free-tier limits.

**Test the place name.** For cities where a single administrative boundary works with OSMnx, test it first:

```python
import osmnx as ox
gdf = ox.geocode_to_gdf("Your Place Name, Country")
print(gdf.geometry.iloc[0].geom_type)  # should be Polygon or MultiPolygon
```

If Nominatim returns a polygon, use `osmnx_place` as a single string in your config. If it returns a point or fails, use the Geofabrik approach.

**For the Geofabrik approach.** Download the regional `.osm.pbf` file from [Geofabrik](https://download.geofabrik.de), add the four `osm_*` fields to your config and run `01_prepare_osm.ipynb` to clip the file to your bounding box. Check that the clipped graph is fully connected (one strongly connected component) before proceeding.

**Define zones.** Choose a handful of zone names (the example configs use five), define bounding boxes that tile your area without overlapping and set a center coordinate for each zone. A center is a reference point on the road network. The apps use it to find the nearest intersection and to measure distances to the zone, so it should sit on or near a real road. It's usually inside the zone's box, but it doesn't have to be. Describe the zones in the config comments as "logical simplified zones" rather than official boundaries.

**Assign vehicles.** Add the list of vehicles, each with an ID, a driver name and a home zone. The example configs have two vehicles in each zone.

**Name the schema.** Give the city its own `schema_name`, which names the Lakehouse schema, so that its analytics tables stay separate from other cities.

**Check adjacency.** Review the zone topology against a map. Every zone must be reachable from every other, so a simple connected chain is the safest choice, as it also avoids questions about which zones are "really" adjacent. Have a domain expert check the adjacency if possible.

**Run the pipeline.** Check the intersection counts per zone. If any zone has very few intersections (for example, under 50), its bounding box may be misaligned or covering an area with few drivable roads. Adjust the box.

## What each system does that the others can't

It's worth noting why three systems are better than one for this use case.

**Why not just Aura?** Neo4j Aura is purpose-built for graph traversals and spatial queries -- exactly what we use it for. Road networks, zone adjacency and shortest path computation are natural fits for a graph database. The native spatial point on each intersection node supports nearest-neighbor lookups that are fast at this scale and, with a distance filter and the point index, can stay fast as the graph grows. Also, a shortest path query that would require a recursive CTE in SQL is a single function call in Cypher.

**Why not just Lakebase?** Lakebase is purpose-built for operational data -- high-frequency writes, transactional consistency and low-latency row reads. It handles the live vehicle positions exactly as we'd want a production Postgres database to. Foreign key constraints, BIGSERIAL auto-increment and standard SQL all work exactly as expected.

**Why not just Lakehouse?** Lakehouse is excellent for large-scale analytics and historical queries. It's designed for aggregating millions of position records by zone, identifying the busiest road segments over time and running SQL across large Delta tables. The columnar storage and parallel execution make these queries fast at scale, and the Delta table format adds reliability through ACID transactions and time-travel capability.

The three-system architecture isn't complexity for its own sake. Each system does what it's genuinely good at and the results are combined at the application layer.

## Going further

The system as built is a working demo. Here are some directions for making it more realistic.

**Speed-aware movement.** Vehicles currently move one intersection per tick regardless of road speed or distance. Loading `maxspeed` and `length_m` from the ROAD relationships would allow the simulator to calculate the time each edge takes to traverse and advance multiple intersections per tick on fast roads. A vehicle on a major road such as the A3 would visibly move faster than one on a residential street. Remember that the unit of `maxspeed` depends on the country.

**Trip lifecycle.** The `trips` table is in the schema but not used. Adding a full dispatch cycle -- a trip is requested, assigned to the nearest idle vehicle, started and completed -- would make the system much more realistic. Each trip already has columns for a pickup and dropoff zone, a vehicle assignment and timestamps for each state transition.

**Lakehouse Sync.** In the demo, position data moves from Lakebase to Lakehouse via the analytics dashboard's incremental sync. In production, Databricks's Lakebase Change Data Feed replicates Lakebase tables into Unity Catalog Delta tables automatically via CDC. It's currently in Public Preview and a workspace admin enables it from the Previews page. Free Edition accounts need extra setup, because the destination catalog must use external storage. See the [Databricks documentation](https://docs.databricks.com/aws/en/oltp/projects/lakebase-cdf) and the [developer template](https://developers.databricks.com/templates/lakebase-change-data-feed-autoscaling).

**Actual neighborhood boundaries.** The zone bounding boxes are logical approximations. For Merton, the Office for National Statistics publishes ward boundary polygons. For San Francisco, the city publishes official neighborhood boundary GeoJSON. For Singapore, OneMap provides URA planning area polygons. Replacing the bounding boxes with real polygon boundaries would make zone assignment geographically accurate.

**H3 indexing.** Uber's H3 library provides a hexagonal grid system that covers the globe at multiple resolutions. Indexing intersections by H3 cell would make the nearest-intersection and nearest-driver lookups more efficient at scale and is used in production ride-hailing systems.

**Kafka integration.** Replacing the simulator's direct Lakebase writes with a Kafka producer and adding a consumer that writes to Lakebase would make the architecture more production-ready. It would also decouple the simulator from Lakebase. Today the simulator pauses when it loses the Lakebase connection, whereas with Kafka it could keep running, with positions queued until connectivity is restored.
