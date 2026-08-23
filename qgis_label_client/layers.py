"""Creating and configuring the OGC API - Features layers.

HOW LITTLE OF THIS IS DATA ACCESS

None of it. QGIS's native ``OAPIF`` provider does the reading, the paging, the bbox
filtering and -- through Part 4 -- the create, update and delete. What the plugin adds is
the three things the provider has no way to know:

* which collection, with which credential and which as-of filter (:mod:`.core.uri`);
* how the classes should look and read, which comes from the class registry rather than
  from anything compiled in;
* a custom property marking the layer as ours, so the as-of control can find its own
  layers again without guessing from names a user is free to rename.

Style preservation around ``setDataSource`` is the non-obvious part and is explained at
:func:`repoint_layer`.
"""

from __future__ import annotations

from collections.abc import Iterable

from qgis.core import (
    Qgis,
    QgsCategorizedSymbolRenderer,
    QgsDataProvider,
    QgsEditorWidgetSetup,
    QgsFillSymbol,
    QgsLineSymbol,
    QgsMapLayer,
    QgsMarkerSymbol,
    QgsProject,
    QgsRendererCategory,
    QgsVectorLayer,
)
from qgis.PyQt.QtXml import QDomDocument

from .core import asof, styling
from .core.asof import AsOfMechanism
from .core.errors import BackendError
from .core.registry import ClassRegistry, LabelClass
from .core.uri import build_oapif_uri
from .core.urls import normalise_base_url, with_query
from .log import log
from .settings import PluginSettings

#: Marks a layer as created by this plugin, and records which collection it shows.
COLLECTION_PROPERTY = "cvi/collection_id"

#: OAPIF provider key. QGIS registers it from the WFS provider library.
OAPIF_PROVIDER = "OAPIF"


def landing_url(settings: PluginSettings) -> str:
    """The URL handed to the provider as ``url``, with the as-of filter if applicable.

    When the as-of mechanism is ``datetime`` the parameter rides on this URL's query
    string, because that is the only place QGIS's OAPIF provider lets a plugin put an
    arbitrary query parameter. When it is ``cql2`` the URL is left alone and the filter
    goes into the URI's ``filter`` parameter instead -- see :mod:`.core.asof` for why both
    exist.
    """
    base = normalise_base_url(settings.api_base_url)
    as_of = settings.as_of
    if as_of is not None and settings.as_of_mechanism is AsOfMechanism.DATETIME:
        return with_query(base, asof.datetime_query(as_of))
    return base


def build_layer_uri(
    settings: PluginSettings, collection_id: str, registry: ClassRegistry | None
) -> str:
    """Data-source URI for one collection, honouring the current as-of state."""
    as_of = settings.as_of
    cql = None
    if as_of is not None and settings.as_of_mechanism is AsOfMechanism.CQL2:
        fields = registry.fields if registry else None
        cql = asof.cql2_filter(as_of, fields) if fields else asof.cql2_filter(as_of)
    return build_oapif_uri(
        landing_url=landing_url(settings),
        collection_id=collection_id,
        authcfg=settings.authcfg or None,
        page_size=int(settings.get("page_size")),
        restrict_to_request_bbox=bool(settings.get("restrict_to_canvas")),
        cql_filter=cql,
    )


def create_layer(
    settings: PluginSettings,
    collection_id: str,
    display_name: str,
    registry: ClassRegistry | None = None,
) -> QgsVectorLayer:
    """Build a vector layer for one collection. Raises if the provider rejects it."""
    uri = build_layer_uri(settings, collection_id, registry)
    layer = QgsVectorLayer(uri, display_name, OAPIF_PROVIDER)
    if not layer.isValid():
        raise BackendError(
            f"QGIS could not open collection {collection_id!r}. "
            f"{layer.error().summary() or 'The provider gave no reason.'}"
        )
    layer.setCustomProperty(COLLECTION_PROPERTY, collection_id)
    return layer


def is_plugin_layer(layer: QgsMapLayer) -> bool:
    return bool(layer.customProperty(COLLECTION_PROPERTY, ""))


def collection_of(layer: QgsMapLayer) -> str:
    return str(layer.customProperty(COLLECTION_PROPERTY, "") or "")


def plugin_layers(project: QgsProject | None = None) -> list[QgsVectorLayer]:
    """Every vector layer this plugin loaded, in the current project."""
    project = project or QgsProject.instance()
    return [
        layer
        for layer in project.mapLayers().values()
        if layer.type() == Qgis.LayerType.Vector and is_plugin_layer(layer)
    ]


def find_layer_with_fields(
    required: Iterable[str],
    project: QgsProject | None = None,
    excluding: Iterable[str] = (),
) -> QgsVectorLayer | None:
    """First plugin layer carrying all of `required` and none of `excluding`.

    Layers are identified by the fields they expose, not by collection name. A deployment
    is free to call its collections whatever it likes, and this way the QA tools keep
    working when it does -- the field names themselves come from the registry, so even
    those are not compiled in.

    `excluding` exists because "carries these fields" is not always discriminating on its
    own; see :func:`find_label_layer`.
    """
    wanted = {name for name in required if name}
    unwanted = {name for name in excluding if name}
    if not wanted:
        return None
    for layer in plugin_layers(project):
        names = {field.name() for field in layer.fields()}
        if wanted.issubset(names) and not (unwanted & names):
            return layer
    return None


def find_label_layer(
    registry: ClassRegistry, project: QgsProject | None = None
) -> QgsVectorLayer | None:
    """The layer holding labels: it has both an identity and a class.

    The audit collection has both too -- ``label_history`` is keyed on the same
    ``label_id`` and carries the ``class_id`` of each superseded state -- so identity and
    class alone do not tell the two apart, and ``QgsProject.mapLayers()`` returns them in
    an order nobody controls. Picking the audit layer would silently run the coverage
    check over every historical revision of every label, counting a label once per edit
    and classifying geometry that is no longer on the map. Excluding the transaction-time
    columns is what makes the choice deterministic.
    """
    return find_layer_with_fields(
        (registry.fields.label_id, registry.fields.class_id),
        project,
        excluding=(registry.fields.history_id, registry.fields.operation),
    )


def find_extent_layer(
    registry: ClassRegistry, project: QgsProject | None = None
) -> QgsVectorLayer | None:
    """The layer holding survey extents: it says how complete a sweep was."""
    return find_layer_with_fields((registry.fields.class_id, registry.fields.completeness), project)


def repoint_layer(layer: QgsVectorLayer, uri: str) -> None:
    """Swap a layer's data source, keeping its styling and form configuration.

    ``setDataSource`` rebuilds the provider, and the renderer, field aliases and editor
    widgets do not reliably survive that. Losing them is not cosmetic: the categorized
    renderer and the ``class_id`` value map are the plugin's entire contribution to how
    the layer reads, so an as-of change would silently undo the configuration it was
    supposed to leave alone.

    Exporting to a QDomDocument and re-importing afterwards is the cheap, provider-
    agnostic way to hold on to all of it.
    """
    document = QDomDocument()
    layer.exportNamedStyle(document)

    options = QgsDataProvider.ProviderOptions()
    layer.setDataSource(uri, layer.name(), OAPIF_PROVIDER, options, False)

    restored, message = layer.importNamedStyle(document)
    if not restored:
        log(f"Could not restore style on {layer.name()!r} after re-pointing: {message}")
    layer.triggerRepaint()


def _symbol_for(label_class: LabelClass):
    """Build a symbol matching the class's geometry type and registry style."""
    properties = styling.symbol_properties(label_class.geom_type, label_class.style)
    kind = styling.symbol_kind(label_class.geom_type)
    if kind == "marker":
        return QgsMarkerSymbol.createSimple(properties)
    if kind == "line":
        return QgsLineSymbol.createSimple(properties)
    return QgsFillSymbol.createSimple(properties)


def apply_registry(layer: QgsVectorLayer, registry: ClassRegistry) -> None:
    """Configure a label layer from the class registry.

    Everything here is derived from what the server sent. No class name, attribute name
    or enum value is written into the plugin, which is what keeps QGIS and the web viewer
    from drifting apart when someone adds a class on a Tuesday.
    """
    fields = registry.fields
    names = [field.name() for field in layer.fields()]

    if fields.class_id in names:
        _apply_class_renderer(layer, registry)
        _apply_class_value_map(layer, registry)

    _apply_aliases(layer, registry, names)
    _apply_map_tip(layer, registry, names)


def _apply_class_renderer(layer: QgsVectorLayer, registry: ClassRegistry) -> None:
    """Categorize on ``class_id`` using each class's own style block.

    All classes live in one table with a ``class_id`` column, so one layer carries them
    all and a categorized renderer is the natural expression of that. Retired classes are
    included: historical labels still reference them, and dropping their category would
    render those features invisible rather than merely uneditable.
    """
    categories = [
        QgsRendererCategory(
            label_class.class_id,
            _symbol_for(label_class),
            label_class.display_name,
            True,
        )
        for label_class in registry
    ]
    if not categories:
        return
    layer.setRenderer(QgsCategorizedSymbolRenderer(registry.fields.class_id, categories))
    layer.triggerRepaint()


def _apply_class_value_map(layer: QgsVectorLayer, registry: ClassRegistry) -> None:
    """Turn ``class_id`` into a picker of the classes the server currently accepts.

    A list, not free text: ``label_class.class_id`` is a foreign key with a snake_case
    CHECK constraint, and a typo becomes a failed write at save time rather than a
    validation message at edit time. Only active classes are offered, because the
    database refuses new labels on a retired class.
    """
    index = layer.fields().indexOf(registry.fields.class_id)
    if index < 0:
        return
    entries = [{display: stored} for display, stored in registry.value_map()]
    if not entries:
        return
    layer.setEditorWidgetSetup(index, QgsEditorWidgetSetup("ValueMap", {"map": entries}))


def _apply_aliases(layer: QgsVectorLayer, registry: ClassRegistry, names: list[str]) -> None:
    """Give the stable core columns readable names, and flag the JSON containers.

    Only the core columns are aliased. The contents of ``attrs`` are not fields at the
    OAPIF level -- they arrive inside one JSON value -- and inventing aliases for them
    would mean hardcoding attribute names, which is exactly the thing the schema design
    exists to avoid.
    """
    fields = registry.fields
    aliases = {
        fields.label_id: "Label ID (immutable)",
        fields.class_id: "Class",
        fields.name_zh: "Name (中文)",
        fields.name_en: "Name (English)",
        fields.names: "Names (JSON, all languages)",
        fields.attrs: "Attributes (JSON, see class schema)",
        fields.valid_from: "Valid from (on the ground)",
        fields.valid_to: "Valid to (on the ground)",
        fields.capture_id: "Drawn from capture",
        fields.updated_by: "Last edited by",
        fields.updated_at: "Last edited at",
    }
    for name, alias in aliases.items():
        if name in names:
            layer.setFieldAlias(layer.fields().indexOf(name), alias)

    for json_field in (fields.attrs, fields.names):
        index = layer.fields().indexOf(json_field)
        if index >= 0:
            layer.setEditorWidgetSetup(
                index, QgsEditorWidgetSetup("TextEdit", {"IsMultiline": True, "UseHtml": False})
            )


def _apply_map_tip(layer: QgsVectorLayer, registry: ClassRegistry, names: list[str]) -> None:
    """A hover tip that answers the three questions asked most while labeling.

    Chinese name first. 82.6% of compounds have a Chinese name and 8.9% an English one,
    so an English-first tip is blank most of the time.
    """
    fields = registry.fields
    rows: list[str] = []
    if fields.name_zh in names:
        rows.append(f'<b>[% "{fields.name_zh}" %]</b>')
    if fields.name_en in names:
        rows.append(f'[% "{fields.name_en}" %]')
    if fields.class_id in names:
        rows.append(f'<i>[% "{fields.class_id}" %]</i>')
    if fields.label_id in names:
        rows.append(f'<small>[% "{fields.label_id}" %]</small>')
    if rows:
        layer.setMapTipTemplate("<br/>".join(rows))
