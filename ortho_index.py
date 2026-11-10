#!/usr/bin/env python3
"""Orthophoto flight index for the Polish Geoportal (GUGiK).

The national orthophoto mosaic published by GUGiK is a composite of many
acquisition campaigns ("flights"), and the mosaic always shows the most recent
one. Each flight has its own date, ground resolution and colour variant, and
the same area is usually covered by several flights from different years.

This module reads the WFS index `gugik:SkorowidzOrtofomapy<year>` and exposes
per-sheet metadata:

* acquisition date              (`akt_data`)
* ground sample distance        (`piksel`, metres)
* colour variant                (RGB or CIR)
* direct GeoTIFF download link  (`url_do_pobrania`)
* sheet footprint                (GML polygon, EPSG:2180)

The footprints allow a tile to be attributed to the sheet that covers it, which
is what makes it possible to build a mosaic from a single chosen campaign
instead of mixing whatever the server considers current.

The WFS endpoint does not offer GeoJSON, so the GML 3.2 response is parsed
directly.

Usage::

    python ortho_index.py --center 52.085 21.115 --size 1000
    python ortho_index.py --center 52.085 21.115 --months 6-8
    python ortho_index.py --bbox2180 644369 470370 645369 471370 --all
"""

from __future__ import annotations

import argparse
import datetime as dt
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Iterable, Optional

import requests

WFS_URL = "https://mapy.geoportal.gov.pl/wss/service/PZGIK/ORTO/WFS/Skorowidze"
HEADERS = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0"}
CRS = "EPSG:2180"

SEASONS = {
    "winter": (12, 1, 2),
    "spring": (3, 4, 5),
    "summer": (6, 7, 8),
    "autumn": (9, 10, 11),
}


def local_name(tag: str) -> str:
    """Strip the XML namespace from a tag name."""
    return tag.rsplit("}", 1)[-1]


def season_of(date: Optional[dt.date]) -> str:
    if date is None:
        return "unknown"
    for name, months in SEASONS.items():
        if date.month in months:
            return name
    return "unknown"


@dataclass
class Sheet:
    """One indexed sheet of one acquisition flight."""

    sheet_id: str
    year: int
    date: Optional[dt.date]
    gsd: float
    color: str
    url: Optional[str]
    ring: list = field(default_factory=list)   # [(easting, northing), ...] in EPSG:2180
    source: Optional[str] = None
    size_mb: Optional[float] = None

    @property
    def season(self) -> str:
        return season_of(self.date)

    @property
    def bbox(self):
        """Axis-aligned envelope of the sheet footprint, or None."""
        if not self.ring:
            return None
        xs = [point[0] for point in self.ring]
        ys = [point[1] for point in self.ring]
        return min(xs), min(ys), max(xs), max(ys)

    def covers(self, x: float, y: float) -> bool:
        """Return True if the point (easting, northing) falls inside the sheet."""
        if not self.ring:
            return False
        envelope = self.bbox
        if not (envelope[0] <= x <= envelope[2] and envelope[1] <= y <= envelope[3]):
            return False
        inside = False
        count = len(self.ring)
        for i in range(count):
            x1, y1 = self.ring[i]
            x2, y2 = self.ring[(i + 1) % count]
            if (y1 > y) != (y2 > y):
                crossing = (x2 - x1) * (y - y1) / (y2 - y1) + x1
                if x < crossing:
                    inside = not inside
        return inside

    def as_dict(self) -> dict:
        return {
            "sheet_id": self.sheet_id,
            "year": self.year,
            "date": self.date.isoformat() if self.date else None,
            "season": self.season,
            "gsd": self.gsd,
            "color": self.color,
            "url": self.url,
            "source": self.source,
        }


def _parse_poslist(text: str):
    """Parse a GML posList.

    WFS 2.0 returns EPSG:2180 in authority axis order (northing, easting),
    while the rest of this project works with (easting, northing). The pairs
    are therefore swapped here.
    """
    values = [float(v) for v in text.replace(",", " ").split()]
    return [(values[i + 1], values[i]) for i in range(0, len(values) - 1, 2)]


def _parse_feature(feature) -> Optional[Sheet]:
    props, ring = {}, []
    for element in feature:
        name = local_name(element.tag)
        if name == "boundedBy":
            continue
        if name == "msGeometry":
            for node in element.iter():
                if local_name(node.tag) == "posList" and node.text:
                    ring = _parse_poslist(node.text)
                    break
            continue
        if name == "akt_data":
            instant = next((n for n in element.iter() if local_name(n.tag) == "timePosition"), None)
            props["akt_data"] = instant.text if instant is not None else None
        else:
            props[name] = (element.text or "").strip()

    sheet_id = props.get("godlo")
    if not sheet_id:
        return None

    date = None
    raw_date = props.get("akt_data")
    if raw_date:
        try:
            date = dt.date.fromisoformat(raw_date[:10])
        except ValueError:
            date = None

    try:
        year = int(props.get("akt_rok") or (date.year if date else 0))
    except ValueError:
        year = date.year if date else 0

    try:
        gsd = float(props.get("piksel") or 0)
    except ValueError:
        gsd = 0.0

    try:
        size_mb = float(props.get("wlk_pliku_mb") or 0) or None
    except ValueError:
        size_mb = None

    return Sheet(
        sheet_id=sheet_id,
        year=year,
        date=date,
        gsd=gsd,
        color=(props.get("kolor") or "?").upper(),
        url=props.get("url_do_pobrania"),
        ring=ring,
        source=props.get("zrodlo_danych"),
        size_mb=size_mb,
    )


def fetch_sheets(
    bbox2180,
    years: Iterable[int] = (),
    session: Optional[requests.Session] = None,
    timeout: int = 60,
    max_per_year: int = 500,
) -> list:
    """Return every flight sheet intersecting the area, year by year.

    Years without coverage simply yield no features and are skipped. Results
    are de-duplicated on (sheet id, year, date, colour variant).
    """
    if not years:
        years = range(2018, dt.date.today().year + 1)

    session = session or requests.Session()
    session.headers.setdefault("User-Agent", HEADERS["User-Agent"])

    xmin, ymin, xmax, ymax = bbox2180
    # The bbox parameter follows the same authority axis order as the geometry.
    bbox = f"{ymin:.1f},{xmin:.1f},{ymax:.1f},{xmax:.1f},{CRS}"

    sheets, seen = [], set()
    for year in years:
        start = 0
        while True:
            params = {
                "service": "WFS",
                "version": "2.0.0",
                "request": "GetFeature",
                "typeNames": f"gugik:SkorowidzOrtofomapy{year}",
                "srsname": CRS,
                "bbox": bbox,
                "count": str(max_per_year),
                "STARTINDEX": str(start),
            }
            try:
                response = session.get(WFS_URL, params=params, timeout=timeout)
            except requests.RequestException:
                break
            if response.status_code != 200 or b"ExceptionReport" in response.content[:400]:
                break
            try:
                root = ET.fromstring(response.content)
            except ET.ParseError:
                break

            members = [el for el in root if local_name(el.tag) == "member"]
            if not members:
                break

            for member in members:
                children = list(member)
                if not children:
                    continue
                sheet = _parse_feature(children[0])
                if sheet is None:
                    continue
                key = (sheet.sheet_id, sheet.year, sheet.date, sheet.color)
                if key in seen:
                    continue
                seen.add(key)
                sheets.append(sheet)

            if len(members) < max_per_year:
                break
            start += len(members)

    return sheets


def in_box(sheet: Sheet, bbox2180) -> bool:
    """True if the sheet envelope intersects the area."""
    envelope = sheet.bbox
    if not envelope:
        return False
    xmin, ymin, xmax, ymax = bbox2180
    return not (envelope[2] < xmin or envelope[0] > xmax
                or envelope[3] < ymin or envelope[1] > ymax)


def pick_dates(
    sheets: list,
    bbox2180,
    months=(6, 7, 8),
    prefer: str = "newest",
    min_gsd: Optional[float] = None,
) -> dict:
    """Choose the best flight for every sheet intersecting the area.

    Parameters
    ----------
    months:
        Accepted acquisition months; defaults to June-August.
    prefer:
        ``"newest"`` takes the most recent flight within the season,
        ``"sharpest"`` takes the finest ground sample distance first.
    min_gsd:
        Discard flights coarser than this value, in metres per pixel.

    Returns
    -------
    dict
        ``{sheet_id: Sheet}``. A sheet with no in-season flight falls back to
        its most recent available flight so that the mosaic has no holes; the
        ``in_season`` flag in the metadata records that this happened.
    """
    by_sheet = {}
    for sheet in sheets:
        if sheet.color not in ("RGB", "?"):
            continue                      # CIR has false colours; unusable for colour work
        if not in_box(sheet, bbox2180):
            continue
        if min_gsd is not None and sheet.gsd and sheet.gsd > min_gsd:
            continue
        by_sheet.setdefault(sheet.sheet_id, []).append(sheet)

    chosen = {}
    for sheet_id, variants in by_sheet.items():
        in_season = [s for s in variants if s.date and s.date.month in months]
        pool = in_season or variants
        if prefer == "sharpest":
            pool.sort(key=lambda s: (s.gsd or 99, -(s.date or dt.date.min).toordinal()))
        else:
            pool.sort(
                key=lambda s: ((s.date or dt.date.min).toordinal(), -(s.gsd or 0)),
                reverse=True,
            )
        chosen[sheet_id] = pool[0]
    return chosen


def is_in_season(sheet: Sheet, months=(6, 7, 8)) -> bool:
    """True if the sheet was acquired within the accepted months."""
    return bool(sheet.date and sheet.date.month in months)


def _parse_span(spec: str):
    """Parse ``"6-8"`` or ``"5,7,9"`` into a tuple of integers."""
    if "," in spec:
        return tuple(int(part) for part in spec.split(","))
    if "-" in spec:
        start, end = spec.split("-")
        return tuple(range(int(start), int(end) + 1))
    return (int(spec),)


def main():
    from pyproj import Transformer

    parser = argparse.ArgumentParser(
        description="List orthophoto flights available for an area"
    )
    area = parser.add_mutually_exclusive_group(required=True)
    area.add_argument("--center", nargs=2, type=float, metavar=("LAT", "LON"))
    area.add_argument("--bbox", nargs=4, type=float,
                      metavar=("LON_MIN", "LAT_MIN", "LON_MAX", "LAT_MAX"))
    area.add_argument("--bbox2180", nargs=4, type=float,
                      metavar=("XMIN", "YMIN", "XMAX", "YMAX"))
    parser.add_argument("--size", type=float, default=1000.0,
                        help="side of the square in metres (with --center)")
    parser.add_argument("--years", default="2018-2026",
                        help="year range to query, e.g. 2018-2026")
    parser.add_argument("--months", default="6-8",
                        help="accepted acquisition months, e.g. 6-8 or 5,7,9")
    parser.add_argument("--all", action="store_true",
                        help="list every flight instead of the summary only")
    args = parser.parse_args()

    years = _parse_span(args.years)
    months = _parse_span(args.months)

    if args.center:
        x, y = Transformer.from_crs("EPSG:4326", CRS, always_xy=True).transform(
            args.center[1], args.center[0]
        )
        half = args.size / 2
        bbox = (x - half, y - half, x + half, y + half)
        label = f"{args.center[0]:.5f}, {args.center[1]:.5f} ({args.size:.0f} m)"
    elif args.bbox:
        transformer = Transformer.from_crs("EPSG:4326", CRS, always_xy=True)
        points = [
            transformer.transform(lon, lat)
            for lon in (args.bbox[0], args.bbox[2])
            for lat in (args.bbox[1], args.bbox[3])
        ]
        xs, ys = zip(*points)
        bbox = (min(xs), min(ys), max(xs), max(ys))
        label = f"bbox {args.bbox}"
    else:
        bbox = tuple(args.bbox2180)
        label = f"bbox2180 {bbox}"

    print(f"Area: {label}")
    print(f"EPSG:2180 bbox: {bbox[0]:.0f}, {bbox[1]:.0f}, {bbox[2]:.0f}, {bbox[3]:.0f}\n")

    sheets = fetch_sheets(bbox, years=years)
    if not sheets:
        print("No flights indexed for this area and year range.")
        return

    print(f"Sheets in index: {len(sheets)}  (years {min(years)}-{max(years)})\n")
    print(f"{'sheet':24s} {'date':12s} {'season':8s} {'gsd':>6s} {'color':6s} {'size':>8s}  url")
    print("-" * 112)
    shown = sheets if args.all else sorted(
        sheets, key=lambda s: ((s.date or dt.date.min).toordinal(), s.sheet_id), reverse=True
    )
    for sheet in shown:
        mark = "  <-- IN SEASON" if sheet.date and sheet.date.month in months else ""
        size = f"{sheet.size_mb:.0f}" if sheet.size_mb else "-"
        print(f"{sheet.sheet_id:24s} {str(sheet.date):12s} {sheet.season:8s} "
              f"{sheet.gsd:6.2f} {sheet.color:6s} {size:>6s}MB  {(sheet.url or '')[-48:]}{mark}")

    chosen = pick_dates(sheets, bbox, months=months)
    in_season = sum(1 for sheet in chosen.values() if is_in_season(sheet, months))
    print(f"\nSelected flights for {len(chosen)} sheets, in season {months}: {in_season}")
    for sheet_id, sheet in sorted(chosen.items()):
        flag = "OK " if is_in_season(sheet, months) else "!! "
        print(f"  {flag}{sheet_id:24s} {sheet.date}  {sheet.gsd:.2f} m  {sheet.color}")


if __name__ == "__main__":
    main()
