# Chapter 4: The Operational Layer

## What the Operational Layer Does

Aura holds the road network, which is the structure of the city. But while vehicles are moving, we need somewhere to record what's actually happening: where each vehicle is right now, where it's been and what state it's in. This is the operational layer.

The operational layer needs to handle high-frequency writes. Ten vehicles writing their position every two seconds means 300 writes per minute, sustained for as long as the simulator runs. It also needs to serve the Streamlit application, which reads the latest position of every vehicle on every refresh. These are classic OLTP characteristics: small, frequent reads and writes, low latency and strong consistency.

Databricks Lakebase is a managed Postgres database hosted inside Databricks. It handles the operational workload exactly as a production Postgres instance would, using standard `psycopg2` connectivity from Python. Credentials are obtained from the Lakebase Connect dialog in the Databricks UI.

## Creating the Lakebase Project

Before running this notebook, you'll need a Lakebase project named `vehicle-tracker` in your Databricks workspace. The project name is deliberately generic -- it's city-agnostic, so the same project works whether you're running the Merton, San Francisco or Singapore config. When you switch cities, you re-run this notebook to drop and recreate the tables; the project itself stays the same.

Create the project from the Lakebase section of the Databricks UI.

## Setting Up Credentials

Lakebase supports two connection methods.

### Default Using OAuth Token

The OAuth token expires after one hour. This is intentional -- it's Databricks's authentication model and it enforces a natural session boundary. For a demo, this is actually useful as the system stops writing after an hour unless you actively renew it. When the token expires, generate a fresh one from the Lakebase Connect dialog in the Databricks UI and update `LAKEBASE_TOKEN` before reconnecting.

### Native Postgres Password

For longer-running sessions, Lakebase supports native Postgres password authentication. Enable it from the Lakebase UI: Settings -> Database connections -> check "Allow Password (Native Postgres roles)".

Then create a dedicated application role in the Lakebase SQL editor:

```sql
-- 1. Create the application role
CREATE ROLE vehicle_tracker
    LOGIN
    PASSWORD 'your_strong_password_here';

-- 2. Allow access to the public schema
GRANT USAGE
    ON SCHEMA public
    TO vehicle_tracker;

-- 3. Grant CRUD access to existing tables
GRANT SELECT, INSERT, UPDATE, DELETE
    ON ALL TABLES IN SCHEMA public
    TO vehicle_tracker;

-- 4. Grant sequence access to existing sequences
GRANT USAGE, SELECT
    ON ALL SEQUENCES IN SCHEMA public
    TO vehicle_tracker;

-- 5. Identify the role that creates the database objects
SELECT current_user;

-- 6. Grant CRUD access to future tables created by that role
ALTER DEFAULT PRIVILEGES FOR ROLE <your_databricks_username>
    IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE
    ON TABLES
    TO vehicle_tracker;

-- 7. Grant sequence access to future sequences
ALTER DEFAULT PRIVILEGES FOR ROLE <your_databricks_username>
    IN SCHEMA public
    GRANT USAGE, SELECT
    ON SEQUENCES
    TO vehicle_tracker;
```

Run `SELECT current_user;` first (step 5), note the result and substitute it into steps 6 and 7. Steps 6 and 7 are important, as without them, tables recreated when you switch cities (by re-running this notebook) won't automatically get the right permissions.

Replace `LAKEBASE_TOKEN` with `LAKEBASE_PASSWORD` in all connection code and set:

```bash
export LAKEBASE_USER="vehicle_tracker"
export LAKEBASE_PASSWORD="your_strong_password_here"
```

The password doesn't expire, so you won't need to refresh it between sessions.

## The Three Tables

The operational layer uses three tables. Their design reflects the different access patterns each one serves.

### vehicles

```sql
CREATE TABLE vehicles (
    vehicle_id   TEXT PRIMARY KEY,
    driver_name  TEXT NOT NULL,
    zone         TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'idle',
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
)
```

One row per vehicle. This table changes slowly and the simulator updates `status` between `idle` and `en_route` as vehicles start and finish routes, but the vehicle IDs, driver names and home zones don't change during a run. The Streamlit application reads this table to populate the driver list in the nearest-driver panel.

### vehicle_positions

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

One row per position update. This is the high-frequency table and the simulator inserts a new row for every vehicle on every tick. The `BIGSERIAL` primary key auto-increments and `recorded_at` defaults to the current timestamp. The `current_zone` column records which zone the vehicle is in at the time of the update, written by the simulator from its in-memory zone lookup. The Streamlit application reads the latest position per vehicle from this table on every refresh.

### trips

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

-- Vehicle lookup by status and zone (used by nearest-driver query)
CREATE INDEX idx_vehicles_status_zone
ON vehicles (status, zone);

-- Trip lookup by status (used by dispatch logic)
CREATE INDEX idx_trips_status
ON trips (status);
```

The composite index on `vehicle_positions(vehicle_id, recorded_at DESC)` is the most important, as it makes the "latest position per vehicle" query fast regardless of how many rows the table contains. Without it, every map refresh would scan the entire table.

## Seeding Vehicles

The vehicles are seeded from the city config. Ten vehicles, two per zone, with names chosen to reflect the international character of each city's driver population:

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
- For San Francisco, the zones are Noe Valley, Castro, Glen Park, Bernal Heights and Visitacion Valley
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

`autocommit = True` is required for DDL statements (CREATE TABLE, DROP TABLE, CREATE INDEX). Without it, DDL runs inside a transaction and some statements fail silently.

## Verifying the Setup

After seeding, we verify the table row counts and vehicle distribution:

```
vehicles             10 rows
vehicle_positions     0 rows
trips                 0 rows

Vehicles per zone:
  Colliers Wood   2
  Mitcham         2
  Morden          2
  Raynes Park     2
  Wimbledon       2
```

Zero rows in `vehicle_positions` is correct at this stage -- the simulator hasn't run yet. After Chapter 5, this table will have thousands of rows.

## Gotchas

**Token expiry.** The OAuth token expires after one hour. If you're running the simulator for longer sessions, either refresh the token or set up native Postgres password authentication as described above.

**Drop order.** When re-running the notebook to recreate tables, the drop order matters because of foreign key constraints. Drop `trips` first, then `vehicle_positions`, then `vehicles`. Reversing the order fails with a constraint violation.

**autocommit.** Set `conn.autocommit = True` immediately after connecting. If you forget, DDL statements may appear to succeed but then silently roll back when the connection closes.

**ALTER DEFAULT PRIVILEGES.** If you switch to native password authentication and later re-run this notebook to recreate tables (when switching cities), the new tables won't automatically inherit the permissions unless you ran the `ALTER DEFAULT PRIVILEGES` steps. This is the most common gotcha with Postgres role setup -- existing grants cover existing tables; default privileges cover future ones.

**Free tier daily limit.** Databricks free accounts have a "free daily limit" on usage. If you hit this limit, the connection will be refused. Wait until the next day or contact Databricks support.
