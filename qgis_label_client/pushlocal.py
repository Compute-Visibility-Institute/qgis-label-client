"""Incremental upload of reviewed local vector layers using the existing publisher."""

from __future__ import annotations

import hashlib
import json
from contextlib import suppress
from dataclasses import replace
from pathlib import Path
from threading import Event
from uuid import UUID

from qgis.core import Qgis, QgsApplication, QgsProject
from qgis.PyQt.QtWidgets import QAction, QDialog

from . import client, publish
from .core.errors import ConfigurationError, LabelClientError
from .core.publish import LayerChoice, build_plan
from .core.pushlocal import UploadLedger, fingerprint, spatial_fingerprint
from .core.routing import geometry_family
from .publishdialog import PublishDialog

MAPPING_PROPERTY = "cvi/push_local_mapping"
LOCAL_STATE_PROPERTY = "cvi/local_push_state"
LOCAL_NAME_PROPERTY = "cvi/local_push_original_name"


def _identity(value):
    try:
        return str(UUID(str(value))) if value else ""
    except (ValueError, TypeError):
        return ""


class ExistingFeatures:
    """Worker-owned server inventory and durable before-send journal."""

    def __init__(self, request, ledger, active, source_namespaces=None):
        self.request, self.ledger, self.active = request, ledger, active
        self.keys, self.identities, self.spatial, self.selected = {}, {}, {}, set()
        self.selected_spatial = set()
        self.source_namespaces = source_namespaces or {}
        self.source_refs = {}

    def check_active(self):
        if not self.active.is_set():
            raise ConfigurationError("The connection changed. No more local features were sent.")

    def load(self, feedback):
        for collection in sorted({self.request.target_for(p) for p in self.request.layers}):
            keys, identities, spatial, pages = set(), {}, set(), set()
            offset = 0
            while True:
                self.check_active()
                if feedback is not None and feedback.isCanceled():
                    raise ConfigurationError("Checking existing features was cancelled.")
                document = client.fetch_features(
                    self.request.base_url,
                    collection,
                    self.request.authcfg,
                    {"limit": 1000, "offset": offset},
                    feedback,
                    track=self.request.track,
                )
                features = document.get("features") if isinstance(document, dict) else None
                if not isinstance(features, list):
                    raise ConfigurationError("Could not enumerate existing features safely.")
                signature = hashlib.sha256(
                    json.dumps(features, sort_keys=True).encode()
                ).hexdigest()
                if features and signature in pages:
                    raise ConfigurationError("The server repeated a page; nothing was uploaded.")
                pages.add(signature)
                for feature in features:
                    key = fingerprint(feature, self.request.fields)
                    keys.add(key)
                    spatial.add(spatial_fingerprint(feature, self.request.fields))
                    identity = _identity(
                        (feature.get("properties") or {}).get(self.request.fields.label_id)
                    )
                    if identity:
                        identities[identity] = key
                offset += len(features)
                total = document.get("numberMatched")
                has_next = any(link.get("rel") == "next" for link in document.get("links", []))
                if isinstance(total, int) and offset >= total:
                    break
                if not features or (len(features) < 1000 and not has_next):
                    if isinstance(total, int) and offset < total:
                        raise ConfigurationError("The server returned an incomplete feature list.")
                    break
                if offset >= 100000:
                    raise ConfigurationError(
                        "Over 100,000 features require a reviewed bulk import."
                    )
            self.keys[collection], self.identities[collection] = keys, identities
            self.spatial[collection] = spatial

    def skip(self, collection, feature, source_values, source_ref=None):
        self.check_active()
        key = fingerprint(feature, self.request.fields)
        source = ""
        if source_ref is not None:
            layer_id, fid = source_ref
            source = f"{self.source_namespaces.get(layer_id, layer_id)}:{fid}"
        identity = _identity(source_values.get(self.request.fields.label_id))
        if identity and identity in self.identities[collection]:
            if self.identities[collection][identity] != key:
                return (
                    "review",
                    "This server label identity already exists with different geometry or attributes; edit the server layer instead of creating a copy.",
                )
            self.ledger.observe(collection, key, source)
            return "present", "The source label identity already exists on the server."
        if key in self.keys[collection]:
            self.ledger.observe(collection, key, source)
            return "present", "The same geometry and attributes already exist on the server."
        if spatial_fingerprint(feature, self.request.fields) in self.spatial[collection]:
            return (
                "review",
                "This geometry and class already exist with different attributes; review before creating a duplicate.",
            )
        state = self.ledger.state(collection, key)
        prior_source = self.ledger.source_fingerprint(collection, source) if source else ""
        if state or identity or prior_source:
            return "review", (
                "This feature has a previous server identity or upload attempt but no current "
                "matching record. It was not recreated; review its history first."
            )
        if (collection, key) in self.selected:
            return "present", "An identical feature is already included in this upload."
        spatial = spatial_fingerprint(feature, self.request.fields)
        if (collection, spatial) in self.selected_spatial:
            return (
                "review",
                "Another local feature has this geometry and class with different attributes; review their differences.",
            )
        self.selected.add((collection, key))
        self.selected_spatial.add((collection, spatial))
        if source:
            self.source_refs[(collection, key)] = source
        return "", ""

    def started(self, collection, features, reason):
        self.check_active()
        keys = [fingerprint(f, self.request.fields) for f in features]
        sources = [
            (self.source_refs[(collection, key)], key)
            for key in keys
            if (collection, key) in self.source_refs
        ]
        self.ledger.reserve(collection, keys, reason, sources)

    def finished(self, collection, features, state):
        self.ledger.finish(
            collection, [fingerprint(f, self.request.fields) for f in features], state
        )


class PushAllLocal:
    def __init__(self, plugin):
        self.plugin = plugin
        self.active = None
        self.closed = False
        self.watches = {}
        self.revisions = {}

    def install(self, menu_name):
        action = QAction("Push all local", self.plugin.iface.mainWindow())
        action.setToolTip(
            "Upload missing local points, lines and polygons to the connected track; review new mappings."
        )
        action.triggered.connect(self.push_all)
        self.plugin.iface.addPluginToMenu(menu_name, action)
        self.plugin.teardown.add(
            "menu: push all local", lambda: self.plugin.iface.removePluginMenu(menu_name, action)
        )
        project = QgsProject.instance()
        for label, signal in (
            ("local source additions", project.layersAdded),
            ("local source project read", project.readProject),
        ):
            signal.connect(self.attach_sources)
            self.plugin.teardown.add(
                label, lambda item=signal: item.disconnect(self.attach_sources)
            )
        self.attach_sources()

    def attach_sources(self, *_args):
        if self.closed:
            return
        for layer in publish.local_vector_layers():
            try:
                saved = json.loads(layer.customProperty(MAPPING_PROPERTY, ""))
            except (ValueError, TypeError):
                continue
            if (
                not isinstance(saved, dict)
                or not saved.get("publish")
                or layer.id() in self.watches
            ):
                continue
            bindings = []
            for name in (
                "featureAdded",
                "featureDeleted",
                "geometryChanged",
                "attributeValueChanged",
            ):
                signal = getattr(layer, name)

                def callback(*_, target=layer, change=name):
                    self.source_changed(target, change)

                signal.connect(callback)
                bindings.append((signal, callback))
            self.watches[layer.id()] = (layer, bindings)
            self.revisions.setdefault(layer.id(), 0)

    def source_changed(self, layer, change):
        if self.closed:
            return
        self.revisions[layer.id()] = self.revisions.get(layer.id(), 0) + 1
        previous = layer.customProperty(LOCAL_STATE_PROPERTY, "")
        state = "review" if change == "featureDeleted" or previous == "review" else "unpushed"
        layer.setCustomProperty(LOCAL_STATE_PROPERTY, state)
        original = layer.customProperty(LOCAL_NAME_PROPERTY, "") or layer.name()
        layer.setCustomProperty(LOCAL_NAME_PROPERTY, original)
        layer.setName(f"[Unpushed] {original}")
        if self.plugin.pending is not None:
            self.plugin.pending._warn(
                f"{original}: local changes have not been pushed. Push all local adds missing "
                "features; changes to existing server labels and local deletions require review. "
                "Save the source file and QGIS project before closing; memory layers need "
                "to be exported to a file."
            )

    def cancel(self):
        if self.active is not None:
            self.active.clear()

    def close(self):
        self.closed = True
        self.cancel()
        for _layer, bindings in self.watches.values():
            for signal, callback in bindings:
                with suppress(RuntimeError, TypeError):
                    signal.disconnect(callback)
        self.watches.clear()

    def on_connected(self, callback=None):
        self.push(automatic=True, callback=callback)

    def push_all(self):
        """The explicit menu action also flushes native local edit buffers first."""

        def resume():
            self.push(callback=self.plugin.startup.refresh_layers)

        if self.plugin.pending is not None:
            self.plugin.pending.on_connected(resume)
        else:
            resume()

    def push(self, _checked=False, *, automatic=False, callback=None):
        plugin = self.plugin
        callback_context = (
            plugin._session.generation,
            plugin.settings.api_base_url,
            plugin.settings.oauth_email,
            plugin.settings.track,
        )

        def complete():
            current = (
                plugin._session.generation,
                plugin.settings.api_base_url,
                plugin.settings.oauth_email,
                plugin.settings.track,
            )
            if callback is not None and not self.closed and current == callback_context:
                callback()

        if self.closed or plugin.dock is None or plugin.publishing:
            complete()
            return
        if not plugin.registry or plugin._current_write_access() is not True:
            if not automatic:
                plugin._message(
                    "Connect with label write access before pushing local features.",
                    Qgis.MessageLevel.Warning,
                )
            complete()
            return
        track = plugin.current_track()
        if track is None or not plugin.settings.oauth_email:
            complete()
            return
        if plugin._defer_until_fresh(lambda: self.push(automatic=automatic, callback=callback)):
            return
        context = (plugin.settings.api_base_url, plugin.settings.oauth_email.casefold(), track.name)
        generation = plugin._session.generation

        def same_context():
            current = plugin.current_track()
            return (
                generation == plugin._session.generation
                and current is not None
                and (
                    plugin.settings.api_base_url,
                    plugin.settings.oauth_email.casefold(),
                    current.name,
                )
                == context
            )

        local = [
            layer
            for layer in publish.local_vector_layers()
            if publish.is_local_provider(layer)
            and layer.providerType().lower()
            in {"memory", "ogr", "spatialite", "gpkg", "delimitedtext"}
            and not any(
                marker in layer.source().lower()
                for marker in ("http://", "https://", "ftp://", "pg:", "/vsi")
            )
            and geometry_family(publish.geometry_type_name(layer))
            in {"Point", "LineString", "Polygon"}
        ]
        if not local:
            if not automatic:
                plugin._message(
                    "No supported local point, line or polygon layers to push. Server layers use their normal Save Layer Edits."
                )
            complete()
            return
        routes = plugin._label_routes()
        if not routes:
            complete()
            return
        sources, choices = [], {}
        for layer in local:
            source = replace(publish.describe_layer(layer), previous=None)
            sources.append(source)
            try:
                saved = json.loads(layer.customProperty(MAPPING_PROPERTY, ""))
            except (ValueError, TypeError):
                saved = None
            if isinstance(saved, dict) and saved.get("context") == list(context):
                choices[source.layer_id] = LayerChoice(
                    layer_id=source.layer_id,
                    publish=bool(saved.get("publish")),
                    class_id=saved.get("class_id"),
                    skip_damaged_names=bool(saved.get("skip_damaged_names")),
                    include_style=False,
                )
        plan = build_plan(sources, plugin.registry, choices=choices, track=track, routes=routes)
        review_ids = {
            item.source.layer_id
            for item in plan
            if item.source.layer_id not in choices or item.problems()
        }
        if review_ids:
            dialog = PublishDialog(
                [source for source in sources if source.layer_id in review_ids],
                plugin.registry,
                plugin.iface.mainWindow(),
                track=track,
                routes=routes,
                bootstrap_style_supported=plugin.bootstrap_style_supported,
            )
            dialog.setWindowTitle(f"Push missing local features — review mapping for {track.name}")
            try:
                accepted = dialog.exec() == QDialog.DialogCode.Accepted
                reviewed = dialog.plan()
            finally:
                dialog.deleteLater()
            if not accepted:
                complete()
                return
            choices.update({item.source.layer_id: item.choice for item in reviewed})
            plan = build_plan(sources, plugin.registry, choices=choices, track=track, routes=routes)
            # Remember reviewed mappings, including deliberate exclusions, with their
            # exact destination. Reconnect never guesses a class for a new local layer.
            for item in plan:
                layer = QgsProject.instance().mapLayer(item.source.layer_id)
                if layer is not None:
                    layer.setCustomProperty(
                        MAPPING_PROPERTY,
                        json.dumps(
                            {
                                "context": context,
                                "publish": item.choice.publish,
                                "class_id": item.choice.class_id,
                                "skip_damaged_names": item.choice.skip_damaged_names,
                            }
                        ),
                    )
            self.attach_sources()
        selected = list(plan.selected())
        if not selected:
            complete()
            return
        if not same_context() or plugin._current_write_access() is not True:
            plugin._message(
                "The connection changed during the review. Reconnect before pushing.",
                Qgis.MessageLevel.Warning,
            )
            complete()
            return
        if plan.problems():
            plugin._fail(" ".join(plan.problems()))
            complete()
            return
        if any(item.choice.declare_extent for item in selected):
            plugin._message(
                "Push all local does not recreate survey extents. Uncheck Survey extent in the review and retry.",
                Qgis.MessageLevel.Warning,
            )
            complete()
            return
        try:
            prepared = publish.prepare(selected)
        except LabelClientError as exc:
            plugin._fail(str(exc))
            complete()
            return
        request = publish.PublishRequest(
            base_url=context[0],
            collection_id=routes.untyped,
            authcfg=plugin.settings.authcfg_for(track.name),
            layers=prepared,
            fields=plugin.registry.fields,
            track=track.name,
            capabilities_path=plugin.settings.get("capabilities_path"),
            chunk_size=plugin.settings.get("publish_chunk_size"),
            verified_bulk=plugin.bulk_capability,
            bootstrap_style_supported=plugin.bootstrap_style_supported,
        )
        path = Path(QgsApplication.qgisSettingsDirPath()) / "cvi-unpushed" / "local-uploads.sqlite"
        revisions = {
            item.source.layer_id: self.revisions.get(item.source.layer_id, 0) for item in selected
        }
        source_namespaces = {
            layer.id(): hashlib.sha256(
                json.dumps(
                    [
                        layer.providerType(),
                        layer.id() if layer.providerType() == "memory" else layer.source(),
                    ]
                ).encode()
            ).hexdigest()
            for layer in local
        }
        active = Event()
        active.set()
        self.active = active
        plugin.publishing = True
        plugin.publish_action.setEnabled(False)
        plugin.dock.set_publish_status("Checking which local features are already stored…")

        def work(feedback):
            ledger = UploadLedger(path, *context)
            try:
                guard = ExistingFeatures(request, ledger, active, source_namespaces)
                guard.load(feedback)
                request.feature_guard = guard
                return publish.publish(request, feedback)
            finally:
                ledger.close()

        def done(report):
            self.active = None
            plugin._end_publish()
            if self.closed:
                return
            if not same_context():
                plugin._message(
                    f"The earlier upload to {context[0]}, track {track.name}, finished. "
                    + report.summary(),
                    Qgis.MessageLevel.Warning,
                )
                return
            if report.clean:
                for item in selected:
                    layer = QgsProject.instance().mapLayer(item.source.layer_id)
                    if (
                        layer is not None
                        and self.revisions.get(layer.id(), 0) == revisions[layer.id()]
                        and layer.customProperty(LOCAL_STATE_PROPERTY, "") != "review"
                    ):
                        original = layer.customProperty(LOCAL_NAME_PROPERTY, "")
                        if original:
                            layer.setName(original)
                            layer.removeCustomProperty(LOCAL_NAME_PROPERTY)
                        layer.removeCustomProperty(LOCAL_STATE_PROPERTY)
            if automatic and report.clean:
                plugin.dock.set_publish_status(report.summary())
                plugin._message(report.summary())
            else:
                plugin._on_published(selected, routes.untyped, report, track.name, context[0])
            complete()

        def failed(message):
            self.active = None
            plugin._end_publish()
            if not self.closed:
                plugin._fail(message)
                if same_context():
                    complete()

        try:
            plugin.tasks.run(
                "Push missing local features", work, done, failed, deliver_when_cancelled=True
            )
        except Exception as exc:  # noqa: BLE001 - release publishing state if task submission fails
            failed(str(exc))
