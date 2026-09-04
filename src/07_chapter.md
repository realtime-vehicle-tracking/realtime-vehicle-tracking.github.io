# Chapter 7: Bringing It All Together

## The Full System

With all six chapters complete, the system looks like this:

- **Neo4j Aura.** Holds the road network with thousands of intersections and thousands of road segments for Merton, loaded once from OpenStreetMap and unchanged for the life of the demo. It answers graph questions: shortest path between zones, zone adjacency, nearest intersection to a GPS coordinate.

- **Databricks Lakebase.** Holds the live operational data with vehicle positions written every two seconds by the simulator, vehicle statuses and trip records. It answers OLTP questions: where is vehicle V003 right now, which vehicles are currently idle, how many positions have been written in the last 10 minutes.

- **Databricks Lakehouse.** Holds the analytical history with position data synced from Lakebase into a Delta table, aggregated by zone and road segment. It answers analytical questions: which zones have been busiest in the last hour, which roads carry the most vehicle traffic.

None of these systems knows about the others. Aura knows nothing about Lakebase positions. Lakebase knows nothing about road names. Lakehouse knows nothing about zone topology. The intelligence sits in the application layer -- the simulator, the Streamlit applications and the analytics notebook -- which orchestrates queries across all three and combines the results.

## Running the System

The order of operations matters. Work through the notebooks in sequence:

1. `00_prepare_osm.ipynb` -- For new cities only or if you wish to rerun the data generation for San Francisco or Singapore. Skip for Merton.
2. `02_road_network.ipynb` -- Loads the road network into Aura
3. `03_zones.ipynb` -- Assigns intersections to zones and builds the adjacency graph
4. `04_lakebase.ipynb` -- Creates the Lakebase tables and seeds the vehicles
5. `05_simulator.ipynb` -- Starts the simulator in the background
6. `06_analytics.ipynb` -- Optional, for running four analytics queries interactively
7. In a terminal: `streamlit run app.py`
8. In a second terminal: `streamlit run analytics_app.py --server.port 8502`

**Switching cities.** Copy `config_sf.yaml` or `config_sg.yaml` to `config.yaml` and re-run notebooks 2 through 5. Notebook 2 clears the existing graph before loading the new city. Both Streamlit applications pick up the new city automatically on restart.

## Adding a New City

The YAML config system makes it straightforward to adapt the project to any city covered by OpenStreetMap. The process for adding a new city is:

**Choose an area.** Pick a part of a city that's large enough to have interesting routing (at least a few thousand intersections) but small enough to fit within Neo4j Aura's free-tier limits.

**Test the place name.** For cities where a single administrative boundary works with OSMnx, test it first:

```python
import osmnx as ox
gdf = ox.geocode_to_gdf("Your Place Name, Country")
print(gdf.geometry.iloc[0].geom_type)  # should be Polygon or MultiPolygon
```

If Nominatim returns a polygon, use `osmnx_place` as a single string in your config. If it returns a point or fails, use the Geofabrik approach.

**For the Geofabrik approach.** Download the regional `.osm.pbf` file from [Geofabrik](https://download.geofabrik.de), add the four `osm_*` fields to your config and run `00_prepare_osm.ipynb` to clip the file to your bounding box. Check that the clipped graph is fully connected (one strongly connected component) before proceeding.

**Define zones.** Choose five zone names, define bounding boxes that tile your area without overlapping and set center coordinates within each box. Describe them in the config comments as "logical simplified zones" rather than official boundaries.

**Check adjacency.** Review the zone topology against a map. A simple connected chain is the safest choice, as it avoids questions about which zones are "really" adjacent. Have a domain expert check the adjacency if possible.

**Run the pipeline.** Check the intersection counts per zone. If any zone has very few intersections (under 50), its bounding box may be misaligned or covering an area with few drivable roads. Adjust the box.

## What Each System Does That the Others Can't

It's worth noting why three systems are better than one for this use case.

**Why not just Aura?** Aura is excellent for graph queries and spatial lookups. But it's not designed for high-frequency OLTP writes. Inserting 300 rows per minute, continuously, with low latency and strong consistency guarantees, is what Postgres is built for. Lakebase gives us a production-grade Postgres instance with no infrastructure to manage.

**Why not just Lakebase?** Lakebase can store the road network as a table of edges and you can find shortest paths with recursive CTEs. But the queries are verbose, slow for deep traversals and don't scale to multi-hop zone reachability queries. A graph database handles these naturally.

**Why not just Lakehouse?** Lakehouse is excellent for large-scale analytics and historical queries. But it's not designed for low-latency row reads or high-frequency inserts. Reading the latest position of 10 vehicles from a Delta table on every 3-second refresh would be much slower and more expensive than reading from Lakebase.

The three-system architecture isn't complexity for its own sake. Each system does what it's genuinely good at and the results are combined at the application layer.

## Going Further

The system as built is a working demo. Here are some directions for making it more realistic.

**Speed-aware movement.** Vehicles currently move one intersection per tick regardless of road speed or distance. Loading `maxspeed` and `length_m` from the ROAD relationships would allow the simulator to calculate the time each edge takes to traverse and advance multiple intersections per tick on fast roads. A vehicle on the A3 (50 mph) would visibly move faster than one on a residential street (20 mph).

**Trip lifecycle.** The `trips` table is in the schema but not used. Adding a full dispatch cycle -- idle -> dispatched -> carrying passenger -> idle -- would make the system much more realistic. Each trip would have a pickup and dropoff zone, a vehicle assignment and timestamps for each state transition.

**Lakehouse Sync.** In the demo, position data moves from Lakebase to Lakehouse via the analytics dashboard's incremental sync. In production, Databricks's Lakebase Change Data Feed replicates Lakebase tables into Unity Catalog Delta tables automatically via CDC. This requires enabling the feature from the workspace Previews page and is available on paid Databricks accounts. See the [developer template](https://developers.databricks.com/templates/lakebase-change-data-feed-autoscaling).

**Actual neighborhood boundaries.** The zone bounding boxes are logical approximations. For Merton, ONS and Ordnance Survey provide ward and postcode boundary polygons. For Singapore, OneMap provides URA planning area polygons. For San Francisco, the city publishes official neighborhood boundary GeoJSON. Replacing the bounding boxes with real polygon boundaries would make zone assignment geographically accurate.

**H3 indexing.** Uber's H3 library provides a hexagonal grid system that covers the globe at multiple resolutions. Indexing intersections by H3 cell would make the nearest-driver query more efficient at scale and is the approach used in production ride-hailing systems.

**Kafka integration.** Replacing the simulator's direct Lakebase writes with a Kafka producer and adding a consumer that writes to Lakebase, would make the architecture more production-ready. It would also decouple the simulator from Lakebase, as the simulator could run even when Lakebase is temporarily unavailable, with positions queued in Kafka until connectivity is restored.
