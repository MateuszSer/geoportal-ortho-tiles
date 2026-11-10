# Background: data availability and service behaviour

This note records why the default imagery looks the way it does, how the flight index is
structured, and which service quirks the code works around. It is reference material; usage
is documented in the main [README](../README.md).

## Why the default mosaic is spring imagery

The national orthophoto service publishes one current mosaic, composited from the most
recent campaign available for each sheet. GUGiK flies most areas in **early spring**,
typically April–May. That window is preferred for photogrammetric reasons: no foliage
occlusion, better image matching, and lower processing cost.

The consequence for anything that depends on vegetation or water is significant — bare
trees, drained ponds, covered pools, and gardens that do not separate from their surroundings
by colour. Summer conditions (vegetation present, water in pools) require a summer campaign,
and selecting one means going around the current mosaic and asking for a specific date.

## The flight index (WFS)

`Skorowidze` is a WFS 2.0 service with one feature type per year
(`gugik:SkorowidzOrtofomapy<year>`), covering 1957 to the present. Each feature describes
one sheet of one campaign:

| Field | Meaning |
| --- | --- |
| `godlo` | sheet designation, e.g. `N-34-139-C-a-4-4` |
| `akt_data` | acquisition date (`gml:TimeInstant`) |
| `akt_rok` | acquisition year |
| `piksel` | ground sample distance in metres |
| `kolor` | colour variant, `RGB` or `CIR` |
| `zrodlo_danych` | data source, e.g. `Zdj. cyfrowe` |
| `url_do_pobrania` | direct GeoTIFF link on the open data portal |
| `wlk_pliku_mb` | sheet size in megabytes |
| `msGeometry` | sheet footprint as a GML polygon |

Two details matter when consuming this service:

**Axis order.** WFS 2.0 returns EPSG:2180 in authority axis order, `(northing, easting)`.
WMS 1.1.1 with `SRS=EPSG:2180` returns `(easting, northing)`. A bbox or geometry read from
WFS and used directly against WMS silently addresses a different part of the country.
`ortho_index._parse_poslist` swaps the pairs on parse, and `fetch_sheets` writes the bbox
parameter in `(northing, easting)` order.

**Overlapping sheet grids.** Two sheet grids coexist in the index: the current
`N-34-…` series and an older `7.170.…` series. A tile can therefore fall inside several
sheets of the same year, with different dates and resolutions. Selection resolves this by
preferring sheets acquired within the requested months, then the most recent date, then
the finer resolution.

## Access paths

| Path | Resolution | Date control | Reliability |
| --- | --- | --- | --- |
| WMTS mosaic | 25 cm / 10 cm | none | high |
| Time-enabled WMS | 25 cm / 10 cm | `TIME` parameter | medium |
| GeoTIFF sheets | native (5–25 cm) | implied by the sheet | high |
| Esri World Imagery | ~30 cm | none | high, but not Polish national data |

The GeoTIFF path is preferred for reproducible work: the files are the authoritative
product, they carry their own georeferencing, and a local cache makes reruns free. The
time-enabled WMS is convenient for small areas where downloading a 44 MB sheet is wasteful,
but see the quirks below.

## Service quirks

**`WMS/StandardResolution` returns 404.** The non-time WMS counterpart of the standard
mosaic is not served, while `WMS/HighResolution` is. The time-enabled variants
(`StandardResolutionTime`, `HighResolutionTime`) work and are used instead.

**`TIME` must match a campaign date.** The archive maps a requested instant onto the
dataset valid at that time. Requesting a date with no coverage returns a blank image with
HTTP 200 rather than an error. Values close to a real acquisition date work; arbitrary
dates in the same month may not.

**Blank responses under load.** The time-enabled service intermittently returns a small,
uniform image with HTTP 200, and latency ranges from under a second to roughly thirty
seconds. The client retries blank tiles and falls back through a chain of dates
(explicit date → sheet date → current mosaic), which is why `--retries` exists.

**`HighResolutionTime` falls back to standard resolution.** Where no 10 cm data exists for
the requested date, the service returns the 25 cm mosaic instead of failing, so a response
being present does not imply 10 cm content.

**libtiff is not thread-safe.** Decoding tiled GeoTIFFs concurrently makes libtiff share a
global temporary file and fail with `decoder error`. Sheets are therefore decoded once,
under a lock, and then cropped from memory.

## Verification

The behaviour above was confirmed against the live services:

- A tile cropped from a GeoTIFF sheet and the same tile rendered by
  `StandardResolutionTime` with the matching `TIME` differ by a mean absolute error of
  **0.71 / 255** per pixel — well within JPEG and resampling noise, and confirming that
  the sheet georeferencing and the WMS grid agree.
- The same area downloaded through `--source opendata` and through
  `--source wms-time-std` produced identical previews (mean RGB `[61, 70, 67]`) and the
  same per-tile dates.
- Esri imagery reprojected to EPSG:2180 aligns with the GUGiK mosaic at zero pixel offset
  (best alignment found by exhaustive shift search over ±30 px).

## Example: available flights, Konstancin (52.085, 21.115)

| Date | GSD | Season | Note |
| --- | --- | --- | --- |
| 2026-04-05 | 0.05 m | spring | sharpest available, water often covered |
| 2025-04-27 | 0.05 m | spring | |
| 2025-04-04 | 0.05 m | spring | |
| 2024-07-18 | 0.25 m | summer | |
| 2024-04-11 | 0.05 m | spring | |
| 2023-05-11 | 0.25 m | spring | the campaign shown by the current mosaic |
| 2022-06-05 | 0.25 m | summer | |
| 2020-07-24 | 0.25 m | summer | |
| 2019-06-04 | 0.25 m | summer | |

The pattern is typical: high-resolution campaigns are spring, summer campaigns are 25 cm.
Combining the two is often the best approach — spring 5 cm to delineate structures,
summer 25 cm to establish that water is present.
