# CVI Label Client

## Class layers and attribute columns

The plugin asks the server for its class-layer capability on **Connect**.
When supported, **Add editable layers** adds separate platform class layers. A class that
accepts several geometry families has separate point, line and polygon child layers;
multipart features stay in their corresponding family. Source attributes appear as
ordinary editable columns using the original field names as aliases. New features
receive the layer's class automatically.
Generated layer names start with **CVI** so they remain recognizable outside a
group. Existing generated names update on connection; custom user names are kept.

**Add read only layers** uses those same class layers and attribute columns, with
editing disabled. Read-only and editable copies can coexist in the project. On
older servers, the read-only action keeps using the legacy current-label views.

On servers advertising `native_add_field`, editable class layers support QGIS's
native **New field / Add Field** action in the attribute table. Enter a name and
type, edit values, then use **Save Layer Edits** as usual. Adding a field is local;
feature saves write its values into the existing JSON source attributes. There is
no separate column-creation API or database migration. Other clients discover the
column from saved data when they reconnect. Text, integer, decimal, boolean, date
and date/time columns are supported; JSON stores dates as text. A column that has
no saved values has no inferable server type. Local column definitions survive in
the saved QGIS project, and uncommitted fields and values use the recovery journal.

**Remove unused fields on import**, under **Label layers**, is enabled by default.
When adding supported class layers, optional attributes that are NULL/missing on
every feature in that class and geometry family are omitted from the local layer.
The decision covers the whole selected track, not just the visible map. Zero,
false and empty text count as values. Server data and required identity fields are
retained. Turn the option off before adding layers to include all discovered
attributes. It does not remove columns from an active editing session or prune a
newly added local field when refreshing.

New classes and fields are discovered on Connect. Save or recover pending edits before
reconnecting to load a changed field schema; existing dirty layers are never replaced.
Local uploads continue through the established bulk API, preserving all source data.
Servers without the new capability keep the existing geometry-layer interface. An
authentication, network or malformed-capability error is reported rather than silently
downgrading. Version 0.3.1 is distributed through the usual stable plugin repository.
The new backend capability is enabled on development servers first; the same plugin
keeps the legacy interface on production servers until they advertise support.
Use a separate QGIS profile when testing the development server so your production
project and saved connection stay separate.

The **Environment** selector is collapsed at the bottom of the panel, immediately
below **Bootstrap**. The current track remains visible beneath the connection controls.

A QGIS 3.44 plugin for a bitemporal geospatial labeling backend that speaks
**OGC API - Features** (Parts 1, 2 and 4).

Legacy layers use QGIS's native OAPIF provider. Capability-enabled class layers use
the plugin's provider to support native field creation and JSON-backed columns;
the attribute table, edit buffer, undo and digitising remain native QGIS controls.
These class layers load their collection into a local in-memory cache, then save
feature edits through the authenticated API with revision conflict checks.

---

## What it does, and why each part exists

| | Why QGIS cannot do it |
|---|---|
| **Authentication** — signs in with Google, stores the resulting token in `QgsAuthManager`, puts its seven-character id in every layer URI, and **renews it before it expires** | QGIS can hold a credential, but something has to obtain it, reference it and replace it. QGIS 3.44 can carry an ID token itself, but only by evicting the `X-Track` header from the credential and only until the first refresh, after which it sends an expired one forever |
| **Collections and class vocabulary** — read from the backend at connect time | Categories, styles, attribute schemas and form order live in the server's class registry. Anything compiled into the plugin would drift from the web UI the first time someone adds a class |
| **As-of date** — pins layers to one instant of *valid* time | The Temporal Controller cannot drive `datetime`: its filter is a function node the Part 1 compiler refuses, so it filters client-side and downloads the whole collection. Sending the instant server-side is a plugin job |
| **Historical view** — adds a read-only layer showing what the team *believed* at a chosen instant | Transaction time has no OGC parameter at all. It travels as a per-layer request header, which is also the only transport that reaches the `OPTIONS` probe QGIS uses to decide whether a layer is editable |
| **History tracks** — chooses which isolated dataset you are working in, and puts it in every request | The isolation is row-level security in the database. QGIS has no concept of it, and the native provider makes its own requests, so the track has to be in each layer's own data source or it is never sent |
| **QA** — a label's edit history, and a survey-coverage check | Both are questions about the backend's schema, not about the map |
| **Bootstrap** — publishes the local vector layers already open in the project as the founding dataset | The provider edits a collection it is already connected to. It has no concept of a shapefile that has never been part of one, and no way to map a decade of ad-hoc column names onto a class registry |

The attribute table and digitising tools are stock QGIS. Class-provider caching and
JSON field mapping are implemented by the plugin; legacy layers retain native
OAPIF reading, paging and writes.

---

## Installing

### From the plugin repository (recommended)

**Plugins → Manage and Install Plugins → Settings → Add**, then:

```
https://github.com/Compute-Visibility-Institute/qgis-label-client/releases/latest/download/plugins.xml
```

No username, no password, no VPN, and **no QGIS master password prompt during install**.
The plugin then behaves like an official one: searchable, installable, with upgrade badges.

Version 0.2.0 uses the stable feed. **Show also experimental plugins** is not required.

### From a zip

Download the `.zip` from a [release](https://github.com/Compute-Visibility-Institute/qgis-label-client/releases)
and use **Install from ZIP**. No auto-updates this way.

---

## Configuring

Open the **CVI Label Client** panel from the toolbar, then:

1. **API URL** — the landing page of your deployment's OGC API - Features endpoint.
   New profiles default to the reference deployment's public API. A saved URL takes
   precedence; use the API URL shown on your deployment's setup page for another backend.
2. **Sign in with Google** — opens your browser. Pick your work account, approve the
   consent screen, close the tab. The resulting token goes straight into `QgsAuthManager`
   (`qgis-auth.db`, encrypted) and the plugin keeps only the seven-character reference.
   The first credential you ever store makes QGIS ask you to set a **master password**,
   which is **unrecoverable if forgotten**. That is why signing in is an explicit button
   and not a side effect of loading a layer — and why it is worth letting your operating
   system's keychain remember it, since the token renewal needs that database unlocked.
3. **Connect** — lists the collections, the history tracks and the class registry.
4. Check the current track shown below **Connection**. New profiles on the production
   server select production (`default`). To change it, expand **History track** at the
   bottom of the panel.
5. Under **Label layers**, choose **Add read only layers** to view current labels or
   **Add editable layers** to work on them. The editable action adds separate platform
   class layers when supported by the server. Historical views belong to the history
   controls. Survey extents are not offered in the add-layer UI. Imagery is managed
   outside this plugin.

Preferences live in `QgsSettings`; credentials stay in `qgis-auth.db`. Saved QGIS
projects retain layer configuration and locally added field definitions. Unpushed
edits have private recovery journals in the QGIS profile. No credential is written
to a project file or an edit journal.

### Upgrading an existing profile after the deployment moves

Version 0.1.0 changes the default API URL for new profiles. Existing profiles usually
have a saved URL because signing in and connecting persist the field. Updating the
plugin does not change that saved value or the sources in a saved QGIS project.

After the operator confirms the new deployment is ready:

1. Save or discard any outstanding edits while the old service is still available,
   then save a backup copy of the QGIS project. Keep that copy for rollback.
2. Copy the API URL from the new deployment's setup page into **API URL**, then click
   **Connect**. Sign in again if prompted and select the same history track.
3. Note the collections, each historical view's transaction-time instant, valid-time
   settings, and any custom layer styling. Remove the old remote label/history/extent
   layers from this project, keeping your local source layers. **Connect** alone does
   not change existing layer sources, and already-loaded collections are skipped.
4. Load the same checked collections, restore any historical views with their original
   instants, and reapply your saved custom styling.
5. Check the layer sources show the new API, the intended track and time views are
   selected, and your access permissions match expectations. Save the migrated project
   separately before continuing work.

An explicitly configured alternate backend stays unchanged. The plugin does not infer
that two different hosts represent the same deployment.

### Staying signed in

A Google ID token lives about an hour; an editing session does not. The plugin therefore
holds a **refresh token** — encrypted in `qgis-auth.db` as an *auth setting*, deliberately
**not** in the credential's header map, because that map is emitted verbatim as request
headers and a refresh token does not expire. Renewal happens three ways, because no one of
them covers every case:

| | Covers |
|---|---|
| A timer at *expiry minus five minutes* | The normal afternoon. Silent, no browser |
| A check on the way into any action that will put a credential on the wire | A laptop suspended over lunch, where the timer fired late or not at all |
| A repair on `HTTP 401` | Everything else — and it is a **net, not a fix** |

That last row is the honest limit. QGIS's native OAPIF provider makes its own requests and
no plugin code is in their path, so a renewed credential cannot un-fail a request that
already failed: the plugin renews, then says *reload the layer*. This is why the timer is
the primary mechanism rather than the 401 handler.

After reopening QGIS, **Connect** renews an expired sign-in before connecting when its
renewal token is still valid. If a new browser sign-in is needed, use **Sign in with
Google**, then **Connect**; signing out first is unnecessary. Both renewal and browser
sign-in update every saved track credential, even before tracks have been rediscovered,
while retaining the credential IDs used by saved projects and loaded layers.

Signing out removes every stored credential and requests revocation at Google. Open
editing sessions and unsaved changes stay in QGIS, but cannot be saved while signed
out. Signing back into the **same account and backend** restores the existing layers'
credential references without rebuilding their providers or replacing edit buffers.
The plugin retains only the previous email, backend URL and nonsecret credential IDs
for this purpose, including across a plugin reload; the tokens themselves are deleted.
Permissions are checked again, and the server still rejects saves if write access was
removed. Failed saves are not replayed automatically: use **Save Layer Edits** after
signing in. A different account does not reconnect the old editing session.

### Upgrading and reconnecting existing layers

See [the update guide](docs/updating.md) for installation and reload steps.
Upgrading the plugin does not require deleting or re-importing its editable layers.
They are native QGIS layers and survive plugin unload, together with their saved
connection references. Save the QGIS project before an upgrade; if restarting QGIS,
save or export unsaved feature edits first because project files do not store them.

At startup, the **CVI — sign in and connect** popup provides **Sign out**, **Sign in
with Google**, and **Connect**. An existing valid sign-in can go straight to Connect.
The popup is optional: toggle **Show connection prompt on startup** in the panel's
**Backend** section. Open it manually using **Plugins → CVI Label Client → Sign in
and connect…**.

**Connect** refreshes the available collections, tracks, permissions, class/style
metadata, and clean loaded live layers. Native providers fetch current data again
using their existing track, canvas restriction and time filters; this does not download
the whole database or change historical views. Clean editing sessions refresh their
provider data without stopping editing. Pending local changes are kept intact until
those changes are handled. Connect can
repair an outdated credential reference on a non-editing layer; it refuses to rebuild
a provider while editing so unsaved changes are not discarded.

### Unpushed edits and recovery

Editable CVI layers are marked **[Unpushed: count]** while their changes exist only
locally. Each edit saves a private recovery journal in the QGIS profile's
`cvi-unpushed` directory. The journal keeps attributes (including Chinese text and
typed dates), geometry, and its original account, server, track and collection;
it contains no login tokens. Save the QGIS project too so its layers can be reopened.

**Connect** uploads never-submitted pending edits for the same signed-in account
after checking write access and the original server versions, then refreshes clean
layers. Edits whose server versions changed stay **Unpushed: needs review**. These
are checks before a normal native QGIS save, not a new atomic server locking protocol.
A failed or interrupted save also needs review: native feature creation has no
idempotency key, so replaying an uncertain save could create duplicate features.

Use **Plugins → CVI Label Client → Unpushed edits…** to see saved recovery copies,
restore them locally for review, or explicitly discard them. Cancelling edits in
QGIS stops their automatic upload but retains a recovery copy for deliberate review.
An already-open edited buffer is never replaced by recovery. New edits made before
old recovery is restored are kept separately and require review. Missing original
layers must be reopened before recovery can be applied; the journal itself remains
available if the project is unavailable.

**Warn about unpushed edits**, in the panel's **Label layers** section, toggles the
edit-warning popups independently of the
startup connection prompt. Turning popups off does not turn off journaling or the
layer markers. Recovery write errors always remain visible in the QGIS message bar;
keep QGIS open and export the edited layer if its local recovery copy cannot be saved.
Field/schema edits require explicit saving or export rather than automatic recovery.

### Push all local features

**Plugins → CVI Label Client → Push all local** saves eligible native pending edits,
then checks local point, line and polygon layers (including multipart geometries).
Connect also performs this check. New or invalid class mappings open the existing
upload review; previously reviewed mappings and exclusions are remembered for that
account, server and track. Remote database/WFS layers and unsupported geometries are
not imported automatically.

Before creating features, the plugin reads the destination collections and compares
server label IDs, geometry and attributes. It normalizes coordinate precision to nine
decimal places and ignores polygon ring start/winding and multipart order; line
direction remains significant. Existing matches are skipped. Matching geometry with
different attributes, changed previously uploaded source rows, and uncertain earlier
uploads are held for review instead of creating another copy. This action only creates
missing features: edit the server layers to change or delete existing server labels.

The private `cvi-unpushed/local-uploads.sqlite` journal records content hashes, source
feature references and request outcomes before uploads, without tokens or feature
payloads. Interrupted writes are not automatically replayed. Source references use the
local provider/source and feature ID; replacing files, changing IDs or independently
redrawing a feature can make its prior identity impossible to infer. Separate machines
do not share this journal, and simultaneous imports need coordination.

Reviewed local source layers show **[Unpushed]** after edits and use the same warning
toggle. Save local source files and the QGIS project before closing; export memory
layers to a file. The upload journal does not preserve their unsaved feature payloads.

These workflows are included in version 0.2.0. Save remote edits and your QGIS
project before upgrading; recovery journals do not replace saving local source files.

### Why the plugin runs the OAuth flow itself

QGIS 3.44 can carry an OpenID `id_token` into a header, via the OAuth2 auth method's
`extraTokens` map. Two things rule it out here, and both fail silently:

- **it would evict `X-Track`.** One auth config has one method, and `extraTokens` maps
  token-endpoint *response fields* onto headers — it cannot carry a constant. The auth
  config is the only channel that reaches the native provider's requests (see
  `core/recorded.py`), so the track would stop travelling and every read and write would
  resolve to the deployment default;
- **the ID token is captured once and never refreshed.** QGIS sets `extraTokens` only on
  the initial code exchange, so after about an hour it sends an expired JWT forever.

Running the flow in the plugin keeps the header transport that is already proven and buys
a genuinely fresh token every hour with no browser round trip. No client secret is
embedded: this is a Google *Desktop app* client in a public repository, so a secret in it
would not be one, and PKCE (S256) is what binds the exchange.

---

## History tracks

A **track** is an isolated dataset sharing one deployment — one for kicking the tyres, one
the analysts build for real. Labels drawn on one are invisible from the other.

**The plugin does not implement that isolation and cannot weaken it.** It is row-level
security in the database, keyed on a session variable the auth edge sets from an
`X-Track` header. What the plugin does is much smaller, and it exists because every
failure in this area produces data that looks completely correct:

- **It says which track you are on**, in the panel banner, in the publish preview, on the
  Publish button itself, in the confirmation, in the results dialog and in the history
  dialog's title. A polygon drawn into the wrong dataset is indistinguishable from a
  correct one, so the defence is visibility rather than validation.
- **It puts the track in each layer's own data source**, as an `X-Track` request header in
  the URI and in the credential the layer names. That is where it has to be: QGIS's native
  OAPIF provider makes the reads *and* the Part 4 writes itself, so anything not attached
  to the layer is not sent. It also means a layer cannot be redirected by a stale setting,
  and a `.qgz` reopens on the track it was saved on — with the panel saying so when that
  disagrees with your own selection.
- **It carries a canary.** Track-scoped layers also get a `"track_id" = '<uuid>'` clause in
  their filter. Under row-level security that is redundant; if the track ever stops
  reaching the database, `app.track()` falls back to the deployment's *default* track and
  answers with somebody else's polygons. With the clause, the layer goes **empty** instead,
  and empty-and-wrong is enormously better than populated-and-wrong. A second check
  compares the first returned feature's `track_id` against the track you asked for and
  warns in the message bar if they differ.

Some things follow from that and are worth knowing before they surprise you:

- **New profiles select the deployment's default track after Connect.** On the hosted
  production server this is production (`default`). The panel's list and default flag
  come from `GET {api}/v1/tracks`; a server restricted to development selects `dev`
  instead. The selector is at the bottom of the panel, initially collapsed, while the
  current track remains visible below Connection.
- **Existing profiles keep their saved track choice**, including after uninstalling and
  reinstalling the plugin. If your profile was set to `dev`, choose `default` in
  **History track** before publishing to production.
- **Switching tracks is refused while any plugin layer has unsaved edits.** Switching
  re-points every layer, and `setDataSource` on a dirty layer discards the edit buffer with
  no prompt and no undo.
- **A stored track the backend no longer offers resolves to nothing, not to the default.**
  Answering a request for one dataset from another is the contamination failure in reverse:
  you would conclude your track was empty. The panel says so and every write is refused.
- **An archived track is readable and not writable.** The panel marks it, and publishing
  into one is blocked before the preview opens rather than discovered one refused feature
  at a time.
- **Publishing requires a resolved track.** A fresh profile resolves to the server's
  declared default after Connect. If no default is available or a saved track is missing,
  publishing stays blocked until an available track is selected.
- **Credentials are stored one per track** (same token, plus the `X-Track` header), and one
  more that names no track. Every hourly renewal rewrites all of them **under their
  existing ids**, so saved projects and already-loaded layers keep working. Signing in happens *before* Connect — you need a credential to
  discover what tracks exist — so a first sign-in stores only the un-tracked entry and
  signing in again after connecting fans it out. Either way the track travels in the layer
  URI, so nothing breaks in between. Signing out removes all of them.

---

## What the backend has to provide

Five endpoints. Two are standard OGC API - Features; three are not, and all three of those
are configurable paths so a deployment can mount them anywhere.

| Endpoint | Standard? | Used for |
|---|---|---|
| `GET {api}/collections` | OAPIF Part 1 | Collection discovery |
| `GET {api}/collections/{id}/items` | OAPIF Parts 1 and 4 | Everything QGIS's provider does, plus history queries |
| `GET {api}/v1/classes` | no | The class registry |
| `GET {api}/v1/tracks` | no | The history-track list |

A backend with no `/v1/tracks` (404) is treated as a deployment with no history tracks:
the panel shows an empty list, reads work, and every write is refused. A response that is
*not* a track list is an error, because "the plugin could not read the track list" must
never look like "this deployment has no tracks".

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

### Imagery

The plugin does not add or refresh imagery layers. Existing raster layers in saved
QGIS projects are left untouched. Use the web imagery catalogue or manage local
rasters directly in QGIS.

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

> **The Temporal Controller does not drive `datetime`, and cannot be made to.** Its filter
> is built as `make_datetime(...)` — a function node where QGIS's Part 1 compiler requires a
> literal — and every temporal mode wraps its comparison in `OR <field> IS NULL`, a
> top-level `OR` the compiler will not walk. So the controller filters entirely on the
> client. That is why this control exists at all. It is also why sliding the controller over
> a historical layer is harmless: the two axes never share a code path.

---

## The historical view, and why the layer is read-only

The other axis. **Transaction time** is when the team *believed* something, as distinct from
when it was true on the ground. Ticking **Pin a historical layer to an instant** in the
panel's *Dataset as saved on a date* box and pressing **Add historical layer** gives
you a layer showing the labels as the team believed them at that instant — **including
labels deleted since, and the superseded geometry of labels edited since**.

Two properties are worth stating plainly, because both are deliberate:

- **It adds a layer. It does not re-point the ones you have.** Unlike the as-of control
  above, which re-points everything. The whole use case is having the live layer and a
  historical one open at once — and two historical ones at different instants if you are
  comparing beliefs.
- **The layer is read-only, and QGIS greys the pencil out by itself.** Editing a past belief
  is incoherent: it would mean editing what you used to think. This is enforced rather than
  documented, in four places, and the first of them is not this plugin.

### How the instant actually travels

**Corrected by measurement.** The original argument was that the instant rides the OAPIF
URI's `http-header:` vocabulary onto every provider request including the `OPTIONS`
editability probe. Captured against a bare HTTP listener on QGIS 3.44.13, half of it
survives and half does not — `core/recorded.py` holds the full record:

| Measured | Consequence |
|---|---|
| **`http-header:` parameters never reach the wire at all.** A layer URI carrying `X-Track`, `X-Recorded-At` and a marker header sent none of the three | The URI header is inert. It is still emitted, because it costs one parameter and a build that started honouring it would send the same value twice |
| The same headers carried by an **`APIHeader` auth configuration** arrived intact | This is why the *track* has always worked: the credential carries `X-Track`. It is also why Google sign-in had to keep the `APIHeader` method rather than switching to QGIS's OAuth2 one |
| A **landing-URL query parameter does survive**, on every request the provider builds except the probe | `?recorded_at=` is what actually pins a historical layer |

The probe is genuinely unpinned, exactly as `computeCapabilities` predicts, and that costs
nothing here: the historical collection is `editable: false` on the server, so the probe
answers `Allow: HEAD, GET` pinned or not and QGIS reports no write capabilities at all.
`layers.provider_advertises_writes` raises if that ever stops being true, and the layer's
own echo column is checked against the instant that was asked for — so a pin that fails to
arrive refuses the layer by name instead of quietly showing the present.

### What you see

The layer is named so it cannot be mistaken for the live one in a tree that truncates from
the right:

```
[BELIEVED 2026-01-15 08:00Z] Labels — read-only        the historical layer
Labels                                                 the live, editable one
```

**BELIEVED**, never *as-of*: the box above says "as of" and means the other axis, and if
both said it a screenshot would not tell you which question produced the map. Three visual
states, with the class colours unchanged in all three so the two layers stay comparable:

| State | How it draws |
|---|---|
| live | solid stroke, full opacity, class colour |
| believed, still true today | dashed stroke, 55% opacity, class colour |
| believed, since deleted or corrected | dashed stroke in an alert colour |

Hovering a superseded feature adds one line to the map tip: *believed until …* — which is
the question a historical layer exists to answer.

The status line under both boxes always names **both** axes, even when one of them is off:

```
Believed: 2026-01-15 08:00Z (fixed)  ·  Valid: Temporal Controller (client-side)
```

Each control on its own reads as "the" time control. Naming both means neither can be read
as the only one in play.

### The canary, and what an empty layer means

The view echoes the instant it actually resolved at on every row, and the layer filter
compares that against what was asked for. Redundant when everything works — and that is the
point: the backing view falls back to `now()` when no instant reaches it, so a header lost
to a proxy would answer a January request with **today's** data. With the canary the layer
comes back **empty** instead. Empty-and-wrong is enormously better than
populated-and-wrong, because somebody notices it.

Which means an empty historical layer has two possible causes, and the panel says which:
either nothing was believed to exist at that instant — a perfectly valid answer, reported
with the earliest instant the track has a record of — or something is broken. An instant in
the future is refused outright, by the picker and by the backend: the belief set at a future
time is simply the current one, so the layer would be *full* under a caption asserting
something nobody has ever believed.

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

The result names the history track it checked, because both layers are scoped to one and
"all labels are inside an exhaustive extent" is a different fact about a test dataset than
about the analysts' one — the sentence alone cannot tell them apart.

---

## Publishing local layers (the one-time bootstrap)

Use **Publish local layers…** in the panel's **Bootstrap** section.

The layer list follows the Layers panel from top to bottom, including nested groups.
Use **Order → A → Z** for alphabetical sorting, or switch back to **Layers panel**.
Changing order preserves your selections and settings. **Uncheck all** clears the
selection so you can choose just the layers you want to publish.

The first deployment starts with an empty backend and a folder of Esri Shapefiles that has
been version-controlled by being copied and dated. This action reads the vector layers open
in the project — excluding the ones this plugin loaded, which are already on the server —
and creates them as labels. It replaces a command-line loader that ran against a PostgreSQL
DSN, on the grounds that an analyst will never run one.

**The first thing the preview says is which history track these features would join** —
above the table, in the window title, and in the Publish button's own text. That placement
is deliberate. Every other warning on that screen appears only when there is something to
warn about, so a clean preview would otherwise say nothing at all about *where* 1,246
permanent features are going — and "where" is the one decision on the screen that was made
in another panel, minutes earlier, possibly by whoever saved the project rather than by the
person clicking now. Publishing with no track selected, or into an archived track, is
**blocked**: unlike the survey-extent warning, there is no defensible version of "into a
dataset nobody named".

Publishing the same shapefile into a *second* track is not a duplicate — it is how a test
dataset gets populated — so it stays pre-selected and gets its own sentence rather than the
"you have published this before" warning, which would otherwise train people to click
through the warning that catches the real duplicate.

**Nothing else is sent until the preview is confirmed.** The dialog lists each layer with
its feature count, geometry type, CRS and a checkbox, and a class combo populated from the
live registry. Classes are guessed from the layer name and are always overridable; an ambiguous
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

**Which collection each layer goes to is decided from its geometry type, and shown.**
Labels are stored one collection per geometry family — a cooling unit is a Point, a
powerline is a LineString — because an untyped collection cannot tell QGIS what it holds:
OGC API - Features has no way to declare a geometry type, pygeoapi reports `geometry-any`,
and QGIS falls back to inferring the type by *sampling features*. An empty collection
samples as nothing, the layer is treated as non-spatial, and the Edit menu offers "Add
Record" where it should offer "Add Polygon Feature" — every digitizing tool gone, on the
day a deployment is empty. A QGIS vector layer has exactly one geometry type, so a polygon
layer publishes to the polygon collection. **No collection id is compiled into the
plugin**: the routes are resolved against what `/collections` lists, exactly as the class
vocabulary is read from the registry, and a deployment still serving a single untyped
collection keeps working unchanged. A layer that matches no collection — a mixed or
unknown-geometry layer above all — is **refused by name and by geometry type before
anything is sent**, because a point published into the polygon collection is rejected
feature by feature by the server and 872 refusals read as an outage. Class remains an
attribute rather than a layer: there are three collections however many classes exist, and
adding one is still a single row in `label_class`.

Two more things the dialog says out loud, because both are silent failures otherwise:

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

- every provider field is copied into `attrs.source_attributes` under its original name,
  before any name cleanup or numeric conversion. This includes original text, whitespace,
  nulls, zero, colliding name columns, and names omitted from the canonical name display.
  A source column named `source_attributes` is nested inside this archive;
- legacy columns are matched onto the class's own `attr_schema`, so `No. Cooler` and
  `No. Coolim` converge on one attribute and `No. transf`/`No. Transf` on another, without
  this plugin containing any of those names;
- `Name:ch` / `Name_en` / `Name` become `names` as `{"zh": …, "en": …}`, with an unmarked
  column filed by content;
- classes that allow additional attributes preserve unmatched columns under their exact
  source names, including nulls, empty text, and zero. For example, `Company`, `Location`,
  `Area_sqm`, and the source `id` remain inside `attrs`; `Area_sqm` keeps its stated units;
- when a canonical conversion fails or two columns conflict, the original column is
  retained separately and the report explains why. If an extra top-level copy would
  conflict with a canonical attribute, the original value stays in the archive. A row
  is refused if the archive is incompatible with the class schema or a canonical mapping
  claims its reserved key. A closed class schema must allow a `source_attributes` object;
  other source fields remain in that archive when the class prohibits extra top-level
  attributes. Empty name cells are still omitted from `names`;
- single-part geometries are promoted to the multi-part type the class declares, anything
  outside EPSG:4326 is reprojected, and invalid geometries are skipped and reported rather
  than sent for the server to reject;
- **no identity is invented.** Source `id` is provenance inside `attrs`, while `label_id`
  is `uuid DEFAULT gen_random_uuid()`. Identity is the server's.

The archive stores the values QGIS reads from the provider. Ordinary text fields retain
their text exactly. Editor widgets such as Value Map or Value Relation may display a
label instead of the stored code; the archive currently preserves that code, not the
widget's display label. Values without a JSON representation are reported and the row
is refused. Source field aliases, widget configuration, and provider field definitions
are not part of the uploaded attributes.

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

Each published layer is stamped with a `cvi/published` custom property — recording the
track it went to, alongside the collection, class and count — and the project is marked
dirty so the stamp survives closing QGIS. Publishing it again *into the same track* warns
first, because the server assigns identity and therefore nothing here can recognise a
repeat. Counts accumulate per track, not across them.

---

## Development

Ordinary changes are implemented and reported without automatically running tests,
builds, native QGIS checks or releases. Run validation only when explicitly asked
to test or publish/deploy. A branch push or pull request does not start the test
workflow, and an earlier deployment approval does not authorize future releases.

```bash
./scripts/dev-link.sh          # when setting up a local QGIS development profile
python -m pip install -r requirements-dev.txt # one-time requested setup
pytest                         # when tests are requested; no QGIS required
ruff check . && ruff format --check .
```

For requested interactive QGIS work, install **Plugin Reloader** and bind it to a key: two seconds a cycle against
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
│   ├── recorded.py      transaction-time instants: the other axis, one file each
│   ├── registry.py      the class vocabulary
│   ├── routing.py       geometry type -> which collection to publish into
│   ├── expressions.py   QGIS-expression quoting, shared by asof and tracks
│   ├── styling.py       registry style block -> QGIS symbol properties
│   ├── teardown.py      the undo stack that makes unload() correct
│   ├── tracks.py        history tracks: parsing, resolution and the canary
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

Release only on an explicit publication/deployment request. Ordinary edits do not
change version numbers or create tags. For requested checks without a release,
use `gh workflow run test.yml --ref BRANCH`. Reuse successful checks for unchanged
code instead of repeating validation through several agents or environments.

```bash
# On an explicit release request, update metadata.txt, __init__.__version__, and CHANGELOG.md.
git tag -a v0.1.0 -m "v0.1.0 — custom API domain"
git push origin v0.1.0
```

The release workflow verifies the tag matches `metadata.txt` (a mismatch means the plugin
manager's upgrade detection silently never fires), runs the tests, and publishes both the
zip and a `plugins.xml` to a GitHub Release. A semver pre-release suffix — `v0.2.0-beta1` —
is flagged experimental automatically, which gives a canary channel for free.

Follow [the release checklist](docs/releasing.md), including the OAuth substitution
and archive checks, before announcing an update.

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
