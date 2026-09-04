# Chapter 6: The Streamlit Apps

## Two Dashboards

The system has two Streamlit dashboards. The vehicle tracker (`app.py`, port 8501) shows vehicles moving on a live map with trail history, nearest-driver lookup and shortest path queries between zones. The analytics dashboard (`analytics_app.py`, port 8502) shows zone activity over time and the busiest road segments, updated every 30 seconds from a Lakehouse Delta table.

Run them alongside the simulator:

```bash
streamlit run app.py
streamlit run analytics_app.py --server.port 8502
```

Both read from `config.yaml` at startup, so switching cities means updating the config and restarting both apps.

## The Vehicle Tracker

### Auto-Refresh

The vehicle tracker refreshes every three seconds. `streamlit-autorefresh` handles this with a single call at the top of the script:

```python
st_autorefresh(interval=3000, key="map_refresh")
```

Every three seconds the entire script re-runs from top to bottom, re-querying Lakebase for the latest positions and redrawing the map. Streamlit's session state preserves the user's selected zones and the last shortest path across refreshes.

### Connections

Both the Neo4j driver and the Lakebase connection are cached with `@st.cache_resource`. This means they're created once when the application starts and reused on every refresh, rather than opening a new connection every three seconds:

```python
@st.cache_resource
def get_neo4j_driver():
    return GraphDatabase.driver(
        os.environ["NEO4J_URI"],
        auth=(os.environ["NEO4J_USERNAME"], os.environ["NEO4J_PASSWORD"])
    )
```

The application checks whether the connection is alive before using it and reconnects if needed. This handles the case where the Lakebase OAuth token expires mid-session and the next refresh detects the dropped connection and reconnects automatically.

### The Map

The map uses pydeck with a CARTO Voyager basemap -- no Mapbox token required:

```python
map_style = "https://basemaps.cartocdn.com/gl/voyager-gl-style/style.json"
```

Three pydeck layers sit on top of the basemap:

1. **IconLayer.** One car icon per vehicle at its current position. The icons are PNG files loaded from a CDN. SVG icons don't work with pydeck's IconLayer, as they render silently as nothing. The icon size is fixed regardless of zoom level, which means vehicles remain visible when zoomed out.
2. **PathLayer (trails).** A line showing each vehicle's last 20 positions. The trail fades naturally as old positions are replaced by new ones. The color matches the vehicle's home zone color, making it easy to see which zone a vehicle started from.
3. **PathLayer (shortest path).** A black line showing the road-network shortest path between two selected zones, when requested. This layer is only present when the user has clicked "Find shortest path". It persists across refreshes -- stored in `st.session_state` -- so the path stays on screen while vehicles continue moving around it.

### Stale Data Warning

If no position records have been written in the last 30 seconds, the application shows a warning banner:

```python
age = (datetime.now(timezone.utc) - latest_ts).total_seconds()
if age > STALE_SECONDS:
    st.warning(f"No position updates in {int(age)}s. Is the simulator running?")
```

The map still shows the last known positions -- useful for seeing where vehicles were when the simulator stopped. This behavior differs from the analytics dashboard, which calls `st.stop()` on stale data since there's nothing useful to show without fresh positions.

### Nearest Driver

The nearest-driver feature answers the question "which vehicle is currently closest to this zone?" using haversine distance from the zone center:

```python
def haversine(lat1, lon1, lat2, lon2):
    R = 6371000
    phi1, phi2 = radians(lat1), radians(lat2)
    dphi = radians(lat2 - lat1)
    dlambda = radians(lon2 - lon1)
    a = sin(dphi/2)**2 + cos(phi1)*cos(phi2)*sin(dlambda/2)**2
    return 2 * R * asin(sqrt(a))
```

The sidebar lets the user select a zone and the application queries the latest position of every vehicle from Lakebase, computes the haversine distance from each vehicle to the selected zone's center and returns the closest one. The zone centers are verified road-network coordinates loaded from config and they map to real intersections in the road graph.

### Shortest Path

The shortest path feature lets the user pick a source and destination zone and find the road-network path between them. Clicking "Find shortest path" triggers a two-step Aura query: first find the nearest intersection to each zone center, then run `shortestPath()` between them:

```python
result = session.run("""
    MATCH (s:Intersection {node_id: $start}),
          (e:Intersection {node_id: $end})
    MATCH path = shortestPath((s)-[:ROAD*..300]->(e))
    RETURN [n IN nodes(path) | [n.lat, n.lon]] AS coords,
           length(path) AS hops
""", start=start_id, end=end_id)
```

The result is a list of coordinate pairs that pydeck draws as a black line on the map. The path and its metadata (number of hops and approximate distance) persist in `st.session_state` so they stay visible across the 3-second refresh cycle.

A key implementation detail is that pydeck re-renders layers when their data changes, but it also re-renders when its `key` parameter changes. We set the key to include the selected zone names:

```python
key=f"map_{st.session_state.path_from}_{st.session_state.path_to}"
```

This forces a full re-render whenever the user changes the selected zones, which clears the old path immediately.

### Zone Activity Chart

The sidebar shows a bar chart of position updates per zone over the last 10 minutes. This uses the `current_zone` column in `vehicle_positions` and not the home zone from the `vehicles` table. The distinction matters: a vehicle that started in Wimbledon but is currently in Colliers Wood contributes to Colliers Wood's activity count, not Wimbledon's.

```sql
SELECT current_zone, COUNT(*) AS updates
FROM vehicle_positions
WHERE recorded_at >= NOW() - INTERVAL '10 minutes'
  AND current_zone IS NOT NULL
GROUP BY current_zone
ORDER BY updates DESC
```

## The Analytics Dashboard

The analytics dashboard connects to three systems:

1. Aura for road names in the cross-system join
2. Lakebase for live position data
3. Lakehouse for Delta table storage and SQL analytics

All three connections are from a local Jupyter notebook process using standard Python connectors.

### Incremental Sync

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

The first refresh loads the full hour of history; subsequent refreshes only load the new records since the last sync. This makes refreshes fast even when the Delta table contains tens of thousands of rows.

### Simulator Check

Before syncing, the dashboard checks whether the simulator is producing fresh data:

```python
def simulator_is_running(lb_conn):
    cursor.execute("""
        SELECT COUNT(*) FROM vehicle_positions
        WHERE recorded_at >= NOW() - INTERVAL '30 seconds'
    """)
    return cursor.fetchone()[0] > 0
```

If no positions have been written in the last 30 seconds, the dashboard shows a warning and calls `st.stop()`, halting the render. The auto-refresh continues firing, so the dashboard recovers automatically when the simulator starts again.

### Zone Activity Over Time

The first chart shows position update counts per zone per minute over the last hour:

```sql
SELECT current_zone AS zone,
       DATE_TRUNC('minute', recorded_at) AS minute,
       COUNT(*) AS updates
FROM vehicle_positions_delta
WHERE current_zone IS NOT NULL
GROUP BY current_zone, DATE_TRUNC('minute', recorded_at)
ORDER BY minute, zone
```

This query runs on Lakehouse against the Delta table, not against Lakebase directly. For a large dataset, Lakehouse columnar storage and parallel execution make this faster than running the equivalent query on Lakebase.

### Top Road Segments -- The Cross-System Join

The second chart is the most interesting query in the system. It answers "which named roads carry the most vehicle traffic?" To answer this requires data from two different databases.

Lakebase has the position records (lat, lon per vehicle per tick). Aura has the road names (what named road does each intersection belong to). Neither system alone can answer the question.

The join runs in Python using pandas. The dashboard loads intersection coordinates and road names from Aura once at startup (cached for an hour) and position coordinates from Lakehouse on each refresh. It rounds both sets of coordinates to three decimal places (~100m precision) and joins on the rounded values:

```python
pos_df["lat_r"]  = pos_df["lat"].round(3)
pos_df["lon_r"]  = pos_df["lon"].round(3)
road_df["lat_r"] = road_df["lat"].round(3)
road_df["lon_r"] = road_df["lon"].round(3)

joined = pos_df.merge(
    road_df[["road_name", "highway", "lat_r", "lon_r"]],
    on=["lat_r", "lon_r"],
    how="inner"
)
```

The result is a count of vehicle passes per named road -- Morden Road, London Road, and so on, colored by highway type. Primary roads tend to dominate because the BFS simulator routes vehicles along main roads when finding shortest paths.

### Simulator Control

The sidebar has two control buttons:

1. **Stop Simulator.** Writes `simulator.stop` to disk. The simulator checks for this file at the top of each tick, deletes it and exits cleanly via `KeyboardInterrupt`. This triggers the existing shutdown code, which resets all vehicle statuses to `idle` in Lakebase before the process exits.
2. **Reset Delta Table.** Clears the `last_position_id` counter in session state and marks the Delta table as needing recreation. On the next refresh, the dashboard drops the existing Delta table, recreates it and loads the full hour of history from scratch. Useful when you restart the simulator or switch cities.

### Production Path: Lakehouse Sync

In this demo, position data flows from Lakebase to Lakehouse via the analytics dashboard's incremental sync. In production, Databricks provides a native CDC feature called Lakebase Change Data Feed that replicates Lakebase tables into Unity Catalog Delta tables automatically, with no application-level sync code needed.

Lakehouse Sync requires a workspace admin to enable it from the Databricks workspace Previews page. It's currently in Public Preview and isn't available on free-tier Databricks accounts. The [developer template](https://developers.databricks.com/templates/lakebase-change-data-feed-autoscaling) walks through the setup.

## Gotchas

**use_container_width deprecated.** Streamlit deprecated `use_container_width` in favor of `width='stretch'` in recent versions. Use `st.plotly_chart(fig, width='stretch')` not `use_container_width=True`.

**pydeck IconLayer needs PNG not SVG.** SVG icons render silently as nothing in pydeck's IconLayer. Use a PNG URL. The car icon used in this demo comes from a CDN and is loaded fresh on each render.

**CARTO basemap, no Mapbox token.** The [CARTO Voyager basemap](https://basemaps.cartocdn.com/gl/voyager-gl-style/style.json) shows roads, labels and satellite imagery without requiring a Mapbox account or API key.

**@st.cache_resource and schema changes.** `@st.cache_resource` caches the database connection across the entire Streamlit session. If you drop and recreate the Lakebase tables (by re-running notebook 4), the cached connection still points at the old session. Restart Streamlit to pick up the new schema.

**Shortest path and pydeck key.** Without a unique key on the pydeck chart, changing the selected zones doesn't clear the old path from the map. Set the key to include the zone names: `key=f"map_{from_zone}_{to_zone}"`.

**Aggressive Aura health checks.** Calling `driver.verify_connectivity()` on every render creates a new connection to Aura on every 3-second refresh. Check instead whether the driver object is `None` and reconnect only when necessary.

**current_zone vs home zone.** The zone activity chart must read `current_zone` from `vehicle_positions`, not `zone` from the `vehicles` table. The home zone is static; current zone reflects where the vehicle actually is right now.
