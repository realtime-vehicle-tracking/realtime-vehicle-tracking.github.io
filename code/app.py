import math
import os
import pandas as pd
import plotly.graph_objects as go
import psycopg2
import pydeck as pdk
import streamlit as st

from config_validator import load_config, ConfigError, zone_colors, zone_centers
from datetime import datetime, timezone
from neo4j import GraphDatabase
from neo4j.exceptions import ServiceUnavailable, SessionExpired
from streamlit_autorefresh import st_autorefresh

# ---------------------------------------------------------------------------
# Page config
# ---------------------------------------------------------------------------

try:
    _cfg = load_config("config.yaml")
except (FileNotFoundError, ConfigError) as e:
    raise SystemExit(f"Config error: {e}")

st.set_page_config(
    page_title = f"{_cfg['city']['name']} Vehicle Tracker",
    page_icon  = "🚗",
    layout     = "wide"
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

REFRESH_MS        = 3000
TRAIL_LENGTH      = 20    # recent positions per vehicle for trail
STALE_SECONDS     = 30    # warn if no position update in this many seconds
NEO4J_TIMEOUT     = 10    # seconds for Neo4j connection/transaction timeout

# PNG icon -- pydeck IconLayer requires raster PNG, not SVG
CAR_ICON_URL = "https://cdn-icons-png.flaticon.com/512/744/744465.png"

ZONE_COLORS = {**zone_colors(_cfg), "Unknown": [128, 128, 128]}

ZONE_CENTERS = zone_centers(_cfg)

ZONE_NAMES = list(ZONE_CENTERS.keys())

MAP_LAT  = _cfg["city"]["map_lat"]
MAP_LON  = _cfg["city"]["map_lon"]
MAP_ZOOM = _cfg["city"]["map_zoom"]

# ---------------------------------------------------------------------------
# Connection helpers
# ---------------------------------------------------------------------------

def _make_pg_conn():
    conn = psycopg2.connect(
        host     = os.environ["LAKEBASE_HOST"],
        user     = os.environ["LAKEBASE_USER"],
        password = os.environ["LAKEBASE_TOKEN"],
        dbname   = os.environ["LAKEBASE_DBNAME"],
        sslmode  = "require",
        port     = 5432
    )
    conn.autocommit = True
    return conn

def _make_neo4j_driver():
    return GraphDatabase.driver(
        os.environ["NEO4J_URI"],
        auth = (os.environ["NEO4J_USERNAME"], os.environ["NEO4J_PASSWORD"]),
        connection_timeout         = NEO4J_TIMEOUT,
        max_transaction_retry_time = NEO4J_TIMEOUT,
    )

@st.cache_resource
def get_pg_conn():
    try:
        return _make_pg_conn()
    except psycopg2.OperationalError as e:
        if "OAuth" in str(e) or "not authorized" in str(e).lower():
            st.error("Lakebase token has expired. Update LAKEBASE_TOKEN and restart the app.")
        else:
            st.error(f"Lakebase connection failed: {e}")
        return None
    except Exception as e:
        st.error(f"Lakebase connection failed: {e}")
        return None

@st.cache_resource
def get_neo4j_driver():
    try:
        return _make_neo4j_driver()
    except Exception as e:
        st.error(f"Neo4j connection failed: {e}")
        return None

def _pg_alive(conn):
    """Return True if the Lakebase connection is still open and usable."""
    if conn is None:
        return False
    if conn.closed != 0:
        return False
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT 1")
        cursor.close()
        return True
    except Exception:
        return False

def _neo4j_alive(driver):
    """Return True if the Neo4j driver can reach the server."""
    if driver is None:
        return False
    try:
        driver.verify_connectivity()
        return True
    except Exception:
        return False

def get_live_pg():
    """Return the cached Lakebase connection, reconnecting only on failure."""
    conn = get_pg_conn()
    if conn is None or conn.closed != 0:
        try:
            conn = _make_pg_conn()
        except psycopg2.OperationalError as e:
            if "OAuth" in str(e) or "not authorized" in str(e).lower():
                st.error("Lakebase token has expired. Update LAKEBASE_TOKEN and restart the app.")
            else:
                st.error(f"Lakebase reconnect failed: {e}")
            return None
        except Exception as e:
            st.error(f"Lakebase reconnect failed: {e}")
            return None
    return conn

def get_live_neo4j():
    """Return the cached Neo4j driver, reconnecting only on failure."""
    driver = get_neo4j_driver()
    if driver is None:
        try:
            driver = _make_neo4j_driver()
        except Exception as e:
            st.error(f"Neo4j reconnect failed: {e}")
            return None
    return driver

def pg_query(conn, func, *args, **kwargs):
    """Run a Lakebase query with error handling."""
    if conn is None:
        return None
    try:
        return func(conn, *args, **kwargs)
    except Exception as e:
        st.warning(f"Lakebase query failed: {e}. Refresh the page.")
        st.cache_resource.clear()
        return None

def neo4j_query(driver, func, *args, **kwargs):
    """Run a Neo4j query with error handling."""
    if driver is None:
        return None
    try:
        return func(driver, *args, **kwargs)
    except (ServiceUnavailable, SessionExpired) as e:
        st.warning(f"Neo4j connection lost: {e}. Refresh the page.")
        st.cache_resource.clear()
        return None
    except Exception as e:
        st.warning(f"Neo4j query failed: {e}")
        return None

# ---------------------------------------------------------------------------
# Data queries
# ---------------------------------------------------------------------------

def get_vehicle_positions(conn):
    cursor = conn.cursor()
    cursor.execute("""
        SELECT DISTINCT ON (vp.vehicle_id)
            vp.vehicle_id,
            v.driver_name,
            v.zone,
            vp.lat,
            vp.lon,
            vp.recorded_at
        FROM vehicle_positions vp
        JOIN vehicles v ON v.vehicle_id = vp.vehicle_id
        ORDER BY vp.vehicle_id, vp.recorded_at DESC
    """)
    rows = cursor.fetchall()
    cursor.close()
    return pd.DataFrame(rows, columns=["vehicle_id", "driver_name", "zone", "lat", "lon", "recorded_at"])

def get_vehicle_trails(conn):
    """Last TRAIL_LENGTH positions per vehicle for the path layer."""
    cursor = conn.cursor()
    cursor.execute("""
        SELECT vehicle_id, lat, lon
        FROM (
            SELECT
                vp.vehicle_id,
                vp.lat,
                vp.lon,
                vp.recorded_at,
                ROW_NUMBER() OVER (
                    PARTITION BY vp.vehicle_id
                    ORDER BY vp.recorded_at DESC
                ) AS rn
            FROM vehicle_positions vp
        ) ranked
        WHERE rn <= %s
        ORDER BY vehicle_id, recorded_at ASC
    """, (TRAIL_LENGTH,))
    rows = cursor.fetchall()
    cursor.close()
    df = pd.DataFrame(rows, columns=["vehicle_id", "lat", "lon"])
    trails = []
    for vid, group in df.groupby("vehicle_id"):
        trails.append({
            "vehicle_id": vid,
            "path":  [[row.lon, row.lat] for row in group.itertuples()],
            "color": ZONE_COLORS.get("Unknown") + [140],
        })
    return trails

def get_zone_demand(conn):
    cursor = conn.cursor()
    cursor.execute("""
        SELECT current_zone AS zone, COUNT(*) AS updates
        FROM vehicle_positions
        WHERE recorded_at >= NOW() - INTERVAL '10 minutes'
          AND current_zone IS NOT NULL
        GROUP BY current_zone
        ORDER BY updates DESC
    """)
    rows = cursor.fetchall()
    cursor.close()
    return pd.DataFrame(rows, columns=["zone", "updates"])

def get_latest_timestamp(conn):
    """Return the most recent recorded_at across all vehicles."""
    cursor = conn.cursor()
    cursor.execute("SELECT MAX(recorded_at) FROM vehicle_positions")
    result = cursor.fetchone()[0]
    cursor.close()
    return result

def get_nearby_zones(driver, zone_name):
    with driver.session() as session:
        result = session.run("""
            MATCH (z:Zone {name: $zone})-[:ADJACENT_TO*0..1]->(nearby:Zone)
            RETURN collect(DISTINCT nearby.name) AS zones
        """, zone=zone_name)
        return result.single()["zones"]

def get_shortest_path(driver, from_zone, to_zone):
    """Find shortest path between nearest intersections to two zone centres.
    Returns (coords, hops, dist_km) or (None, 0, 0.0) if no path found."""
    from_lat, from_lon = ZONE_CENTERS[from_zone]
    to_lat,   to_lon   = ZONE_CENTERS[to_zone]
    with driver.session() as session:
        # Find nearest start intersection
        r1 = session.run("""
            MATCH (i:Intersection)
            RETURN i.node_id AS node_id
            ORDER BY point.distance(
                i.location,
                point({latitude: $lat, longitude: $lon})
            ) ASC LIMIT 1
        """, lat=from_lat, lon=from_lon)
        start_id = r1.single()["node_id"]

        # Find nearest end intersection
        r2 = session.run("""
            MATCH (i:Intersection)
            RETURN i.node_id AS node_id
            ORDER BY point.distance(
                i.location,
                point({latitude: $lat, longitude: $lon})
            ) ASC LIMIT 1
        """, lat=to_lat, lon=to_lon)
        end_id = r2.single()["node_id"]

        # Find shortest path between them
        r3 = session.run("""
            MATCH (start:Intersection {node_id: $start_id})
            MATCH (end:Intersection   {node_id: $end_id})
            MATCH path = shortestPath((start)-[:ROAD*..300]->(end))
            RETURN [n IN nodes(path) | [n.lon, n.lat]] AS coords,
                   length(path) AS hops,
                   round(reduce(total = 0.0, r IN relationships(path) |
                       total + coalesce(r.length_m, 0.0)) / 1000.0, 2) AS dist_km
        """, start_id=start_id, end_id=end_id)
        record = r3.single()
        if record:
            return record["coords"], record["hops"], record["dist_km"]
        return None, 0, 0.0

def haversine(lat1, lon1, lat2, lon2):
    """Straight-line distance in km between two lat/lon points."""
    R = 6371
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat/2)**2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon/2)**2
    return R * 2 * math.asin(math.sqrt(a))

# ---------------------------------------------------------------------------
# Sidebar
# ---------------------------------------------------------------------------

st.sidebar.title(f"{_cfg['city']['name']} Vehicle Tracker")
st.sidebar.markdown(f"Live fleet operations dashboard for {_cfg['city']['name']}.")
st.sidebar.divider()

st.sidebar.subheader("Nearest Driver Query")
selected_zone = st.sidebar.selectbox(
    "Find a driver near:",
    ZONE_NAMES,
    key="nearest_zone"
)

st.sidebar.divider()

st.sidebar.subheader("Vehicle Trails")
show_trails = st.sidebar.toggle("Show trails", value=True)

st.sidebar.divider()

st.sidebar.subheader("Shortest Path")
from_zone  = st.sidebar.selectbox("From zone:", ZONE_NAMES, index=0, key="from_zone")
to_zone    = st.sidebar.selectbox("To zone:", ZONE_NAMES, index=len(ZONE_NAMES)-1, key="to_zone")
run_path   = st.sidebar.button("Find shortest path")
clear_path = st.sidebar.button("Clear path")

# ---------------------------------------------------------------------------
# Auto-refresh
# ---------------------------------------------------------------------------

st_autorefresh(interval=REFRESH_MS, key="map_refresh")

# ---------------------------------------------------------------------------
# Session state initialisation
# ---------------------------------------------------------------------------

if "path_coords" not in st.session_state:
    st.session_state.path_coords = None
if "path_hops" not in st.session_state:
    st.session_state.path_hops = 0
if "path_dist_km" not in st.session_state:
    st.session_state.path_dist_km = 0.0
if "path_from" not in st.session_state:
    st.session_state.path_from = None
if "path_to" not in st.session_state:
    st.session_state.path_to = None

# ---------------------------------------------------------------------------
# Load data
# ---------------------------------------------------------------------------

pg    = get_live_pg()
neo4j = get_live_neo4j()

positions = pg_query(pg, get_vehicle_positions)
if positions is None:
    positions = pd.DataFrame()

zone_demand = pg_query(pg, get_zone_demand)
if zone_demand is None:
    zone_demand = pd.DataFrame()

# ---------------------------------------------------------------------------
# Stale data warning
# ---------------------------------------------------------------------------

if not positions.empty:
    latest_ts = pg_query(pg, get_latest_timestamp)
    if latest_ts is not None:
        now = datetime.now(timezone.utc)
        if latest_ts.tzinfo is None:
            latest_ts = latest_ts.replace(tzinfo=timezone.utc)
        age = (now - latest_ts).total_seconds()
        if age > STALE_SECONDS:
            st.warning(f"No position updates in {int(age)}s -- the simulator may have stopped.")

# ---------------------------------------------------------------------------
# Main layout
# ---------------------------------------------------------------------------

st.markdown(f"### 🚗 {_cfg['city']['name']} Vehicle Tracker")

col_map, col_right = st.columns([3, 1])

# ---------------------------------------------------------------------------
# Map
# ---------------------------------------------------------------------------

with col_map:
    layers = []

    # Vehicle trails
    if show_trails and not positions.empty:
        trails = pg_query(pg, get_vehicle_trails)
        if trails is None:
            trails = []

        # Colour trails by vehicle home zone
        zone_lookup = dict(zip(positions["vehicle_id"], positions["zone"]))
        for t in trails:
            t["color"] = ZONE_COLORS.get(
                zone_lookup.get(t["vehicle_id"], "Unknown"),
                ZONE_COLORS["Unknown"]
            ) + [140]

        if trails:
            trail_layer = pdk.Layer(
                "PathLayer",
                data             = trails,
                get_path         = "path",
                get_color        = "color",
                get_width        = 2,
                width_min_pixels = 1,
                pickable         = False,
            )
            layers.append(trail_layer)

    # Shortest path -- persisted in session state across auto-refreshes
    if clear_path:
        st.session_state.path_coords = None
        st.session_state.path_hops     = 0
        st.session_state.path_dist_km  = 0.0
        st.session_state.path_from   = None
        st.session_state.path_to     = None

    if run_path and from_zone != to_zone:
        result = neo4j_query(neo4j, get_shortest_path, from_zone, to_zone)
        path_coords, hops, dist_km = result if result else (None, 0, 0.0)
        if path_coords:
            st.session_state.path_coords   = path_coords
            st.session_state.path_hops     = hops
            st.session_state.path_dist_km  = dist_km
            st.session_state.path_from     = from_zone
            st.session_state.path_to       = to_zone
        else:
            st.sidebar.warning(f"No path found between {from_zone} and {to_zone}.")

    path_coords = st.session_state.path_coords
    hops        = st.session_state.path_hops
    dist_km     = st.session_state.path_dist_km

    if path_coords:
        path_layer = pdk.Layer(
            "PathLayer",
            data             = [{"path": path_coords, "color": [0, 0, 0, 240]}],
            get_path         = "path",
            get_color        = "color",
            get_width        = 8,
            width_min_pixels = 4,
            pickable         = False,
            cap_rounded      = True,
            joint_rounded    = True,
        )
        layers.append(path_layer)

    # Vehicle icons
    if not positions.empty:
        icon_data = {
            "url":     CAR_ICON_URL,
            "width":   128,
            "height":  128,
            "anchorY": 128,
        }
        positions["icon"]    = [icon_data] * len(positions)
        positions["tooltip"] = positions.apply(
            lambda r: f"{r['vehicle_id']} — {r['driver_name']} ({r['zone']})", axis=1
        )

        icon_layer = pdk.Layer(
            "IconLayer",
            data         = positions,
            get_icon     = "icon",
            get_position = ["lon", "lat"],
            get_size     = 4,
            size_scale   = 8,
            pickable     = True,
        )
        layers.append(icon_layer)

    view = pdk.ViewState(
        latitude  = MAP_LAT,
        longitude = MAP_LON,
        zoom      = MAP_ZOOM,
        pitch     = 0,
        bearing   = 0,
    )

    st.pydeck_chart(
        pdk.Deck(
            layers             = layers,
            initial_view_state = view,
            tooltip            = {"text": "{tooltip}"},
            map_style          = "https://basemaps.cartocdn.com/gl/voyager-gl-style/style.json",
        ),
        height = 500,
        key    = f"map_{st.session_state.path_from}_{st.session_state.path_to}",
    )

    if positions.empty:
        st.info("No position data yet. Start the simulator.")
    else:
        caption = f"Showing {len(positions)} vehicles. Refreshes every {REFRESH_MS // 1000}s."
        if st.session_state.path_coords:
            caption += f" Route: {st.session_state.path_from} → {st.session_state.path_to} ({st.session_state.path_dist_km} km, {st.session_state.path_hops} intersections)."
        st.caption(caption)

# ---------------------------------------------------------------------------
# Right column
# ---------------------------------------------------------------------------

with col_right:
    st.markdown("**Nearest Driver**")

    nearby_zones = neo4j_query(neo4j, get_nearby_zones, selected_zone)
    if nearby_zones is None:
        nearby_zones = []

    if not positions.empty and nearby_zones:
        nearby = positions[positions["zone"].isin(nearby_zones)]
        if nearby.empty:
            st.warning(f"No vehicles near {selected_zone}.")
        else:
            zone_lat, zone_lon = ZONE_CENTERS.get(selected_zone, (MAP_LAT, MAP_LON))
            nearby = nearby.copy()
            nearby["dist_km"] = nearby.apply(
                lambda r: haversine(r["lat"], r["lon"], zone_lat, zone_lon), axis=1
            )
            best = nearby.loc[nearby["dist_km"].idxmin()]
            st.markdown(f"""
| | |
|---|---|
| **Vehicle** | {best['vehicle_id']} |
| **Driver** | {best['driver_name']} |
| **Zone** | {best['zone']} |
""")
        st.caption(f"Zones checked: {', '.join(sorted(nearby_zones))}")

    st.divider()
    st.markdown("**Zone Activity**")
    st.caption("Position updates (last 10 min)")

    if zone_demand.empty:
        st.info("Start the simulator to see zone activity.")
    else:
        colors = [
            f"rgb({ZONE_COLORS.get(z, ZONE_COLORS['Unknown'])[0]},"
            f"{ZONE_COLORS.get(z, ZONE_COLORS['Unknown'])[1]},"
            f"{ZONE_COLORS.get(z, ZONE_COLORS['Unknown'])[2]})"
            for z in zone_demand["zone"]
        ]
        fig = go.Figure(go.Bar(
            x            = zone_demand["zone"],
            y            = zone_demand["updates"],
            marker_color = colors,
            text         = zone_demand["updates"],
            textposition = "auto",
        ))
        fig.update_layout(
            margin     = dict(l=0, r=0, t=4, b=0),
            height     = 280,
            showlegend = False,
            xaxis      = dict(title=None, tickangle=-45),
            yaxis      = dict(title="Updates"),
            font       = dict(size=11),
        )
        st.plotly_chart(fig, width="stretch")

    # Shortest path result box
    if st.session_state.path_coords:
        st.divider()
        st.markdown("**Shortest Path**")
        dist_km = st.session_state.path_dist_km
        hops    = st.session_state.path_hops
        avg_speed_kmh = 30
        est_mins = round((dist_km / avg_speed_kmh) * 60, 1)
        st.markdown(f"""
| | |
|---|---|
| **From** | {st.session_state.path_from} |
| **To** | {st.session_state.path_to} |
| **Distance** | {dist_km} km |
| **Intersections** | {hops} |
| **Est. drive time** | {est_mins} min |
""")
        st.caption("Est. at 30 km/h average.")
