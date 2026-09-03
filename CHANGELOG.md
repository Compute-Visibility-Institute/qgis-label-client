# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and `qgis-plugin-ci` copies the
most recent entries into `metadata.txt` at release time.

## [Unreleased]

## [0.0.1] - 2026-09-03

### Added

- **Sign in with Google, and stay signed in.** The credential is no longer a token pasted
  into a password box: **Sign in with Google** opens the system browser, runs an
  authorization-code flow with **PKCE (S256)** against a loopback listener, and stores the
  resulting Google ID token in `QgsAuthManager` exactly as before — same `APIHeader`
  method, same `Authorization: Bearer …` plus `X-Track` config map, same seven-character
  id in every layer URI.

  **The client secret is embedded in the released ZIP, and PKCE does not replace it.**
  That is the opposite of what this entry first claimed, and the correction is worth
  keeping: Google refuses the code exchange for this Desktop client with
  `invalid_request: client_secret is missing`, *after* the analyst has already approved
  the consent screen — the most confusing place a flow can fail. PKCE is additional, not
  a substitute. The secret is substituted into the ZIP by the release workflow and is
  never committed, so a source checkout has `CLIENT_SECRET = ""` and asks the analyst to
  supply one; an installed-app secret is a client *identifier* rather than a
  confidential credential, which is what makes shipping it in a public artifact
  acceptable.

  **Why the plugin runs the flow rather than QGIS's own OAuth2 auth method.** QGIS 3.44
  can carry an `id_token` into a header through `extraTokens`, and it was rejected for two
  reasons that both fail silently. It would **evict `X-Track`** — one auth config has one
  method, and `extraTokens` maps token-endpoint *response fields* onto headers, so it
  cannot carry a constant; since the auth config is the only channel that reaches the
  native provider's requests, every read and every Part 4 write would resolve to the
  deployment's default track. And QGIS captures the `id_token` **only on the initial code
  exchange** — neither the refresh reply nor the synchronous refresh updates it — so after
  about an hour it sends an expired JWT forever.

  **Expiry is handled explicitly, in three places, because no one of them is enough.** A
  timer renews at *expiry minus five minutes*, silently and with no browser. A check on the
  way into anything that will put a credential on the wire covers the laptop that slept
  through that timer. A repair on `HTTP 401` covers the rest — and it is a **net, not a
  fix**: QGIS's own OAPIF provider makes the requests that fail and no plugin code is in
  their path, so the plugin renews the credential and then says, in words, *reload the
  layer*. Every renewal rewrites all the auth configs **under their existing ids** and
  clears QGIS's cached copy of each, so saved `.qgz` projects and already-loaded layers
  keep working and the provider's next request carries the new token rather than a cached
  old one.

  **The refresh token is stored as an encrypted auth setting, never in the config map.**
  Under the `APIHeader` method every key in that map becomes an outgoing HTTP header, so a
  refresh token there would be transmitted on every single request — and unlike the ID
  token beside it, a refresh token does not expire. Signing out removes every credential,
  destroys the local refresh token **and** revokes the grant at Google, so "signed out" is
  true on both sides.

  **A 403 now reaches the analyst verbatim.** The backend distinguishes "your credential is
  bad" from "you authenticated perfectly and are not on the access list", and the second
  message names the address it saw and says that signing in again will not help. Replacing
  it with the word "Forbidden" is how somebody re-signs-in forever and files a bug, so the
  server's own sentence is passed through and a **Copy my address** button sits next to it.

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

  **The instant travels as a landing-URL `?recorded_at=` query parameter**, corrected from
  the header transport this entry originally claimed. Measured against QGIS 3.44.13 with a
  bare HTTP listener: a layer URI's `http-header:` parameters **never reach the wire at
  all**, while the same headers carried by an `APIHeader` auth configuration arrive intact
  — which is why the *track* has always worked and why the instant, which had no such
  channel, never arrived. A landing-URL query parameter does survive, on every request the
  provider builds except the `OPTIONS` editability probe. That probe being unpinned costs
  nothing: the historical collection is `editable: false` on the server, so it answers
  `Allow: HEAD, GET` either way. The plugin also holds the layer read-only on its own side,
  because `setDataSource` recomputes that from provider capabilities on every re-point, and
  says loudly if the server ever advertises a pinned layer as writable. The full record is
  in `core/recorded.py`.

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

- **The QA tools examined one label layer and reported on the project.** With labels
  served as one collection per geometry type, "the label layer" is two or three layers.
  The coverage check took the first, found nothing wrong with the points it never read,
  and reported a clean project — a silently partial check, which is worse than none
  because its silence is believed. It now runs over every label layer, names each in the
  result and unions the classes with no exhaustive extent; the history dialog looks for
  the selection across all of them instead of telling somebody with exactly one label
  selected to select exactly one label.

- **A categorized label layer drew nothing for the classes of another geometry.** All
  classes live in one table, so the renderer covers the whole registry on every label
  layer — which now means fill symbols on a point layer, and QGIS draws nothing at all for
  a category whose symbol type does not match. Categories are built for the *layer's*
  geometry in the class's own colours; dropping them instead would be the same
  invisibility by another route.

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

- **A collection that mixes geometry types is refused rather than loaded.** QGIS types an
  OGC API - Features layer by *sampling features* — the standard cannot declare a
  collection's geometry type and pygeoapi answers `geometry-any` — so a collection holding
  points, lines and polygons became whichever shape sampled first and hid the rest with no
  error anywhere: 872 of 1,246 features invisible on the real corpus, and nobody goes
  looking for what they cannot see is missing. Such a layer is now refused at load, with a
  sentence naming the collection and pointing at the deployment's per-geometry collections
  — which the backend publishes for the read-only views as well, each typed at the
  database, and which QGIS therefore types correctly whether or not they hold a feature.

  **Recognised by field, never by id.** A mixed collection is the one carrying
  `geom_family`; no collection id is compiled into the plugin, exactly as no class id is.
  A deployment that has not published the typed collections yet gets the message rather
  than a layer that quietly shows a third of its data.

  **Splitting the layer with a filter was tried and measured not to work.** The subset
  filter does reach the provider, but QGIS types the layer *before* applying it, so each
  part came back filtered to one family and typed as another and drew nothing at all.
  The historical-view control forgets a remembered mixed collection when it refuses one,
  so the next attempt asks again instead of repeating the refusal for ever.

- **A publish is routed to a collection by the layer's geometry type.** The backend
  replaces the single untyped `label` collection with one per geometry family, because an
  untyped collection cannot declare a geometry type to QGIS: OGC API - Features has no way
  to say it, pygeoapi reports `geometry-any`, and QGIS therefore infers the type by
  *sampling features*. An empty collection samples as nothing, QGIS treats the layer as
  non-spatial, and the Edit menu offers "Add Record" instead of "Add Polygon Feature" —
  every digitizing tool gone, on the first day of a deployment, which is when they are
  most needed.

  A QGIS vector layer has exactly one geometry type, so the plugin now chooses the
  destination per layer. **The ids are not compiled in**: the routes are resolved against
  what `/collections` actually lists, by geometry word, exactly as class vocabulary is
  read from the registry. The remembered `label_collection` setting becomes a *hint about
  which group*, matched by stem, so a value stored before the split still selects the
  split collections with no migration and no re-prompt. A deployment still serving one
  untyped collection keeps working unchanged, and one this cannot read falls back to
  asking, which is what it did before.

  **A layer that matches no collection is refused**, by name and by geometry type, before
  anything is sent — a mixed or unknown-geometry layer most of all. Publishing a point
  into the polygon collection would be rejected by `app.label_check()` feature by feature,
  and 872 refusals read as a backend outage rather than as a layer that needed splitting.
  The preview shows the destination per row and the summary names every collection the run
  would write to, because it is a decision the analyst never makes and can only check.

  **Class stays an attribute, not a layer.** There are three collections however many
  classes exist; adding one remains a single row in `label_class`, with no migration, no
  new collection and no plugin release.

- **Credentials are stored one per history track** (the same token, plus the `X-Track`
  header), alongside one that names no track. `authcfg` becomes `authcfg_by_track`; an
  existing profile's single `authcfg` is promoted onto the un-tracked entry on read rather
  than being rewritten, so upgrading does not sign anybody out and downgrading still works.
  Signing out removes every entry.

## [0.0.0] - 2026-08-23

<!-- Renumbered from 0.1.0, which was written before anything was tagged and never
     released -- `gh release list` was empty when v0.0.1 was cut. 0.1.0 is reserved
     for the release after the europe-west1 move, when the default API URL stops
     being a run.app hostname. Renumbering a version nobody ever installed costs
     nothing; shipping two different 0.1.0s would cost the plugin manager's
     upgrade detection, which keys on this number. -->

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
