# Real-time vehicle tracking with Neo4j, Databricks Lakebase and OpenStreetMap

This book builds a real-time fleet operations dashboard using three database systems:

1. Neo4j Aura for the road network graph.
2. Databricks Lakebase for live vehicle positions.
3. Databricks Lakehouse for historical analytics.

Ten simulated vehicles move around a city following real road connections loaded from OpenStreetMap. Two Streamlit dashboards show live positions and analytics.

The primary demo uses the London Borough of Merton. Additional example configurations are provided for San Francisco and Singapore. The whole system is driven by a single YAML configuration file.

## How the code is organized

All runnable code lives in the `code/` directory alongside `config.yaml`. The same directory holds the two example configs for San Francisco and Singapore, `config_validator.py` and the two Streamlit apps, `app.py` and `analytics_app.py`. The simulator script is written by the Chapter 5 notebook.

The chapter markdown files live in `src/`.

Notebooks are numbered to match the chapters -- `02_road_network.ipynb` corresponds to Chapter 2, and so on. The one notebook for Chapter 1, `01_prepare_osm.ipynb`, is optional. It prepares the local OpenStreetMap files for San Francisco, Singapore or a new city, and Merton doesn't need it.
