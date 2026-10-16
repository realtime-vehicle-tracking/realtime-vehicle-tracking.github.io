# Chapter 6: The Streamlit apps

## Two dashboards

The system has two Streamlit dashboards. The vehicle tracker (`app.py`, port 8501) shows vehicles moving on a live map with trail history, nearest-driver lookup and shortest path queries between zones. The analytics dashboard (`analytics_app.py`, port 8502) shows zone activity over time and the busiest road segments, updated every 30 seconds from a Lakehouse Delta table. A notebook version of the analytics, `06_analytics.ipynb`, is described at the end of the chapter.

Run them alongside the simulator:

```bash
streamlit run app.py
streamlit run analytics_app.py --server.port 8502
```

Both read from `config.yaml` at startup, so switching cities means updating the config and restarting both apps.

## The vehicle tracker

### Auto-refresh

The vehicle tracker refreshes every three seconds. `streamlit-autorefresh` handles this with a single call:

```python
REFRESH_MS = 3000
st_autorefresh(interval=REFRESH_MS, key="map_refresh")
```

Every three seconds the entire script re-runs from top to bottom, re-querying Lakebase for the latest positions and redrawing the map. Streamlit's session state preserves the user's selections and the last shortest path across refreshes.

### Connections

Both the Neo4j driver and the Lakebase connection are cached with `@st.cache_resource`. This means they're created once when the application starts and reused on every refresh, rather than opening a new connection every three seconds:

```python
def _make_neo4j_driver():
    return GraphDatabase.driver(
        os.environ["NEO4J_URI"],
        auth = (os.environ["NEO4J_USERNAME"], os.environ["NEO4J_PASSWORD"]),
        connection_timeout         = NEO4J_TIMEOUT,
        max_transaction_retry_time = NEO4J_TIMEOUT,
    )

@st.cache_resource
def get_neo4j_driver():
    try:
        return _make_neo4j_driver()
    except Exception as e:
        st.error(f"Neo4j connection failed: {e}")
        return None
```

The application handles failures in three ways:

1. If the cached Lakebase connection has closed, `get_live_pg()` opens a new one.
2. If a query fails, the `pg_query()` and `neo4j_query()` wrappers show a warning and clear the cached connections, so the next refresh builds fresh ones.
3. The Neo4j driver is rebuilt only when it's missing. We don't health-check Aura on every refresh (see the Gotchas).

Reconnecting can't rescue an expired Lakebase token. The app reads `LAKEBASE_TOKEN` from the environment, so once the token has expired a new connection is refused and the app shows "Lakebase token has expired. Update LAKEBASE_TOKEN and restart the app." Generate a fresh token, update the environment variable and restart Streamlit.

### The map

The map uses pydeck with a CARTO Voyager basemap -- no Mapbox token required:

```python
map_style = "https://basemaps.cartocdn.com/gl/voyager-gl-style/style.json"
```

Three pydeck layers sit on top of the basemap:

1. **IconLayer.** One car icon per vehicle at its current position. The icons are PNG files loaded from a CDN. SVG icons don't work with pydeck's IconLayer, as they render silently as nothing. The icon size is fixed regardless of zoom level, which means vehicles remain visible when zoomed out.
2. **PathLayer (trails).** A line showing each vehicle's last 20 positions. The trail follows the vehicle as new positions arrive and the oldest ones drop off. The color matches the vehicle's home zone color, making it easy to see which zone a vehicle belongs to. A sidebar toggle switches the trails on and off.
3. **PathLayer (shortest path).** A black line showing the road-network shortest path between two selected zones, when requested. This layer is only present after the user has clicked "Find shortest path". It persists across refreshes -- stored in `st.session_state` -- so the path stays on screen while vehicles continue moving around it. The "Clear path" button removes it.

### Stale data warning

If no position records have been written in the last 30 seconds, the application shows a warning banner:

```python
age = (now - latest_ts).total_seconds()
if age > STALE_SECONDS:
    st.warning(f"No position updates in {int(age)}s -- the simulator may have stopped.")
```

The map still shows the last known positions -- useful for seeing where vehicles were when the simulator stopped. This behavior differs from the analytics dashboard, which calls `st.stop()` on stale data since there's nothing useful to show without fresh positions.

### Nearest driver

The nearest-driver feature answers the question "which vehicle is closest to this zone?" The user picks a zone in the sidebar and the answer appears in the panel on the right of the map. It works in three steps.

First, the application asks Aura for the selected zone and the zones adjacent to it:

```cypher
MATCH (z:Zone {name: $zone})-[:ADJACENT_TO*0..1]->(nearby:Zone)
RETURN collect(DISTINCT nearby.name) AS zones
```

Second, it keeps only the vehicles that are currently in one of those zones, using the `current_zone` of each vehicle's latest position. Third, it computes the haversine distance from each remaining vehicle's latest position to the selected zone's center and picks the closest one:

```python
def haversine(lat1, lon1, lat2, lon2):
    """Straight-line distance in km between two lat/lon points."""
    R = 6371
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat/2)**2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon/2)**2
    return R * 2 * math.asin(math.sqrt(a))
```

The panel shows the vehicle ID, the driver and the current zone, with a caption listing the zones that were checked. The zone centers come from the config. They're chosen to sit close to real intersections in the road graph.

### Shortest path

The shortest path feature lets the user pick a source and destination zone and find the road-network path between them. Clicking "Find shortest path" triggers a series of Aura queries: first find the nearest intersection to each zone center, then run `shortestPath()` between them:

```python
r3 = session.run("""
    MATCH (start:Intersection {node_id: $start_id})
    MATCH (end:Intersection   {node_id: $end_id})
    MATCH path = shortestPath((start)-[:ROAD*..300]->(end))
    RETURN [n IN nodes(path) | [n.lon, n.lat]] AS coords,
           length(path) AS hops,
           round(reduce(total = 0.0, r IN relationships(path) |
               total + coalesce(r.length_m, 0.0)) / 1000.0, 2) AS dist_km
""", start_id=start_id, end_id=end_id)
```

The result is a list of coordinate pairs that pydeck draws as a black line on the map, together with the number of hops and the total length in kilometers, summed from the `length_m` property on each `ROAD` relationship. The path and its metadata persist in `st.session_state` so they stay visible across the 3-second refresh cycle. A result box in the right-hand panel shows the distance, the hop count and an estimated drive time that assumes an average speed of 30 km/h.

A key implementation detail is that pydeck re-renders layers when their data changes, but it also re-renders when its `key` parameter changes. We set the key to include the zones of the path that's currently stored:

```python
key=f"map_{st.session_state.path_from}_{st.session_state.path_to}"
```

The key changes whenever a new path is stored or the path is cleared, which forces a full re-render and removes the old path from the map straight away.

### Zone activity chart

The right-hand panel also shows a bar chart of position updates per zone over the last 10 minutes. This uses the `current_zone` column in `vehicle_positions` and not the home zone from the `vehicles` table. The distinction matters: a vehicle that started in Wimbledon but is currently in Colliers Wood contributes to Colliers Wood's activity count, not Wimbledon's.

```sql
SELECT current_zone AS zone, COUNT(*) AS updates
FROM vehicle_positions
WHERE recorded_at >= NOW() - INTERVAL '10 minutes'
  AND current_zone IS NOT NULL
GROUP BY current_zone
ORDER BY updates DESC
```

## The analytics dashboard

The analytics dashboard connects to three systems:

1. Aura for road names in the cross-system join.
2. Lakebase for live position data.
3. Lakehouse for Delta table storage and SQL analytics.

All three connections are made from the local Python process that runs the Streamlit app, using standard Python connectors.

### Incremental sync

On each 30-second refresh, the dashboard reads new position records from Lakebase and appends them to a Delta table in Lakehouse. It tracks the last `position_id` it synced in `st.session_state` and only reads records with a higher ID:

```sql
SELECT vp.position_id, vp.vehicle_id, v.zone AS home_zone,
       vp.current_zone, vp.lat, vp.lon, vp.recorded_at
FROM vehicle_positions vp
JOIN vehicles v ON v.vehicle_id = vp.vehicle_id
WHERE vp.position_id > %s
  AND vp.recorded_at >= NOW() - INTERVAL '60 minutes'
ORDER BY vp.position_id
```

The first refresh of a session loads up to the last hour of history; subsequent refreshes only load the new records since the last sync. Both charts then read only the last 60 minutes from the Delta table, so the dashboard always shows a rolling hour.

### Simulator check

Before syncing, the dashboard checks whether the simulator is producing fresh data:

```python
def simulator_is_running(lb_conn):
    cursor = lb_conn.cursor()
    cursor.execute("""
        SELECT COUNT(*) FROM vehicle_positions
        WHERE recorded_at >= NOW() - INTERVAL '30 seconds'
    """)
    count = cursor.fetchone()[0]
    cursor.close()
    return count > 0
```

If no positions have been written in the last 30 seconds, the dashboard shows a warning and calls `st.stop()`, halting the render. The auto-refresh continues firing, so the dashboard recovers automatically when the simulator starts again.

### Zone activity over time

The first chart shows position update counts per zone per minute over the last hour:

```sql
SELECT current_zone AS zone,
       DATE_TRUNC('minute', recorded_at) AS minute,
       COUNT(*) AS updates
FROM vehicle_positions_delta
WHERE current_zone IS NOT NULL
  AND recorded_at >= current_timestamp() - INTERVAL 60 MINUTES
GROUP BY current_zone, DATE_TRUNC('minute', recorded_at)
ORDER BY minute, zone
```

This query runs on Lakehouse against the Delta table, not against Lakebase directly. Aggregating over a long history is the kind of work Lakehouse is built for, which is why the history lives there and not in the operational database.

### Top road segments -- the cross-system join

The second chart is the most interesting query in the system. It answers "which named roads carry the most vehicle traffic?" To answer this requires data from two different databases.

Lakebase has the position records (lat, lon per vehicle per tick). Aura has the road names (what named road starts at each intersection). Neither system alone can answer the question.

The join runs in Python using pandas. The dashboard loads the named road segments from Aura once (cached for an hour) and the last hour of position coordinates from Lakehouse on each refresh. The simulator writes the exact coordinates of an intersection at every tick, so each position can be matched to the intersection it sits on. We round both sets of coordinates to six decimal places to guard against floating-point noise, remove duplicate road rows and join on the rounded values:

```python
pos_df["lat_r"] = pos_df["lat"].round(6)
pos_df["lon_r"] = pos_df["lon"].round(6)

roads = road_df[["road_name", "highway", "lat", "lon"]].copy()
roads["lat_r"] = roads["lat"].round(6)
roads["lon_r"] = roads["lon"].round(6)
roads = roads[["road_name", "highway", "lat_r", "lon_r"]].drop_duplicates()

joined = pos_df.merge(roads, on=["lat_r", "lon_r"], how="inner")
```

The result is a count of vehicle passes per named road, colored by highway type. A pass is one position record at an intersection where that road starts. An intersection where several named roads meet counts toward each of them, so the counts show relative traffic and shouldn't be read as an exact number of vehicles. In the notebook run, 4,110 positions produced 7,749 position-road pairs and London Road and Morden Road led the chart. The exact ranking changes from run to run, because the routes are random.

### Simulator control

The sidebar has two control buttons:

1. **Stop Simulator.** Writes `simulator.stop` to disk. The simulator checks for this file at the top of each tick, deletes it and exits cleanly via `KeyboardInterrupt`. This triggers the existing shutdown code, which resets all vehicle statuses to `idle` in Lakebase before the process exits.
2. **Reset Delta Table.** Clears the `last_position_id` counter in session state and marks the Delta table as needing recreation. On the next refresh, the dashboard drops the existing Delta table, recreates it and loads up to the last hour of history from scratch. Useful when you restart the simulator or switch cities.

### The analytics notebook

`06_analytics.ipynb` runs the same kind of analysis as a one-time snapshot. It loads the last hour of positions from Lakebase into a Delta table in one go, draws four charts -- zone activity over time, current vehicle distribution by zone, position volume per minute and the top road segments -- and then drops the Delta table and the schema it created. The volume chart is a quick way to check the simulator's throughput. In our run it peaked at 270 updates in a minute, a little under the 300 you'd expect from ten vehicles writing every two seconds, because each tick also takes time to write the rows.

The notebook and `analytics_app.py` use the same Delta table, so don't run them at the same time.

### Production path: Lakehouse sync

In this demo, position data flows from Lakebase to Lakehouse via the analytics dashboard's incremental sync. In production, Databricks provides a native CDC feature called Lakebase Change Data Feed that replicates Lakebase tables into Unity Catalog Delta tables automatically, with no application-level sync code needed.

The feature requires a workspace admin to enable it from the Databricks workspace Previews page and it's currently in Public Preview. Free Edition accounts need extra setup, because the destination must be a catalog whose managed storage is an external location. The [Databricks documentation](https://docs.databricks.com/aws/en/oltp/projects/lakebase-cdf) and the [developer template](https://developers.databricks.com/templates/lakebase-change-data-feed-autoscaling) walk through the setup.

## Gotchas

**use_container_width deprecated.** Streamlit deprecated `use_container_width` in favor of `width='stretch'` in recent versions. Use `st.plotly_chart(fig, width='stretch')` not `use_container_width=True`.

**pydeck IconLayer needs PNG not SVG.** SVG icons render silently as nothing in pydeck's IconLayer. Use a PNG URL. The car icon used in this demo is loaded from a CDN.

**CARTO basemap, no Mapbox token.** The [CARTO Voyager basemap](https://basemaps.cartocdn.com/gl/voyager-gl-style/style.json) shows roads and labels without requiring a Mapbox account or API key.

**Cached connections and restarts.** `@st.cache_resource` keeps the database connections and the Aura driver for the life of the Streamlit process. After you re-run the notebooks or switch cities, restart Streamlit so that both apps start from a clean state.

**Shortest path and pydeck key.** Without a unique key on the pydeck chart, storing or clearing a path doesn't reliably redraw the map. Build the key from the stored path's zones: `key=f"map_{st.session_state.path_from}_{st.session_state.path_to}"`.

**Aggressive Aura health checks.** Calling `driver.verify_connectivity()` on every render creates a new connection to Aura on every 3-second refresh. Check instead whether the driver object is `None` and reconnect only when necessary.

**current_zone vs home zone.** The zone activity chart must read `current_zone` from `vehicle_positions`, not `zone` from the `vehicles` table. The home zone is static; current zone reflects where the vehicle actually is right now. The nearest-driver lookup follows the same rule and uses `current_zone`.

**One browser tab.** Each browser session starts with a fresh session state, so its first sync drops and recreates the Delta table. Keep a single tab open on the analytics dashboard, because a second tab (or a page reload) resets the table and both tabs then append the same new records to the table.

**Expired token.** Both apps read `LAKEBASE_TOKEN` from the environment. Once the token has expired, update the variable and restart the app.
