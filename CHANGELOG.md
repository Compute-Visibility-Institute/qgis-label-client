# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and `qgis-plugin-ci` copies the
most recent entries into `metadata.txt` at release time.

## [Unreleased]

### Added

- **The historical view: what the team believed at a chosen instant.** The panel gains a
  second time control, *Historical view (transaction time)*, and a menu entry beside it.
  Ticking it and pressing **Add historical layer** adds a read-only layer showing the
  labels as they were believed at one instant — including labels deleted since, and the
  superseded geometry of labels edited since.

  **Two time axes, two boxes, two vocabularies.** *Valid time* is when a label was true on
  the ground; *transaction time* is when we believed it. The existing box says **as-of** and
  never "believed"; the new one says **believed** and never "as of". That is not decoration:
  two controls that both said "as of" and meant different axes is the exact failure this
  feature is most exposed to, and a screenshot has to be unambiguous about which question
  produced the map. The status line under both now always names **both** axes, even when one
  is off — each control on its own reads as "the" time control.

  **It adds a layer rather than re-pointing the ones you have**, unlike the as-of control.
  The point of a historical view is comparing it against the live layer, and against another
  historical layer at a different instant.

  **The instant travels as an `X-Recorded-At` request header**, per layer, in the OAPIF
  URI's `http-header:` vocabulary — where `X-Track` already goes. A query parameter cannot
  do this job: `QgsOapifProvider::computeCapabilities` builds the `OPTIONS` probe that
  decides whether a layer is editable and appends **no** query parameters, while
  `sendOPTIONS` **does** install the URI's headers. A query-only pin would therefore report
  the collection writable, QGIS would enable editing, and an edit made on January's map
  would land on the live row — the present edited while looking at the past, silently. As a
  header it is on every request the provider makes, including the probe, so the server
  answers without the write verbs and QGIS greys the pencil out by itself. The plugin also
  holds the layer read-only on its own side, because `setDataSource` recomputes that from
  provider capabilities on every re-point, and says loudly if the server ever advertises a
  pinned layer as writable.

  **The layer is unmistakable in the Layers panel.** `[BELIEVED 2026-01-15 08:00Z] Labels —
  read-only`, with the discriminating token first because the layer tree truncates from the
  right. Dashed strokes at 55% opacity, an alert stroke colour on beliefs that have since
  ended, and one extra map-tip line — *believed until …* — on those. The class colours are
  unchanged, because the two layers have to stay comparable.

  **A canary, as with tracks.** The view echoes the instant it actually resolved at, and the
  layer filter compares it against what was asked for. The backing view falls back to `now()`
  when no instant reaches it, so a lost header would answer a January request with today's
  data; with the canary the layer comes back **empty** instead. An empty layer is then
  reported as a fact — with the earliest instant the track has a record of — rather than left
  looking like an outage. A future instant is refused by the picker and by the backend: the
  belief set at a future time is the current one, so the layer would be *full* under a
  caption asserting something nobody has ever believed.

  The instant is stored on the layer, not in a setting. A `.qgz` reopens on what it was saved
  with, a track switch carries it through rather than quietly turning a historical layer into
  a live one, and the remembered value in settings is the picker's opening value and nothing
  more.

- **History tracks.** The backend now holds more than one isolated dataset in one
  deployment — one for kicking the tyres, one the analysts build for real — and the panel
  gains a **History track** group, above Collections because it scopes everything below it.

  **The plugin does not implement the isolation and cannot weaken it.** That is row-level
  security in the database, keyed on a session variable the auth edge sets from an
  `X-Track` header. What the plugin adds is the part that is silent when it goes wrong:
  saying which dataset you are in, and making sure the track actually reaches the database
  on requests no plugin code is in the path of.

  The track is put in the layer's own data source — as an `X-Track` request header in the
  URI and in the credential the layer names — because QGIS's native OAPIF provider does the
  reads *and* the Part 4 writes itself. It follows that a layer cannot be redirected by a
  stale setting and that a `.qgz` reopens on the track it was saved on; the panel warns when
  a loaded layer's track disagrees with the selected one rather than silently redirecting
  anybody's edits. Switching tracks is refused while any plugin layer has unsaved edits,
  because re-pointing a layer with a dirty edit buffer discards it with no prompt and no
  undo.

  Track-scoped layers also carry a `"track_id" = '<uuid>'` clause in their filter. Under
  row-level security that is redundant, and that is the point: if the track ever stops
  reaching the database, `app.track()` falls back to the deployment's *default* track and
  answers with another dataset's polygons. With the clause the layer comes back **empty**
  instead, and a second check compares the first returned feature's `track_id` against the
  track that was asked for. Empty-and-wrong is enormously better than populated-and-wrong.

  A stored track the backend no longer offers resolves to **nothing**, never to the default:
  answering a request for one dataset from another is the contamination failure in reverse,
  and the annotator would conclude their track was empty. Reads with no track selected fall
  back to the deployment default; writes are refused. An archived track is readable and not
  writable, and the panel says so.

  Track names are **data**, exactly like class names: none appears anywhere in this
  repository, and the list comes from `GET {api}/v1/tracks` at runtime. A backend without
  that endpoint (404) is treated as a deployment with no tracks — reads work, writes are
  refused — while a response that is not a track list is an error, because "could not read
  the track list" must never look like "this deployment has no tracks".

  **The publish preview leads with the track**, above the table, in the window title and in
  the Publish button's own text. Every other warning on that screen appears only when there
  is something to warn about, so a clean preview said nothing at all about where 1,246
  permanent features were going — and that is the one decision on the screen made in another
  panel, minutes earlier, possibly by whoever saved the project. Publishing with no track
  selected, or into an archived track, is now blocked outright. Publishing the same
  shapefile into a *second* track is not treated as a duplicate: it is how a test dataset
  gets populated, and calling it one would train people to click through the warning that
  catches the real duplicate. The results dialog, the coverage report and the history dialog
  all name the track they are about.


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
  plugin containing either name. The resolved mapping for every column is shown in the
  preview, because a structural matcher cannot know that a column is wrong for reasons
  outside the schema — `Compounds.Area` matches an area attribute and would be square
  degrees — and the plugin must not carry a list of columns to distrust. Empty values are
  omitted rather than written as nulls, single-part geometries are promoted to the
  multi-part type the class declares, anything not already in EPSG:4326 is reprojected, and
  invalid geometries are skipped and reported instead of being sent for the server to
  reject. A layer whose CRS QGIS cannot determine is refused outright: an invalid CRS makes
  `QgsCoordinateTransform` a silent no-op, so its coordinates would be stored verbatim in a
  4326 column with nothing anywhere raising.

  Runs in a `QgsTask` with progress and cancellation, one feature per request, and nothing
  is ever sent twice: a save is not atomic, there is no `If-Match`, and identity is
  server-assigned, so retrying an ambiguous failure would duplicate rows that nothing
  afterwards could tell apart. A `429` from the auth edge is waited out rather than counted
  as a refusal. Per-feature failures are counted and attributed to a named row without
  aborting the run, and the report survives a cancel and an unexpected exception alike,
  because by then some of it is already on a server.

  A survey extent is declared with the `completeness` value the user chose — never a
  default — and only if the run earned it: a layer that published nothing, or an
  `exhaustive` claim over a layer whose features did not all reach the database, is refused
  with a reason. Each published layer is stamped with a custom property and the project is
  marked dirty so the stamp survives closing QGIS, so a second publish is warned about
  rather than silently doubling the data.

### Fixed

- **The QA tools could have picked a historical layer as the layer being worked on.**
  `find_label_layer` discriminates by exposed fields, and the transaction-time view carries
  the same `label_id` and `class_id` as the live collection — so the survey-coverage check,
  the history dialog and the canvas selection they drive could all have run over a belief the
  team has since revised, including labels deleted long ago, with nothing on screen to say
  so. `QgsProject.mapLayers()` order is not something the plugin controls. Excluded two
  independent ways, because they fail in different circumstances: by the echo column, whose
  name comes from the registry, and by the property the plugin stamps on its own historical
  layers.

- **`core/asof.py` described a mechanism that is not the one running.** The `cql2` fallback
  does reach every items request, but not as `filter=…&filter-lang=cql2-text`: QGIS enables
  its CQL2 path only when the server advertises the two `ogcapi-features-3` filter
  conformance classes, and pygeoapi 0.24.0 advertises neither. The provider falls back to its
  Part 1 compiler, which translates the first conjunct to a `datetime=` range and evaluates
  the `OR` conjunct client-side. Semantically still right; it over-fetches. Documented, along
  with the finding that the Temporal Controller cannot drive `datetime` at all.

### Changed

- **Credentials are stored one per history track** (the same token, plus the `X-Track`
  header), alongside one that names no track. `authcfg` becomes `authcfg_by_track`; an
  existing profile's single `authcfg` is promoted onto the un-tracked entry on read rather
  than being rewritten, so upgrading does not sign anybody out and downgrading still works.
  Signing out removes every entry.

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
