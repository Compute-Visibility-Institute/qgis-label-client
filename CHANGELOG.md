# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and `qgis-plugin-ci` copies the
most recent entries into `metadata.txt` at release time.

## [Unreleased]

### Added

- **Publish local layers…** — a one-time bootstrap that reads the vector layers already
  open in the QGIS project and POSTs them to the backend as the founding dataset,
  replacing the `tools/load_snapshot.py` command-line loader that an analyst was never
  going to run.

  Nothing is sent until a preview dialog is confirmed. It shows each layer, its feature
  count, the class it would map to — guessed from the layer name, always overridable from
  a combo box populated from the live class registry — and a checkbox per layer. It also
  says the two things the loader had to print in a terminal nobody read: how many names
  carry the UTF-7 truncation signature (`数据中心` stored as `数据中X8`), and which classes
  are being published with no `labeled_extent` declared for them.

  Legacy columns are matched onto the class's own `attr_schema` rather than through a
  lookup table, so `No. Cooler` and `No. Coolim` converge on one attribute without the
  plugin containing either name. Empty values are omitted rather than written as nulls,
  single-part geometries are promoted to the multi-part type the class declares, anything
  not already in EPSG:4326 is reprojected, and invalid geometries are skipped and reported
  instead of being sent for the server to reject.

  Runs in a `QgsTask` with progress and cancellation. Per-feature failures are counted and
  attributed without aborting the run. Each published layer is stamped with a custom
  property, so a second publish is warned about rather than silently doubling the data —
  the server assigns identity, so nothing client-side can deduplicate.

## [0.1.0] - 2026-08-23

### Added

- Token authentication against the labeling API, stored in `QgsAuthManager` and referenced
  from layer URIs by its seven-character id. No credential is ever written to a `.qgz` or
  to plugin settings.
- Signed imagery URL refresh: fetches fresh short-lived URLs and re-points raster layer
  sources, preserving each layer's renderer so a false-colour NIR view survives the swap.
- Collection discovery from `/collections`, and layer configuration driven entirely by the
  backend's class registry — categorized renderer, `class_id` value map, field aliases and
  map tips.
- As-of-date control on valid time, sent either as the OGC `datetime` parameter or as a
  CQL2 filter over `valid_from`/`valid_to`.
- QA: a label's full edit history keyed on its immutable `label_id`, and a coverage check
  that flags labels sitting outside any exhaustive `labeled_extent` for their class.
- Dock panel, toolbar entry and Plugins-menu entries, all detached on unload.

[Unreleased]: https://github.com/Compute-Visibility-Institute/qgis-label-client/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/Compute-Visibility-Institute/qgis-label-client/releases/tag/v0.1.0
