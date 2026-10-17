# Chapter 4: The Operational Layer

## What the operational layer does

Aura holds the road network, which is the structure of the city. But while vehicles are moving, we need somewhere to record what's actually happening: where each vehicle is right now, where it's been and what state it's in. This is the operational layer.

The operational layer needs to handle high-frequency writes. Ten vehicles writing their position every two seconds means up to about 300 writes per minute, sustained for as long as the simulator runs. It also needs to serve the Streamlit application, which reads the latest position of every vehicle on every refresh. These are classic OLTP characteristics: small, frequent reads and writes, low latency and strong consistency.

Databricks Lakebase is a managed Postgres database hosted inside Databricks. It handles the operational workload exactly as a production Postgres instance would, using standard `psycopg2` connectivity from Python. Credentials are obtained from the Lakebase Connect dialog in the Databricks UI.

## Creating the Lakebase project

Before running this notebook, you'll need a Lakebase project named `vehicle-tracker` in your Databricks workspace. The project name is deliberately generic -- it's city-agnostic, so the same project works whether you're running the Merton, San Francisco or Singapore config. When you switch cities, you re-run this notebook to drop and recreate the tables; the project itself stays the same.

**Warning:** Running the notebook drops the `vehicles`, `vehicle_positions` and `trips` tables and all the data in them. Only run it against a project you're happy to reset.

Create the project from the Lakebase section of the Databricks UI.

## Setting up credentials

Lakebase connections use an OAuth token as the password. The token expires after one hour. This is intentional -- it's Databricks's authentication model and it enforces a natural session boundary. For a demo, this is actually useful as it gives us a natural session boundary: after the hour is up, no new connection can be made unless you renew the token. When the token expires, generate a fresh one from the Lakebase Connect dialog in the Databricks UI and update `LAKEBASE_TOKEN` before reconnecting.

## The three tables

The operational layer uses three tables. Their design reflects the different access patterns each one serves.

### Vehicles

```sql
CREATE TABLE vehicles (
    vehicle_id  TEXT PRIMARY KEY,
    driver_name TEXT NOT NULL,
    zone        TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'idle',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
)
```

One row per vehicle. This table changes slowly and the simulator updates `status` between `idle` and `en_route` as vehicles start and finish routes, but the vehicle IDs, driver names and home zones don't change during a run. The Streamlit application joins this table to the latest positions to show each vehicle's driver and home zone in the map tooltips and the nearest-driver panel.

### Vehicle_positions

```sql
CREATE TABLE vehicle_positions (
    position_id  BIGSERIAL PRIMARY KEY,
    vehicle_id   TEXT NOT NULL REFERENCES vehicles(vehicle_id),
    lat          DOUBLE PRECISION NOT NULL,
    lon          DOUBLE PRECISION NOT NULL,
    speed_kmh    DOUBLE PRECISION,
    current_zone TEXT,
    recorded_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
)
```

One row per position update. This is the high-frequency table and the simulator inserts a new row for every vehicle on every tick. The `BIGSERIAL` primary key auto-increments and `recorded_at` defaults to the current timestamp. The `current_zone` column records which zone the vehicle is in at the time of the update, written by the simulator from its in-memory zone lookup. The `speed_kmh` column is part of the schema for completeness, but the simulator in this book doesn't populate it, so it stays NULL. The Streamlit application reads the latest position per vehicle from this table on every refresh.

### Trips

```sql
CREATE TABLE trips (
    trip_id      TEXT PRIMARY KEY,
    vehicle_id   TEXT REFERENCES vehicles(vehicle_id),
    pickup_lat   DOUBLE PRECISION NOT NULL,
    pickup_lon   DOUBLE PRECISION NOT NULL,
    dropoff_lat  DOUBLE PRECISION NOT NULL,
    dropoff_lon  DOUBLE PRECISION NOT NULL,
    pickup_zone  TEXT,
    dropoff_zone TEXT,
    status       TEXT NOT NULL DEFAULT 'requested',
    requested_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    started_at   TIMESTAMPTZ,
    completed_at TIMESTAMPTZ
)
```

One row per trip, tracking the full lifecycle from request to completion. The simulator doesn't write trips in this demo and the table is included in the schema because it's what a production system would need and it's a natural extension point for readers who want to add trip dispatch logic.

## Indexes

Three indexes support the primary access patterns:

```sql
-- Latest positions per vehicle (used by every Streamlit map refresh)
CREATE INDEX idx_vehicle_positions_vehicle_id
ON vehicle_positions (vehicle_id, recorded_at DESC);

-- Vehicle lookup by status and zone (for dispatch-style queries readers might add)
CREATE INDEX idx_vehicles_status_zone
ON vehicles (status, zone);

-- Trip lookup by status (for the trip dispatch logic readers might add)
CREATE INDEX idx_trips_status
ON trips (status);
```

The composite index on `vehicle_positions(vehicle_id, recorded_at DESC)` is the most important, as it makes the "latest position per vehicle" query fast regardless of how many rows the table contains. Without it, every map refresh would scan the entire table. The other two indexes anticipate dispatch-style lookups that the demo apps don't run.

## Seeding vehicles

The vehicles are seeded from the city config. Ten vehicles, two per zone, with a varied set of driver names:

```python
vehicles = [
    (v["id"], v["driver"], v["zone"])
    for v in cfg["vehicles"]
]
cursor.executemany("""
    INSERT INTO vehicles (vehicle_id, driver_name, zone)
    VALUES (%s, %s, %s)
""", vehicles)
```

- For Merton, the zones are Wimbledon, Raynes Park, Colliers Wood, Mitcham and Morden.
- For San Francisco, the zones are Noe Valley, Castro, Glen Park, Bernal Heights and Visitacion Valley.
- For Singapore, the zones are Toa Payoh, Bishan, Ang Mo Kio, Serangoon and Novena.

## Connecting to Lakebase

The `psycopg2` connection requires SSL and uses port 5432:

```python
conn = psycopg2.connect(
    host     = os.environ["LAKEBASE_HOST"],
    user     = os.environ["LAKEBASE_USER"],
    password = os.environ["LAKEBASE_TOKEN"],
    dbname   = os.environ["LAKEBASE_DBNAME"],
    sslmode  = "require",
    port     = 5432
)
conn.autocommit = True
```

`autocommit = True` makes each statement commit immediately, including DDL statements such as `CREATE TABLE`, `DROP TABLE` and `CREATE INDEX`. Without it, changes sit in an open transaction and are rolled back if the connection closes before a commit.

## Verifying the setup

After seeding, we verify the table row counts and the vehicle distribution, and we print a sample of the seeded vehicles:

```
vehicles             10 rows
vehicle_positions    0 rows
trips                0 rows

Vehicles per zone:
  Colliers Wood   2
  Mitcham         2
  Morden          2
  Raynes Park     2
  Wimbledon       2

Sample vehicles:
  ID       Driver             Zone            Status
  V001     Alice Mensah       Wimbledon       idle
  V002     Ben Okafor         Wimbledon       idle
  V003     Clara Singh        Raynes Park     idle
  V004     David Petrov       Raynes Park     idle
  V005     Elena Costa        Colliers Wood   idle
```

Zero rows in `vehicle_positions` is correct at this stage -- the simulator hasn't run yet. Once the simulator is running in Chapter 5, this table grows by a little under 300 rows a minute.

## Gotchas

**Token expiry.** The OAuth token expires after one hour. If you want to keep the simulator running longer, generate a fresh token, update `LAKEBASE_TOKEN` and restart the simulator from Chapter 5.

**Drop order.** The notebook drops `trips` first, then `vehicle_positions`, then `vehicles`, and uses `CASCADE`. Dropping a table that other tables reference fails without `CASCADE`, so keep the order and the `CASCADE` if you adapt the notebook.

**autocommit.** Set `conn.autocommit = True` immediately after connecting. If you forget, DDL statements may appear to succeed but then silently roll back when the connection closes.

**Free tier daily limit.** Databricks free accounts have a "free daily limit" on usage. If you hit this limit, the connection will be refused. Wait until the next day or contact Databricks support.
