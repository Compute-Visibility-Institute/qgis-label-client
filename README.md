# CVI Label Client

A QGIS 3.44 plugin for a bitemporal geospatial labeling backend that speaks
**OGC API - Features** (Parts 1, 2 and 4).

It is deliberately thin. QGIS's native OAPIF provider already reads *and* writes vector
features with no plugin code at all, so this plugin covers only the five things QGIS
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
| `GET {api}/classes` | no | The class registry |
| `GET {api}/imagery/signed-urls` | no | Minting short-lived signed URLs |

### Class registry

```jsonc
{
  "fields": { "class_id": "class_id" },      // optional: override core column names
  "classes": [
    {
      "class_id": "example_class",           // snake_case, matches label_class.class_id
      "geom_type": "MultiPolygon",
      "label_en": "Example class",
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

**No class name, attribute name or enum value appears anywhere in this plugin.** Adding an
attribute is a row update on the server; the QGIS forms, the renderer legend and the web UI
all pick it up without a release. That property is asserted by the test suite.

### Signed imagery URLs

```jsonc
{
  "expires_at": "2026-08-23T18:00:00Z",
  "assets": [
    {
      "capture_id": "…",
      "stac_id": "…",             // the scene stem
      "asset": "visual",          // derivative role: visual, nir, analysis, …
      "url": "https://storage.googleapis.com/…?X-Goog-Signature=…",
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
  `finished()` is back on the main thread.
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
