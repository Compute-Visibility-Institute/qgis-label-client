"""The worker half of the bootstrap publish: what it sends, and how it counts.

The decisions live in :mod:`~qgis_label_client.core.publish` and are tested there. What is
tested HERE is the part that cannot be pure -- the loop that reads a layer, POSTs it and
attributes every departure from a clean run to a specific row -- because the accounting is
where a publish lies to the user most cheaply. "50 rejected by the server" when the user
pressed cancel, or a feature counted as published that the server never saw, are both
reports that read as facts.

Three properties get a test each because each one, wrong, produces duplicate or missing
rows in a founding dataset that nothing afterwards can tell apart:

* **nothing is ever sent twice.** A save is not atomic, there is no ETag and identity is
  the server's, so a retry of an ambiguous failure is how duplicates are made.
* **a 429 is not a refusal.** The auth edge caps writes at a couple per second; counting
  its "not yet" as "no" would end a 1,246-feature bootstrap with most of the dataset
  reported refused and a layer stamped as published.
* **every refusal names its row.** After a partial run that is the only way to know what
  still needs sending.

QGIS is not running, so the two things the loop touches -- a feature source and a geometry
-- are replaced by fakes that answer the four questions the loop actually asks (is it null,
is it empty, is it valid, what is its GeoJSON). That is a deliberately small surface: the
alternative is a QGIS emulator, and the moment these fakes have to grow a fifth answer the
test belongs against a real QGIS instead.
"""

from __future__ import annotations

import json

import pytest
from snapshot_fixtures import REGISTRY, SNAPSHOT_LAYERS, TRACK

from qgis_label_client import client
from qgis_label_client import publish as publish_tools
from qgis_label_client.core.errors import BackendError, ConfigurationError
from qgis_label_client.core.fields import COMPLETENESS_EXHAUSTIVE, COMPLETENESS_PARTIAL
from qgis_label_client.core.legacy import map_fields
from qgis_label_client.core.publish import LayerChoice, LayerPlan, SourceLayer

COMPOUND = REGISTRY.get("compound")

SQUARE = {"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]]}


class FakeGeometry:
    """The four questions :func:`~qgis_label_client.publish._publish_layer` asks."""

    def __init__(self, geojson=SQUARE, *, valid: bool = True, empty: bool = False) -> None:
        self.geojson = geojson
        self._valid = valid
        self._empty = empty
        self.transformed = False

    def isNull(self) -> bool:  # noqa: N802 - Qt naming
        return self.geojson is None

    def isEmpty(self) -> bool:  # noqa: N802
        return self._empty

    def isGeosValid(self) -> bool:  # noqa: N802
        return self._valid

    def transform(self, _transform) -> None:
        self.transformed = True


class FakeFeature:
    def __init__(self, values: dict, geometry: FakeGeometry | None = None) -> None:
        self._values = values
        self._geometry = geometry if geometry is not None else FakeGeometry()

    def attribute(self, name):
        return self._values.get(name)

    def geometry(self) -> FakeGeometry:
        return self._geometry


class FakeSource:
    """Stands in for QgsVectorLayerFeatureSource, which is all the worker ever sees."""

    def __init__(self, features) -> None:
        self._features = list(features)

    def getFeatures(self):  # noqa: N802 - Qt naming
        return iter(self._features)


def _serialisable(feature: dict) -> dict:
    """Assert the feature would survive the request body, then hand it back.

    Checked on every recorded POST rather than in one test, because the failure it guards
    is not "this feature is wrong" but "the run dies here": ``json.dumps`` raises
    ``TypeError``, which is not a ``BackendError``, so it escapes the per-feature handler
    and the task alike and takes the report of everything already published with it.
    """
    json.dumps(feature)
    return feature


class Recorder:
    """Records what was POSTed, and refuses whatever it was told to refuse."""

    def __init__(self, *, refuse: tuple = (), throttle: int = 0) -> None:
        self.sent: list[dict] = []
        self.extents: list[dict] = []
        self.attempts: list[str] = []
        #: Which collection each accepted feature was created in, in order. A publish now
        #: fans out by geometry type, and a feature sent to the wrong collection is
        #: rejected by the server's own geometry check -- 872 times, in a report that
        #: reads like an outage. So the destination is recorded, not assumed.
        self.collections: list[str] = []
        #: Every track name the worker asked for, one per attempt. A publish that sent a
        #: single feature to a track the run was not pinned to would be invisible in the
        #: counts and permanent in the data, so it is recorded rather than assumed.
        self.tracks: list[str] = []
        self._refuse = refuse
        #: How many times each feature is answered with a 429 before it is accepted. A
        #: negative number means "always", which is how the give-up path is reached.
        self._throttle = throttle
        self._throttled: dict[str, int] = {}

    def create_feature(self, base_url, collection_id, feature, authcfg, feedback=None, track=""):
        self.tracks.append(track)
        if collection_id == "extent":
            self.extents.append(_serialisable(feature))
            return None
        name = feature.get("properties", {}).get("names", {}).get("en", "")
        self.attempts.append(name)
        self.collections.append(collection_id)
        if self._throttle:
            seen = self._throttled.get(name, 0)
            if self._throttle < 0 or seen < self._throttle:
                self._throttled[name] = seen + 1
                raise BackendError(f"HTTP 429 for {name}", status=429, retry_after=0.01)
        if name in self._refuse:
            raise BackendError(f"HTTP 422: {name} was refused")
        self.sent.append(_serialisable(feature))
        return None

    def names(self) -> list[str]:
        return [f["properties"]["names"]["en"] for f in self.sent]


@pytest.fixture
def recorder(monkeypatch) -> Recorder:
    """Intercept the create call and make geometry handling deterministic."""
    rec = Recorder()
    monkeypatch.setattr(client, "create_feature", rec.create_feature)
    # The loop copies the geometry before transforming it, so the copy constructor has to
    # hand back something that still answers the four questions.
    monkeypatch.setattr(publish_tools, "QgsGeometry", lambda geometry: geometry)
    monkeypatch.setattr(publish_tools, "geometry_as_geojson", lambda geometry: geometry.geojson)
    # The backoff is real time, and the test suite is not the place to spend it.
    monkeypatch.setattr(publish_tools.time, "sleep", lambda _seconds: None)
    return rec


def _prepared(
    features,
    *,
    completeness: str = "",
    transform=None,
    extent=None,
    count=None,
    name: str = "Compounds",
    collection_id: str = "",
):
    source = SourceLayer(
        layer_id=name.lower(),
        name=name,
        geometry_type="Polygon",
        crs_authid="EPSG:4326",
        feature_count=len(features) if count is None else count,
        field_names=tuple(SNAPSHOT_LAYERS["Compounds"]),
    )
    plan = LayerPlan(
        source=source,
        choice=LayerChoice(
            layer_id=source.layer_id,
            publish=True,
            class_id="compound",
            extent_completeness=completeness,
        ),
        label_class=COMPOUND,
        mappings=map_fields(source.field_names, COMPOUND),
        collection_id=collection_id,
    )
    return publish_tools.PreparedLayer(
        plan=plan,
        source=FakeSource(features),
        field_names=source.field_names,
        mappings=plan.mappings,
        transform=transform,
        extent_geojson=extent,
        collection_id=collection_id,
    )


def _request(prepared, *more, **kwargs) -> publish_tools.PublishRequest:
    # A track is required, not defaulted: publish() refuses without one, because 1,246
    # identical 403s from the edge is not a report anybody can act on. Every test here
    # supplies it so that the refusal itself stays testable in one place.
    kwargs.setdefault("track", TRACK.name)
    return publish_tools.PublishRequest(
        base_url="https://api.example.org/oapif",
        collection_id=kwargs.pop("collection_id", "label"),
        authcfg="",
        layers=[prepared, *more],
        **kwargs,
    )


def _features(count: int, **kwargs) -> list[FakeFeature]:
    return [FakeFeature({"Name_en": f"Site {n}"}, **kwargs) for n in range(count)]


MULTIPOLYGON_EXTENT = {"type": "MultiPolygon", "coordinates": [SQUARE["coordinates"]]}


class Cancelling:
    """A feedback handle that reports cancellation after `after` progress steps."""

    def __init__(self, after: int) -> None:
        self._after = after
        self._steps = 0

    def isCanceled(self) -> bool:  # noqa: N802 - Qt naming
        cancelled = self._steps >= self._after
        self._steps += 1
        return cancelled

    def setProgress(self, _percent) -> None:  # noqa: N802
        pass


# --- what actually goes on the wire -----------------------------------------


def test_every_feature_is_its_own_request(recorder):
    report = publish_tools.publish(_request(_prepared(_features(120))))

    assert len(recorder.sent) == 120
    assert report.published == 120
    assert report.failed == 0 and report.skipped == 0
    assert report.clean


def test_there_is_no_batch_create_at_all(recorder):
    # The regression this guards is a duplicate in the founding dataset. A batch that
    # fails ambiguously cannot be retried safely -- a save is not atomic, there is no
    # If-Match, and identity is the server's -- so the only safe batch API is none.
    assert not hasattr(client, "create_features")


def test_nothing_sent_carries_a_client_side_identity(recorder):
    publish_tools.publish(_request(_prepared(_features(3))))

    for feature in recorder.sent:
        assert "id" not in feature
        assert set(feature["properties"]) <= {"class_id", "names", "attrs"}
        assert "label_id" not in feature["properties"]


def test_a_refused_feature_names_the_row_it_came_from(recorder):
    # "190 rejected" is a number. The operator's next question -- which 190, and what is
    # left to re-send -- has no answer anywhere unless the refusal carries the row.
    recorder._refuse = ("Site 2",)

    report = publish_tools.publish(_request(_prepared(_features(5))))

    assert report.published == 4
    assert report.failed == 1
    assert recorder.names() == ["Site 0", "Site 1", "Site 3", "Site 4"]
    issue = next(iter(report.outcomes[0].issues.values()))
    assert any("Site 2" in subject for subject in issue.subjects)


def test_a_refused_feature_is_never_offered_a_second_time(recorder):
    # The duplicate-making move: re-sending anything after an ambiguous failure. Every
    # feature is attempted exactly once, refused or not.
    recorder._refuse = ("Site 1", "Site 3")

    publish_tools.publish(_request(_prepared(_features(6))))

    assert recorder.attempts == [f"Site {n}" for n in range(6)]


# --- the auth edge asking us to slow down -----------------------------------


def test_a_throttled_feature_is_waited_out_rather_than_counted_as_refused(recorder):
    # 120 writes a minute per principal. Counting "not yet" as "no" would report most of
    # a bootstrap as refused, stamp the layer published, and leave nobody able to say
    # which rows landed.
    recorder._throttle = 2

    report = publish_tools.publish(_request(_prepared(_features(3))))

    assert report.published == 3
    assert report.failed == 0
    assert len(recorder.attempts) == 9  # two refusals then an acceptance, each


def test_a_throttle_that_never_lifts_is_reported_as_a_failure_not_retried_forever(recorder):
    recorder._throttle = -1

    report = publish_tools.publish(
        _request(_prepared(_features(1)), max_throttle_retries=2),
    )

    assert report.published == 0
    assert report.failed == 1
    assert len(recorder.attempts) == 3  # the first attempt plus two retries


def test_a_wait_ends_early_when_the_run_is_cancelled():
    # Otherwise "cancel" means "cancel in half a minute", and the task sits in a sleep
    # holding the run open.
    assert publish_tools._wait(5.0, Cancelling(after=0)) is False


def test_a_wait_is_capped_however_long_the_server_asks_for(monkeypatch):
    slept: list[float] = []
    monkeypatch.setattr(publish_tools.time, "sleep", lambda seconds: slept.append(seconds))

    assert publish_tools._wait(3600.0, None) is True
    assert sum(slept) == pytest.approx(publish_tools.MAX_BACKOFF_SECONDS)


# --- counting honestly ------------------------------------------------------


def test_a_feature_with_no_geometry_is_skipped_not_failed(recorder):
    features = [*_features(2), FakeFeature({"Name_en": "Empty"}, FakeGeometry(None))]
    report = publish_tools.publish(_request(_prepared(features)))

    outcome = report.outcomes[0]
    assert outcome.published == 2
    assert outcome.skipped_no_geometry == 1
    assert outcome.failed == 0


def test_an_invalid_geometry_is_refused_here_rather_than_by_the_server(recorder):
    features = [*_features(1), FakeFeature({"Name_en": "Bowtie"}, FakeGeometry(valid=False))]
    report = publish_tools.publish(_request(_prepared(features)))

    outcome = report.outcomes[0]
    assert outcome.skipped_invalid_geometry == 1
    assert outcome.published == 1
    assert any("invalid geometry" in message for message in outcome.issues)


def test_a_cancelled_run_charges_nothing_to_the_server(recorder):
    # The distinction the report exists to keep: nobody refused these, so calling them
    # failures would report a deliberate stop as a backend problem.
    # Cancelled while the first feature was in flight: drafted, never sent, refused by
    # nobody.
    prepared = _prepared(_features(10))
    report = publish_tools.publish(_request(prepared), Cancelling(after=2))

    assert report.cancelled
    assert report.failed == 0
    assert report.published == 0
    assert report.outcomes[0].not_sent == 1
    assert recorder.sent == []


def test_a_stopped_run_says_how_much_of_the_layer_it_never_read(recorder):
    prepared = _prepared(_features(10))
    report = publish_tools.publish(_request(prepared), Cancelling(after=6))

    outcome = report.outcomes[0]
    assert outcome.never_reached == 10 - outcome.read
    assert "never read" in outcome.line()


def test_an_unexpected_error_keeps_the_report_of_what_already_landed(recorder, monkeypatch):
    # Row 900 raising a RuntimeError has the same property as row 900 being cancelled:
    # 899 features are already on a server. Losing the report loses the only record of
    # which ones.
    calls = {"n": 0}
    real = recorder.create_feature

    def explode(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] > 3:
            raise RuntimeError("wrapped C/C++ object has been deleted")
        return real(*args, **kwargs)

    monkeypatch.setattr(client, "create_feature", explode)
    report = publish_tools.publish(_request(_prepared(_features(10))))

    assert report.published == 3
    assert "RuntimeError" in report.error
    assert "second copy" in report.summary()
    assert report.clean is False


def test_a_polygon_layer_is_promoted_to_the_multipolygon_its_class_wants(recorder):
    report = publish_tools.publish(_request(_prepared(_features(3))))

    assert report.outcomes[0].promoted == 3
    assert all(f["geometry"]["type"] == "MultiPolygon" for f in recorder.sent)


def test_reprojection_is_recorded_on_the_outcome(recorder):
    prepared = _prepared(_features(2), transform=object())
    report = publish_tools.publish(_request(prepared))

    assert report.outcomes[0].reprojected is True
    assert all(feature.geometry().transformed for feature in prepared.source._features)


# --- the survey extent ------------------------------------------------------


def test_the_declared_completeness_is_the_one_the_user_chose(recorder):
    # Never assumed here. 'exhaustive' is the only value that licenses the export
    # pipeline to sample unlabeled ground inside the polygon as negative.
    prepared = _prepared(
        _features(2), completeness=COMPLETENESS_PARTIAL, extent=MULTIPOLYGON_EXTENT
    )
    report = publish_tools.publish(_request(prepared, extent_collection="extent"))

    assert len(recorder.extents) == 1
    assert recorder.extents[0]["properties"]["completeness"] == COMPLETENESS_PARTIAL
    # The caveat is what tells a human the polygon is a box and names no capture.
    caveat = recorder.extents[0]["properties"]["caveat"]
    assert "bounding box" in caveat and "capture" in caveat
    assert report.classes_without_extent == ()


def test_an_exhaustive_sweep_is_declared_when_every_feature_landed(recorder):
    prepared = _prepared(
        _features(2), completeness=COMPLETENESS_EXHAUSTIVE, extent=MULTIPOLYGON_EXTENT
    )
    report = publish_tools.publish(_request(prepared, extent_collection="extent"))

    assert recorder.extents[0]["properties"]["completeness"] == COMPLETENESS_EXHAUSTIVE
    assert report.outcomes[0].extent_declared is True


def test_a_layer_that_published_nothing_declares_no_extent(recorder):
    # The failure this exists to stop: every feature refused, and an exhaustive survey
    # claim written over the bounding box anyway. classes_without_extent cannot catch it,
    # because that set is built from the classes that DID publish.
    recorder._refuse = tuple(f"Site {n}" for n in range(3))
    prepared = _prepared(
        _features(3), completeness=COMPLETENESS_EXHAUSTIVE, extent=MULTIPOLYGON_EXTENT
    )

    report = publish_tools.publish(_request(prepared, extent_collection="extent"))

    assert recorder.extents == []
    assert report.outcomes[0].extent_declared is False
    assert "nothing from this layer was published" in report.outcomes[0].extent_problem


def test_an_exhaustive_claim_is_refused_when_some_features_did_not_land(recorder):
    # A refused feature is a thing on the ground that is not in the database, so
    # "everything of this class inside the polygon is labeled" is false by inspection.
    recorder._refuse = ("Site 1",)
    prepared = _prepared(
        _features(4), completeness=COMPLETENESS_EXHAUSTIVE, extent=MULTIPOLYGON_EXTENT
    )

    report = publish_tools.publish(_request(prepared, extent_collection="extent"))

    assert recorder.extents == []
    assert "not exhaustive" in report.outcomes[0].extent_problem
    assert report.classes_without_extent == ("compound",)


def test_a_partial_sweep_is_still_declared_when_a_feature_was_refused(recorder):
    # 'partial' claims only that somebody looked, which a refused row does not falsify.
    recorder._refuse = ("Site 1",)
    prepared = _prepared(
        _features(4), completeness=COMPLETENESS_PARTIAL, extent=MULTIPOLYGON_EXTENT
    )

    report = publish_tools.publish(_request(prepared, extent_collection="extent"))

    assert len(recorder.extents) == 1
    assert report.outcomes[0].extent_declared is True


def test_publishing_without_an_extent_names_the_class_in_the_warning(recorder):
    report = publish_tools.publish(_request(_prepared(_features(2))))

    assert report.classes_without_extent == ("compound",)
    assert "UNKNOWN" in report.coverage_warning()


def test_one_layers_extent_does_not_silence_the_warning_for_another(recorder):
    # An extent is a claim about an area. A campus-sized sweep says nothing about a
    # second layer of the same class covering the rest of the country.
    declared = _prepared(
        _features(2), completeness=COMPLETENESS_EXHAUSTIVE, extent=MULTIPOLYGON_EXTENT
    )
    silent = _prepared(_features(2))
    request = _request(declared, extent_collection="extent")
    request.layers.append(silent)

    report = publish_tools.publish(request)

    assert len(recorder.extents) == 1
    assert report.classes_without_extent == ("compound",)


def test_a_cancelled_run_never_claims_a_completed_sweep(recorder):
    # An extent says "everything of this class inside here is labeled". A run that stopped
    # part way through has not earned that claim.
    prepared = _prepared(
        _features(10), completeness=COMPLETENESS_EXHAUSTIVE, extent=MULTIPOLYGON_EXTENT
    )
    report = publish_tools.publish(
        _request(prepared, extent_collection="extent"), Cancelling(after=4)
    )

    assert report.cancelled
    assert recorder.extents == []
    assert report.outcomes[0].extent_declared is False
    assert "cancelled" in report.outcomes[0].extent_problem


# --- the history track ------------------------------------------------------


def test_every_request_names_the_track_including_the_survey_extent(recorder):
    """The extent matters as much as the features and is easier to lose.

    A survey extent is a claim made *inside* a dataset. One landing on the wrong track
    tells the export pipeline it may sample unlabeled ground there as background -- the
    poisoning labeled_extent exists to prevent, one dataset over, and invisible because
    nobody can see an extent on a map.
    """
    prepared = _prepared(
        _features(3), completeness=COMPLETENESS_EXHAUSTIVE, extent=MULTIPOLYGON_EXTENT
    )
    publish_tools.publish(_request(prepared, extent_collection="extent"))
    assert recorder.extents, "the extent was not declared, so this proves nothing"
    assert set(recorder.tracks) == {TRACK.name}


def test_a_run_with_no_track_is_refused_before_anything_is_sent(recorder):
    """1,246 identical 403s is not a report anybody can act on.

    The edge refuses an untracked write, correctly. Discovering that one round trip at a
    time would spend the whole bootstrap to learn something knowable before the first one.
    """
    with pytest.raises(ConfigurationError, match="No history track selected"):
        publish_tools.publish(_request(_prepared(_features(3)), track=""))
    assert recorder.sent == []


def test_the_report_carries_the_track_the_run_actually_used(recorder):
    report = publish_tools.publish(_request(_prepared(_features(2))))
    assert report.track == TRACK.name
    assert f"on track {TRACK.name}" in report.summary()


# --- one run, several collections -------------------------------------------


def test_each_layer_is_sent_to_its_own_collection(recorder):
    """The destination is the layer's, not the run's.

    Labels are stored one collection per geometry type, so a project of compounds and
    cooling units writes to two of them in a single publish. A run-wide destination would
    send one of the two to a collection whose column type refuses it, feature by feature,
    with the report reading as a backend fault.
    """
    report = publish_tools.publish(
        _request(
            _prepared(_features(3), name="Compounds", collection_id="label_polygon"),
            _prepared(_features(2), name="CoolingUnits", collection_id="label_point"),
            collection_id="",
        )
    )
    assert recorder.collections == ["label_polygon"] * 3 + ["label_point"] * 2
    assert report.published == 5


def test_a_layer_with_no_collection_of_its_own_uses_the_run_fallback(recorder):
    # A deployment still serving one untyped collection: no per-layer route, one
    # collection for the run. This is the pre-split behaviour and it has to keep working.
    publish_tools.publish(_request(_prepared(_features(2))))
    assert set(recorder.collections) == {"label"}


def test_a_layer_with_nowhere_to_go_is_refused_before_anything_is_sent(recorder):
    """Named in the refusal, and refused before the first request.

    Reachable without the dialog, which is why the check is here as well as in the plan.
    Defaulting to whichever collection the request happened to carry is the one outcome
    that must not happen: the rows land somewhere permanent, the server assigns their
    identities, and nothing afterwards can find them again to move them.
    """
    with pytest.raises(ConfigurationError, match="Compounds"):
        publish_tools.publish(_request(_prepared(_features(3)), collection_id=""))
    assert recorder.sent == []


def test_the_report_says_which_collection_each_layer_went_to(recorder):
    # After a partial run the question is "what still needs sending, and to where". With
    # several collections in one run, the counts alone no longer answer the second half.
    report = publish_tools.publish(
        _request(
            _prepared(_features(1), collection_id="label_polygon"),
            collection_id="",
        )
    )
    assert "label_polygon" in report.detail_lines()[0]
