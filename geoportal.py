#!/usr/bin/env python3
"""Download Polish Geoportal orthophoto and cut it into georeferenced tiles.

The tool fetches orthoimagery for an area in EPSG:2180 (the Polish CS92 grid)
and writes it as square tiles with world files, plus a CSV index, a GeoJSON
footprint layer and an optional mosaic preview.

Imagery sources (``--source``):

===================  ===========================================================
``wmts-standard``    national mosaic, 25 cm - this is the default, and it is
                     usually a spring acquisition
``wmts-high``        high-resolution mosaic, 10 cm where available (cities)
``wms-high``         same product served over WMS instead of WMTS
``wms-standard``     the WMS counterpart of ``wmts-standard`` (currently 404)
``wms-time-std``     time-enabled WMS archive, 25 cm, date selected with ``TIME``
``wms-time-high``    time-enabled WMS archive, 10 cm, date selected with ``TIME``
``opendata``         native GeoTIFF sheets from the open data portal
``esri``             Esri World Imagery, reprojected from Web Mercator
===================  ===========================================================

The first four sources expose only the current mosaic. To request a specific
acquisition campaign - for example a summer flight instead of the default
spring one - use the time-enabled WMS sources or the GeoTIFF sheets, together
with ``--summer`` or ``--time``. Flight dates come from the WFS index in
:mod:`ortho_index`.

Output layout::

    <out>/images/tile_r000_c000.png   image tile
    <out>/images/tile_r000_c000.pgw   world file
    <out>/tiles.csv                   name,row,col,xmin,ymin,xmax,ymax,blank[,date]
    <out>/tiles.geojson               tile footprints, EPSG:2180
    <out>/meta.json                   source, grid, sheet selection, date histogram
    <out>/preview.jpg                 mosaic, written with --mosaic

Requirements::

    pip install -r requirements.txt

Examples::

    python geoportal.py --list-layers
    python geoportal.py --center 52.085 21.115 --size 1000 --out data/area --mosaic
    python geoportal.py --center 52.085 21.115 --size 1000 --source opendata \\
        --summer --out data/area-summer --mosaic
    python geoportal.py --list-flights --center 52.085 21.115 --size 1000
    python geoportal.py --center 52.085 21.115 --source opendata \\
        --time 2024-07-18 --out data/area-2024-07-18
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import io
import json
import math
import os
import sys
import threading
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from functools import lru_cache
from pathlib import Path

import requests
from PIL import Image
from pyproj import Transformer

import ortho_index

# Some 5 cm sheets are close to a billion pixels; the source is a trusted
# government service, so the PIL safety limit is raised deliberately.
Image.MAX_IMAGE_PIXELS = 600_000_000

GUGIK = "https://mapy.geoportal.gov.pl/wss/service/PZGIK/ORTO"
SOURCES = {
    "wmts-standard": ("wmts", f"{GUGIK}/WMTS/StandardResolution"),
    "wmts-high": ("wmts", f"{GUGIK}/WMTS/HighResolution"),
    "wms-high": ("wms", f"{GUGIK}/WMS/HighResolution"),
    "wms-standard": ("wms", f"{GUGIK}/WMS/StandardResolution"),
    # Time-enabled archive: one TIME value per request selects the campaign.
    "wms-time-std": ("wms-time", f"{GUGIK}/WMS/StandardResolutionTime", "Raster"),
    "wms-time-high": ("wms-time", f"{GUGIK}/WMS/HighResolutionTime", "Image"),
    # Native GeoTIFF sheets, full resolution, no server-side resampling.
    "opendata": ("tif", "https://opendata.geoportal.gov.pl/ortofotomapa", None),
    # Commercial fallback imagery, reprojected from Web Mercator.
    "esri": ("esri", "https://server.arcgisonline.com/ArcGIS/rest/services/"
                     "World_Imagery/MapServer/export"),
}
CRS = "EPSG:2180"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0",
    "Accept": "image/png,image/jpeg,image/*,text/xml,*/*;q=0.8",
}
SESSION = requests.Session()
SESSION.headers.update(HEADERS)


# --------------------------------------------------------------------- helpers

def local_name(tag: str) -> str:
    """Strip the XML namespace from a tag name."""
    return tag.rsplit("}", 1)[-1]


def first_child(element, name):
    return next((child for child in element if local_name(child.tag) == name), None)


def children_named(element, name):
    return [child for child in element if local_name(child.tag) == name]


def child_text(element, name, default=None):
    child = first_child(element, name)
    return child.text.strip() if child is not None and child.text else default


def http_get(url, params, retries=4, timeout=90):
    """GET with retries. Client errors other than timeouts are not retried."""
    last_error = None
    for attempt in range(retries):
        try:
            response = SESSION.get(url, params=params, timeout=timeout)
            if response.status_code == 200:
                return response
            last_error = f"HTTP {response.status_code} ({response.url}): {response.text[:200]}"
            if response.status_code in (400, 404):
                break
        except requests.RequestException as exc:
            last_error = str(exc)
        time.sleep(2 ** attempt)
    raise RuntimeError(last_error)


# ------------------------------------------------------------------------ WMTS

class WMTS:
    """Minimal WMTS client: capabilities parsing, tile matrix choice, tile fetch."""

    def __init__(self, url, layer=None, cache_dir=None, sleep=0.0):
        self.url, self.sleep = url, sleep
        self.cache_dir = cache_dir

        response = http_get(url, {"SERVICE": "WMTS", "REQUEST": "GetCapabilities"})
        root = ET.fromstring(response.content)
        contents = first_child(root, "Contents")

        self.layers = {}
        for layer in children_named(contents, "Layer"):
            identifier = child_text(layer, "Identifier")
            self.layers[identifier] = {
                "title": child_text(layer, "Title", ""),
                "formats": [f.text for f in children_named(layer, "Format")],
                "styles": [child_text(s, "Identifier") for s in children_named(layer, "Style")],
                "tms": [child_text(link, "TileMatrixSet")
                        for link in children_named(layer, "TileMatrixSetLink")],
            }

        self.tms = {}
        for matrix_set in children_named(contents, "TileMatrixSet"):
            identifier = child_text(matrix_set, "Identifier")
            matrices = []
            for matrix in children_named(matrix_set, "TileMatrix"):
                corner_n, corner_e = map(float, child_text(matrix, "TopLeftCorner").split()[:2])
                matrices.append({
                    "id": child_text(matrix, "Identifier"),
                    # WMTS scale denominator assumes 0.28 mm pixels.
                    "res": float(child_text(matrix, "ScaleDenominator")) * 0.00028,
                    "tl": (corner_n, corner_e),
                    "tw": int(child_text(matrix, "TileWidth")),
                    "th": int(child_text(matrix, "TileHeight")),
                    "mw": int(child_text(matrix, "MatrixWidth")),
                    "mh": int(child_text(matrix, "MatrixHeight")),
                })
            self.tms[identifier] = {"crs": child_text(matrix_set, "SupportedCRS", ""),
                                    "matrices": matrices}

        self.layer = layer or next(iter(self.layers))
        if self.layer not in self.layers:
            raise RuntimeError(f"Unknown layer {self.layer}. Available: {list(self.layers)}")

        info = self.layers[self.layer]
        self.style = info["styles"][0] if info["styles"] else "default"
        self.format = next((f for f in info["formats"] if "jpeg" in f),
                           info["formats"][0] if info["formats"] else "image/jpeg")

        candidates = [tms for tms in info["tms"]
                      if "2180" in tms or "2180" in self.tms.get(tms, {}).get("crs", "")]
        if not candidates:
            raise RuntimeError(
                f"Layer {self.layer} has no EPSG:2180 tile matrix set. "
                f"Available: {info['tms']}"
            )
        self.tms_id = candidates[0]

    def describe(self):
        for identifier, info in self.layers.items():
            print(f"Layer: {identifier}  ({info['title']})")
            print(f"  formats: {info['formats']}, tile matrix sets: {info['tms']}")
        print(f"\nZoom levels of {self.tms_id}:")
        for matrix in self.tms[self.tms_id]["matrices"]:
            print(f"  {matrix['id']:20s} {matrix['res']:8.3f} m/px   tile {matrix['tw']}px")

    def pick_matrix(self, gsd):
        """Pick the finest zoom level that is not finer than the requested GSD."""
        matrices = sorted(self.tms[self.tms_id]["matrices"], key=lambda m: m["res"])
        finer = [m for m in matrices if m["res"] <= gsd * 1.0001]
        chosen = dict(finer[-1] if finer else matrices[0])

        # EPSG:2180 uses (northing, easting) axis order. The grid origin lies
        # north-west of Poland, so northing is the larger of the two values.
        corner_n, corner_e = chosen["tl"]
        chosen["tlx"], chosen["tly"] = (corner_e, corner_n) if corner_n > corner_e \
            else (corner_n, corner_e)
        return chosen

    def _fetch(self, matrix, row, col):
        if not (0 <= row < matrix["mh"] and 0 <= col < matrix["mw"]):
            return None

        extension = "jpg" if "jpeg" in self.format else "png"
        cache_path = None
        if self.cache_dir:
            cache_path = (self.cache_dir
                          / matrix["id"].replace(":", "_")
                          / f"{row}_{col}.{extension}")
            if cache_path.exists():
                return Image.open(cache_path).convert("RGB")

        params = {
            "SERVICE": "WMTS", "REQUEST": "GetTile", "VERSION": "1.0.0",
            "LAYER": self.layer, "STYLE": self.style, "FORMAT": self.format,
            "TILEMATRIXSET": self.tms_id, "TILEMATRIX": matrix["id"],
            "TILEROW": row, "TILECOL": col,
        }
        try:
            response = http_get(self.url, params)
        except RuntimeError:
            return None                     # tile outside the data coverage
        if not response.headers.get("Content-Type", "").startswith("image"):
            return None

        if cache_path:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_bytes(response.content)
        time.sleep(self.sleep)
        return Image.open(io.BytesIO(response.content)).convert("RGB")

    def render(self, bbox, size, matrix):
        """Compose the requested bbox (EPSG:2180) as a size x size image."""
        fetch = self._cached_fetch
        x0, y0, x1, y1 = bbox
        span_x = matrix["tw"] * matrix["res"]
        span_y = matrix["th"] * matrix["res"]
        eps = 1e-6

        col0 = math.floor((x0 - matrix["tlx"]) / span_x)
        col1 = math.floor((x1 - matrix["tlx"] - eps) / span_x)
        row0 = math.floor((matrix["tly"] - y1) / span_y)
        row1 = math.floor((matrix["tly"] - y0 - eps) / span_y)

        width = (col1 - col0 + 1) * matrix["tw"]
        height = (row1 - row0 + 1) * matrix["th"]
        mosaic = Image.new("RGB", (width, height), (255, 255, 255))

        for row in range(row0, row1 + 1):
            for col in range(col0, col1 + 1):
                tile = fetch(matrix["id"], row, col)
                if tile is not None:
                    mosaic.paste(tile, ((col - col0) * matrix["tw"],
                                        (row - row0) * matrix["th"]))

        mosaic_x0 = matrix["tlx"] + col0 * span_x
        mosaic_y1 = matrix["tly"] - row0 * span_y
        box = (
            (x0 - mosaic_x0) / matrix["res"],
            (mosaic_y1 - y1) / matrix["res"],
            (x1 - mosaic_x0) / matrix["res"],
            (mosaic_y1 - y0) / matrix["res"],
        )
        return mosaic.resize((size, size), Image.LANCZOS, box=box)

    def prepare(self, gsd):
        matrix = self.pick_matrix(gsd)
        matrices = {matrix["id"]: matrix}

        @lru_cache(maxsize=512)
        def cached(matrix_id, row, col):
            return self._fetch(matrices[matrix_id], row, col)

        self._cached_fetch = cached
        return matrix


# ------------------------------------------------------------------------- WMS

def wms_layers(url):
    """Return [(name, title), ...] for a WMS endpoint."""
    for version in ("1.3.0", "1.1.1"):
        try:
            response = http_get(url, {"SERVICE": "WMS", "REQUEST": "GetCapabilities",
                                      "VERSION": version})
        except RuntimeError:
            continue
        root = ET.fromstring(response.content)
        layers = []
        for element in root.iter():
            if local_name(element.tag) == "Layer" and child_text(element, "Name"):
                layers.append((child_text(element, "Name"), child_text(element, "Title", "")))
        return layers
    raise RuntimeError(f"GetCapabilities failed for {url}")


def wms_get_map(url, layer, bbox, size, fmt="image/png", date=None):
    """WMS GetMap in EPSG:2180. ``date`` adds the TIME parameter."""
    xmin, ymin, xmax, ymax = bbox
    params = {
        "SERVICE": "WMS", "VERSION": "1.1.1", "REQUEST": "GetMap",
        "LAYERS": layer, "STYLES": "", "SRS": CRS,
        "BBOX": f"{xmin:.3f},{ymin:.3f},{xmax:.3f},{ymax:.3f}",
        "WIDTH": size, "HEIGHT": size, "FORMAT": fmt,
    }
    if date:
        params["TIME"] = date

    response = http_get(url, params)
    if not response.headers.get("Content-Type", "").startswith("image"):
        raise RuntimeError(f"WMS error: {response.text[:300]}")
    return Image.open(io.BytesIO(response.content)).convert("RGB")


# ------------------------------------------------------- Esri World Imagery

def _warp_3857_to_2180(image, src_bbox, dst_bbox, size):
    """Reproject a Web Mercator image onto an EPSG:2180 grid.

    Over a tile of a few hundred metres the meridian convergence is constant,
    so an affine mapping from output pixels to input pixels is accurate well
    below one pixel.
    """
    to_mercator = Transformer.from_crs(CRS, "EPSG:3857", always_xy=True)
    scale_x = image.width / (src_bbox[2] - src_bbox[0])
    scale_y = image.height / (src_bbox[3] - src_bbox[1])
    span_x = dst_bbox[2] - dst_bbox[0]
    span_y = dst_bbox[3] - dst_bbox[1]

    def source_pixel(px, py):
        easting = dst_bbox[0] + px / size * span_x
        northing = dst_bbox[3] - py / size * span_y
        x, y = to_mercator.transform(easting, northing)
        return (x - src_bbox[0]) * scale_x, (src_bbox[3] - y) * scale_y

    (u0, v0), (u1, v1), (u2, v2) = source_pixel(0, 0), source_pixel(size, 0), source_pixel(0, size)
    coeffs = (
        (u1 - u0) / size, (u2 - u0) / size, u0,
        (v1 - v0) / size, (v2 - v0) / size, v0,
    )
    return image.transform((size, size), Image.AFFINE, coeffs, Image.BILINEAR)


def esri_get_map(url, bbox2180, size, fmt="png", pad=0.08):
    """Fetch Esri World Imagery for an EPSG:2180 bbox and warp it onto the grid."""
    to_mercator = Transformer.from_crs(CRS, "EPSG:3857", always_xy=True)
    points = [to_mercator.transform(x, y)
              for x in (bbox2180[0], bbox2180[2])
              for y in (bbox2180[1], bbox2180[3])]
    xs, ys = zip(*points)
    src_x0, src_y0, src_x1, src_y1 = min(xs), min(ys), max(xs), max(ys)
    pad_x, pad_y = (src_x1 - src_x0) * pad, (src_y1 - src_y0) * pad
    src = (src_x0 - pad_x, src_y0 - pad_y, src_x1 + pad_x, src_y1 + pad_y)

    # Native resolution is about 0.3 m. The requested image must keep the
    # bbox aspect ratio, otherwise the server expands the bbox and the result
    # is silently stretched.
    native = 0.3
    width = int(max(256, min(4096, math.ceil((src[2] - src[0]) / native))))
    height = int(max(256, min(4096, math.ceil((src[3] - src[1]) / native))))

    params = {
        "bbox": f"{src[0]},{src[1]},{src[2]},{src[3]}",
        "bboxSR": "3857", "imageSR": "3857",
        "size": f"{width},{height}", "format": fmt, "f": "image",
    }
    response = http_get(url, params)
    if not response.headers.get("Content-Type", "").startswith("image"):
        raise RuntimeError(f"Esri error: {response.text[:300]}")
    image = Image.open(io.BytesIO(response.content)).convert("RGB")
    return _warp_3857_to_2180(image, src, bbox2180, size)


# ------------------------------------------------------- GeoTIFF sheet access

_tif_decode_lock = threading.Lock()   # libtiff shares a global "tempfile.tif"


class SheetTIF:
    """A GeoTIFF orthophoto sheet from the open data portal (EPSG:2180)."""

    def __init__(self, path):
        self.path = path
        self.image = Image.open(path)
        tiepoint = self.image.tag_v2.get(33922)      # ModelTiepointTag
        pixel_scale = self.image.tag_v2.get(33550)   # ModelPixelScaleTag
        if not tiepoint or not pixel_scale:
            raise RuntimeError(f"No georeferencing in {path}")

        self.x0, self.y0 = float(tiepoint[3]), float(tiepoint[4])   # north-west corner
        self.res = float(pixel_scale[0])
        self.width, self.height = self.image.size
        self.bbox = (
            self.x0,
            self.y0 - self.height * self.res,
            self.x0 + self.width * self.res,
            self.y0,
        )
        # Decode once, under a lock: libtiff is not thread-safe, and after this
        # call crops are served from memory.
        with _tif_decode_lock:
            self.image.load()

    def crop(self, bbox, size, gsd):
        xmin, ymin, xmax, ymax = bbox
        col0 = round((xmin - self.x0) / self.res)
        col1 = round((xmax - self.x0) / self.res)
        row0 = round((self.y0 - ymax) / self.res)
        row1 = round((self.y0 - ymin) / self.res)

        col0, row0 = max(0, col0), max(0, row0)
        col1, row1 = min(self.width, col1), min(self.height, row1)

        out = Image.new("RGB", (max(1, col1 - col0), max(1, row1 - row0)), (255, 255, 255))
        if col1 > col0 and row1 > row0:
            out.paste(self.image.crop((col0, row0, col1, row1)).convert("RGB"), (0, 0))
        return out.resize((size, size), Image.LANCZOS) if out.size != (size, size) else out


_tif_locks: dict = {}
_tif_locks_guard = threading.Lock()
_download_lock = threading.Lock()


def lock_for(key):
    with _tif_locks_guard:
        return _tif_locks.setdefault(key, threading.Lock())


def download_tif(url, path, session=None, timeout=300):
    """Download a sheet to ``path`` atomically, verifying the Content-Length."""
    if path.exists() and path.stat().st_size > 1_000_000:
        return path

    session = session or SESSION
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_suffix(f".{os.getpid()}.{threading.get_ident()}.part")

    with _download_lock:      # requests.Session is not fully thread-safe
        response = session.get(url, stream=True, timeout=timeout)
        response.raise_for_status()
        expected = int(response.headers.get("Content-Length") or 0)
        with open(partial, "wb") as handle:
            for chunk in response.iter_content(1 << 20):
                handle.write(chunk)

    actual = partial.stat().st_size if partial.exists() else 0
    if expected and actual != expected:
        partial.unlink(missing_ok=True)
        raise RuntimeError(f"Incomplete download of {path.name}: {actual}/{expected} bytes")

    partial.replace(path)
    return path


def sheet_for(bbox, chosen, months=(6, 7, 8)):
    """Return the sheet covering a tile: in-season first, then the most recent."""
    cx = (bbox[0] + bbox[2]) / 2
    cy = (bbox[1] + bbox[3]) / 2
    candidates = [sheet for sheet in chosen.values() if sheet.covers(cx, cy)]
    if not candidates:
        return None
    in_season = [sheet for sheet in candidates
                 if sheet.date and sheet.date.month in months]
    pool = in_season or candidates
    return max(pool, key=lambda s: (s.date or dt.date.min, -s.gsd))


def pick_tile_date(bbox, chosen, months=(6, 7, 8), default=None):
    """Acquisition date of the sheet covering the tile, or ``default``."""
    sheet = sheet_for(bbox, chosen, months)
    return sheet.date.isoformat() if sheet and sheet.date else default


# ------------------------------------------------------------------- geometry

def wgs84_to_2180_bbox(lon_min, lat_min, lon_max, lat_max):
    transformer = Transformer.from_crs("EPSG:4326", CRS, always_xy=True)
    points = [transformer.transform(lon, lat)
              for lon in (lon_min, lon_max) for lat in (lat_min, lat_max)]
    xs, ys = zip(*points)
    return min(xs), min(ys), max(xs), max(ys)


def center_to_2180_bbox(lat, lon, size_m):
    x, y = Transformer.from_crs("EPSG:4326", CRS, always_xy=True).transform(lon, lat)
    half = size_m / 2
    return x - half, y - half, x + half, y + half


def make_grid(area, tile_px, gsd, overlap):
    """Split the area into tiles. Returns (tiles, rows, cols, stride)."""
    xmin, ymin, xmax, ymax = area
    tile_m = tile_px * gsd
    stride = tile_m * (1 - overlap)
    n_cols = max(1, math.ceil((xmax - xmin - tile_m) / stride) + 1)
    n_rows = max(1, math.ceil((ymax - ymin - tile_m) / stride) + 1)

    tiles = []
    for row in range(n_rows):
        for col in range(n_cols):
            x0 = xmin + col * stride
            y1 = ymax - row * stride
            tiles.append((row, col, (x0, y1 - tile_m, x0 + tile_m, y1)))
    return tiles, n_rows, n_cols, stride


def write_world_file(path, bbox, gsd):
    xmin, _, _, ymax = bbox
    path.write_text(
        f"{gsd:.6f}\n0.0\n0.0\n{-gsd:.6f}\n{xmin + gsd / 2:.3f}\n{ymax - gsd / 2:.3f}\n"
    )


def is_blank(image, threshold=0.98):
    """True if the tile is (almost) uniformly white or black."""
    gray = image.resize((64, 64)).convert("L")
    pixels = gray.get_flattened_data() if hasattr(gray, "get_flattened_data") \
        else list(gray.getdata())
    pixels = list(pixels)
    white = sum(p > 245 for p in pixels) / len(pixels)
    black = sum(p < 10 for p in pixels) / len(pixels)
    return white > threshold or black > threshold


def parse_span(spec, default):
    """``"6-8"`` -> (6, 7, 8); ``"5,7,9"`` -> (5, 7, 9); empty -> default."""
    if not spec:
        return tuple(default)
    if "," in spec:
        return tuple(int(part) for part in spec.split(","))
    if "-" in spec:
        start, end = spec.split("-")
        return tuple(range(int(start), int(end) + 1))
    return (int(spec),)


def print_sheets(sheets, chosen, months):
    print(f"\nSheets in the GUGiK index: {len(sheets)}")
    print(f"{'sheet':22s} {'date':11s} {'season':8s} {'gsd':>5s} {'color':5s} {'MB':>4s}  GeoTIFF")
    print("-" * 104)
    for sheet in sorted(sheets, key=lambda s: (s.date or dt.date.min), reverse=True):
        if sheet.color != "RGB":
            continue
        mark = "  <-- IN SEASON" if sheet.date and sheet.date.month in months else ""
        size = f"{sheet.size_mb:.0f}" if sheet.size_mb else "-"
        print(f"{sheet.sheet_id:22s} {str(sheet.date):11s} {sheet.season:8s} "
              f"{sheet.gsd:5.2f} {sheet.color:5s} {size:>4s}  {sheet.url}{mark}")

    print(f"\nSelection for {len(chosen)} sheets (season {months}):")
    for sheet_id, sheet in sorted(chosen.items()):
        flag = "OK " if sheet.date and sheet.date.month in months else "!! "
        print(f"  {flag}{sheet_id:22s} {sheet.date}  {sheet.gsd:.2f} m  {sheet.color}")


def sheets_meta(chosen, months):
    return [
        {
            "sheet_id": sheet_id,
            "date": sheet.date.isoformat() if sheet.date else None,
            "gsd": sheet.gsd,
            "color": sheet.color,
            "in_season": bool(sheet.date and sheet.date.month in months),
            "url": sheet.url,
        }
        for sheet_id, sheet in sorted(chosen.items())
    ]


# ----------------------------------------------------------------------- main

def main():
    parser = argparse.ArgumentParser(
        description="Geoportal orthophoto to georeferenced tiles (EPSG:2180)"
    )
    area = parser.add_mutually_exclusive_group()
    area.add_argument("--center", nargs=2, type=float, metavar=("LAT", "LON"),
                      help="centre of a square area")
    area.add_argument("--bbox", nargs=4, type=float,
                      metavar=("LON_MIN", "LAT_MIN", "LON_MAX", "LAT_MAX"),
                      help="area as a WGS84 bounding box")
    area.add_argument("--bbox2180", nargs=4, type=float,
                      metavar=("XMIN", "YMIN", "XMAX", "YMAX"),
                      help="area directly in EPSG:2180")

    parser.add_argument("--size", type=float, default=1000,
                        help="side of the square in metres (with --center)")
    parser.add_argument("--out", type=Path, default=Path("tiles_out"),
                        help="output directory (default: tiles_out)")
    parser.add_argument("--source", choices=SOURCES.keys(), default="wmts-standard",
                        help="imagery source (default: wmts-standard)")
    parser.add_argument("--layer", default=None,
                        help="service layer name (default: taken from the service)")

    parser.add_argument("--gsd", type=float, default=0.25,
                        help="output ground sample distance in metres (default: 0.25)")
    parser.add_argument("--tile", type=int, default=640,
                        help="tile side in pixels (default: 640)")
    parser.add_argument("--overlap", type=float, default=0.0,
                        help="tile overlap, 0..0.9 (default: 0.0)")
    parser.add_argument("--format", choices=["png", "jpg"], default="png",
                        help="tile image format (default: png)")
    parser.add_argument("--mosaic", action="store_true",
                        help="also write preview.jpg with the whole area")
    parser.add_argument("--max-tiles", type=int, default=500,
                        help="abort above this tile count (default: 500)")

    parser.add_argument("--summer", action="store_true",
                        help="select a summer flight (June-August)")
    parser.add_argument("--time", default=None,
                        help="explicit acquisition date, e.g. 2024-07-18")
    parser.add_argument("--months", default="6-8",
                        help="accepted acquisition months for --summer (default: 6-8)")
    parser.add_argument("--prefer", choices=["newest", "sharpest"], default="newest",
                        help="newest flight in season, or finest pixel (default: newest)")
    parser.add_argument("--min-gsd", type=float, default=None,
                        help="discard flights coarser than this value in metres")
    parser.add_argument("--years", default=None,
                        help="year range queried in the index, e.g. 2018-2026")
    parser.add_argument("--list-flights", action="store_true",
                        help="list available flights and exit")
    parser.add_argument("--list-layers", action="store_true",
                        help="list service layers and exit")

    parser.add_argument("--workers", type=int, default=0,
                        help="parallel tile downloads (0 = auto: 4 WMS/TIF, 1 WMTS)")
    parser.add_argument("--retries", type=int, default=3,
                        help="retries when the server returns an empty tile (default: 3)")
    parser.add_argument("--sleep", type=float, default=0.1,
                        help="pause between requests in seconds (default: 0.1)")
    parser.add_argument("--tif-cache", type=Path, default=None,
                        help="directory for GeoTIFF sheets (default: OUT/_tif)")

    args = parser.parse_args()

    if args.center:
        area_bbox = center_to_2180_bbox(*args.center, args.size)
    elif args.bbox:
        area_bbox = wgs84_to_2180_bbox(*args.bbox)
    elif args.bbox2180:
        area_bbox = tuple(args.bbox2180)
    else:
        parser.error("one of --center, --bbox or --bbox2180 is required")

    if not 0 <= args.overlap < 0.9:
        parser.error("--overlap must be in the range [0, 0.9)")

    months = parse_span(args.months, (6, 7, 8))
    years = parse_span(args.years, range(2018, 2027)) if args.years else range(2018, 2027)

    kind, url = SOURCES[args.source][:2]
    default_layer = SOURCES[args.source][2] if len(SOURCES[args.source]) > 2 else None

    # WMTS and the plain WMS mosaics have no time axis; --summer/--time switch
    # to the time-enabled archive so that a date can actually be requested.
    if (args.summer or args.time) and kind in ("wmts", "wms"):
        replacement = "wms-time-high" if args.source in ("wmts-high", "wms-high") \
            else "wms-time-std"
        print(f"{args.source} has no time axis; using {replacement}")
        args.source = replacement
        kind, url = SOURCES[replacement][:2]
        default_layer = SOURCES[replacement][2]

    if args.list_layers:
        if kind == "wmts":
            WMTS(url, args.layer, cache_dir=args.out / "_wmts_cache").describe()
        else:
            for name, title in wms_layers(url):
                print(f"{name:30s} {title}")
        return

    chosen = {}
    if args.list_flights or kind in ("wms-time", "tif"):
        print(f"Reading flight index (WFS), years {min(years)}-{max(years)} ... ",
              end="", flush=True)
        sheets = ortho_index.fetch_sheets(area_bbox, years=years)
        chosen = ortho_index.pick_dates(
            sheets, area_bbox, months=months, prefer=args.prefer, min_gsd=args.min_gsd
        )
        print(f"{len(sheets)} sheets, {len(chosen)} selected")

        if args.time:
            matching = {sid: sheet for sid, sheet in chosen.items()
                        if sheet.date and sheet.date.isoformat() == args.time}
            if matching:
                chosen = matching

        if args.list_flights:
            print_sheets(sheets, chosen, months)
            return

        if not chosen:
            print("No indexed flights; falling back to the current mosaic (no TIME).")

    wmts = None
    if kind == "wmts":
        wmts = WMTS(url, args.layer, cache_dir=args.out / "_wmts_cache", sleep=args.sleep)
    elif kind in ("wms", "wms-time"):
        if default_layer:
            args.layer = args.layer or default_layer
        else:
            args.layer = args.layer or wms_layers(url)[-1][0]

    tiles, n_rows, n_cols, stride = make_grid(area_bbox, args.tile, args.gsd, args.overlap)

    print(f"Source: {args.source} ({url})")
    if wmts:
        matrix = wmts.prepare(args.gsd)
        print(f"Layer: {wmts.layer}, grid {wmts.tms_id}, level {matrix['id']} "
              f"({matrix['res']:.3f} m/px)")
        if matrix["res"] > args.gsd * 1.05:
            print(f"  note: finest level ({matrix['res']:.3f} m) is coarser than --gsd; "
                  f"tiles will be upsampled")
    else:
        print(f"Layer: {args.layer}")

    if kind in ("wms-time", "tif") and chosen:
        used = sorted({(sheet.date.isoformat(), sheet.gsd)
                       for sheet in chosen.values() if sheet.date})
        listed = ", ".join(f"{date} ({gsd:.2f} m)" for date, gsd in used[:6])
        print(f"Selected flights: {len(used)} -> {listed}{' ...' if len(used) > 6 else ''}")

    print(f"Tile: {args.tile}px x {args.gsd} m = {args.tile * args.gsd:.0f} m, "
          f"stride {stride:.0f} m")
    print(f"Grid: {n_rows} x {n_cols} = {len(tiles)} tiles")

    if len(tiles) > args.max_tiles:
        sys.exit(
            f"Too many tiles ({len(tiles)} > {args.max_tiles}). "
            f"Reduce the area or raise --max-tiles."
        )

    image_dir = args.out / "images"
    image_dir.mkdir(parents=True, exist_ok=True)
    world_suffix = ".pgw" if args.format == "png" else ".jgw"

    records, features = [], []
    mosaic = None
    if args.mosaic:
        mosaic_width = int(round((n_cols - 1) * stride / args.gsd)) + args.tile
        mosaic_height = int(round((n_rows - 1) * stride / args.gsd)) + args.tile
        mosaic = Image.new("RGB", (mosaic_width, mosaic_height), (255, 255, 255))

    tif_cache = {}
    tif_dir = args.tif_cache or (args.out / "_tif")

    def get_tif(sheet):
        """Return a decoded SheetTIF, downloading and caching it if needed."""
        if sheet.sheet_id in tif_cache:
            return tif_cache[sheet.sheet_id]

        with lock_for(sheet.sheet_id):
            if sheet.sheet_id not in tif_cache:
                name = (f"{sheet.date}_{sheet.sheet_id.replace('/', '_')}"
                        f"_{int(round(sheet.gsd * 100))}cm.tif")
                path = tif_dir / name

                for _ in range(2):
                    try:
                        if not path.exists():
                            print(f"\n  downloading sheet {sheet.sheet_id} "
                                  f"({sheet.date}), ~{sheet.size_mb or '?'} MB ...")
                        download_tif(sheet.url, path)
                        tif = SheetTIF(path)
                        break
                    except Exception as exc:          # corrupt or truncated download
                        print(f"\n  ! {path.name}: {exc} - downloading again")
                        path.unlink(missing_ok=True)
                else:
                    raise RuntimeError(f"Could not load sheet {path.name}")

                if len(tif_cache) >= 2:
                    tif_cache.clear()             # keep memory bounded
                tif_cache[sheet.sheet_id] = tif

        return tif_cache[sheet.sheet_id]

    def fetch(bbox):
        """Return (image, acquisition_date) for one tile."""
        if wmts:
            return wmts.render(bbox, args.tile, matrix), None
        if kind == "esri":
            return esri_get_map(url, bbox, args.tile), None
        if kind == "tif":
            sheet = sheet_for(bbox, chosen, months)
            if sheet is None or not sheet.url:
                return Image.new("RGB", (args.tile, args.tile), (255, 255, 255)), None
            tif = get_tif(sheet)
            date = sheet.date.isoformat() if sheet.date else None
            return tif.crop(bbox, args.tile, args.gsd), date

        fmt = "image/png" if args.format == "png" else "image/jpeg"
        # Preference order: explicit date, the covering sheet's date, current mosaic.
        order = []
        if args.time:
            order.append(args.time)
        sheet_date = pick_tile_date(bbox, chosen, months)
        if sheet_date:
            order.append(sheet_date)
        order.append(None)

        candidates, seen = [], set()
        for date in order:
            if date not in seen:
                seen.add(date)
                candidates.append(date)

        best = None
        for date in candidates:
            for attempt in range(max(1, args.retries)):
                image = wms_get_map(url, args.layer, bbox, args.tile, fmt, date=date)
                if not is_blank(image):
                    return image, date
                best = (image, date)
                time.sleep(0.4 * (attempt + 1))
            if date is None:
                break
        return best

    workers = args.workers or (1 if wmts else 4)
    pending = [
        (row, col, bbox) for (row, col, bbox) in tiles
        if not (image_dir / f"tile_r{row:03d}_c{col:03d}.{args.format}").exists()
    ]

    tile_dates = {}
    if pending:
        def job(item):
            row, col, bbox = item
            path = image_dir / f"tile_r{row:03d}_c{col:03d}.{args.format}"
            image, date = fetch(bbox)
            if args.format == "jpg":
                image.save(path, quality=95)
            else:
                image.save(path)
            write_world_file(path.with_suffix(world_suffix), bbox, args.gsd)
            return (row, col), date, is_blank(image)

        print(f"Downloading {len(pending)} tiles with {workers} worker(s) ...")
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(job, item) for item in pending]
            for processed, future in enumerate(as_completed(futures), 1):
                (row, col), date, blank = future.result()
                tile_dates[(row, col)] = (date, blank)
                flag = "  (blank)" if blank else ""
                print(f"\r[{processed}/{len(pending)}] r{row:03d} c{col:03d}{flag}   ",
                      end="", flush=True)
        print()

    for index, (row, col, bbox) in enumerate(tiles, 1):
        path = image_dir / f"tile_r{row:03d}_c{col:03d}.{args.format}"
        date, blank = tile_dates.get((row, col), (None, None))
        image = Image.open(path).convert("RGB")
        if blank is None:
            blank = is_blank(image)

        x0, y0, x1, y1 = bbox
        record = {
            "name": path.name, "row": row, "col": col,
            "xmin": round(x0, 3), "ymin": round(y0, 3),
            "xmax": round(x1, 3), "ymax": round(y1, 3),
            "blank": int(blank),
        }
        if kind in ("wms-time", "tif"):
            record["date"] = date or ""
        records.append(record)

        features.append({
            "type": "Feature",
            "properties": {"name": path.name, "blank": blank},
            "geometry": {
                "type": "Polygon",
                "coordinates": [[[x0, y0], [x1, y0], [x1, y1], [x0, y1], [x0, y0]]],
            },
        })

        if mosaic is not None:
            offset = (int(round(col * stride / args.gsd)), int(round(row * stride / args.gsd)))
            mosaic.paste(image, offset)

        if workers <= 1:
            flag = "  (blank)" if blank else ""
            print(f"\r[{index}/{len(tiles)}] {path.name}{flag}   ", end="", flush=True)
    print()

    with open(args.out / "tiles.csv", "w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(records[0].keys()))
        writer.writeheader()
        writer.writerows(records)

    (args.out / "tiles.geojson").write_text(json.dumps({
        "type": "FeatureCollection",
        "crs": {"type": "name", "properties": {"name": "urn:ogc:def:crs:EPSG::2180"}},
        "features": features,
    }))

    meta = {
        "source": args.source,
        "url": url,
        "layer": wmts.layer if wmts else args.layer,
        "crs": CRS,
        "gsd": args.gsd,
        "tile_px": args.tile,
        "overlap": args.overlap,
        "area_bbox": area_bbox,
        "n_tiles": len(tiles),
        "n_blank": sum(record["blank"] for record in records),
    }
    if wmts:
        meta.update({
            "wmts_tms": wmts.tms_id,
            "wmts_matrix": matrix["id"],
            "wmts_res": matrix["res"],
        })
    if kind in ("wms-time", "tif"):
        histogram = {}
        for record in records:
            histogram[record["date"]] = histogram.get(record["date"], 0) + 1
        meta.update({
            "time": args.time,
            "summer": args.summer,
            "months": list(months),
            "sheets": sheets_meta(chosen, months),
            "tile_dates": histogram,
        })
    (args.out / "meta.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False))

    if mosaic is not None:
        if max(mosaic.size) > 6000:
            scale = 6000 / max(mosaic.size)
            mosaic = mosaic.resize((int(mosaic.width * scale), int(mosaic.height * scale)))
        mosaic.save(args.out / "preview.jpg", quality=90)

    print(f"Done: {len(tiles)} tiles, {meta['n_blank']} blank -> {args.out}")


if __name__ == "__main__":
    main()
