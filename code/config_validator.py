"""
config_validator.py -- Load and validate config.yaml for the vehicle tracking system.

Usage:
    from config_validator import load_config
    cfg = load_config("config.yaml")

The returned dict is guaranteed to have the correct structure and types.
A ConfigError is raised with a descriptive message if validation fails.
"""

import yaml
from pathlib import Path

class ConfigError(Exception):
    """Raised when config.yaml fails validation."""
    pass

def _require(d, key, section):
    if key not in d or d[key] is None:
        raise ConfigError(f"Missing required field '{key}' in [{section}]")
    return d[key]

def _require_type(value, expected_type, field):
    if not isinstance(value, expected_type):
        raise ConfigError(
            f"Field '{field}' must be {expected_type.__name__}, "
            f"got {type(value).__name__}"
        )
    return value

def _validate_city(city):
    _require(city, "name",         "city")
    _require(city, "osmnx_place",  "city")
    _require(city, "network_type", "city")
    _require(city, "map_lat",      "city")
    _require(city, "map_lon",      "city")
    _require(city, "map_zoom",     "city")
    _require(city, "schema_name",  "city")

    _require_type(city["map_lat"],  (int, float), "city.map_lat")
    _require_type(city["map_lon"],  (int, float), "city.map_lon")
    _require_type(city["map_zoom"], (int, float), "city.map_zoom")

    place = city["osmnx_place"]
    if not isinstance(place, (str, list)):
        raise ConfigError("city.osmnx_place must be a string or a list of strings")
    if isinstance(place, list) and not all(isinstance(p, str) for p in place):
        raise ConfigError("city.osmnx_place list items must all be strings")

    if city["network_type"] not in ("drive", "walk", "bike", "all"):
        raise ConfigError(
            f"city.network_type must be one of: drive, walk, bike, all. "
            f"Got '{city['network_type']}'"
        )

    # osm_file, osm_source, osm_source_file are optional strings -- no validation needed
    # osm_bbox is optional -- validate if present
    if "osm_bbox" in city and city["osm_bbox"] is not None:
        bbox = city["osm_bbox"]
        if not isinstance(bbox, list) or len(bbox) != 4:
            raise ConfigError("city.osm_bbox must be a list of 4 numbers: [south, north, west, east]")
        if not all(isinstance(v, (int, float)) for v in bbox):
            raise ConfigError("city.osm_bbox values must all be numbers")
        south, north, west, east = bbox
        if south >= north:
            raise ConfigError("city.osm_bbox: south must be less than north")

def _validate_zones(zones):
    if not zones:
        raise ConfigError("At least one zone is required under [zones]")

    names = set()
    for i, z in enumerate(zones):
        prefix = f"zones[{i}]"
        for field in ("name", "lat_min", "lat_max", "lon_min", "lon_max",
                      "color", "center_lat", "center_lon"):
            _require(z, field, prefix)

        name = z["name"]
        if name in names:
            raise ConfigError(f"Duplicate zone name '{name}'")
        names.add(name)

        if z["lat_min"] >= z["lat_max"]:
            raise ConfigError(f"Zone '{name}': lat_min must be less than lat_max")
        if z["lon_min"] >= z["lon_max"]:
            raise ConfigError(f"Zone '{name}': lon_min must be less than lon_max")

        color = z["color"]
        if not isinstance(color, list) or len(color) != 3:
            raise ConfigError(f"Zone '{name}': color must be a list of 3 integers [R, G, B]")
        if not all(isinstance(c, int) and 0 <= c <= 255 for c in color):
            raise ConfigError(f"Zone '{name}': color values must be integers between 0 and 255")

    return names

def _validate_adjacency(adjacency, zone_names):
    if adjacency is None:
        return
    for i, pair in enumerate(adjacency):
        if not isinstance(pair, list) or len(pair) != 2:
            raise ConfigError(f"adjacency[{i}]: each pair must be a list of exactly 2 zone names")
        a, b = pair
        if a not in zone_names:
            raise ConfigError(f"adjacency[{i}]: unknown zone '{a}'")
        if b not in zone_names:
            raise ConfigError(f"adjacency[{i}]: unknown zone '{b}'")
        if a == b:
            raise ConfigError(f"adjacency[{i}]: a zone cannot be adjacent to itself ('{a}')")

def _validate_vehicles(vehicles, zone_names):
    if not vehicles:
        raise ConfigError("At least one vehicle is required under [vehicles]")

    ids = set()
    for i, v in enumerate(vehicles):
        prefix = f"vehicles[{i}]"
        for field in ("id", "driver", "zone"):
            _require(v, field, prefix)

        vid = v["id"]
        if vid in ids:
            raise ConfigError(f"Duplicate vehicle id '{vid}'")
        ids.add(vid)

        if v["zone"] not in zone_names:
            raise ConfigError(
                f"Vehicle '{vid}': zone '{v['zone']}' is not defined in [zones]"
            )

def load_config(path="config.yaml"):
    """
    Load and validate config.yaml. Returns the validated config dict.
    Raises ConfigError with a descriptive message if validation fails.
    Raises FileNotFoundError if the file does not exist.
    """
    config_path = Path(path)
    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found: {path}")

    with open(config_path, "r") as f:
        cfg = yaml.safe_load(f)

    if cfg is None:
        raise ConfigError("config.yaml is empty")

    for section in ("city", "zones", "vehicles"):
        if section not in cfg:
            raise ConfigError(f"Missing required section [{section}]")

    _validate_city(cfg["city"])
    zone_names = _validate_zones(cfg["zones"])
    _validate_adjacency(cfg.get("adjacency"), zone_names)
    _validate_vehicles(cfg["vehicles"], zone_names)

    return cfg

def zone_colors(cfg):
    """Return {zone_name: [R, G, B]} dict from config."""
    return {z["name"]: z["color"] for z in cfg["zones"]}

def zone_centers(cfg):
    """Return {zone_name: (center_lat, center_lon)} dict from config."""
    return {z["name"]: (z["center_lat"], z["center_lon"]) for z in cfg["zones"]}

def zone_adjacency(cfg):
    """Return {zone_name: [adjacent_zone_names, ...]} dict from config."""
    adj = {z["name"]: [z["name"]] for z in cfg["zones"]}  # include self for zone-bias routing
    for pair in (cfg.get("adjacency") or []):
        a, b = pair
        adj[a].append(b)
        adj[b].append(a)
    return adj
