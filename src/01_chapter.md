# Chapter 1: Architecture and Setup

## What We're Building

Real-time vehicle tracking sits at the intersection of three distinct data problems:

1. **Operational problem** -- where are my vehicles right now and where have they been?
2. **Graph problem** -- what's the fastest route between two points on an actual road network and which zones are reachable from where?
3. **Analytical problem** -- which areas of the city are seeing the most activity and which roads carry the most traffic?

Each of those problems has a natural home in a different kind of database. That's the core idea behind this book. We're not going to use one database and make it do everything. We're going to use three and let each one do what it does best.

The system we'll build is a fleet operations dashboard for the London Borough of Merton -- a compact urban borough in south London with a dense road network that's large enough to be interesting but small enough to run on free-tier accounts. Ten simulated vehicles move around the borough following real road connections loaded from OpenStreetMap. A live Streamlit dashboard shows their positions, trails and the shortest path between any two zones. A separate analytics dashboard shows zone activity over time and which named roads carry the most traffic.

We'll also show how to adapt the entire system to a different city by changing a single configuration file. Additional example configuration files are provided for San Francisco and Singapore.

## The Three-System Architecture

The architecture has three layers, each serving a different purpose.

**Neo4j Aura** is the graph layer. It holds the road network for Merton -- 3,203 intersections and 7,276 road segments loaded from OpenStreetMap via the OSMnx library. Aura answers the questions that a relational database handles with difficulty:

- What's the shortest path between two points along drivable roads?
- Which zones border which other zones?
- Which intersections are the most connected hubs of the network?

The native spatial index on intersection nodes makes nearest-neighbor lookups fast enough to run on every map refresh.

**Databricks Lakebase** is the operational layer. It's a fully managed Postgres database, hosted inside Databricks, that accepts the high-frequency position writes from our vehicle simulator. Every two seconds, ten vehicles each write their current coordinates to a `vehicle_positions` table. Lakebase handles this with standard `psycopg2` connectivity, foreign key constraints and BIGSERIAL auto-increment IDs. The token-based authentication enforces a natural session boundary, as the system runs for an hour at a time, which is the right behavior for a demo.

**Databricks Lakehouse** is the analytics layer. We'll connect to it from a local Jupyter notebook. Position data flows from Lakebase into a Delta table, where we run aggregations, such as zone demand over time, position volume trends and the cross-system join that answers "which named roads carry the most traffic?" by combining Lakebase position data with Aura road names.

The relationship between these systems is as follows:

- Aura is where the *structure* lives -- the road graph that neither Lakebase nor Lakehouse knows anything about.
- Lakebase is where data is *written*, in real time, by the simulator.
- Lakehouse is where we *analyse* the history that Lakebase accumulates.

Each system has a clear role and none of the roles overlap.

## What Aura Adds

A natural question is: why Aura? We already have a Postgres database. Can't we just store road network data there too? We could store it there. But querying it could be difficult. The shortest path between two points requires traversing a graph -- following edges from node to node, keeping track of visited nodes and finding the minimum-cost sequence. In SQL this means recursive CTEs, which are slow for deep traversals. In Cypher, Aura's query language, it's a single function call. For example:

```cypher
MATCH path = shortestPath((start)-[:ROAD*..300]->(end))
RETURN length(path) AS hops
```

The zone adjacency queries are similarly clean. "Which zones can be reached within two hops from Morden?" is a two-line Cypher query. In SQL it would require multiple self-joins or a recursive CTE that grows in complexity with each additional hop.

Also, the road network *is* a graph. Intersections are nodes. Roads are edges. The connectivity between them is the point. Storing that as rows and columns in a relational table is possible but goes against the natural structure of the data.

## The Configuration File

Every city-specific value in the system lives in a single YAML file called `config.yaml`. The zone definitions, vehicle assignments, map coordinates and OpenStreetMap place name all come from this file. Switching from London to San Francisco or Singapore means copying a different config file into place and re-running the notebooks.

There are three example configs:

1. `config.yaml` -- the London Borough of Merton (the default)
2. `config_sf.yaml` -- five neighborhoods in southern San Francisco
3. `config_sg.yaml` -- five planning areas in central Singapore

The supporting code in `config_validator.py` validates the config file on load and raises clear errors if anything is missing or malformed.

## What You'll Need

To follow along you'll need accounts and access to these services:

1. **Neo4j Aura** -- the free tier is sufficient. Create an account at [Get Started for Free](http://console.neo4j.io/graphacademy) and note your URI, username and password.
2. **Databricks** -- a free trial account gives us access to both Lakebase (managed Postgres) and a SQL warehouse. Create an account at [Databricks Free Edition](https://login.databricks.com/select-product?provider=DB_FREE_TIER). Create a Lakebase project named `vehicle-tracker` and a SQL warehouse before running the notebooks.
3. **Python** -- we'll use Python 3.12 throughout. The notebooks run in a local Jupyter environment. A virtual environment is recommended. For example:

```bash
python3 -m venv myenv
source myenv/bin/activate
```

All dependencies are installed using `%pip install` cells at the top of each notebook, with pinned versions for stability.

## Environment Variables

Credentials are passed to notebooks and the Streamlit applications using environment variables. Set these before starting Jupyter:

```bash
# Neo4j Aura
export NEO4J_URI=neo4j+s://xxxx.databases.neo4j.io
export NEO4J_USERNAME=your_username_here
export NEO4J_PASSWORD=your_password_here

# Databricks Lakebase
export LAKEBASE_HOST=ep-xxxx.database.eu-west-1.cloud.databricks.com
export LAKEBASE_USER=your_databricks_email_here
export LAKEBASE_TOKEN=your_oauth_token_here
export LAKEBASE_DBNAME=databricks_postgres

# Databricks Lakehouse
export DATABRICKS_SERVER_HOSTNAME=dbc-xxxx.cloud.databricks.com
export DATABRICKS_HTTP_PATH=/sql/1.0/warehouses/xxxx
export DATABRICKS_TOKEN=your_personal_access_token_here
```

The Lakebase token expires after one hour. This is by design -- it's Databricks's authentication model for OAuth-based access and it gives us a natural session boundary. When it expires, we need to generate a fresh token from the Lakebase Connect dialog in the Databricks UI and update the `LAKEBASE_TOKEN` environment variable.

For longer-running sessions, Chapter 4 explains how to set up and use a native Postgres role with a permanent password instead.

## Chapter Overview

Here's what each chapter covers:

**Chapter 2: The Road Network** walks through loading the Merton road network from OpenStreetMap into Aura using OSMnx. We'll cover the data cleaning required to handle the quirks of OSM data, such as list-valued properties, duplicate edges and missing speed limits. We'll also describe the Aura schema that makes spatial queries efficient.

**Chapter 3: Zones and Graph Queries** defines the five zones that divide the borough, assigns each intersection to its zone and builds the zone adjacency graph. We'll show the Cypher queries that make the zone topology useful, such as nearest neighbors, multi-hop reachability and complex intersection counts.

**Chapter 4: The Operational Layer** sets up the Lakebase tables that hold live vehicle data. We'll cover the table design, the indexes that make time-series queries fast and the credential options for connecting from Python.

**Chapter 5: The Simulator** writes a vehicle simulator that loads the road network from Aura, places ten vehicles in their home zones and moves each one along Breadth-First Search (BFS)-computed shortest paths. The simulator runs as a background process and writes position updates to Lakebase every two seconds.

**Chapter 6: The Streamlit Apps** builds two Streamlit dashboards. The first one shows vehicles moving on a live map with trail history and shortest path queries. The second one connects to Lakehouse to show zone activity trends and road segment traffic, updated every 30 seconds.

**Chapter 7: Bringing It All Together** covers the full system running end-to-end, the gotchas we encountered along the way and ideas for going further.

Let's start by loading the road network.
