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
