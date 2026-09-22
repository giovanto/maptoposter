# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased] - Community Contributions

### Added (2026-08-21 — ports of open upstream PRs)
- **Sea/ocean rendering from coastline** ([PR #193](https://github.com/originalankur/maptoposter/pull/193) by @utaysi) — polygonizes the viewport against OSM `natural=coastline` ways and classifies land/water by the OSM direction convention; coastal cities finally get their sea
- **Line-mapped rivers** ([PR #227](https://github.com/originalankur/maptoposter/pull/227) by @gedankenstuecke) — `waterway=river` LineStrings drawn in the water color, width follows `--line-scale`; adapted to the cached `fetch_features` path
- **`--mark <lat,lon> <Text> <position>`** ([PR #219](https://github.com/originalankur/maptoposter/pull/219) by @5H4DE) — repeatable point markers with text labels; label offset made viewport-relative, sizes follow the poster scale factor
- **`--gpx <file>`** ([PR #224](https://github.com/originalankur/maptoposter/pull/224) by @tmchow) — travel route overlay; all themes gained a `route_color` key
- **`--line-scale <f>`** ([PR #194](https://github.com/originalankur/maptoposter/pull/194) by @utaysi) — road thickness multiplier
- **`--output-directory <dir>`** ([PR #152](https://github.com/originalankur/maptoposter/pull/152) by @fgtham)
- **OSMnx response cache** ([PR #146](https://github.com/originalankur/maptoposter/pull/146) by @cbrunnkvist)

### Fixed (2026-08-21)
- **Coordinate footer for western/southern hemispheres** — showed a minus sign together with the hemisphere letter (e.g. `-74.0060° W`); now prints `74.0060° W`

### Added (2026-08-30)
- **`--no-gradient`**: disables the top and bottom gradient fades while keeping the title block
- **`--title-scale <f>`**: multiplier for title, subtitle and coordinate type size (default 1.0)
- **`--point-scale <f>`**: multiplier for the `--point` ring marker (default 1.0)
- **Theme iterations**: `terracotta_v2`, `midnight_blue_v2`, `neon_cyberpunk_v2`

### Added (2026-09-21)
- **`--edge-marks`**: a `--mark` outside the viewport is drawn as a direction stone on the frame edge, a triangle pointing at the real bearing plus the label and the true distance in km, instead of being silently clipped. Lets a tight same-scale series reference places far outside the frame.
- **`--no-attribution`**: omits the OpenStreetMap credit. ODbL still requires attribution for any produced work you publish, distribute or sell; intended for private prints only.

### Added (2026-09-22)
- **Local PBF data source** (`MAPTOPOSTER_SOURCE=pbf`, `MAPTOPOSTER_PBF=<planet.osm.pbf>`): frames are cut with `osmium extract -s smart`, the street graph built with `osmnx.graph_from_xml` (highway ways only, `MAPTOPOSTER_NETWORK` honoured), features read with `osmium tags-filter` + `osmium export`, clipped to the frame. No Overpass, no rate limits, any frame size; a 7.5 km frame renders in ~15 s. Verified layer-for-layer against Overpass on the same frame (buildings, parks, forests, water within 0.1 %).
- **`--sticker LAT,LON PNG TEXT`** (repeatable) and **`--sticker-size` / `--sticker-mm`**: per-location image markers, sized in ground metres or in millimetres on the sheet; outside the frame they take the edge position (with `--edge-marks`) and print their distance.
- **`--scalebar`**: fixed-length bar in the mat (a fifth of the map width) labelled with the distance it spans.
- **`--coords-at LAT,LON`**: footer coordinates independent of the frame centre (for recentred frames).
- **`--edge-marks`**, **`--no-attribution`**, **`--network` env**, **`OVERPASS_TIMEOUT`**, **`road_widths.minor`** theme key (footways/paths/cycleways as their own width class).
- Theme `herbarium_green`: sustainable modes lead in green, cars recede toward paper.

- **Transit as two layers**: trams and light rail at full weight; heavy rail, narrow gauge and surface metro (`MAPTOPOSTER_TRANSIT_HEAVY`) thinner and lighter, scale-aware, tunnels excluded. Orange now reads consistently as "rail transport you can see".

- **Edge anchors** carry an outward arrow and a readable boxed label; `--edge-label full|distance|none`.
- **`--door-mm`**: the first sticker (the door) can take its own paper size, e.g. a larger photo cutout.
- Edge-anchor labels set in the footer's voice (light, letter-spaced, muted).

### Fixed (2026-09-22)
- Feature cache keys (pickle and osmium export) now encode tag values, not just keys; a broadened layer could be served for a narrower request.
- Polygon layers were reprojected with `project_gdf`, which picks a UTM zone from the layer's own centroid; a layer spanning far west of the frame landed in a different zone than the streets and was painted off-axis. All layers now use the graph's CRS.
- Graph cache key ignored the network type; a `drive` graph could be served for an `all` request.
- Icon fringing: icons are resampled with premultiplied alpha and drawn 1:1.

### Fixed (2026-09-21)
- **`--icon` fringing**: icons were resampled by `imshow` with unpremultiplied alpha and a lanczos kernel, whose negative lobes ring at a hard silhouette and leave a stippled halo, worst when a large cutout is scaled far down. Icons are now resampled with premultiplied alpha to the exact target pixel size and drawn 1:1.

### Added
- **uv package manager support** ([PR #20](https://github.com/originalankur/maptoposter/pull/20))
  - Added `pyproject.toml` with project metadata and dependencies
  - Added `uv.lock` for reproducible builds
  - Added shebang to `create_map_poster.py` for direct execution
  - Updated README with uv installation instructions
- **Python version specification** - `requires-python = ">=3.11"` in pyproject.toml (fixes [#79](https://github.com/originalankur/maptoposter/issues/79))
- **Coordinate override** - `--latitude` and `--longitude` arguments to override the geocoded center point (existing from upstream PR #106, clarifies [#100](https://github.com/originalankur/maptoposter/issues/100))
  - Still requires `--city` and `--country` for display name
  - Useful for precise location control

### Fixed
- **Z-order bug** - Roads now render above parks and water features (fixes [#39](https://github.com/originalankur/maptoposter/issues/39), relates to [PR #42](https://github.com/originalankur/maptoposter/pull/42))
  - Water layer: `zorder=1` → `zorder=0.5`
  - Parks layer: `zorder=2` → `zorder=0.8`
  - Roads remain at `zorder=2` (matplotlib default), ensuring proper layering
- **Text scaling for landscape orientations** - Font size now scales based on `min(height, width)` instead of just width (fixes [#112](https://github.com/originalankur/maptoposter/issues/112))

### Changed
- Updated `.gitignore` with poster outputs, Python build artifacts, IDE files, and OS-specific files

---

## [0.3.0] - 2026-01-27 (Maintainer: @originalankur)

### Added
- **Custom coordinates support** - `--latitude` and `--longitude` arguments ([#106](https://github.com/originalankur/maptoposter/pull/106))
- **Emerald theme** - Lush dark green aesthetic with mint accents ([#114](https://github.com/originalankur/maptoposter/pull/114))
- **GitHub Actions** - PR checks workflow ([#98](https://github.com/originalankur/maptoposter/pull/98))
- **Conflict labeling** - Auto-label PRs with merge conflicts

### Changed
- **Default theme** changed from `feature_based` to `terracotta` ([#131](https://github.com/originalankur/maptoposter/pull/131))
- **Default distance** changed from 12000m to 18000m ([#128](https://github.com/originalankur/maptoposter/pull/128))
- **Max dimensions** enforced at 20 inches for width/height (supports up to 4K resolution) ([#128](https://github.com/originalankur/maptoposter/pull/128), [#129](https://github.com/originalankur/maptoposter/pull/129))

### Removed
- `feature_based` theme ([#131](https://github.com/originalankur/maptoposter/pull/131))

### Fixed
- Cache directory handling ([#109](https://github.com/originalankur/maptoposter/pull/109))
- Dynamic font scaling based on poster width

---

## [0.2.1] - 2026-01-18 (Maintainer: @originalankur)

### Added
- **SVG/PDF export** - `--format` flag for vector output ([#57](https://github.com/originalankur/maptoposter/pull/57))
- **Variable poster dimensions** - `-W` and `-H` arguments ([#59](https://github.com/originalankur/maptoposter/pull/59))
- **Caching** - Downloaded OSM data is now cached locally
- **Rate limiting** - 0.3s delay between API requests

### Fixed
- Map warping issues with variable dimensions ([#59](https://github.com/originalankur/maptoposter/pull/59))
- Edge nodes retention for complete road networks ([#27](https://github.com/originalankur/maptoposter/pull/27))
- Point geometry filtering to prevent dots on maps
- Dynamic font size adjustment for long city names
- Nominatim timeout increased to 10 seconds

### Changed
- Graph projection to linear coordinates for proper aspect ratio
- Improved cache handling with hashed filenames and error handling

---

## [0.2.0] - 2026-01-17 (Tag: v0.2)

### Added
- Example poster images in README
- Initial theme collection

---

## [0.1.0] - 2026-01-17 (Initial Release)

### Added
- Initial maptoposter source code
- README with usage instructions
- 17 built-in themes:
  - autumn, blueprint, contrast_zones, copper_patina
  - forest, gradient_roads, japanese_ink, midnight_blue
  - monochrome_blue, neon_cyberpunk, noir, ocean
  - pastel_dream, sunset, terracotta, warm_beige
- Core features:
  - City/country based map generation
  - Customizable themes via JSON
  - Road hierarchy coloring
  - Water and park feature rendering
  - Typography with Roboto font
  - Coordinate display
  - OSM attribution
