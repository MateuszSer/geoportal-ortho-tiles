# Geoportal Ortho Tiles

Download Polish national orthophoto from the [Geoportal](https://www.geoportal.gov.pl)
(GUGiK) and cut it into georeferenced tiles ready for machine learning or GIS work.

Geoportals publish a *single current mosaic*, which is composited from the most recent
acquisition campaign per sheet. Those campaigns are usually flown in early spring, so the
default imagery shows bare trees, drained ponds and covered swimming pools. This tool
reads the official flight index, so you can request a **specific campaign by date** — for
example a summer flight — and still get plain, georeferenced tiles on an EPSG:2180 grid.

```bash
python geoportal.py --center 52.085 21.115 --size 1000 \
    --source opendata --summer --out data/area --mosaic
```

## Features

- Four imagery backends: WMTS mosaic, time-enabled WMS archive, native GeoTIFF sheets,
  and Esri World Imagery as a fallback.
- Flight selection by season, explicit date, resolution or preference (newest / sharpest).
- Tiles are attributed to the sheet that covers them, so a mosaic can stay on one campaign
  instead of mixing whatever the server considers current.
- Output: PNG/JPG tiles, world files (`.pgw` / `.jgw`), CSV index, GeoJSON footprints,
  JSON metadata and an optional mosaic preview.
- Resumable: existing tiles are skipped, so an interrupted run can simply be repeated.
- Reprojection of Web Mercator imagery onto the EPSG:2180 grid without GDAL.

## Requirements

- Python 3.9 or newer
- `requests`, `pyproj`, `pillow`

## Installation

```bash
git clone https://github.com/MateuszSer/geoportal-ortho-tiles.git
cd geoportal-ortho-tiles
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## Quick start

```bash
# 1. Which flights exist for this area?
python ortho_index.py --center 52.085 21.115 --size 1000 --years 2019-2026

# 2. Download a summer flight as tiles
python geoportal.py --center 52.085 21.115 --size 1000 \
    --source opendata --summer \
    --out data/area --mosaic --workers 6

# 3. Check the result
python -c "import json; print(json.load(open('data/area/meta.json'))['tile_dates'])"
```

Before downloading, the tool prints the flights it selected, which is the quickest way to
confirm that the requested season was actually available:

```
Selected flights: 2 -> 2024-07-18 (0.25 m), 2026-04-05 (0.05 m)
```

That list covers every sheet intersecting the area, so it can include an out-of-season
fallback for sheets that have no summer flight. The authoritative per-tile answer is
`tile_dates` in `meta.json`.

## Usage

### Area

Exactly one of `--center`, `--bbox` or `--bbox2180` is required.

| Flag | Example | Meaning |
| --- | --- | --- |
| `--center LAT LON` | `--center 52.085 21.115` | centre of a square; side set by `--size` |
| `--bbox LON_MIN LAT_MIN LON_MAX LAT_MAX` | `--bbox 21.10 52.08 21.13 52.10` | area as a WGS84 bounding box |
| `--bbox2180 XMIN YMIN XMAX YMAX` | `--bbox2180 644369 470370 645369 471370` | area directly in EPSG:2180 |
| `--size METRES` | `--size 1000` (default) | side of the square, with `--center` only |

### Imagery source

| Source | Product | Notes |
| --- | --- | --- |
| `wmts-standard` | national mosaic, 25 cm | default; usually a spring campaign |
| `wmts-high` | high-resolution mosaic, 10 cm | cities and other 10 cm coverage |
| `wms-high` | same product over WMS | alternative to `wmts-high` |
| `wms-standard` | WMS counterpart of the mosaic | returns 404 at the time of writing |
| `wms-time-std` | time-enabled WMS archive, 25 cm | date selected with `TIME` |
| `wms-time-high` | time-enabled WMS archive, 10 cm | date selected with `TIME` |
| `opendata` | native GeoTIFF sheets | full resolution, most reliable |
| `esri` | Esri World Imagery | fallback, reprojected to EPSG:2180 |

`wmts-*` and `wms-high` expose only the current mosaic. Passing `--summer` or `--time`
with one of them switches automatically to the corresponding `wms-time-*` source.

### Flight selection

| Flag | Default | Purpose |
| --- | --- | --- |
| `--season NAME` | `summer` for date-aware sources | campaign season: `spring`, `summer`, `autumn`, `winter` or `all` |
| `--summer` | off | shorthand for `--season summer` |
| `--time YYYY-MM-DD` | — | request an exact acquisition date |
| `--months 5,7,9` | — | explicit month list; overrides `--season` |
| `--prefer newest\|sharpest` | `newest` | newest flight within the season, or finest ground sample distance |
| `--min-gsd METRES` | — | discard flights coarser than this value |
| `--years 2018-2026` | `2018-2026` | years queried in the WFS index |
| `--list-flights` | — | print available flights and exit |

### Tiles and output

| Flag | Default | Purpose |
| --- | --- | --- |
| `--tile PX` | `640` | tile side in pixels |
| `--tile-size LENGTH` | `tile × gsd` (160 m) | tile footprint on the ground: `160`, `160m` or `0.5km` |
| `--gsd METRES` | `0.25` | ground sample distance; footprint is `tile × gsd` when `--tile-size` is not used |
| `--overlap 0..0.9` | `0.0` | tile overlap; 0.1–0.2 is useful when objects fall on tile edges |
| `--format png\|jpg` | `png` | image format; PNG writes `.pgw`, JPG writes `.jgw` |
| `--mosaic` | off | additionally write `preview.jpg` for the whole area |
| `--max-tiles N` | `500` | abort above this tile count |
| `--out DIR` | `tiles_out` | output directory; existing tiles are skipped |

### Network and performance

| Flag | Default | Purpose |
| --- | --- | --- |
| `--workers N` | `0` (auto: 4 for WMS/TIF, 1 for WMTS) | parallel tile downloads |
| `--retries N` | `3` | retries when the WMS archive returns an empty tile |
| `--sleep SECONDS` | `0.1` | pause between requests |
| `--tif-cache DIR` | `OUT/_tif` | shared GeoTIFF cache; reuse it across areas |

## Output

```
data/area/
├── images/
│   ├── tile_r000_c000.png      tile
│   └── tile_r000_c000.pgw      world file (.jgw with --format jpg)
├── tiles.csv                   name,row,col,xmin,ymin,xmax,ymax,blank[,date]
├── tiles.geojson               tile footprints, EPSG:2180
├── meta.json                   source, grid, sheet selection, date histogram
├── preview.jpg                 mosaic (with --mosaic)
├── _tif/                       GeoTIFF cache (source opendata)
└── _wmts_cache/                server tile cache (wmts-* sources)
```

`meta.json` carries the parameters used and, for date-aware sources, a per-tile breakdown:

```json
{
  "source": "opendata",
  "crs": "EPSG:2180",
  "gsd": 0.25,
  "tile_px": 640,
  "tile_size_m": 160.0,
  "n_tiles": 49,
  "n_blank": 0,
  "sheets": [{"sheet_id": "N-34-139-C-a-4-4", "date": "2024-07-18", "gsd": 0.25,
              "color": "RGB", "in_season": true, "url": "…"}],
  "tile_dates": {"2024-07-18": 49}
}
```

`tile_dates` is the field to inspect when you need to know whether the mosaic is
radiometrically consistent: more than one date means a sheet boundary crosses the area.

## Choosing parameters

Tile footprint is `--tile × --gsd`. If you prefer to think in ground units, pass
`--tile-size` instead — it converts to pixels using `--gsd`, so the resolution stays fixed:

```bash
# 500 m tiles, still at 25 cm/pixel -> 2000 x 2000 px per tile
python geoportal.py --center 52.085 21.115 --size 2000 --source opendata \
    --tile-size 0.5km --out data/big-tiles --mosaic
```

The tile count grows with the square of the area:

| Area (`--size`) | `--tile 640`, `--gsd 0.25` (160 m) | `--tile 1024`, `--gsd 0.25` (256 m) |
| --- | --- | --- |
| 500 m | 16 | 4 |
| 1000 m | 49 | 16 |
| 2000 m | 169 | 64 |
| 3000 m | 361 | 144 |
| 4000 m | 625 (raise `--max-tiles`) | 256 |

- A 640 × 640 PNG tile is roughly 0.6 MB, so 49 tiles take about 31 MB.
- One 25 cm sheet is about 44 MB; a 5 cm sheet is 0.3–1.2 GB.
- A `--gsd` finer than the available imagery only upsamples; the tool warns when the
  WMTS source cannot match the request.
- `--tile 1024` means fewer requests; `--tile 512` means more, smaller tiles.

## How it works

1. The area is converted to an EPSG:2180 bounding box and split into a regular tile grid.
2. For date-aware sources, `ortho_index` queries the WFS index for every year, parses the
   GML footprints and metadata, and selects one flight per sheet.
3. Each tile is attributed to the sheet covering its centre, then:
   - `opendata` crops the tile from the cached GeoTIFF sheet,
   - `wms-time-*` requests it with that sheet's `TIME`,
   - `wmts-*` composes it from the server tile cache.
4. Tiles are written with a world file, and the area index, metadata and optional
   mosaic are produced.

More background on data availability and service behaviour: [docs/background.md](docs/background.md).

## Data sources

| Source | Endpoint |
| --- | --- |
| WMTS mosaics | `mapy.geoportal.gov.pl/wss/service/PZGIK/ORTO/WMTS/{Standard,High}Resolution` |
| WMS archive | `mapy.geoportal.gov.pl/wss/service/PZGIK/ORTO/WMS/{Standard,High}ResolutionTime` |
| Flight index | `mapy.geoportal.gov.pl/wss/service/PZGIK/ORTO/WFS/Skorowidze` |
| GeoTIFF sheets | `opendata.geoportal.gov.pl/ortofotomapa/` |
| Esri World Imagery | `server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer` |

GUGiK data is open and free to use; attribute it as *GUGiK / geoportal.gov.pl*. Esri World
Imagery requires attribution, and bulk downloading conflicts with its terms of use — treat
`--source esri` as a verification aid rather than a production source.

## Troubleshooting

| Symptom | Cause | Fix |
| --- | --- | --- |
| Every tile is `blank=1` | no data for the requested date | `--list-flights`; check `in_season` in `meta.json` |
| Tiles are spring imagery despite `--season` | no flight exists in that season for some sheets | the run prints a `note:` line; check `tile_dates` in `meta.json` |
| `Too many tiles (N > 500)` | area too large for the grid | reduce `--size`, increase `--tile` or `--gsd`, or raise `--max-tiles` |
| Empty tiles from `wms-time-*` | the archive returns blank images intermittently | increase `--retries`, reduce `--workers`, or switch to `--source opendata` |
| `OSError: decoder error` on a sheet | truncated download | delete the file from the cache and rerun |
| Requests take tens of seconds | server-side throttling | lower `--workers`, raise `--retries` |
| `ModuleNotFoundError` | dependencies missing | `.venv/bin/pip install -r requirements.txt` |

## Limitations

- 5 cm imagery is only available from spring campaigns; summer flights are typically 25 cm.
  A 25 cm mosaic still resolves a domestic pool (8 × 4 m) as roughly 32 × 16 pixels.
- Winter imagery does not exist in the national product: campaigns run roughly April to
  October, and the only index entry falling in December–February is a 2007 sheet carrying a
  year-only placeholder date (`2007-01-01`) whose scene is summer. `--season winter` and
  `--season autumn` work, but they will fall back and print a note when no such flight exists.
- A mosaic is not guaranteed to come from a single date. Sheets are selected independently,
  and `tile_dates` reports the result.
- The time-enabled WMS archive is convenient but unreliable; the GeoTIFF path is preferred
  for anything reproducible.
- Sheet overlap exists between two sheet grids, which is why selection prefers in-season
  sheets before falling back to the most recent one.
- No license file is included yet.

## Project structure

| Path | Purpose |
| --- | --- |
| `geoportal.py` | command line entry point: download and tile |
| `ortho_index.py` | WFS flight index: dates, resolution, colour, GeoTIFF links |
| `docs/background.md` | data availability, service behaviour, verification notes |
| `requirements.txt` | dependencies |

## Acknowledgements

Built with assistance from [DeepSeek Harness](https://github.com/deepseek-ai), an AI coding
agent, which contributed the initial implementation, the service research and the
documentation.
