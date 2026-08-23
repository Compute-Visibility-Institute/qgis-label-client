"""The main-thread half: capturing what the worker may touch, and stamping what happened.

Two things are load-bearing here and neither is visible from the pure core.

* ``prepare()`` is the thread boundary. Everything the worker will read has to be captured
  on this side of it, and anything that cannot be captured has to fail here, loudly, rather
  than as a null dereference on a worker thread where the traceback is swallowed.
* ``stamp_published()`` is the whole idempotency mechanism. It pairs plans to outcomes by
  position, and if that pairing is ever wrong the warning on the second run names the wrong
  layer -- which is worse than no warning, because it is believed.
"""

from __future__ import annotations

import pytest
from snapshot_fixtures import REGISTRY, SNAPSHOT_LAYERS

from qgis_label_client import publish as publish_tools
from qgis_label_client.core.errors import ConfigurationError
from qgis_label_client.core.publish import (
    PUBLISHED_PROPERTY,
    LayerChoice,
    LayerPlan,
    PublishRecord,
    PublishReport,
    SourceLayer,
    parse_record,
)

COMPOUND = REGISTRY.get("compound")
STORAGE = "EPSG:4326"


class FakeCrs:
    def __init__(self, authid: str = STORAGE, *, valid: bool = True) -> None:
        self._authid = authid
        self._valid = valid

    def authid(self) -> str:
        return self._authid

    def description(self) -> str:
        return self._authid or "a CRS with no authority code"

    def isValid(self) -> bool:  # noqa: N802 - Qt naming
        return self._valid

    def __eq__(self, other) -> bool:
        return isinstance(other, FakeCrs) and other._authid == self._authid

    def __hash__(self) -> int:
        return hash(self._authid)


class FakeField:
    def __init__(self, name: str) -> None:
        self._name = name

    def name(self) -> str:
        return self._name


class FakeLayer:
    def __init__(self, layer_id: str, crs: FakeCrs, columns=()) -> None:
        self._id = layer_id
        self._crs = crs
        self._fields = [FakeField(name) for name in columns]
        self.properties: dict[str, str] = {}

    def id(self) -> str:
        return self._id

    def crs(self) -> FakeCrs:
        return self._crs

    def fields(self):
        return self._fields

    def setCustomProperty(self, key: str, value) -> None:  # noqa: N802 - Qt naming
        self.properties[key] = value

    def customProperty(self, key: str, default=None):  # noqa: N802
        return self.properties.get(key, default)


class FakeProject:
    def __init__(self, layers) -> None:
        self._layers = {layer.id(): layer for layer in layers}
        self.dirty = False

    def mapLayer(self, layer_id: str):  # noqa: N802 - Qt naming
        return self._layers.get(layer_id)

    def transformContext(self):  # noqa: N802
        return object()

    def setDirty(self, dirty: bool = True) -> None:  # noqa: N802 - Qt naming
        self.dirty = dirty


@pytest.fixture(autouse=True)
def _storage_crs(monkeypatch):
    """Make the storage CRS comparable without a projection database."""
    monkeypatch.setattr(publish_tools, "_target_crs", lambda: FakeCrs(STORAGE))


def _plan(layer_id: str = "compounds", *, crs_authid: str = STORAGE, crs_valid: bool = True):
    source = SourceLayer(
        layer_id=layer_id,
        name="Compounds",
        geometry_type="Polygon",
        crs_authid=crs_authid,
        crs_valid=crs_valid,
        feature_count=190,
        field_names=tuple(SNAPSHOT_LAYERS["Compounds"]),
    )
    return LayerPlan(
        source=source,
        choice=LayerChoice(layer_id=layer_id, publish=True, class_id="compound"),
        label_class=COMPOUND,
    )


class FakeAttributeFeature:
    def __init__(self, values: dict) -> None:
        self._values = values

    def attribute(self, name):
        return self._values.get(name)


class FakeScannableLayer(FakeLayer):
    """A layer describe_layer() can be run against, with a provider it can be asked about."""

    def __init__(self, layer_id: str, provider: str, features=(), columns=()) -> None:
        super().__init__(layer_id, FakeCrs(STORAGE), columns)
        self._provider = provider
        self._features = list(features)
        self.scans = 0

    def name(self) -> str:
        return "Compounds"

    def providerType(self) -> str:  # noqa: N802 - Qt naming
        return self._provider

    def featureCount(self) -> int:  # noqa: N802
        return len(self._features)

    def getFeatures(self, _request=None):  # noqa: N802
        self.scans += 1
        return iter(self._features)


@pytest.fixture
def _no_wkb(monkeypatch):
    """describe_layer asks QGIS to spell the WKB type; the stub cannot."""
    monkeypatch.setattr(publish_tools, "geometry_type_name", lambda _layer: "Polygon")


# --- what the preview reads on the main thread ------------------------------


def test_a_layer_on_local_disk_has_its_names_scanned(_no_wkb):
    layer = FakeScannableLayer(
        "compounds",
        "ogr",
        [FakeAttributeFeature({"Name:ch": "快手智能云乌兰察布数据中X8"})],
        SNAPSHOT_LAYERS["Compounds"],
    )
    described = publish_tools.describe_layer(layer)

    assert layer.scans == 1
    assert described.scanned == 1
    assert described.damaged_names == 1


def test_a_layer_on_a_remote_provider_is_not_read_while_the_dialog_is_built(_no_wkb):
    # 20,000 rows over a WAN, on the main thread, before the preview appears -- for a
    # layer nobody has said they want to publish yet.
    layer = FakeScannableLayer(
        "compounds",
        "postgres",
        [FakeAttributeFeature({"Name:ch": "快手智能云乌兰察布数据中X8"})],
        SNAPSHOT_LAYERS["Compounds"],
    )
    described = publish_tools.describe_layer(layer)

    assert layer.scans == 0
    assert described.scanned == 0
    # And the plan says so, because a scan that did not happen returns the same zero as a
    # scan that found nothing.
    plan = LayerPlan(
        source=described,
        choice=LayerChoice(layer_id="compounds", publish=True, class_id="compound"),
        label_class=COMPOUND,
    )
    assert any("NOT scanned" in note for note in plan.notes())


def test_a_provider_that_cannot_count_in_advance_is_not_reported_as_empty(_no_wkb):
    layer = FakeScannableLayer("compounds", "WFS", (), SNAPSHOT_LAYERS["Compounds"])
    layer.featureCount = lambda: -1

    described = publish_tools.describe_layer(layer)

    assert described.count_known is False
    assert described.feature_count == 0
    plan = LayerPlan(
        source=described,
        choice=LayerChoice(layer_id="compounds", publish=True, class_id="compound"),
        label_class=COMPOUND,
    )
    # "I cannot tell you yet" must not become "there is nothing here", which the preview
    # makes a blocking problem.
    assert plan.problems() == ()


# --- the thread boundary ----------------------------------------------------


def test_a_layer_already_in_the_storage_crs_needs_no_transform():
    project = FakeProject([FakeLayer("compounds", FakeCrs(STORAGE), SNAPSHOT_LAYERS["Compounds"])])
    prepared = publish_tools.prepare([_plan()], project)

    assert len(prepared) == 1
    assert prepared[0].transform is None
    assert prepared[0].field_names == tuple(SNAPSHOT_LAYERS["Compounds"])
    # The columns were mapped here, on the main thread, against the chosen class.
    assert any(m.target == "cooling_unit_count" for m in prepared[0].mappings)


def test_a_projected_layer_gets_its_transform_built_on_the_main_thread():
    project = FakeProject([FakeLayer("compounds", FakeCrs("EPSG:32649"))])
    prepared = publish_tools.prepare([_plan(crs_authid="EPSG:32649")], project)

    assert prepared[0].transform is not None


def test_a_layer_with_no_usable_crs_is_refused_rather_than_reprojected():
    # QgsCoordinateTransform short-circuits to a no-op when either CRS is invalid, so the
    # alternative to refusing is writing projected coordinates into a 4326 column with
    # nothing anywhere raising.
    project = FakeProject([FakeLayer("compounds", FakeCrs("", valid=False))])
    with pytest.raises(ConfigurationError, match="coordinate reference system"):
        publish_tools.prepare([_plan(crs_authid="", crs_valid=False)], project)


def test_a_layer_that_left_the_project_is_reported_not_dereferenced():
    with pytest.raises(ConfigurationError, match="no longer in the project"):
        publish_tools.prepare([_plan()], FakeProject([]))


def test_unselected_layers_are_never_prepared():
    plan = _plan()
    plan = LayerPlan(
        source=plan.source,
        choice=LayerChoice(layer_id="compounds", publish=False),
        label_class=COMPOUND,
    )
    assert publish_tools.prepare([plan], FakeProject([])) == []


# --- the idempotency stamp --------------------------------------------------


def _report(*published: int) -> PublishReport:
    report = PublishReport()
    for index, count in enumerate(published):
        report.outcome_for(f"Layer {index}", "compound").published = count
    return report


def test_publishing_stamps_the_layer_so_a_second_run_can_warn():
    layer = FakeLayer("compounds", FakeCrs(STORAGE))
    publish_tools.stamp_published([_plan()], _report(190), "label", FakeProject([layer]))

    record = parse_record(layer.customProperty(PUBLISHED_PROPERTY, ""))
    assert record.feature_count == 190
    assert record.collection_id == "label"
    assert record.published_at
    assert "SECOND copy" in record.describe()


def test_a_second_run_accumulates_rather_than_overwriting_the_count():
    layer = FakeLayer("compounds", FakeCrs(STORAGE))
    plan = _plan()
    plan = LayerPlan(
        source=SourceLayer(
            layer_id="compounds",
            name="Compounds",
            feature_count=190,
            previous=PublishRecord(feature_count=190),
        ),
        choice=plan.choice,
        label_class=COMPOUND,
    )
    publish_tools.stamp_published([plan], _report(190), "label", FakeProject([layer]))

    assert parse_record(layer.customProperty(PUBLISHED_PROPERTY, "")).feature_count == 380


def test_the_project_is_marked_dirty_so_the_record_survives_closing_qgis():
    # The record lives in a layer custom property, which is in memory until the project
    # file is saved -- and QGIS does not prompt for a change it was not told about. Without
    # this the analyst closes QGIS unprompted, reopens the same shapefiles, and every layer
    # is pre-ticked for a second publish with no warning anywhere.
    layer = FakeLayer("compounds", FakeCrs(STORAGE))
    project = FakeProject([layer])

    publish_tools.stamp_published([_plan()], _report(190), "label", project)

    assert project.dirty is True


def test_a_layer_that_published_nothing_is_not_stamped():
    layer = FakeLayer("compounds", FakeCrs(STORAGE))
    project = FakeProject([layer])
    publish_tools.stamp_published([_plan()], _report(0), "label", project)
    assert layer.properties == {}
    # Nothing was written, so there is nothing to prompt anybody to save.
    assert project.dirty is False


def test_a_cancelled_run_stamps_only_the_layers_it_reached():
    # Fewer outcomes than plans. The layers the run never got to must come back next time
    # with no record at all, because nothing of theirs is on the server.
    first = FakeLayer("first", FakeCrs(STORAGE))
    second = FakeLayer("second", FakeCrs(STORAGE))
    plans = [_plan("first"), _plan("second")]

    publish_tools.stamp_published(plans, _report(40), "label", FakeProject([first, second]))

    assert PUBLISHED_PROPERTY in first.properties
    assert second.properties == {}
