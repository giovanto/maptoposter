#!/usr/bin/env python3
"""
City Map Poster Generator

This module generates beautiful, minimalist map posters for any city in the world.
It fetches OpenStreetMap data using OSMnx, applies customizable themes, and creates
high-quality poster-ready images with roads, water features, and parks.
"""

import argparse
import asyncio
import json
import math
import os
import pickle
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from typing import cast

import matplotlib.colors as mcolors
import matplotlib.pyplot as plt
import numpy as np
import osmnx as ox
import pyproj
from geopandas import GeoDataFrame
from geopy.geocoders import Nominatim
from lat_lon_parser import parse
from matplotlib.font_manager import FontProperties
from networkx import MultiDiGraph
from shapely.geometry import Point, box as shapely_box
from shapely.ops import linemerge, polygonize, unary_union
from tqdm import tqdm

from font_management import load_fonts


class CacheError(Exception):
    """Raised when a cache operation fails."""


CACHE_DIR_PATH = os.environ.get("CACHE_DIR", "cache")
CACHE_DIR = Path(CACHE_DIR_PATH)
CACHE_DIR.mkdir(exist_ok=True)

# Configure OSMnx: its own HTTP response cache complements the pickle cache above
ox.settings.use_cache = True
ox.settings.log_console = False

# Alternate Overpass instance (e.g. https://overpass.kumi.systems/api/interpreter)
# for when overpass-api.de rate-limits your IP
if os.environ.get("OVERPASS_TIMEOUT"):
    # Wide frames with a full network can exceed Overpass' default 180 s
    ox.settings.requests_timeout = int(os.environ["OVERPASS_TIMEOUT"])
NETWORK_TYPE = os.environ.get("MAPTOPOSTER_NETWORK", "all")  # all | drive | walk | bike
# Data source: "overpass" (osmnx -> Overpass, default) or "pbf" (local planet file via osmium + DuckDB;
# no server, no rate limit, deterministic against a dated extract). MAPTOPOSTER_PBF points at the file.
DATA_SOURCE = os.environ.get("MAPTOPOSTER_SOURCE", "overpass")
# Transit overlay: which railway= values count as "public transport you can see".
# MAPTOPOSTER_TRANSIT=tram,light_rail restores the tram-only overlay.
TRANSIT_RAILWAYS = os.environ.get("MAPTOPOSTER_TRANSIT", "tram,light_rail").split(",")          # street-level, full weight
TRANSIT_HEAVY = os.environ.get("MAPTOPOSTER_TRANSIT_HEAVY", "rail,narrow_gauge,subway").split(",")  # heavy rail, thinner
# GDAL's OSM driver silently drops features once its temp budget (default 100 MB) is exceeded on big frames.
os.environ.setdefault("OSM_MAX_TMPFILE_SIZE", "8192")
PBF_PATH = os.environ.get("MAPTOPOSTER_PBF", "")
if os.environ.get("OVERPASS_URL"):
    ox.settings.overpass_url = os.environ["OVERPASS_URL"]
    ox.settings.overpass_rate_limit = False  # mirrors don't expose slot status

THEMES_DIR = "themes"
FONTS_DIR = "fonts"
POSTERS_DIR = "posters"

FILE_ENCODING = "utf-8"

FONTS = load_fonts()


def _cache_path(key: str) -> str:
    """
    Generate a safe cache file path from a cache key.

    Args:
        key: Cache key identifier

    Returns:
        Path to cache file with .pkl extension
    """
    safe = key.replace(os.sep, "_")
    return os.path.join(CACHE_DIR, f"{safe}.pkl")


def cache_get(key: str):
    """
    Retrieve a cached object by key.

    Args:
        key: Cache key identifier

    Returns:
        Cached object if found, None otherwise

    Raises:
        CacheError: If cache read operation fails
    """
    try:
        path = _cache_path(key)
        if not os.path.exists(path):
            return None
        with open(path, "rb") as f:
            return pickle.load(f)
    except Exception as e:
        raise CacheError(f"Cache read failed: {e}") from e


def cache_set(key: str, value):
    """
    Store an object in the cache.

    Args:
        key: Cache key identifier
        value: Object to cache (must be picklable)

    Raises:
        CacheError: If cache write operation fails
    """
    try:
        if not os.path.exists(CACHE_DIR):
            os.makedirs(CACHE_DIR)
        path = _cache_path(key)
        with open(path, "wb") as f:
            pickle.dump(value, f, protocol=pickle.HIGHEST_PROTOCOL)
    except Exception as e:
        raise CacheError(f"Cache write failed: {e}") from e


# Font loading now handled by font_management.py module


def is_latin_script(text):
    """
    Check if text is primarily Latin script.
    Used to determine if letter-spacing should be applied to city names.

    :param text: Text to analyze
    :return: True if text is primarily Latin script, False otherwise
    """
    if not text:
        return True

    latin_count = 0
    total_alpha = 0

    for char in text:
        if char.isalpha():
            total_alpha += 1
            # Latin Unicode ranges:
            # - Basic Latin: U+0000 to U+007F
            # - Latin-1 Supplement: U+0080 to U+00FF
            # - Latin Extended-A: U+0100 to U+017F
            # - Latin Extended-B: U+0180 to U+024F
            if ord(char) < 0x250:
                latin_count += 1

    # If no alphabetic characters, default to Latin (numbers, symbols, etc.)
    if total_alpha == 0:
        return True

    # Consider it Latin if >80% of alphabetic characters are Latin
    return (latin_count / total_alpha) > 0.8


def parse_gpx(gpx_path):
    """
    Parse a GPX file and extract trackpoints as (lat, lon) tuples.

    Args:
        gpx_path: Path to GPX file

    Returns:
        List of (lat, lon) tuples from <trkpt> elements
    """
    try:
        tree = ET.parse(gpx_path)
        root = tree.getroot()
        namespace = "{http://www.topografix.com/GPX/1/1}"
        trackpoints = []
        for trkpt in root.findall(f".//{namespace}trkpt"):
            lat = trkpt.attrib.get("lat")
            lon = trkpt.attrib.get("lon")
            if lat is not None and lon is not None:
                trackpoints.append((float(lat), float(lon)))
        return trackpoints
    except FileNotFoundError:
        print(f"✗ GPX file not found: {gpx_path}")
        return []
    except (ET.ParseError, ValueError) as e:
        print(f"✗ Failed to parse GPX file '{gpx_path}': {e}")
        return []


def generate_output_filename(city, theme_name, output_format):
    """
    Generate unique output filename with city, theme, and datetime.
    """
    if not os.path.exists(POSTERS_DIR):
        os.makedirs(POSTERS_DIR)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    city_slug = city.lower().replace(" ", "_")
    ext = output_format.lower()
    filename = f"{city_slug}_{theme_name}_{timestamp}.{ext}"
    return os.path.join(POSTERS_DIR, filename)


def get_available_themes():
    """
    Scans the themes directory and returns a list of available theme names.
    """
    if not os.path.exists(THEMES_DIR):
        os.makedirs(THEMES_DIR)
        return []

    themes = []
    for file in sorted(os.listdir(THEMES_DIR)):
        if file.endswith(".json"):
            theme_name = file[:-5]  # Remove .json extension
            themes.append(theme_name)
    return themes


def load_theme(theme_name="terracotta"):
    """
    Load theme from JSON file in themes directory.
    """
    theme_file = os.path.join(THEMES_DIR, f"{theme_name}.json")

    if not os.path.exists(theme_file):
        print(f"⚠ Theme file '{theme_file}' not found. Using default terracotta theme.")
        # Fallback to embedded terracotta theme
        return {
            "name": "Terracotta",
            "description": "Mediterranean warmth - burnt orange and clay tones on cream",
            "bg": "#F5EDE4",
            "text": "#8B4513",
            "gradient_color": "#F5EDE4",
            "water": "#A8C4C4",
            "parks": "#E8E0D0",
            "road_motorway": "#A0522D",
            "road_primary": "#B8653A",
            "road_secondary": "#C9846A",
            "road_tertiary": "#D9A08A",
            "road_residential": "#E5C4B0",
            "road_default": "#D9A08A",
        }

    with open(theme_file, "r", encoding=FILE_ENCODING) as f:
        theme = json.load(f)
        print(f"✓ Loaded theme: {theme.get('name', theme_name)}")
        if "description" in theme:
            print(f"  {theme['description']}")
        return theme


# Load theme (can be changed via command line or input)
THEME = dict[str, str]()  # Will be loaded later


def create_gradient_fade(ax, color, location="bottom", zorder=10):
    """
    Creates a fade effect at the top or bottom of the map.
    """
    vals = np.linspace(0, 1, 256).reshape(-1, 1)
    gradient = np.hstack((vals, vals))

    rgb = mcolors.to_rgb(color)
    my_colors = np.zeros((256, 4))
    my_colors[:, 0] = rgb[0]
    my_colors[:, 1] = rgb[1]
    my_colors[:, 2] = rgb[2]

    if location == "bottom":
        my_colors[:, 3] = np.linspace(1, 0, 256)
        extent_y_start = 0
        extent_y_end = 0.25
    else:
        my_colors[:, 3] = np.linspace(0, 1, 256)
        extent_y_start = 0.75
        extent_y_end = 1.0

    custom_cmap = mcolors.ListedColormap(my_colors)

    xlim = ax.get_xlim()
    ylim = ax.get_ylim()
    y_range = ylim[1] - ylim[0]

    y_bottom = ylim[0] + y_range * extent_y_start
    y_top = ylim[0] + y_range * extent_y_end

    ax.imshow(
        gradient,
        extent=[xlim[0], xlim[1], y_bottom, y_top],
        aspect="auto",
        cmap=custom_cmap,
        zorder=zorder,
        origin="lower",
    )


def get_edge_colors_by_type(g):
    """
    Assigns colors to edges based on road type hierarchy.
    Returns a list of colors corresponding to each edge in the graph.
    """
    edge_colors = []

    for _u, _v, data in g.edges(data=True):
        # Get the highway type (can be a list or string)
        highway = data.get('highway', 'unclassified')

        # Handle list of highway types (take the first one)
        if isinstance(highway, list):
            highway = highway[0] if highway else 'unclassified'

        # Assign color based on road type
        if highway in ["motorway", "motorway_link"]:
            color = THEME["road_motorway"]
        elif highway in ["trunk", "trunk_link", "primary", "primary_link"]:
            color = THEME["road_primary"]
        elif highway in ["secondary", "secondary_link"]:
            color = THEME["road_secondary"]
        elif highway in ["tertiary", "tertiary_link"]:
            color = THEME["road_tertiary"]
        elif highway in ["residential", "living_street", "unclassified"]:
            color = THEME["road_residential"]
        else:
            color = THEME['road_default']

        edge_colors.append(color)

    return edge_colors


def get_edge_widths_by_type(g):
    """
    Assigns line widths to edges based on road type.
    Major roads get thicker lines.
    """
    edge_widths = []

    # Themes may override the width ramp (e.g. flatten it so roads read as texture)
    ramp = THEME.get("road_widths", {})
    w_motorway = ramp.get("motorway", 1.2)
    w_primary = ramp.get("primary", 1.0)
    w_secondary = ramp.get("secondary", 0.8)
    w_tertiary = ramp.get("tertiary", 0.6)
    w_default = ramp.get("default", 0.4)
    # Footways, paths, cycleways, tracks: with network_type='all' these outnumber
    # streets several to one and, drawn at the default width, read as roads.
    w_minor = ramp.get("minor", 0.15)

    for _u, _v, data in g.edges(data=True):
        highway = data.get('highway', 'unclassified')

        if isinstance(highway, list):
            highway = highway[0] if highway else 'unclassified'

        # Assign width based on road importance
        if highway in ["motorway", "motorway_link"]:
            width = w_motorway
        elif highway in ["trunk", "trunk_link", "primary", "primary_link"]:
            width = w_primary
        elif highway in ["secondary", "secondary_link"]:
            width = w_secondary
        elif highway in ["tertiary", "tertiary_link"]:
            width = w_tertiary
        elif highway in ["footway", "path", "pedestrian", "steps", "cycleway", "track", "bridleway", "corridor"]:
            width = w_minor
        else:
            width = w_default

        edge_widths.append(width)

    return edge_widths


def _mode_of_highway(highway):
    """Classify an OSM highway value into a mobility mode."""
    if isinstance(highway, list):
        highway = highway[0] if highway else "unclassified"
    if highway in ("cycleway",):
        return "bike"
    if highway in ("footway", "pedestrian", "path", "steps", "track", "bridleway", "living_street"):
        return "walk"
    if highway in ("busway", "bus_guideway"):
        return "transit"
    return "car"


def get_edge_colors_mobility(g):
    """
    Color edges by mobility mode: sustainable modes lead, car network recedes.
    """
    bike = THEME.get("mode_bike", "#2E7D4F")
    walk = THEME.get("mode_walk", "#9BB8A0")
    transit = THEME.get("mode_transit", "#C05B3C")
    car = THEME.get("mode_car")
    if not car:
        bg_rgb = mcolors.to_rgb(THEME["bg"])
        tx_rgb = mcolors.to_rgb(THEME["text"])
        car = mcolors.to_hex(tuple(0.78 * b + 0.22 * t for b, t in zip(bg_rgb, tx_rgb)))
    palette = {"bike": bike, "walk": walk, "transit": transit, "car": car}
    return [palette[_mode_of_highway(d.get("highway", "unclassified"))]
            for _u, _v, d in g.edges(data=True)]


def get_edge_widths_mobility(g):
    """
    Width edges by mobility mode: bike/transit bold, walk light, car hairline.
    """
    widths = {"bike": 1.6, "walk": 0.7, "transit": 1.6, "car": 0.45}
    return [widths[_mode_of_highway(d.get("highway", "unclassified"))]
            for _u, _v, d in g.edges(data=True)]


def get_coordinates(city, country):
    """
    Fetches coordinates for a given city and country using geopy.
    Includes rate limiting to be respectful to the geocoding service.
    """
    coords = f"coords_{city.lower()}_{country.lower()}"
    cached = cache_get(coords)
    if cached:
        print(f"✓ Using cached coordinates for {city}, {country}")
        return cached

    print("Looking up coordinates...")
    geolocator = Nominatim(user_agent="city_map_poster", timeout=10)

    # Add a small delay to respect Nominatim's usage policy
    time.sleep(1)

    try:
        location = geolocator.geocode(f"{city}, {country}")
    except Exception as e:
        raise ValueError(f"Geocoding failed for {city}, {country}: {e}") from e

    # If geocode returned a coroutine in some environments, run it to get the result.
    if asyncio.iscoroutine(location):
        try:
            location = asyncio.run(location)
        except RuntimeError as exc:
            # If an event loop is already running, try using it to complete the coroutine.
            loop = asyncio.get_event_loop()
            if loop.is_running():
                # Running event loop in the same thread; raise a clear error.
                raise RuntimeError(
                    "Geocoder returned a coroutine while an event loop is already running. "
                    "Run this script in a synchronous environment."
                ) from exc
            location = loop.run_until_complete(location)

    if location:
        # Use getattr to safely access address (helps static analyzers)
        addr = getattr(location, "address", None)
        if addr:
            print(f"✓ Found: {addr}")
        else:
            print("✓ Found location (address not available)")
        print(f"✓ Coordinates: {location.latitude}, {location.longitude}")
        try:
            cache_set(coords, (location.latitude, location.longitude))
        except CacheError as e:
            print(e)
        return (location.latitude, location.longitude)

    raise ValueError(f"Could not find coordinates for {city}, {country}")


def _resample_icon(icon_img, target_px):
    """Resample an RGBA icon to target_px wide with PREMULTIPLIED alpha, so the
    silhouette does not fringe. Returns uint8 RGBA ready for imshow(interpolation="antialiased")."""
    arr = icon_img
    if arr.dtype != np.uint8:
        arr = (np.clip(arr, 0, 1) * 255).astype(np.uint8)
    if arr.shape[-1] != 4 or arr.shape[1] <= target_px:
        return arr
    from PIL import Image as _Image
    al = arr[..., 3:4].astype(np.float32) / 255.0
    pm = np.dstack([arr[..., :3].astype(np.float32) * al, arr[..., 3:4].astype(np.float32)]).astype(np.uint8)
    th = max(16, int(round(target_px * arr.shape[0] / arr.shape[1])))
    sm = np.asarray(_Image.fromarray(pm, "RGBA").resize((max(16, target_px), th), _Image.LANCZOS)).astype(np.float32)
    a2 = sm[..., 3:4] / 255.0
    rgb2 = np.divide(sm[..., :3], np.where(a2 == 0, 1.0, a2))
    return np.dstack([np.clip(rgb2, 0, 255), sm[..., 3:4]]).astype(np.uint8)


def get_crop_limits(g_proj, center_lat_lon, fig, dist):
    """
    Crop inward to preserve aspect ratio while guaranteeing
    full coverage of the requested radius.
    """
    lat, lon = center_lat_lon

    # Project center point into graph CRS
    center = (
        ox.projection.project_geometry(
            Point(lon, lat),
            crs="EPSG:4326",
            to_crs=g_proj.graph["crs"]
        )[0]
    )
    center_x, center_y = center.x, center.y

    fig_width, fig_height = fig.get_size_inches()
    aspect = fig_width / fig_height

    # Start from the *requested* radius
    half_x = dist
    half_y = dist

    # Cut inward to match aspect
    if aspect > 1:  # landscape → reduce height
        half_y = half_x / aspect
    else:  # portrait → reduce width
        half_x = half_y * aspect

    return (
        (center_x - half_x, center_x + half_x),
        (center_y - half_y, center_y + half_y),
    )


def _is_land_polygon(polygon, coastline_geom):
    """
    Determine if a polygon is land using the OSM coastline direction convention.

    In OpenStreetMap, coastlines are oriented with land on the LEFT and water
    on the RIGHT when following the direction of the way. This function checks
    which side of the nearest coastline segment a polygon falls on.

    Args:
        polygon: A Shapely Polygon to classify
        coastline_geom: The projected coastline geometry (LineString or MultiLineString)

    Returns:
        True if the polygon is on the land side, False if water
    """
    test_point = polygon.representative_point()

    # Find the nearest individual LineString segment
    if coastline_geom.geom_type == 'MultiLineString':
        nearest_line = min(coastline_geom.geoms, key=lambda l: l.distance(test_point))
    elif coastline_geom.geom_type == 'LineString':
        nearest_line = coastline_geom
    else:
        return False

    # Project test point onto the nearest coastline
    param = nearest_line.project(test_point)

    # Get local direction of coastline at the nearest point
    epsilon = 1.0  # 1 meter in projected CRS
    p1 = nearest_line.interpolate(max(0, param - epsilon))
    p2 = nearest_line.interpolate(min(nearest_line.length, param + epsilon))

    # Direction vector of coastline
    dx = p2.x - p1.x
    dy = p2.y - p1.y

    # Vector from coastline point to polygon test point
    nearest_point = nearest_line.interpolate(param)
    cx = test_point.x - nearest_point.x
    cy = test_point.y - nearest_point.y

    # Cross product: positive = left side = land, negative = right side = water
    cross = dx * cy - dy * cx
    return cross > 0


def build_sea_polygons(coastline_gdf, g_proj, crop_xlim, crop_ylim, center_lat_lon):
    """
    Build sea/ocean polygons from OSM coastline data.

    In OpenStreetMap, seas and oceans are defined by coastline lines rather
    than water polygons. This function converts coastline lines into renderable
    water polygons by splitting the viewport into land and water regions.

    Uses the OSM coastline direction convention (land on left, water on right)
    to correctly classify all land masses, even when multiple disconnected
    land polygons exist (e.g. Istanbul's European and Asian sides).

    Args:
        coastline_gdf: GeoDataFrame of coastline LineString features (or None)
        g_proj: Projected graph (used for CRS)
        crop_xlim: (xmin, xmax) tuple from get_crop_limits
        crop_ylim: (ymin, ymax) tuple from get_crop_limits
        center_lat_lon: (lat, lon) tuple of the map center

    Returns:
        GeoDataFrame of water polygons in the projected CRS, or None
    """
    if coastline_gdf is None or coastline_gdf.empty:
        return None

    crs = g_proj.graph["crs"]

    # Filter to line geometries only
    line_mask = coastline_gdf.geometry.type.isin(["LineString", "MultiLineString"])
    coast_lines = coastline_gdf[line_mask]
    if coast_lines.empty:
        return None

    # Project coastline to graph CRS
    try:
        coast_proj = ox.projection.project_gdf(coast_lines, to_crs=crs)
    except Exception:
        try:
            coast_proj = coast_lines.to_crs(crs)
        except Exception:
            return None

    # Build viewport rectangle from crop limits
    viewport = shapely_box(crop_xlim[0], crop_ylim[0], crop_xlim[1], crop_ylim[1])

    # Merge coastline fragments and clip to viewport
    merged = linemerge(list(coast_proj.geometry))
    clipped = merged.intersection(viewport)

    if clipped.is_empty:
        return None

    # Combine clipped coastline with viewport boundary to form closed regions
    combined = unary_union([clipped, viewport.boundary])

    # Create polygons from the line network
    polygons = list(polygonize(combined))
    if not polygons:
        return None

    # Classify each polygon using coastline direction convention.
    # OSM coastlines have land on the left, water on the right.
    water_polys = [p for p in polygons if not _is_land_polygon(p, clipped)]

    if not water_polys:
        return None

    return GeoDataFrame(geometry=water_polys, crs=crs)


def _find_covering_cache(prefix: str, suffix: str, dist: float) -> str | None:
    """
    Find a cache key at the same center whose distance covers the request.

    Cache keys embed the fetch distance; any cached entry at the same
    lat/lon with dist >= requested is a superset of the requested area
    (the axes crop trims the view), so it can be reused offline.

    Args:
        prefix: Key part before the distance (e.g. "graph_{lat}_{lon}_")
        suffix: Key part after the distance ("" for graphs, "_{tags}" for features)
        dist: Requested distance in meters

    Returns:
        The covering cache key with the smallest sufficient distance, or None
    """
    best = None
    file_suffix = f"{suffix}.pkl"
    for fname in os.listdir(CACHE_DIR):
        if not (fname.startswith(prefix) and fname.endswith(file_suffix)):
            continue
        middle = fname[len(prefix):len(fname) - len(file_suffix)]
        try:
            cached_dist = float(middle)
        except ValueError:
            continue
        if cached_dist >= dist and (best is None or cached_dist < best):
            best = cached_dist
    if best is None:
        return None
    return f"{prefix}{best}{suffix}"



# ---------------------------------------------------------------------------
# PBF backend: frame extracts with osmium, graph via osmnx graph_from_xml,
# features via DuckDB spatial (GDAL OSM driver). Same return types as the
# Overpass path, so everything downstream is untouched.
# ---------------------------------------------------------------------------
_PBF_WALKBIKE = {"footway", "path", "pedestrian", "steps", "cycleway", "track", "bridleway",
                 "corridor", "living_street", "service"}
# GDAL OSM driver (osmconf.ini defaults): which tags are real columns per layer;
# everything else lives in the hstore-text column other_tags.
_PBF_COLS = {
    "multipolygons": {"building", "landuse", "leisure", "natural", "amenity", "place", "tourism", "man_made", "water", "waterway"},
    "lines": {"highway", "waterway", "railway", "man_made", "barrier", "natural"},
}

def _pbf_bbox(point, dist):
    lat, lon = point
    dlat = dist / 111320.0
    dlon = dist / (111320.0 * max(0.05, math.cos(math.radians(lat))))
    return (lon - dlon, lat - dlat, lon + dlon, lat + dlat)   # W,S,E,N

def _pbf_frame(point, dist):
    """Cut (and cache) the frame extract from the planet file. Seconds per frame; reused forever."""
    import subprocess
    if not PBF_PATH or not os.path.exists(PBF_PATH):
        raise RuntimeError(f"MAPTOPOSTER_PBF not set or missing: '{PBF_PATH}'")
    w, s_, e, n = _pbf_bbox(point, dist)
    key = f"frame_{point[0]:.5f}_{point[1]:.5f}_{int(dist)}"
    os.makedirs("cache/pbf", exist_ok=True)
    out = f"cache/pbf/{key}.osm.pbf"
    if not os.path.exists(out):
        subprocess.run(["osmium", "extract", "--bbox", f"{w},{s_},{e},{n}", "-s", "smart",
                        "--overwrite", "-o", out, PBF_PATH], check=True, capture_output=True)
    return out, (w, s_, e, n)

def _pbf_graph(point, dist):
    import subprocess
    frame, (w, s_, e, n) = _pbf_frame(point, dist)
    roads = frame.replace(".osm.pbf", "-roads.osm.pbf")
    xml = frame.replace(".osm.pbf", "-roads.osm")
    if not os.path.exists(xml):
        # only highway ways (plus their nodes); otherwise graph_from_xml turns canals and parcels into edges
        subprocess.run(["osmium", "tags-filter", "--overwrite", "-o", roads, frame, "w/highway"], check=True, capture_output=True)
        subprocess.run(["osmium", "cat", "-f", "osm", "--overwrite", "-o", xml, roads], check=True, capture_output=True)
    g = ox.graph_from_xml(xml, simplify=False, retain_all=True)
    if NETWORK_TYPE == "drive":
        drop = [(u, v, k) for u, v, k, d in g.edges(keys=True, data=True)
                if (d.get("highway")[0] if isinstance(d.get("highway"), list) else d.get("highway")) in _PBF_WALKBIKE]
        g.remove_edges_from(drop)
        g.remove_nodes_from([nd for nd, deg in dict(g.degree()).items() if deg == 0])
    g = ox.truncate.truncate_graph_bbox(g, (w, s_, e, n), truncate_by_edge=True)
    return ox.simplify_graph(g) if not g.graph.get("simplified") else g

def _pbf_features(point, dist, tags):
    """Features from the frame extract via osmium (tags-filter + export). Complete and fast; GDAL's OSM
    driver was dropping most building relations in dense frames (Barcelona: 4.7k of 50k)."""
    import subprocess, json as _json
    from shapely.geometry import shape as _shape, box as _box
    frame, _ = _pbf_frame(point, dist)
    # osmium tags-filter expressions: ways+relations carrying the requested tags
    exprs = []
    for k, v in tags.items():
        if v is True:
            exprs.append(f"wr/{k}")
        else:
            exprs += [f"wr/{k}={x}" for x in ([v] if isinstance(v, str) else v)]
    import hashlib as _hl2
    tag_key = "_".join(sorted(tags)) + "-" + _hl2.md5(repr(sorted((k, v if isinstance(v, (str, bool)) else tuple(v)) for k, v in tags.items())).encode()).hexdigest()[:6]
    sub = frame.replace(".osm.pbf", f"-{tag_key}.osm.pbf")
    gj = frame.replace(".osm.pbf", f"-{tag_key}.geojsonseq")
    if not os.path.exists(gj):
        subprocess.run(["osmium", "tags-filter", "--overwrite", "-o", sub, frame, *exprs], check=True, capture_output=True)
        if "railway" in tags:   # transit is about visible infrastructure: drop tunnels (metros surface where they surface)
            sub2 = sub.replace(".osm.pbf", "-notunnel.osm.pbf")
            subprocess.run(["osmium", "tags-filter", "--overwrite", "-i", "-o", sub2, sub, "w/tunnel", "w/layer=-1", "w/layer=-2", "w/layer=-3"], check=True, capture_output=True)
            sub = sub2
        subprocess.run(["osmium", "export", "--overwrite", "-f", "geojsonseq", "--geometry-types=linestring,polygon",
                        "-o", gj, sub], check=True, capture_output=True)
    geoms = []
    with open(gj) as fh:
        for line in fh:
            line = line.strip().lstrip("\x1e")
            if not line:
                continue
            g = _json.loads(line).get("geometry")
            if g:
                geoms.append(_shape(g))
    if not geoms:
        return GeoDataFrame(geometry=[], crs="EPSG:4326")
    gdf = GeoDataFrame(geometry=geoms, crs="EPSG:4326")
    # Clip to the frame (+10 %): complete relations can span far beyond it. Validate first, explode after.
    w, s_, e, n = _pbf_bbox(point, dist * 1.1)
    gdf["geometry"] = gdf.geometry.make_valid()
    gdf["geometry"] = gdf.geometry.intersection(_box(w, s_, e, n))
    gdf = gdf[~gdf.geometry.is_empty].explode(index_parts=False)
    gdf = gdf[gdf.geometry.geom_type.isin(["Polygon", "LineString", "Point"])]
    return GeoDataFrame(gdf[~gdf.geometry.is_empty].reset_index(drop=True), crs="EPSG:4326")


def fetch_graph(point, dist) -> MultiDiGraph | None:
    """
    Fetch street network graph from OpenStreetMap.

    Uses caching to avoid redundant downloads. Fetches all network types
    within the specified distance from the center point.

    Args:
        point: (latitude, longitude) tuple for center point
        dist: Distance in meters from center point

    Returns:
        MultiDiGraph of street network, or None if fetch fails
    """
    lat, lon = point
    src = "pbf_" if DATA_SOURCE == "pbf" else ""
    net = "" if NETWORK_TYPE == "all" else f"{NETWORK_TYPE}_"   # the network type is part of what was fetched
    graph = f"{src}{net}graph_{lat}_{lon}_{dist}"
    cached = cache_get(graph)
    if cached is not None:
        print("✓ Using cached street network")
        return cast(MultiDiGraph, cached)

    covering = _find_covering_cache(f"{src}{net}graph_{lat}_{lon}_", "", dist)
    if covering:
        cached = cache_get(covering)
        if cached is not None:
            print("✓ Using cached street network (larger cached area)")
            return cast(MultiDiGraph, cached)

    try:
        g = _pbf_graph(point, dist) if DATA_SOURCE == "pbf" else ox.graph_from_point(point, dist=dist, dist_type='bbox', network_type=NETWORK_TYPE, truncate_by_edge=True)
        # Rate limit between requests
        time.sleep(0.5)
        try:
            cache_set(graph, g)
        except CacheError as e:
            print(e)
        return g
    except Exception as e:
        print(f"OSMnx error while fetching graph: {e}")
        return None


def fetch_features(point, dist, tags, name) -> GeoDataFrame | None:
    """
    Fetch geographic features (water, parks, etc.) from OpenStreetMap.

    Uses caching to avoid redundant downloads. Fetches features matching
    the specified OSM tags within distance from center point.

    Args:
        point: (latitude, longitude) tuple for center point
        dist: Distance in meters from center point
        tags: Dictionary of OSM tags to filter features
        name: Name for this feature type (for caching and logging)

    Returns:
        GeoDataFrame of features, or None if fetch fails
    """
    lat, lon = point
    import hashlib as _hl
    tag_str = "_".join(tags.keys()) + "_" + _hl.md5(repr(sorted((k, v if isinstance(v, (str, bool)) else tuple(v)) for k, v in tags.items())).encode()).hexdigest()[:6]
    src = "pbf_" if DATA_SOURCE == "pbf" else ""
    features = f"{src}{name}_{lat}_{lon}_{dist}_{tag_str}"
    cached = cache_get(features)
    if cached is not None:
        print(f"✓ Using cached {name}")
        return cast(GeoDataFrame, cached)

    covering = _find_covering_cache(f"{src}{name}_{lat}_{lon}_", f"_{tag_str}", dist)
    if covering:
        cached = cache_get(covering)
        if cached is not None:
            print(f"✓ Using cached {name} (larger cached area)")
            return cast(GeoDataFrame, cached)

    try:
        data = _pbf_features(point, dist, tags) if DATA_SOURCE == "pbf" else ox.features_from_point(point, tags=tags, dist=dist)
        # Rate limit between requests
        time.sleep(0.3)
        try:
            if not (DATA_SOURCE == "pbf" and (data is None or len(data) == 0)):
                cache_set(features, data)
            else:
                print(f"  (empty {name} from PBF, not cached)")
        except CacheError as e:
            print(e)
        return data
    except Exception as e:
        print(f"OSMnx error while fetching features: {e}")
        return None


def create_poster(
    city,
    country,
    point,
    dist,
    output_file,
    output_format,
    width=12,
    height=16,
    country_label=None,
    name_label=None,
    display_city=None,
    display_country=None,
    subtitle=None,
    dates=None,
    line_scale=1.0,
    marks=None,
    points=None,
    gpx_path=None,
    show_title=True,
    margin=False,
    buildings=False,
    mobility=False,
    icon_path=None,
    icon_size=None,
    fonts=None,
    no_gradient=False,
    title_scale=1.0,
    point_scale=1.0,
    no_attribution=False,
    edge_marks=False,
    coords_at=None,
    scalebar=False,
    stickers=None,
    sticker_size=None,
    sticker_mm=None,
    door_mm=None,
):
    """
    Generate a complete map poster with roads, water, parks, and typography.

    Creates a high-quality poster by fetching OSM data, rendering map layers,
    applying the current theme, and adding text labels with coordinates.

    Args:
        city: City name for display on poster
        country: Country name for display on poster
        point: (latitude, longitude) tuple for map center
        dist: Map radius in meters
        output_file: Path where poster will be saved
        output_format: File format ('png', 'svg', or 'pdf')
        width: Poster width in inches (default: 12)
        height: Poster height in inches (default: 16)
        country_label: Optional override for country text on poster
        _name_label: Optional override for city name (unused, reserved for future use)

    Raises:
        RuntimeError: If street network data cannot be retrieved
    """
    # Handle display names for i18n support
    # Priority: display_city/display_country > name_label/country_label > city/country
    display_city = display_city or name_label or city
    display_country = display_country or country_label or country

    print(f"\nGenerating map for {city}, {country}...")

    # Progress bar for data fetching
    with tqdm(
        total=6 + (1 if buildings else 0) + (1 if mobility else 0),
        desc="Fetching map data",
        unit="step",
        bar_format="{l_bar}{bar}| {n_fmt}/{total_fmt}",
    ) as pbar:
        # 1. Fetch Street Network
        pbar.set_description("Downloading street network")
        compensated_dist = dist * (max(height, width) / min(height, width)) / 4  # To compensate for viewport crop
        g = fetch_graph(point, compensated_dist)
        if g is None:
            raise RuntimeError("Failed to retrieve street network data.")
        pbar.update(1)

        # 2. Fetch Water Features
        pbar.set_description("Downloading water features")
        water = fetch_features(
            point,
            compensated_dist,
            tags={"natural": ["water", "bay", "strait"], "waterway": "riverbank"},
            name="water",
        )
        pbar.update(1)

        # 3. Fetch Parks
        pbar.set_description("Downloading parks/green spaces")
        parks = fetch_features(
            point,
            compensated_dist,
            tags={"leisure": "park", "landuse": "grass"},
            name="parks",
        )
        pbar.update(1)

        # 4. Fetch Forests/Woodland
        pbar.set_description("Downloading forests/woodland")
        forests = fetch_features(
            point,
            compensated_dist,
            tags={"landuse": "forest", "natural": "wood"},
            name="forests",
        )
        pbar.update(1)

        # 5. Fetch Rivers mapped as single lines (common for narrower city rivers)
        pbar.set_description("Downloading rivers")
        rivers = fetch_features(
            point,
            compensated_dist,
            tags={"waterway": "river"},
            name="rivers",
        )
        pbar.update(1)

        # 6. Fetch Coastline (seas/oceans are coastline ways in OSM, not water polygons)
        pbar.set_description("Downloading coastline data")
        coastline = fetch_features(
            point,
            compensated_dist,
            tags={"natural": "coastline"},
            name="coastline",
        )
        pbar.update(1)

        # 7. Fetch Buildings (optional figure-ground layer; heavy in dense cities)
        building_footprints = None
        if buildings:
            pbar.set_description("Downloading building footprints")
            building_footprints = fetch_features(
                point,
                compensated_dist,
                tags={"building": True},
                name="buildings",
            )
            pbar.update(1)

        # 8. Fetch tram/light-rail lines for the mobility layout
        transit_rails = None
        transit_heavy = None
        if mobility:
            pbar.set_description("Downloading tram/light-rail lines")
            transit_rails = fetch_features(
                point,
                compensated_dist,
                tags={"railway": TRANSIT_RAILWAYS},
                name="transit_rails",
            )
            transit_heavy = fetch_features(
                point,
                compensated_dist,
                tags={"railway": TRANSIT_HEAVY},
                name="transit_heavy",
            )
            pbar.update(1)

    print("✓ All data retrieved successfully!")

    # 2. Setup Plot
    print("Rendering map...")
    fig, ax = plt.subplots(figsize=(width, height), facecolor=THEME["bg"])
    ax.set_facecolor(THEME["bg"])
    if margin:
        # Gallery-mat layout: map inset with paper margin, bottom-weighted
        ax.set_position((0.11, 0.13, 0.78, 0.78))
    else:
        ax.set_position((0.0, 0.0, 1.0, 1.0))

    # Calculate scale factor based on smaller dimension (reference 12 inches)
    # This ensures text scales properly for both portrait and landscape orientations
    scale_factor = min(height, width) / 12.0

    # Project graph to a metric CRS so distances and aspect are linear (meters)
    g_proj = ox.project_graph(g)

    # Determine cropping limits early (needed for sea polygon construction)
    crop_xlim, crop_ylim = get_crop_limits(g_proj, point, fig, compensated_dist)

    # Build sea/ocean polygons from coastline data
    sea_polys = build_sea_polygons(coastline, g_proj, crop_xlim, crop_ylim, point)

    # 3. Plot Layers
    # Layer 0: Sea/ocean from coastline
    if sea_polys is not None and not sea_polys.empty:
        sea_polys.plot(ax=ax, facecolor=THEME['water'], edgecolor='none', zorder=0.4)

    # Layer 1: Polygons (filter to only plot polygon/multipolygon geometries, not points)
    if water is not None and not water.empty:
        # Filter to only polygon/multipolygon geometries to avoid point features showing as dots
        water_polys = water[water.geometry.type.isin(["Polygon", "MultiPolygon"])]
        if not water_polys.empty:
            # Project water features in the same CRS as the graph
            # Always the graph's CRS: auto-UTM by the layer's own centroid can pick a different zone
            water_polys = water_polys.to_crs(g_proj.graph['crs']) if water_polys.crs else ox.projection.project_gdf(water_polys)
            water_polys.plot(ax=ax, facecolor=THEME['water'], edgecolor='none', zorder=0.5)

    if rivers is not None and not rivers.empty:
        # Line geometries only: polygon riverbanks are already covered by the water layer
        rivers_lines = rivers[rivers.geometry.type.isin(["LineString", "MultiLineString"])]
        if not rivers_lines.empty:
            # Always the graph's CRS: auto-UTM by the layer's own centroid can pick a different zone
            rivers_lines = rivers_lines.to_crs(g_proj.graph['crs']) if rivers_lines.crs else ox.projection.project_gdf(rivers_lines)
            rivers_lines.plot(ax=ax, color=THEME['water'], linewidth=1.5 * line_scale, zorder=0.5)

    if forests is not None and not forests.empty:
        # Filter to only polygon/multipolygon geometries
        forests_polys = forests[forests.geometry.type.isin(["Polygon", "MultiPolygon"])]
        if not forests_polys.empty:
            # Project forest features in the same CRS as the graph
            # Always the graph's CRS: auto-UTM by the layer's own centroid can pick a different zone
            forests_polys = forests_polys.to_crs(g_proj.graph['crs']) if forests_polys.crs else ox.projection.project_gdf(forests_polys)
            # Use 'forests' color if in theme, otherwise use parks color
            forest_color = THEME.get('forests', THEME['parks'])
            forests_polys.plot(ax=ax, facecolor=forest_color, edgecolor='none', zorder=0.6)

    if parks is not None and not parks.empty:
        # Filter to only polygon/multipolygon geometries to avoid point features showing as dots
        parks_polys = parks[parks.geometry.type.isin(["Polygon", "MultiPolygon"])]
        if not parks_polys.empty:
            # Project park features in the same CRS as the graph
            # Always the graph's CRS: auto-UTM by the layer's own centroid can pick a different zone
            parks_polys = parks_polys.to_crs(g_proj.graph['crs']) if parks_polys.crs else ox.projection.project_gdf(parks_polys)
            parks_polys.plot(ax=ax, facecolor=THEME['parks'], edgecolor='none', zorder=0.8)

    if building_footprints is not None and not building_footprints.empty:
        bldg_polys = building_footprints[building_footprints.geometry.type.isin(["Polygon", "MultiPolygon"])]
        if not bldg_polys.empty:
            # Always the graph's CRS: auto-UTM by the layer's own centroid can pick a different zone
            bldg_polys = bldg_polys.to_crs(g_proj.graph['crs']) if bldg_polys.crs else ox.projection.project_gdf(bldg_polys)
            # Theme 'buildings' color, else a 12% text-into-bg blend for a quiet figure-ground tone
            bldg_color = THEME.get('buildings')
            if not bldg_color:
                bg_rgb = mcolors.to_rgb(THEME['bg'])
                tx_rgb = mcolors.to_rgb(THEME['text'])
                bldg_color = mcolors.to_hex(tuple(0.88 * b + 0.12 * t for b, t in zip(bg_rgb, tx_rgb)))
            bldg_polys.plot(ax=ax, facecolor=bldg_color,
                            edgecolor=THEME.get('buildings_edge', 'none'),
                            linewidth=0.3, zorder=0.9)
    # Layer 2: Roads with hierarchy coloring
    if mobility:
        print("Applying mobility-mode colors (sustainable modes lead)...")
        edge_colors = get_edge_colors_mobility(g_proj)
        edge_widths = [w * line_scale for w in get_edge_widths_mobility(g_proj)]
    else:
        print("Applying road hierarchy colors...")
        edge_colors = get_edge_colors_by_type(g_proj)
        edge_widths = [w * line_scale for w in get_edge_widths_by_type(g_proj)]

    # Plot the projected graph and then apply the cropped limits
    ox.plot_graph(
        g_proj, ax=ax, bgcolor=THEME['bg'],
        node_size=0,
        edge_color=edge_colors,
        edge_linewidth=edge_widths,
        show=False,
        close=False,
    )
    ax.set_aspect("equal", adjustable="box")
    ax.set_xlim(crop_xlim)
    ax.set_ylim(crop_ylim)

    # Layer 2.3: Tram/light-rail overlay for the mobility layout
    if mobility and transit_rails is not None and not transit_rails.empty:
        rail_lines = transit_rails[transit_rails.geometry.type.isin(["LineString", "MultiLineString"])]
        if not rail_lines.empty:
            # Always the graph's CRS: auto-UTM by the layer's own centroid can pick a different zone
            rail_lines = rail_lines.to_crs(g_proj.graph['crs']) if rail_lines.crs else ox.projection.project_gdf(rail_lines)
            rail_lines.plot(ax=ax, color=THEME.get("mode_transit", "#C05B3C"),
                            linewidth=1.8 * line_scale, zorder=2.5, alpha=0.95)
    if mobility and transit_heavy is not None and not transit_heavy.empty:
        heavy = transit_heavy[transit_heavy.geometry.type.isin(["LineString", "MultiLineString"])]
        if not heavy.empty:
            heavy = heavy.to_crs(g_proj.graph['crs']) if heavy.crs else ox.projection.project_gdf(heavy)
            # Heavy rail is infrastructure, trams are street life: same hue, half the weight
            # Rail corridors bundle 4-6 parallel tracks; keep each thin and light so the bundle stays a whisper
            # Scale-aware: a lone line at 2.5 km needs ~0.8 to register; a 6-track corridor at 7.5 km fuses above 0.45
            heavy_w = 0.45 * (7500.0 / max(1.0, crop_xlim[1] - crop_xlim[0])) ** 0.5
            heavy.plot(ax=ax, color=THEME.get("mode_transit", "#C05B3C"),
                       linewidth=heavy_w * line_scale, zorder=2.4, alpha=0.55)

    # Layer 2.4: Plain ring markers or a custom image icon — for minimal/series layouts
    if points:
        icon_img = None
        if icon_path:
            try:
                icon_img = plt.imread(icon_path)
            except Exception as e:
                print(f"⚠ Warning: could not load icon '{icon_path}': {e}")
        for pt_lat, pt_lon in points:
            try:
                pt = ox.projection.project_geometry(
                    Point(pt_lon, pt_lat),
                    crs="EPSG:4326",
                    to_crs=g_proj.graph["crs"]
                )[0]
                if icon_img is not None:
                    # Icon sized in ground meters: identical physical size across a
                    # same-scale series. Bottom edge anchored at the point.
                    win_w = crop_xlim[1] - crop_xlim[0]
                    w = icon_size if icon_size else 0.10 * win_w
                    h = w * icon_img.shape[0] / icon_img.shape[1]
                    # Resample the icon ourselves, with PREMULTIPLIED alpha. Letting
                    # imshow do it unpremultiplied (especially with lanczos, whose
                    # negative lobes ring on a hard silhouette) fringes the cutout.
                    draw_img = icon_img
                    if draw_img.shape[-1] == 4:
                        arr = draw_img
                        if arr.dtype != np.uint8:
                            arr = (np.clip(arr, 0, 1) * 255).astype(np.uint8)
                        ax_px_w = ax.get_position().width * fig.get_size_inches()[0] * 300
                        target_px = max(16, int(round(ax_px_w * (w / win_w))))
                        if arr.shape[1] > target_px:
                            from PIL import Image as _Image
                            al = arr[..., 3:4].astype(np.float32) / 255.0
                            pm = np.dstack([
                                (arr[..., :3].astype(np.float32) * al),
                                arr[..., 3:4].astype(np.float32),
                            ]).astype(np.uint8)
                            th = max(16, int(round(target_px * arr.shape[0] / arr.shape[1])))
                            sm = np.asarray(
                                _Image.fromarray(pm, "RGBA").resize(
                                    (target_px, th), _Image.LANCZOS
                                )
                            ).astype(np.float32)
                            a2 = sm[..., 3:4] / 255.0
                            rgb2 = np.divide(sm[..., :3], np.where(a2 == 0, 1.0, a2))
                            draw_img = np.dstack([
                                np.clip(rgb2, 0, 255), sm[..., 3:4]
                            ]).astype(np.uint8)
                    ax.imshow(
                        draw_img,
                        extent=(pt.x - w / 2, pt.x + w / 2, pt.y, pt.y + h),
                        zorder=9.5,
                        interpolation="antialiased",
                    )
                    print(f"✓ Added icon at ({pt_lat}, {pt_lon}), {w:.0f}m wide")
                else:
                    ax.scatter(pt.x, pt.y, s=340 * scale_factor**2 * point_scale**2, facecolor=THEME["bg"],
                               edgecolor=THEME["text"], linewidth=2.5 * scale_factor * point_scale, zorder=9)
                    ax.scatter(pt.x, pt.y, s=90 * scale_factor**2 * point_scale**2, color=THEME["text"], zorder=9.1)
                    print(f"✓ Added point marker at ({pt_lat}, {pt_lon})")
            except Exception as e:
                print(f"⚠ Warning: Could not plot point: {e}")

    # Layer 2.5: Custom markers with text labels
    if marks:
        for mark_lat, mark_lon, mark_text, mark_pos in marks:
            try:
                # Project marker point to map CRS
                mark_pt = ox.projection.project_geometry(
                    Point(mark_lon, mark_lat),
                    crs="EPSG:4326",
                    to_crs=g_proj.graph["crs"]
                )[0]

                inside = (
                    crop_xlim[0] <= mark_pt.x <= crop_xlim[1]
                    and crop_ylim[0] <= mark_pt.y <= crop_ylim[1]
                )

                if not inside and edge_marks:
                    # Direction stone: clamp the mark to the frame edge, point at it,
                    # and state how far away it really is.
                    cx = (crop_xlim[0] + crop_xlim[1]) / 2
                    cy = (crop_ylim[0] + crop_ylim[1]) / 2
                    win_w = crop_xlim[1] - crop_xlim[0]
                    win_h = crop_ylim[1] - crop_ylim[0]
                    inset = 0.07 * win_w
                    vx, vy = mark_pt.x - cx, mark_pt.y - cy
                    t = min(
                        (win_w / 2 - inset) / abs(vx) if vx else float("inf"),
                        (win_h / 2 - inset) / abs(vy) if vy else float("inf"),
                    )
                    ex, ey = cx + vx * t, cy + vy * t
                    angle = math.degrees(math.atan2(-vx, vy))

                    _, _, ground = pyproj.Geod(ellps="WGS84").inv(
                        point[1], point[0], mark_lon, mark_lat
                    )

                    edge_fonts = fonts or FONTS
                    if edge_fonts:
                        font_edge = FontProperties(
                            fname=edge_fonts["bold"], size=11 * scale_factor
                        )
                    else:
                        font_edge = FontProperties(
                            family="monospace", weight="bold", size=11 * scale_factor
                        )

                    ax.plot(
                        ex, ey,
                        marker=(3, 0, angle),
                        color=THEME["text"],
                        markersize=12 * scale_factor,
                        zorder=10,
                    )

                    norm = abs(vx) + abs(vy)
                    off = 0.05 * win_w
                    tx, ty = ex - vx / norm * off, ey - vy / norm * off
                    if abs(vx) >= abs(vy):
                        ha, va = ("right" if vx > 0 else "left"), "center"
                    else:
                        ha, va = "center", ("top" if vy > 0 else "bottom")

                    ax.text(
                        tx, ty,
                        f"{mark_text} \u00b7 {ground / 1000:.1f} km",
                        color=THEME["bg"],
                        ha=ha, va=va,
                        bbox=dict(
                            facecolor=THEME["text"],
                            alpha=0.8,
                            edgecolor="none",
                            boxstyle="round,pad=0.4",
                        ),
                        fontproperties=font_edge,
                        zorder=10,
                    )
                    print(
                        f"\u2192 Edge mark '{mark_text}' at {ground / 1000:.1f} km "
                        f"(outside the frame, clamped to the border)"
                    )
                    continue

                # Draw the marker
                ax.plot(
                    mark_pt.x,
                    mark_pt.y,
                    marker='o',
                    color=THEME["text"],
                    markersize=8 * scale_factor,
                    zorder=9,
                )

                # Determine font for marker
                active_marker_fonts = fonts or FONTS
                if active_marker_fonts:
                    font_marker = FontProperties(
                        fname=active_marker_fonts["bold"], size=12 * scale_factor
                    )
                else:
                    font_marker = FontProperties(
                        family="monospace", weight="bold", size=12 * scale_factor
                    )

                # Label offset relative to the viewport so it holds at any --distance
                offset_val = 0.04 * (crop_xlim[1] - crop_xlim[0])
                pos_map = {
                    'right':       (offset_val, 0, 'left', 'center'),
                    'left':        (-offset_val, 0, 'right', 'center'),
                    'top':         (0, offset_val, 'center', 'bottom'),
                    'bottom':      (0, -offset_val, 'center', 'top'),
                    'topright':    (offset_val, offset_val, 'left', 'bottom'),
                    'topleft':     (-offset_val, offset_val, 'right', 'bottom'),
                    'bottomright': (offset_val, -offset_val, 'left', 'top'),
                    'bottomleft':  (-offset_val, -offset_val, 'right', 'top'),
                }
                dx, dy, ha, va = pos_map.get(mark_pos, pos_map['topright'])

                # Draw the text label next to the marker
                ax.text(
                    mark_pt.x + dx,
                    mark_pt.y + dy,
                    mark_text,
                    color=THEME["bg"],
                    ha=ha,
                    va=va,
                    bbox=dict(
                        facecolor=THEME["text"],
                        alpha=0.8,
                        edgecolor='none',
                        boxstyle='round,pad=0.4',
                    ),
                    fontproperties=font_marker,
                    zorder=10,
                )
                print(f"✓ Added marker at ({mark_lat}, {mark_lon}) with text '{mark_text}' aligned '{mark_pos}'")
            except Exception as e:
                print(f"⚠ Warning: Could not plot marker: {e}")

    # Layer 2.6: Stickers, a PNG per location. Inside the frame: drawn centred on the
    # point with its label below. Outside the frame (with --edge-marks): the sticker
    # replaces the triangle at the clamped edge position, label carries the distance.
    if stickers:
        win_w = crop_xlim[1] - crop_xlim[0]
        win_h = crop_ylim[1] - crop_ylim[0]
        cx = (crop_xlim[0] + crop_xlim[1]) / 2
        cy = (crop_ylim[0] + crop_ylim[1]) / 2
        if sticker_mm:
            # Size on PAPER: the map inset spans 78 % of the sheet width, so mm -> ground metres per frame
            sheet_mm = fig.get_size_inches()[0] * 25.4
            s_w = sticker_mm / (0.78 * sheet_mm) * win_w
        else:
            s_w = sticker_size if sticker_size else 0.08 * win_w
        ax_px_w = ax.get_position().width * fig.get_size_inches()[0] * 300
        target_px = max(16, int(round(ax_px_w * (s_w / win_w))))
        st_fonts = fonts or FONTS
        # Edge-anchor labels share the footer's voice: light weight, small, letter-spaced, muted ink
        font_st = (FontProperties(fname=st_fonts["light"], size=7 * scale_factor) if st_fonts
                   else FontProperties(family="monospace", size=7 * scale_factor))
        for idx, (st_lat, st_lon, st_path, st_text) in enumerate(stickers):
            try:
                if idx == 0 and door_mm:   # the first sticker is the door; it may have its own paper size (e.g. a photo cutout)
                    sheet_mm = fig.get_size_inches()[0] * 25.4
                    s_w = door_mm / (0.78 * sheet_mm) * win_w
                    target_px = max(16, int(round(ax_px_w * (s_w / win_w))))
                elif idx == 1 and door_mm:  # back to the mark size for the rest
                    s_w = sticker_mm / (0.78 * fig.get_size_inches()[0] * 25.4) * win_w if sticker_mm else (sticker_size if sticker_size else 0.08 * win_w)
                    target_px = max(16, int(round(ax_px_w * (s_w / win_w))))
                img = _resample_icon(plt.imread(st_path), target_px)
            except Exception as e:
                print(f"\u26a0 Warning: could not load sticker '{st_path}': {e}")
                continue
            try:
                p = ox.projection.project_geometry(Point(st_lon, st_lat), crs="EPSG:4326",
                                                   to_crs=g_proj.graph["crs"])[0]
                inside = crop_xlim[0] <= p.x <= crop_xlim[1] and crop_ylim[0] <= p.y <= crop_ylim[1]
                if not inside and not edge_marks:
                    continue
                if inside:
                    x, y, label = p.x, p.y, st_text
                else:
                    inset = 0.07 * win_w + s_w / 2
                    vx, vy = p.x - cx, p.y - cy
                    t = min((win_w / 2 - inset) / abs(vx) if vx else float("inf"),
                            (win_h / 2 - inset) / abs(vy) if vy else float("inf"))
                    x, y = cx + vx * t, cy + vy * t
                    _, _, ground = pyproj.Geod(ellps="WGS84").inv(point[1], point[0], st_lon, st_lat)
                    # An edge anchor without its distance reads as "here" when it means "that way"
                    label = f"{st_text} \u00b7 {ground / 1000:.1f} km" if st_text else f"{ground / 1000:.1f} km"
                s_h = s_w * img.shape[0] / img.shape[1]
                ax.imshow(img, extent=(x - s_w / 2, x + s_w / 2, y - s_h / 2, y + s_h / 2),
                          zorder=9.6, interpolation="antialiased")
                if label:
                    ax.text(x, y - s_h / 2 - 0.012 * win_w, " ".join(label), color=THEME["text"], alpha=0.75,
                            ha="center", va="top", fontproperties=font_st, zorder=10)
                print(f"\u2713 Sticker '{st_text}' {'in frame' if inside else 'on the edge'}")
            except Exception as e:
                print(f"\u26a0 Warning: could not plot sticker '{st_text}': {e}")

    # Layer 2.7: GPX Route overlay
    if gpx_path:
        trackpoints = parse_gpx(gpx_path)
        if trackpoints:
            # The graph is in a projected CRS - transform lat/lon to the same CRS
            crs_proj = g_proj.graph['crs']
            transformer = pyproj.Transformer.from_crs("EPSG:4326", crs_proj, always_xy=True)

            route_x = []
            route_y = []
            for lat, lon in trackpoints:
                x, y = transformer.transform(lon, lat)
                route_x.append(x)
                route_y.append(y)

            route_color = THEME.get('route_color', THEME.get('text', '#E74C3C'))
            route_width = 5.0 * scale_factor
            ax.plot(route_x, route_y, color=route_color, linewidth=route_width,
                    solid_capstyle='round', solid_joinstyle='round', zorder=8, alpha=0.9)
            print(f"✓ GPX route rendered ({len(trackpoints)} trackpoints)")

    # Layer 3: Gradients (Top and Bottom) — only for the classic full-bleed title layout
    if show_title and not margin and not no_gradient:
        create_gradient_fade(ax, THEME['gradient_color'], location='bottom', zorder=10)
        create_gradient_fade(ax, THEME['gradient_color'], location='top', zorder=10)

    # Base font sizes (at 12 inches width)
    base_main = 72 * title_scale
    base_sub = 27 * title_scale
    base_coords = 18 * title_scale
    base_attr = 8

    # 4. Typography - use custom fonts if provided, otherwise use default FONTS
    active_fonts = fonts or FONTS
    if active_fonts:
        # font_main is calculated dynamically later based on length
        font_sub = FontProperties(
            fname=active_fonts["light"], size=base_sub * scale_factor
        )
        font_coords = FontProperties(
            fname=active_fonts["regular"], size=base_coords * scale_factor
        )
        font_attr = FontProperties(
            fname=active_fonts["light"], size=base_attr * scale_factor
        )
    else:
        # Fallback to system fonts
        font_sub = FontProperties(
            family="monospace", weight="normal", size=base_sub * scale_factor
        )
        font_coords = FontProperties(
            family="monospace", size=base_coords * scale_factor
        )
        font_attr = FontProperties(family="monospace", size=base_attr * scale_factor)

    # Format city name based on script type
    # Latin scripts: apply uppercase and letter spacing for aesthetic
    # Non-Latin scripts (CJK, Thai, Arabic, etc.): no spacing, preserve case structure
    if is_latin_script(display_city):
        # Latin script: uppercase with letter spacing (e.g., "P  A  R  I  S")
        spaced_city = "  ".join(list(display_city.upper()))
    else:
        # Non-Latin script: no spacing, no forced uppercase
        # For scripts like Arabic, Thai, Japanese, etc.
        spaced_city = display_city

    # Dynamically adjust font size based on city name length to prevent truncation
    # We use the already scaled "main" font size as the starting point.
    base_adjusted_main = base_main * scale_factor
    city_char_count = len(display_city)

    # Heuristic: If length is > 10, start reducing.
    if city_char_count > 10:
        length_factor = 10 / city_char_count
        adjusted_font_size = max(base_adjusted_main * length_factor, 10 * scale_factor)
    else:
        adjusted_font_size = base_adjusted_main

    if active_fonts:
        font_main_adjusted = FontProperties(
            fname=active_fonts["bold"], size=adjusted_font_size
        )
    else:
        font_main_adjusted = FontProperties(
            family="monospace", weight="bold", size=adjusted_font_size
        )

    # --- BOTTOM TEXT ---
    if show_title:
        ax.text(
            0.5,
            0.168,
            spaced_city,
            transform=ax.transAxes,
            color=THEME["text"],
            ha="center",
            fontproperties=font_main_adjusted,
            zorder=11,
        )

        ax.text(
            0.5,
            0.12,
            display_country.upper(),
            transform=ax.transAxes,
            color=THEME["text"],
            ha="center",
            fontproperties=font_sub,
            zorder=11,
        )

        # Optional subtitle and dates
        # Adjust y-positions based on what's present
        if subtitle and dates:
            # Both subtitle and dates
            ax.text(0.5, 0.084, subtitle, transform=ax.transAxes, color=THEME["text"],
                    alpha=0.8, ha="center", fontproperties=font_coords, zorder=11)
            ax.text(0.5, 0.06, dates, transform=ax.transAxes, color=THEME["text"],
                    alpha=0.6, ha="center", fontproperties=font_coords, zorder=11)
            coords_y = 0.036
        elif subtitle:
            # Only subtitle
            ax.text(0.5, 0.084, subtitle, transform=ax.transAxes, color=THEME["text"],
                    alpha=0.8, ha="center", fontproperties=font_coords, zorder=11)
            coords_y = 0.054
        elif dates:
            # Only dates
            ax.text(0.5, 0.084, dates, transform=ax.transAxes, color=THEME["text"],
                    alpha=0.6, ha="center", fontproperties=font_coords, zorder=11)
            coords_y = 0.054
        else:
            coords_y = 0.084
    else:
        # Minimal layout: coordinates are the only typography
        coords_y = 0.05

    lat, lon = point
    if coords_at:
        lat, lon = coords_at   # footer prints the door, not a recentred frame

    lat_hemi = "N" if lat >= 0 else "S"
    lon_hemi = "E" if lon >= 0 else "W"

    if margin:
        # Letter-spaced coordinates set in the mat below the map
        lat_txt = " ".join(f"{abs(lat):.4f}°{lat_hemi}")
        lon_txt = " ".join(f"{abs(lon):.4f}°{lon_hemi}")
        fig.text(
            0.5,
            0.062,
            f"{lat_txt}     {lon_txt}",
            color=THEME["text"],
            alpha=0.7,
            ha="center",
            fontproperties=font_coords,
        )
    else:
        coords = f"{abs(lat):.4f}° {lat_hemi} / {abs(lon):.4f}° {lon_hemi}"
        ax.text(
            0.5,
            coords_y,
            coords,
            transform=ax.transAxes,
            color=THEME["text"],
            alpha=0.7,
            ha="center",
            fontproperties=font_coords,
            zorder=11,
        )

    if show_title:
        ax.plot(
            [0.4, 0.6],
            [0.15, 0.15],
            transform=ax.transAxes,
            color=THEME["text"],
            linewidth=1 * scale_factor,
            zorder=11,
        )

    # --- ATTRIBUTION (bottom right) ---
    if FONTS:
        font_attr = FontProperties(fname=FONTS["light"], size=8)
    else:
        font_attr = FontProperties(family="monospace", size=8)

    if margin:
        # In the mat, right-aligned with the map's right edge
        if not no_attribution:
            fig.text(
                0.89,
                0.025,
                "© OpenStreetMap contributors",
                color=THEME["text"],
                alpha=0.5,
                ha="right",
                va="bottom",
                fontproperties=font_attr,
            )
        if scalebar:
            # Scale bar in the mat, bottom right: a round length close to a fifth of the frame width
            # Fixed bar, honest label: the same bar on every poster (a fifth of the map width),
            # labelled with the distance it spans, so a series at mixed scales reads as one system.
            win_m = crop_xlim[1] - crop_xlim[0]
            nice = win_m / 5
            bar_w = 0.78 / 5
            x1, y = 0.89, 0.030   # below the coordinate line, on the attribution baseline
            x0 = x1 - bar_w
            fig.add_artist(plt.Line2D([x0, x1], [y, y], transform=fig.transFigure, color=THEME["text"], alpha=0.6, linewidth=1.2 * scale_factor))
            for xx in (x0, x1):
                fig.add_artist(plt.Line2D([xx, xx], [y - 0.006, y + 0.006], transform=fig.transFigure, color=THEME["text"], alpha=0.6, linewidth=1.2 * scale_factor))
            label = f"{nice/1000:g} km" if nice >= 1000 else f"{nice:.0f} m"
            fig.text(x0 - 0.008, y, label, color=THEME["text"], alpha=0.6,
                     ha="right", va="center", fontproperties=font_attr)
        # Hairline keyline around the map inset
        fig.add_artist(plt.Rectangle(
            (0.11, 0.13), 0.78, 0.78,
            transform=fig.transFigure, fill=False,
            edgecolor=THEME["text"], alpha=0.3,
            linewidth=1 * scale_factor,
        ))
    elif not no_attribution:
        ax.text(
            0.98,
            0.02,
            "© OpenStreetMap contributors",
            transform=ax.transAxes,
            color=THEME["text"],
            alpha=0.5,
            ha="right",
            va="bottom",
            fontproperties=font_attr,
            zorder=11,
        )

    # 5. Save
    print(f"Saving to {output_file}...")

    fmt = output_format.lower()
    # Fixed canvas: no tight-bbox trimming, so every poster in a series has
    # exactly width*dpi x height*dpi pixels regardless of text or markers
    save_kwargs = dict(
        facecolor=THEME["bg"],
        bbox_inches=None,
    )

    # DPI matters mainly for raster formats
    if fmt == "png":
        save_kwargs["dpi"] = 300

    plt.savefig(output_file, format=fmt, **save_kwargs)

    plt.close()
    print(f"✓ Done! Poster saved as {output_file}")


def print_examples():
    """Print usage examples."""
    print("""
City Map Poster Generator
=========================

Usage:
  python create_map_poster.py --city <city> --country <country> [options]

Examples:
  # Iconic grid patterns
  python create_map_poster.py -c "New York" -C "USA" -t noir -d 12000           # Manhattan grid
  python create_map_poster.py -c "Barcelona" -C "Spain" -t warm_beige -d 8000   # Eixample district grid

  # Waterfront & canals
  python create_map_poster.py -c "Venice" -C "Italy" -t blueprint -d 4000       # Canal network
  python create_map_poster.py -c "Amsterdam" -C "Netherlands" -t ocean -d 6000  # Concentric canals
  python create_map_poster.py -c "Dubai" -C "UAE" -t midnight_blue -d 15000     # Palm & coastline

  # Radial patterns
  python create_map_poster.py -c "Paris" -C "France" -t pastel_dream -d 10000   # Haussmann boulevards
  python create_map_poster.py -c "Moscow" -C "Russia" -t noir -d 12000          # Ring roads

  # Organic old cities
  python create_map_poster.py -c "Tokyo" -C "Japan" -t japanese_ink -d 15000    # Dense organic streets
  python create_map_poster.py -c "Marrakech" -C "Morocco" -t terracotta -d 5000 # Medina maze
  python create_map_poster.py -c "Rome" -C "Italy" -t warm_beige -d 8000        # Ancient street layout

  # Coastal cities
  python create_map_poster.py -c "San Francisco" -C "USA" -t sunset -d 10000    # Peninsula grid
  python create_map_poster.py -c "Sydney" -C "Australia" -t ocean -d 12000      # Harbor city
  python create_map_poster.py -c "Mumbai" -C "India" -t contrast_zones -d 18000 # Coastal peninsula

  # River cities
  python create_map_poster.py -c "London" -C "UK" -t noir -d 15000              # Thames curves
  python create_map_poster.py -c "Budapest" -C "Hungary" -t copper_patina -d 8000  # Danube split

  # List themes
  python create_map_poster.py --list-themes

Options:
  --city, -c        City name (required)
  --country, -C     Country name (required)
  --country-label   Override country text displayed on poster
  --theme, -t       Theme name (default: terracotta)
  --all-themes      Generate posters for all themes
  --distance, -d    Map radius in meters (default: 18000)
  --list-themes     List all available themes

Distance guide:
  4000-6000m   Small/dense cities (Venice, Amsterdam old center)
  8000-12000m  Medium cities, focused downtown (Paris, Barcelona)
  15000-20000m Large metros, full city view (Tokyo, Mumbai)

Available themes can be found in the 'themes/' directory.
Generated posters are saved to 'posters/' directory.
""")


def list_themes():
    """List all available themes with descriptions."""
    available_themes = get_available_themes()
    if not available_themes:
        print("No themes found in 'themes/' directory.")
        return

    print("\nAvailable Themes:")
    print("-" * 60)
    for theme_name in available_themes:
        theme_path = os.path.join(THEMES_DIR, f"{theme_name}.json")
        try:
            with open(theme_path, "r", encoding=FILE_ENCODING) as f:
                theme_data = json.load(f)
                display_name = theme_data.get('name', theme_name)
                description = theme_data.get('description', '')
        except (OSError, json.JSONDecodeError):
            display_name = theme_name
            description = ""
        print(f"  {theme_name}")
        print(f"    {display_name}")
        if description:
            print(f"    {description}")
        print()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Generate beautiful map posters for any city",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python create_map_poster.py --city "New York" --country "USA"
  python create_map_poster.py --city "New York" --country "USA" -l 40.776676 -73.971321 --theme neon_cyberpunk
  python create_map_poster.py --city Tokyo --country Japan --theme midnight_blue
  python create_map_poster.py --city Paris --country France --theme noir --distance 15000
  python create_map_poster.py --list-themes
        """,
    )

    parser.add_argument("--city", "-c", type=str, help="City name")
    parser.add_argument("--country", "-C", type=str, help="Country name")
    parser.add_argument(
        "--latitude",
        "-lat",
        dest="latitude",
        type=str,
        help="Override latitude center point",
    )
    parser.add_argument(
        "--longitude",
        "-long",
        dest="longitude",
        type=str,
        help="Override longitude center point",
    )
    parser.add_argument(
        "--country-label",
        dest="country_label",
        type=str,
        help="Override country text displayed on poster",
    )
    parser.add_argument(
        "--theme",
        "-t",
        type=str,
        default="terracotta",
        help="Theme name (default: terracotta)",
    )
    parser.add_argument(
        "--all-themes",
        "--All-themes",
        dest="all_themes",
        action="store_true",
        help="Generate posters for all themes",
    )
    parser.add_argument(
        "--distance",
        "-d",
        type=int,
        default=18000,
        help="Map radius in meters (default: 18000)",
    )
    parser.add_argument(
        "--width",
        "-W",
        type=float,
        default=12,
        help="Image width in inches (default: 12, max: 20 )",
    )
    parser.add_argument(
        "--height",
        "-H",
        type=float,
        default=16,
        help="Image height in inches (default: 16, max: 20)",
    )
    parser.add_argument(
        "--list-themes", action="store_true", help="List all available themes"
    )
    parser.add_argument(
        "--display-city",
        "-dc",
        type=str,
        help="Custom display name for city (for i18n support)",
    )
    parser.add_argument(
        "--display-country",
        "-dC",
        type=str,
        help="Custom display name for country (for i18n support)",
    )
    parser.add_argument(
        "--subtitle",
        "-s",
        type=str,
        help="Optional subtitle below country name (e.g., 'Historic City Center')",
    )
    parser.add_argument(
        "--dates",
        type=str,
        help="Optional date range (e.g., 'Dec 29-31, 2025')",
    )
    parser.add_argument(
        "--mark",
        nargs="+",
        action="append",
        help="Mark a location: --mark <lat,lon> <Text> <position> (repeatable; positions: left/right/top/bottom/topleft/topright/bottomleft/bottomright)",
    )
    parser.add_argument(
        "--point",
        action="append",
        help="Plain ring marker without label: --point <lat,lon> (repeatable)",
    )
    parser.add_argument(
        "--icon",
        type=str,
        help="PNG image (with alpha) drawn at each --point instead of the ring",
    )
    parser.add_argument(
        "--icon-size",
        type=float,
        help="Icon width in ground meters (default: 10%% of the map window width)",
    )
    parser.add_argument(
        "--no-title",
        action="store_true",
        help="Minimal layout: no city/country title block, coordinates only",
    )
    parser.add_argument(
        "--margin",
        action="store_true",
        help="Gallery-mat layout: map inset with paper margin, letter-spaced coordinates below (implies --no-title)",
    )
    parser.add_argument(
        "--buildings",
        action="store_true",
        help="Render OSM building footprints as a quiet figure-ground layer (heavier download)",
    )
    parser.add_argument(
        "--mobility",
        action="store_true",
        help="Sustainable-mobility layout: cycleways/footpaths/transit lead, car network recedes; adds tram/light-rail overlay",
    )
    parser.add_argument(
        "--line-scale",
        "-ls",
        type=float,
        default=1.0,
        help="Multiplier for road line thickness (default: 1.0, try 2-4 for small areas)",
    )
    parser.add_argument(
        "--font-family",
        type=str,
        help='Google Fonts family name (e.g., "Noto Sans JP", "Open Sans"). If not specified, uses local Roboto fonts.',
    )
    parser.add_argument(
        "--format",
        "-f",
        default="png",
        choices=["png", "svg", "pdf"],
        help="Output format for the poster (default: png)",
    )
    parser.add_argument(
        "--output-directory",
        "-o",
        default=POSTERS_DIR,
        help=f"Output directory for the poster (default: {POSTERS_DIR})",
    )
    parser.add_argument(
        "--no-gradient",
        dest="no_gradient",
        action="store_true",
        help="Disable the top/bottom gradient fades while keeping the title block",
    )
    parser.add_argument(
        "--title-scale",
        dest="title_scale",
        type=float,
        default=1.0,
        help="Multiplier for title/subtitle/coordinate type size (default: 1.0)",
    )
    parser.add_argument(
        "--point-scale",
        dest="point_scale",
        type=float,
        default=1.0,
        help="Multiplier for --point ring marker size (default: 1.0)",
    )
    parser.add_argument(
        "--edge-marks",
        dest="edge_marks",
        action="store_true",
        help=(
            "For --mark locations outside the frame: draw a direction arrow on the "
            "frame edge with the label and real distance, instead of dropping them"
        ),
    )
    parser.add_argument(
        "--sticker",
        dest="stickers",
        nargs=3,
        action="append",
        metavar=("LAT,LON", "PNG", "TEXT"),
        help="Repeatable: a PNG sticker at a location, with a label. Sized in ground "
             "metres (--sticker-size, default 8%% of the frame). Outside the frame it "
             "takes the edge position when --edge-marks is on.",
    )
    parser.add_argument("--door-mm", dest="door_mm", type=float, default=None, help="Paper size in mm for the FIRST sticker (the door), e.g. a larger photo cutout")
    parser.add_argument("--sticker-mm", dest="sticker_mm", type=float, default=None,
        help="Sticker width in millimetres ON PAPER (relative to the rendered sheet size); overrides --sticker-size. "
             "Keeps discs identical across posters of different scales.")
    parser.add_argument(
        "--sticker-size", dest="sticker_size", type=float, default=None,
        help="Sticker width in ground metres (default: 8%% of the frame width)",
    )
    parser.add_argument(
        "--coords-at", dest="coords_at", default=None, metavar="LAT,LON",
        help="Coordinates printed in the footer (default: frame centre). Use the door when the frame is recentred.",
    )
    parser.add_argument("--scalebar", dest="scalebar", action="store_true", help="Draw a scale bar bottom-right in the mat")
    parser.add_argument(
        "--no-attribution",
        dest="no_attribution",
        action="store_true",
        help=(
            "Omit the '© OpenStreetMap contributors' credit. ODbL still requires "
            "attribution for any produced work you publish, distribute or sell; use "
            "this only for private prints."
        ),
    )
    parser.add_argument(
        "--gpx",
        type=str,
        help="Path to a GPX file to overlay a travel route on the map",
    )

    args = parser.parse_args()

    # If no arguments provided, show examples
    if len(sys.argv) == 1:
        print_examples()
        sys.exit(0)

    # List themes if requested
    if args.list_themes:
        list_themes()
        sys.exit(0)

    # Validate required arguments
    if not args.city or not args.country:
        print("Error: --city and --country are required.\n")
        print_examples()
        sys.exit(1)

    # Redirect poster output if requested
    if args.output_directory:
        POSTERS_DIR = os.path.expanduser(args.output_directory)

    # Enforce maximum dimensions
    if args.width > 20:
        print(
            f"⚠ Width {args.width} exceeds the maximum allowed limit of 20. It's enforced as max limit 20."
        )
        args.width = 20.0
    if args.height > 20:
        print(
            f"⚠ Height {args.height} exceeds the maximum allowed limit of 20. It's enforced as max limit 20."
        )
        args.height = 20.0

    available_themes = get_available_themes()
    if not available_themes:
        print("No themes found in 'themes/' directory.")
        sys.exit(1)

    if args.all_themes:
        themes_to_generate = available_themes
    else:
        if args.theme not in available_themes:
            print(f"Error: Theme '{args.theme}' not found.")
            print(f"Available themes: {', '.join(available_themes)}")
            sys.exit(1)
        themes_to_generate = [args.theme]

    # Parse plain point markers
    points_data = []
    if args.point:
        for pt_arg in args.point:
            try:
                lat_str, lon_str = pt_arg.split(",")
                points_data.append((float(lat_str.strip()), float(lon_str.strip())))
            except ValueError:
                print("Error: --point expects <lat,lon>. Example: --point 40.71,-74.00")
                sys.exit(1)

    # Parse Marker Arguments (support multiple marks)
    marks_data = []
    if args.mark:
        valid_positions = ['left', 'right', 'top', 'bottom', 'topleft', 'topright', 'bottomleft', 'bottomright']
        for mark_args in args.mark:
            if len(mark_args) < 2:
                print("Error: --mark requires coordinates and text. Example: --mark 40.71,-74.00 Custom Text topright")
                sys.exit(1)
            try:
                lat_str, lon_str = mark_args[0].split(",")
                mark_lat = float(lat_str.strip())
                mark_lon = float(lon_str.strip())
                last_arg = mark_args[-1].lower()
                if last_arg in valid_positions:
                    mark_pos = last_arg
                    mark_text = " ".join(mark_args[1:-1])
                else:
                    mark_pos = "topright"
                    mark_text = " ".join(mark_args[1:])
                if not mark_text.strip():
                    print("Error: --mark requires text. Example: --mark 40.71,-74.00 Custom Text topright")
                    sys.exit(1)
                marks_data.append((mark_lat, mark_lon, mark_text, mark_pos))
            except ValueError:
                print("Error: Coordinates for --mark must be separated by a comma. Example: 40.71,-74.00")
                sys.exit(1)

    print("=" * 50)
    print("City Map Poster Generator")
    print("=" * 50)

    # Load custom fonts if specified
    custom_fonts = None
    if args.font_family:
        custom_fonts = load_fonts(args.font_family)
        if not custom_fonts:
            print(f"⚠ Failed to load '{args.font_family}', falling back to Roboto")

    # Get coordinates and generate poster
    try:
        if args.latitude and args.longitude:
            lat = parse(args.latitude)
            lon = parse(args.longitude)
            coords = [lat, lon]
            print(f"✓ Coordinates: {', '.join([str(i) for i in coords])}")
        else:
            coords = get_coordinates(args.city, args.country)

        for theme_name in themes_to_generate:
            THEME = load_theme(theme_name)
            output_file = generate_output_filename(args.city, theme_name, args.format)
            parsed_stickers = []
            for st in (args.stickers or []):
                try:
                    la, lo = (float(v) for v in st[0].split(","))
                    parsed_stickers.append((la, lo, st[1], st[2]))
                except ValueError:
                    print(f"\u26a0 Warning: bad sticker location '{st[0]}', expected LAT,LON")
            create_poster(
                args.city,
                args.country,
                coords,
                args.distance,
                output_file,
                args.format,
                args.width,
                args.height,
                country_label=args.country_label,
                display_city=args.display_city,
                display_country=args.display_country,
                subtitle=args.subtitle,
                dates=args.dates,
                line_scale=args.line_scale,
                marks=marks_data,
                points=points_data,
                gpx_path=args.gpx,
                show_title=not (args.no_title or args.margin),
                no_gradient=args.no_gradient,
                title_scale=args.title_scale,
                point_scale=args.point_scale,
                no_attribution=args.no_attribution,
                edge_marks=args.edge_marks,
                scalebar=args.scalebar,
                coords_at=tuple(float(v) for v in args.coords_at.split(",")) if args.coords_at else None,
                stickers=parsed_stickers,
                sticker_size=args.sticker_size,
                sticker_mm=args.sticker_mm,
                door_mm=args.door_mm,
                margin=args.margin,
                buildings=args.buildings,
                mobility=args.mobility,
                icon_path=args.icon,
                icon_size=args.icon_size,
                fonts=custom_fonts,
            )

        print("\n" + "=" * 50)
        print("✓ Poster generation complete!")
        print("=" * 50)

    except Exception as e:
        print(f"\n✗ Error: {e}")
        import traceback

        traceback.print_exc()
        sys.exit(1)
