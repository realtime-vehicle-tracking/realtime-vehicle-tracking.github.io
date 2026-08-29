import os
import time

import pandas as pd
import plotly.express as px
import psycopg2
import streamlit as st

from config_validator import load_config, ConfigError, zone_colors
from databricks import sql as dbsql
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
    page_title = f"{_cfg['city']['name']} Analytics Dashboard",
    page_icon  = "📊",
    layout     = "wide"
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

REFRESH_MS   = 30_000   # analytics refresh every 30 seconds
HISTORY_MINS = 60       # look-back window for zone activity chart

ZONE_COLORS = {**{
    name: f"#{r:02x}{g:02x}{b:02x}"
    for name, (r, g, b) in zone_colors(_cfg).items()
}, "Unknown": "#808080"}

CATALOG_NAME = "workspace"
SCHEMA_NAME  = _cfg["city"]["schema_name"]
TABLE_NAME   = "vehicle_positions_delta"

# ---------------------------------------------------------------------------
# Connections
# ---------------------------------------------------------------------------

@st.cache_resource
def get_lb_conn():
    try:
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
    except Exception as e:
        st.error(f"Lakebase connection failed: {e}")
        return None

@st.cache_resource
def get_db_conn():
    try:
        conn = dbsql.connect(
            server_hostname = os.environ["DATABRICKS_SERVER_HOSTNAME"],
            http_path       = os.environ["DATABRICKS_HTTP_PATH"],
            access_token    = os.environ["DATABRICKS_TOKEN"]
        )
        return conn
    except Exception as e:
        st.error(f"Databricks connection failed: {e}")
        return None

@st.cache_resource
def get_neo4j_driver():
    try:
        return GraphDatabase.driver(
            os.environ["NEO4J_URI"],
            auth = (os.environ["NEO4J_USERNAME"], os.environ["NEO4J_PASSWORD"])
        )
    except Exception as e:
        st.error(f"Neo4j connection failed: {e}")
        return None

# ---------------------------------------------------------------------------
# Neo4j road data -- loaded once at startup and cached
# ---------------------------------------------------------------------------

@st.cache_data(ttl=3600)
def load_road_data():
    """Load named road intersection coordinates from Neo4j. Cached for 1 hour."""
    driver = get_neo4j_driver()
    if driver is None:
        return pd.DataFrame()
    try:
        with driver.session() as session:
            result = session.run("""
                MATCH (a:Intersection)-[r:ROAD]->(b:Intersection)
                WHERE r.name IS NOT NULL
                RETURN r.name AS road_name,
                       r.highway AS highway,
                       a.lat AS lat,
                       a.lon AS lon
            """)
            df = pd.DataFrame([{
                "road_name": rec["road_name"],
                "highway":   rec["highway"],
                "lat_r":     round(rec["lat"], 3),
                "lon_r":     round(rec["lon"], 3),
            } for rec in result])
        return df
    except (ServiceUnavailable, SessionExpired) as e:
        st.warning(f"Neo4j query failed: {e}")
        st.cache_data.clear()
        return pd.DataFrame()

# ---------------------------------------------------------------------------
# Lakebase → Databricks transfer
# ---------------------------------------------------------------------------

# Check if simulator is producing fresh data
def simulator_is_running(lb_conn):
    """Return True if a position record was written in the last 30 seconds."""
    if lb_conn is None:
        return False
    try:
        cursor = lb_conn.cursor()
        cursor.execute("""
            SELECT COUNT(*) FROM vehicle_positions
            WHERE recorded_at >= NOW() - INTERVAL '30 seconds'
        """)
        count = cursor.fetchone()[0]
        cursor.close()
        return count > 0
    except Exception:
        return False

def refresh_delta_table(lb_conn, db_conn):
    """Incrementally sync new positions from Lakebase to Databricks Delta."""
    if lb_conn is None or db_conn is None:
        return 0

    last_id = st.session_state.last_position_id

    lb_cursor = lb_conn.cursor()
    lb_cursor.execute(f"""
        SELECT
            vp.position_id,
            vp.vehicle_id,
            v.zone          AS home_zone,
            vp.current_zone,
            vp.lat,
            vp.lon,
            vp.recorded_at
        FROM vehicle_positions vp
        JOIN vehicles v ON v.vehicle_id = vp.vehicle_id
        WHERE vp.position_id > %s
          AND vp.recorded_at >= NOW() - INTERVAL '{HISTORY_MINS} minutes'
        ORDER BY vp.position_id
    """, (last_id,))
    rows = lb_cursor.fetchall()
    lb_cursor.close()

    if not rows:
        return 0

    df = pd.DataFrame(rows, columns=[
        "position_id", "vehicle_id", "home_zone", "current_zone",
        "lat", "lon", "recorded_at"
    ])

    db_cursor = db_conn.cursor()
    db_cursor.execute(f"USE {CATALOG_NAME}.{SCHEMA_NAME}")

    # Create table on first run
    if not st.session_state.table_exists:
        db_cursor.execute(f"DROP TABLE IF EXISTS {TABLE_NAME}")
        db_cursor.execute(f"""
            CREATE TABLE IF NOT EXISTS {TABLE_NAME} (
                position_id  BIGINT,
                vehicle_id   STRING,
                home_zone    STRING,
                current_zone STRING,
                lat          DOUBLE,
                lon          DOUBLE,
                recorded_at  TIMESTAMP
            ) USING DELTA
        """)
        st.session_state.table_exists = True

    # Insert only new rows
    BATCH_SIZE = 500
    for i in range(0, len(df), BATCH_SIZE):
        batch = df.iloc[i:i+BATCH_SIZE]
        values = ", ".join(
            f"({r.position_id}, '{r.vehicle_id}', '{r.home_zone}', "
            f"'{r.current_zone}', {r.lat}, {r.lon}, '{r.recorded_at}')"
            for r in batch.itertuples()
        )
        db_cursor.execute(f"INSERT INTO {TABLE_NAME} VALUES {values}")

    db_cursor.close()
    st.session_state.last_position_id = int(df["position_id"].max())
    return len(df)

# ---------------------------------------------------------------------------
# Analytics queries
# ---------------------------------------------------------------------------

def get_zone_activity(db_conn):
    """Zone activity over time from Databricks Delta."""
    if db_conn is None:
        return pd.DataFrame()
    try:
        cursor = db_conn.cursor()
        cursor.execute(f"USE {CATALOG_NAME}.{SCHEMA_NAME}")
        cursor.execute(f"""
            SELECT
                current_zone AS zone,
                DATE_TRUNC('minute', recorded_at) AS minute,
                COUNT(*) AS updates
            FROM {TABLE_NAME}
            WHERE current_zone IS NOT NULL
            GROUP BY current_zone, DATE_TRUNC('minute', recorded_at)
            ORDER BY minute, zone
        """)
        df = pd.DataFrame(cursor.fetchall(), columns=["zone", "minute", "updates"])
        cursor.close()
        return df
    except Exception as e:
        st.warning(f"Zone activity query failed: {e}")
        return pd.DataFrame()

def get_top_roads(db_conn, road_df):
    """Top road segments by traffic -- cross-system join."""
    if db_conn is None or road_df.empty:
        return pd.DataFrame()
    try:
        cursor = db_conn.cursor()
        cursor.execute(f"USE {CATALOG_NAME}.{SCHEMA_NAME}")
        cursor.execute(f"SELECT vehicle_id, lat, lon FROM {TABLE_NAME}")
        pos_df = pd.DataFrame(cursor.fetchall(), columns=["vehicle_id", "lat", "lon"])
        cursor.close()

        pos_df["lat_r"] = pos_df["lat"].round(3)
        pos_df["lon_r"] = pos_df["lon"].round(3)

        joined = pos_df.merge(
            road_df[["road_name", "highway", "lat_r", "lon_r"]],
            on=["lat_r", "lon_r"],
            how="inner"
        )

        return (
            joined.groupby(["road_name", "highway"])
            .size()
            .reset_index(name="passes")
            .sort_values("passes", ascending=False)
            .head(15)
        )
    except Exception as e:
        st.warning(f"Top roads query failed: {e}")
        return pd.DataFrame()

# ---------------------------------------------------------------------------
# Session state
# ---------------------------------------------------------------------------

if "schema_ready" not in st.session_state:
    st.session_state.schema_ready = False

if "last_row_count" not in st.session_state:
    st.session_state.last_row_count = 0

if "last_position_id" not in st.session_state:
    st.session_state.last_position_id = 0

if "table_exists" not in st.session_state:
    st.session_state.table_exists = False

# ---------------------------------------------------------------------------
# Auto-refresh
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Sidebar -- controls
# ---------------------------------------------------------------------------

st.sidebar.title(f"{_cfg['city']['name']} Analytics")
st.sidebar.markdown(f"Live analytics dashboard refreshing every 30 seconds.")
st.sidebar.divider()

st.sidebar.markdown("**Simulator Control**")
if st.sidebar.button("🛑 Stop Simulator"):
    with open("simulator.stop", "w") as f:
        f.write("stop")
    st.sidebar.success("Stop signal sent. The simulator will stop at the next tick.")

if st.sidebar.button("🗑 Reset Delta Table"):
    st.session_state.table_exists = False
    st.session_state.last_position_id = 0
    st.sidebar.success("Delta table will be recreated on next refresh.")

st.sidebar.divider()
st.sidebar.caption(f"Last position ID: {st.session_state.last_position_id:,}")

st_autorefresh(interval=REFRESH_MS, key="analytics_refresh")

# ---------------------------------------------------------------------------
# Connections and schema setup
# ---------------------------------------------------------------------------

lb   = get_lb_conn()
db   = get_db_conn()
road = load_road_data()

if not st.session_state.schema_ready and db is not None:
    try:
        cursor = db.cursor()
        cursor.execute(f"CREATE SCHEMA IF NOT EXISTS {CATALOG_NAME}.{SCHEMA_NAME}")
        cursor.close()
        st.session_state.schema_ready = True
    except Exception:
        pass

# ---------------------------------------------------------------------------
# Refresh Delta table
# ---------------------------------------------------------------------------

if not simulator_is_running(lb):
    st.warning("Simulator does not appear to be running. Start it from the Notebook.")
    st.stop()

with st.spinner("Syncing new positions from Lakebase to Databricks..."):
    row_count = refresh_delta_table(lb, db)
st.session_state.last_row_count = row_count

# ---------------------------------------------------------------------------
# Layout
# ---------------------------------------------------------------------------

st.markdown(f"### 📊 {_cfg['city']['name']} Analytics Dashboard")
st.caption(f"Refreshes every {REFRESH_MS // 1000}s. Showing last {HISTORY_MINS} minutes. {row_count:,} records loaded.")

col_left, col_right = st.columns(2)

# ---------------------------------------------------------------------------
# Chart 1 -- Zone activity over time
# ---------------------------------------------------------------------------

with col_left:
    st.markdown("**Zone Activity Over Time**")
    zone_df = get_zone_activity(db)

    if zone_df.empty:
        st.info("No data yet. Start the simulator.")
    else:
        fig = px.line(
            zone_df,
            x     = "minute",
            y     = "updates",
            color = "zone",
            color_discrete_map = ZONE_COLORS,
            labels = {"minute": "Time", "updates": "Position Updates", "zone": "Zone"}
        )
        fig.update_layout(
            height     = 400,
            margin     = dict(l=0, r=0, t=4, b=0),
            legend     = dict(orientation="h", yanchor="bottom", y=1.02),
            font       = dict(size=11),
        )
        st.plotly_chart(fig, width="stretch")

# ---------------------------------------------------------------------------
# Chart 2 -- Top road segments (Lakebase + Neo4j cross-system join)
# ---------------------------------------------------------------------------

with col_right:
    st.markdown("**Top Road Segments by Traffic** *(Lakebase + Neo4j)*")
    top_roads_df = get_top_roads(db, road)

    if top_roads_df.empty:
        st.info("No road match data yet.")
    else:
        fig = px.bar(
            top_roads_df.sort_values("passes"),
            x           = "passes",
            y           = "road_name",
            color       = "highway",
            orientation = "h",
            labels      = {"passes": "Vehicle Passes", "road_name": "Road", "highway": "Type"},
            color_discrete_sequence = px.colors.qualitative.Set2,
        )
        fig.update_layout(
            height  = 400,
            margin  = dict(l=0, r=0, t=4, b=0),
            legend  = dict(orientation="h", yanchor="bottom", y=1.02),
            font    = dict(size=11),
        )
        st.plotly_chart(fig, width="stretch")

st.caption(f"Neo4j: {len(road):,} road segments loaded. Last updated: {pd.Timestamp.now().strftime('%H:%M:%S')}")
