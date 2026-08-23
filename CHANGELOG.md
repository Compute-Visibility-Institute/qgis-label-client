# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and `qgis-plugin-ci` copies the
most recent entries into `metadata.txt` at release time.

## [Unreleased]

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
