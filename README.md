# CVI Label Client

A QGIS 3.44 plugin for a bitemporal geospatial labeling backend that speaks
**OGC API - Features** (Parts 1, 2 and 4).

It is deliberately thin. QGIS's native OAPIF provider already reads *and* writes vector
features with no plugin code at all, so this plugin covers only the six things QGIS
cannot do on its own.

---

## What it does, and why each part exists

| | Why QGIS cannot do it |
|---|---|
| **Authentication** — stores an API token in `QgsAuthManager` and puts its seven-character id in every layer URI | QGIS can hold the credential, but something has to put it there and reference it. Doing this properly is what keeps tokens out of `.qgz` files that get emailed |
| **Imagery credentials** — fetches fresh signed object-storage URLs and re-points raster layer sources | **The clearest reason this plugin exists.** Signed URLs expire, and there is no way to write "a URL, but fetch it fresh" into a project file. A `.qgz` saved on Monday has dead raster layers on Tuesday |
| **Collections and class vocabulary** — read from the backend at connect time | Categories, styles, attribute schemas and form order live in the server's class registry. Anything compiled into the plugin would drift from the web UI the first time someone adds a class |
| **As-of date** — pins layers to one instant of *valid* time | QGIS's Temporal Controller can drive `datetime`, but choosing the mechanism, formatting the instant and re-pointing every layer is plumbing QGIS leaves to the caller |
| **QA** — a label's edit history, and a survey-coverage check | Both are questions about the backend's schema, not about the map |
| **Bootstrap** — publishes the local vector layers already open in the project as the founding dataset | The provider edits a collection it is already connected to. It has no concept of a shapefile that has never been part of one, and no way to map a decade of ad-hoc column names onto a class registry |

Everything else — feature reading, paging, bbox filtering, the attribute table, digitising,
create/update/delete — is stock QGIS.

---

## Installing

### From the plugin repository (recommended)

**Plugins → Manage and Install Plugins → Settings → Add**, then:

```
https://github.com/Compute-Visibility-Institute/qgis-label-client/releases/latest/download/plugins.xml
```

No username, no password, no VPN, and **no QGIS master password prompt during install**.
The plugin then behaves like an official one: searchable, installable, with upgrade badges.

> The plugin is marked **experimental** while it stabilises. Tick
> *Show also experimental plugins* in the same Settings tab, or it will not appear.

### From a zip

Download the `.zip` from a [release](https://github.com/Compute-Visibility-Institute/qgis-label-client/releases)
and use **Install from ZIP**. No auto-updates this way.

---

## Configuring

Open the **CVI Label Client** panel from the toolbar, then:

1. **API URL** — the landing page of your deployment's OGC API - Features endpoint.
   There is no default and there never will be one: this repository is public, so a real
   hostname cannot live in it. The greyed-out hint is a reserved example domain.
2. **Sign in…** — paste your API token. It goes straight into `QgsAuthManager`
   (`qgis-auth.db`, encrypted) and the plugin keeps only the seven-character reference.
   The first credential you ever store makes QGIS ask you to set a **master password**,
   which is **unrecoverable if forgotten**. That is why signing in is an explicit button
   and not a side effect of loading a layer.
3. **Connect** — lists the collections and loads the class registry.
4. Tick the collections you want and **Load checked collections**.
5. **Refresh imagery URLs** at the start of each session.

Nothing is stored anywhere except `QgsSettings` (URLs, page size, as-of state) and
`qgis-auth.db` (the token). No credential is written to a project file.

---

## What the backend has to provide

Four endpoints. Two are standard OGC API - Features; two are not, and both of those are
configurable paths so a deployment can mount them anywhere.

| Endpoint | Standard? | Used for |
|---|---|---|
| `GET {api}/collections` | OAPIF Part 1 | Collection discovery |
| `GET {api}/collections/{id}/items` | OAPIF Parts 1 and 4 | Everything QGIS's provider does, plus history queries |
| `GET {api}/v1/classes` | no | The class registry |
| `GET {api}/v1/imagery/signed-urls` | no | Minting short-lived signed URLs |

The two non-standard paths are settings, not constants — a deployment may mount them
anywhere. The `v1/` prefix in the defaults is the reference backend's own namespace:
everything outside it is proxied to the feature service, so a class-registry request
without the prefix comes back as an OAPIF error about an unknown collection, which
points at the wrong component entirely.

### Class registry

```jsonc
{
  "fields": { "class_id": "class_id" },      // optional: override core column names
  "classes": [
    {
      "class_id": "example_class",           // snake_case, matches label_class.class_id
      "geom_type": "MultiPolygon",
      "label_en": "Example class",           // or "labels": {"en": …, "zh": …}
      "label_zh": "示例",
      "description": "…",
      "attr_schema": { "type": "object", "additionalProperties": true,
                       "properties": { "…": { "type": "string", "enum": ["a", "b"] } } },
      "form":  { "order": ["…"], "widgets": { "…": "select" } },
      "style": { "fill": "#4f9dde66", "stroke": "#4f9dde", "stroke_width": 1.5 },
      "sort_order": 10,
      "active": true
    }
  ]
}
```

A bare JSON array, or a GeoJSON `FeatureCollection` carrying the rows in `properties`,
is also accepted.

**No class name or attribute name appears anywhere in this plugin.** Adding an attribute is
a row update on the server; the QGIS forms, the renderer legend and the web UI all pick it
up without a release.

`tests/test_repo_hygiene.py::test_no_class_or_attribute_name_is_compiled_into_the_plugin`
holds that line: it walks the package's AST and fails if any string *constant* equals a
class id or attribute name from the vocabulary. Comparing whole constants via the AST is
what lets a comment explain why cooling units matter while a dictionary key that reaches
into `attrs` by name still fails the build. It is a deny list, so it cannot know about a
term added after it was written — but the regression it catches is the one that actually
happens.

### Signed imagery URLs

```jsonc
{
  "expires_at": "2026-08-23T18:00:00Z",
  "assets": [
    {
      "capture_id": "…",
      "stac_id": "…",             // the scene stem
      "asset": "visual",          // derivative role: visual, nir, analysis, … ("key" also accepted)
      "url": "https://storage.googleapis.com/…?X-Goog-Signature=…",   // "href" also accepted
      "gs_uri": "gs://…/scene_visual.tif",
      "expires_at": "…"           // optional per-asset override
    }
  ]
}
```

The plugin matches assets to raster layers in two ways:

1. **Explicitly**, when a layer carries the custom property
   `cvi/asset_key = "{stac_id}:{asset}"`. Set this in your project file. It is the reliable
   route.
2. **By object path**, comparing bucket-and-object of the layer's current source against
   each asset's, ignoring the query string. This rescues a project saved with an expired
   URL.

Layers that match nothing are reported in the Log Messages panel rather than skipped
quietly — a raster left on an expired URL keeps drawing from GDAL's cache for a while and
looks fine right up until it doesn't.

---

## The as-of date, and why there are two mechanisms

The backend has **two independent time axes**. This control touches only one of them.

- **Valid time** — when a thing was true on the ground. OGC API - Features has a standard
  `datetime` parameter for it. That is what this control drives.
- **Transaction time** — when *we believed* it. Reproducing a training set means "as we
  understood the world in January, including the mistakes we hadn't caught yet". There is
  **no OGC parameter for it**, and no client-side answer either; it is a server-side query.

For valid time you can choose how the instant is sent:

| Mechanism | How it travels | When to use it |
|---|---|---|
| `datetime` *(default)* | Query parameter on the landing-page URL | The standard. Try this first |
| `cql2` | `filter=…&filter-lang=cql2-text` on every items request | When the server does not propagate query parameters from the landing page to item requests |

`datetime` is the standard and therefore the default. It is *not* guaranteed to arrive:
QGIS's OAPIF provider builds item requests from the links the server returns, so a server
emitting absolute `items` hrefs can drop it. `cql2` expresses the same question directly
against `valid_from` / `valid_to` and rides on a first-class parameter of the QGIS OAPIF
URI, so it cannot be silently discarded. If your as-of view looks suspiciously like the
current state, switch mechanisms — that is the symptom.

> **What the `filter` parameter actually takes.** Not CQL2. The QGIS OAPIF provider parses
> it with `QgsExpression` and does the CQL2 conversion itself, so the plugin sends a QGIS
> expression — `"valid_from" <= '2026-01-01T00:00:00Z' AND …` — and QGIS turns it into
> `filter=("valid_from" <= TIMESTAMP('2026-01-01T00:00:00.000Z'))…&filter-lang=cql2-text`.
> Writing literal CQL2 there does not degrade gracefully: an expression QGIS cannot parse
> makes the layer **invalid**, so you get no data rather than unfiltered data. Verified
> against QGIS 3.44.

---

## Survey coverage QA

`labeled_extent` records **where someone actually looked**, per class and per date. The
coverage check finds labels sitting outside any `completeness = 'exhaustive'` extent for
their class and selects them on the canvas.

Read the result carefully, because the wording is the point:

> Ground outside an exhaustive extent is **UNKNOWN, never negative.**

A detector trained on "no label here" as background learns that unlabeled instances are
background. The check does not say those labels are wrong — a label on unsurveyed ground is
a normal thing to have. It says the *extent* is missing, and that this is worth fixing now
because nobody will remember which sites were swept for which classes a year from now.

A label that falls only inside a `partial` extent is reported too: a qualified sweep does
not license treating its surroundings as negative either.

---

## Publishing local layers (the one-time bootstrap)

**Plugins → CVI Label Client → Publish local layers…**, or the button in the panel's
*Bootstrap* group.

The first deployment starts with an empty backend and a folder of Esri Shapefiles that has
been version-controlled by being copied and dated. This action reads the vector layers open
in the project — excluding the ones this plugin loaded, which are already on the server —
and creates them as labels. It replaces a command-line loader that ran against a PostgreSQL
DSN, on the grounds that an analyst will never run one.

**Nothing is sent until the preview is confirmed.** The dialog lists each layer with its
feature count, geometry type, CRS and a checkbox, and a class combo populated from the live
registry. Classes are guessed from the layer name and are always overridable; an ambiguous
guess is reported as ambiguous rather than resolved arbitrarily. The *Fields* column shows
where every source column would go, in full, on hover — the matcher is structural, so it
maps a column onto whichever declared attribute its concept is a subset of and cannot know
that a column is wrong for reasons outside the schema. `Compounds.Area` is the standing
example: it matches an area attribute perfectly, and any value in it was computed in
EPSG:4326 and is therefore square degrees. Reading the mapping is the check.

A layer with **no valid CRS** — a shapefile with no `.prj` beside it — cannot be published
at all, and the preview says so. QGIS builds a coordinate transform that silently does
nothing when it does not know the source CRS, `label.geom` has no range check, and
`ST_GeometryType` still matches the class, so projected metres would land as degrees of
longitude looking exactly like valid data.

Two things the dialog says out loud, because both are silent failures otherwise:

- **Damaged names.** Six of the seven source `.cpg` files declare UTF-7 and the writer
  never flushed its final escape run, so at least 52% of the Chinese names have lost their
  last character — `数据中心` stored as `数据中X8`. The count is shown before anything is
  sent. The default is to publish them anyway, because `Name_en` often survives where
  `Name:ch` did not, but the alternative is one checkbox away and the choice is visible.
- **Missing survey coverage.** If a class is being published with no `labeled_extent`
  declared for it, the dialog says so in the terms above: the publish records *what was
  found*, not *where anyone looked*. An extent can be created from the layer's bounding
  box, and the choice is a `completeness` **value**, not a tick: *declare nothing* (the
  default), *partial*, or *exhaustive*. Only `exhaustive` licenses the export pipeline to
  treat unlabeled ground inside the polygon as negative, so a tool that picks it whenever a
  box is ticked has answered that question rather than asked it. Whichever is chosen, the
  row carries a caveat recording that the polygon is a bounding box and that it names no
  imagery capture, and the extent is refused outright if the run did not earn it — nothing
  published, or, for `exhaustive`, any feature that did not reach the database.

What happens per feature:

- legacy columns are matched onto the class's own `attr_schema`, so `No. Cooler` and
  `No. Coolim` converge on one attribute and `No. transf`/`No. Transf` on another, without
  this plugin containing any of those names;
- `Name:ch` / `Name_en` / `Name` become `names` as `{"zh": …, "en": …}`, with an unmarked
  column filed by content;
- empty values are omitted rather than written as nulls — only four columns in the source
  have any data at all, and `{"…": null}` would claim somebody looked and found nothing;
- single-part geometries are promoted to the multi-part type the class declares, anything
  outside EPSG:4326 is reprojected, and invalid geometries are skipped and reported rather
  than sent for the server to reject;
- **no identity is invented.** The source `id` column is 0% populated and `label_id` is
  `uuid DEFAULT gen_random_uuid()`. Identity is the server's.

The run happens in a `QgsTask` with progress and cancellation. **One feature per request,
and nothing is ever sent twice.** A save is not atomic — one HTTP request is one edit, and
the first rejection aborts the rest — there is no `ETag`/`If-Match` anywhere, and identity
is assigned by the server, so nothing on this side can ask "did that one land?". Retrying
an ambiguous failure would therefore duplicate rows in the founding dataset with distinct
`label_id`s that nothing afterwards can tell apart. Round trips are cheaper than that. A
`429` is the one exception, and it is not a retry of an unknown outcome: the auth edge caps
writes per principal and says how long its bucket needs, so the client waits and offers the
same feature again. Every refusal names its row, by name where the feature has one and by
position otherwise.

Each published layer is stamped with a `cvi/published` custom property and the project is
marked dirty so the stamp survives closing QGIS; publishing it again warns first, because
the server assigns identity and therefore nothing here can recognise a repeat.

---

## Development

```bash
./scripts/dev-link.sh          # symlink this checkout into the QGIS plugins directory
python -m pip install -r requirements-dev.txt
pytest                         # no QGIS required
ruff check . && ruff format --check .
```

Then install **Plugin Reloader** in QGIS and bind it to a key: two seconds a cycle against
thirty for a restart. A plugin that fails to import fails **silently** in the plugin
manager — look in **View → Panels → Log Messages → Plugins**.

### Layout

```
qgis_label_client/
├── core/            pure Python, imports no QGIS at all  <- the tested half
│   ├── asof.py          valid-time instants, both mechanisms
│   ├── assets.py        signed URLs: parsing, redaction, layer matching
│   ├── collections.py   /collections parsing
│   ├── coverage.py      survey-coverage classification
│   ├── fields.py        names of the stable core columns
│   ├── history.py       audit-trail parsing
│   ├── legacy.py        legacy column names -> the registry's vocabulary
│   ├── names.py         label.names, and the UTF-7 truncation signature
│   ├── publish.py       the bootstrap plan, feature drafting and its report
│   ├── registry.py      the class vocabulary
│   ├── styling.py       registry style block -> QGIS symbol properties
│   ├── teardown.py      the undo stack that makes unload() correct
│   ├── uri.py           QgsDataSourceUri construction
│   └── urls.py          backend URL assembly
├── plugin.py        initGui / unload, and all the wiring
├── dockwidget.py    the panel (a view: no network, no layers, no tasks)
├── auth.py          QgsAuthManager
├── network.py       QgsBlockingNetworkRequest
├── tasks.py         QgsTask, with the three traps closed
├── client.py        the backend calls (worker-thread safe)
├── layers.py        OAPIF layer creation and registry-driven configuration
├── imagery.py       raster source re-pointing
├── qa.py            coverage check
├── publish.py       local-layer reading, reprojection and the publish task
├── publishdialog.py the preview, and the results summary
└── historydialog.py
```

The `core` boundary is enforced by a test: nothing under `core/` may import `qgis`. That is
what lets CI run the interesting logic on a machine with no QGIS, and it is why the
sharp-edged parts — URI construction, URL matching, coverage classification — live there.

### Rules this codebase keeps

These are not style preferences. Each one has cost somebody an afternoon.

- **`unload()` detaches everything.** Reload five times with Plugin Reloader and count
  toolbar buttons; five buttons means it is wrong. Every attachment registers its detach on
  a `Teardown` stack in the same statement, and `tests/test_plugin_lifecycle.py` runs the
  five-reload loop against a recording `iface`.
- **Network goes through `QgsBlockingNetworkRequest` inside a `QgsTask`**, never
  `requests`. Only the QGIS stack inherits the user's proxy config, SSL exceptions and the
  authentication database — which is where the token lives.
- **`QgsTask.run()` is a worker thread.** No Qt widgets, no `iface`, no `QgsProject`. Only
  `finished()` is back on the main thread. A `QgsVectorLayer` is off limits there too:
  build a `QgsVectorLayerFeatureSource` from it on the main thread and iterate *that*,
  which is what the Processing framework does and what `publish.prepare()` does here.
- **An exception escaping `run()` is swallowed silently** — a no-op, not a traceback. The
  task wrapper catches everything and formats the traceback on the worker thread.
- **Hold a Python reference to every `QgsTask`** or it is garbage collected mid-flight and
  the request never completes. `TaskRunner` owns them and cancels them on unload.
- **Import through `qgis.PyQt`, never `PyQt5` or `PyQt6`**, and use **scoped enums**
  (`Qt.DockWidgetArea.RightDockWidgetArea`). Both work on Qt5; only scoped works on Qt6.
  Both rules are enforced by tests, so the October 2026 flip to QGIS 4.2 is a config
  change rather than a migration.
- **Never compute area or length in EPSG:4326.** Storage and interchange are 4326;
  measurement belongs in a projected CRS — UTM 49N per site, an equal-area conic for
  anything spanning the seven UTM zones between 84°E and 125°E. Nothing in this plugin
  measures anything; the coverage check uses `intersects`, which is topological and
  CRS-safe once both layers are in the same CRS.

### Testing

`pytest` runs with no QGIS installed. Modules under `core/` are tested directly; the rest
are exercised against small stubs in `tests/qgis_stubs/`, which exist for exactly one
reason — to make the five-reload teardown test runnable in CI — and are deliberately not a
QGIS emulator. Run the suite inside the QGIS Python environment and the stubs stand aside,
so the same tests exercise the real API.

There are **no imagery fixtures and there never will be**, and no test touches the network.

---

## Releasing

```bash
# bump `version=` in qgis_label_client/metadata.txt and add a CHANGELOG entry, then:
git tag v0.2.0 && git push --tags
```

The release workflow verifies the tag matches `metadata.txt` (a mismatch means the plugin
manager's upgrade detection silently never fires), runs the tests, and publishes both the
zip and a `plugins.xml` to a GitHub Release. A semver pre-release suffix — `v0.2.0-beta1` —
is flagged experimental automatically, which gives a canary channel for free.

---

## Why this repository is public

QGIS is GPL v2-or-later, and the project's position is that plugins distributed through
*any* repository, self-hosted included, must comply and make source available to every
recipient. Publishing satisfies that by construction, permanently, with no legal question
left open.

It cost nothing. The plugin is a thin client: no credentials (those live in
`qgis-auth.db`), no schema secrets, no business logic. Everything valuable sits below the
API line, in a private repository, on infrastructure we run.

It bought the entire distribution story. `plugins.xml` and the zips serve from GitHub
Releases with **no authentication at all** — no VPN host to operate, no HTTP Basic
credentials to distribute, and no master-password support call for a new annotator on their
own laptop.

Two consequences that are now rules rather than advice:

1. **No credential may ever be committed here.** Treat one as an incident, not a cleanup.
2. **No licensed imagery may ever be committed here** — not a sample COG, not a test
   fixture, not a full-resolution screenshot. `tests/test_repo_hygiene.py` fails the build
   on the file extensions, because "just add a small sample raster for the tests" is
   precisely the reflex that breaks it.

---

## Licence

GPL v2 or later. See [LICENSE](LICENSE).

Copyright © 2026 Compute Visibility Institute.
