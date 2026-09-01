"""Which collection a layer's features are created in, decided from its geometry.

WHY THIS IS DECIDED AT ALL, AND WHY IT IS DECIDED HERE

``label.geom`` was ``geometry(Geometry, 4326)`` -- untyped on purpose, because a cooling
unit is a Point and a powerline is a LineString. OGC API - Features cannot declare a
geometry type on a collection, so QGIS infers one by SAMPLING features; an empty collection
samples as nothing, QGIS decides the layer is non-spatial, and every digitizing tool
disappears behind "Add Record". Measured, not theorised: track "default" holds one feature
and samples as MultiPolygon, track "dev" holds none and samples as nothing.

One collection per geometry type fixes that and moves a decision to the client, because a
QGIS vector layer has exactly one geometry type and therefore exactly one destination.

WHAT EACH TEST BELOW IS PROTECTING

* **the ids are not compiled in.** ``label_polygon``/``label_point``/``label_line`` are what
  the backend is being given today; a deployment naming them otherwise is a deployment, and
  a plugin that only worked against ids hardcoded here would turn a rename into a release.
* **a wrong route is worse than no route.** ``app.label_check()`` compares the geometry type
  feature by feature, so a point layer sent to the polygon collection is refused 872 times
  and the report reads like a backend outage.
* **class stays an attribute.** There are three families and there will be three however
  many classes exist. Nothing here may key on a class id: adding a class must stay one row
  in ``label_class``, with no migration and no plugin release.
"""

from __future__ import annotations

import pytest

from qgis_label_client.core import routing

#: The three the backend is being given. Written here, in the test, rather than in the
#: plugin: this is what the code has to cope with, not what it may assume.
TYPED = ["label_polygon", "label_point", "label_line"]

#: What else a real deployment lists alongside them. Every one of these is a trap for a
#: substring match: "labeled_extent" contains "line", "label_history" contains "istor".
NEIGHBOURS = ["labeled_extent", "label_history", "label_recorded"]


# --- reading a geometry type ------------------------------------------------


@pytest.mark.parametrize(
    ("geometry_type", "family"),
    [
        ("Point", routing.POINT),
        ("MultiPoint", routing.POINT),
        ("LineString", routing.LINE),
        ("MultiLineString", routing.LINE),
        ("Polygon", routing.POLYGON),
        ("MultiPolygon", routing.POLYGON),
    ],
)
def test_single_and_multi_part_geometries_share_a_family(geometry_type, family):
    """The snapshot is 872 Point, 351 MultiPolygon and 23 MultiLineString.

    Shapefiles hand back both spellings of the same drawing depending on the layer, and
    the collection stores the family rather than the spelling -- conform_geometry promotes
    between them. Splitting the two would send half the founding dataset nowhere.
    """
    assert routing.geometry_family(geometry_type) == family


@pytest.mark.parametrize("geometry_type", ["PolygonZ", "PointZM", "MultiPolygonZ"])
def test_a_dimensionality_suffix_does_not_change_where_a_layer_goes(geometry_type):
    # Esri tooling writes Z-enabled shapefiles by default and the QGIS layer looks
    # identical. The Z is dropped before storage (the geom column is two-dimensional);
    # reading PolygonZ as an unroutable type would refuse the layer instead.
    assert routing.geometry_family(geometry_type) == routing.geometry_family(
        geometry_type.rstrip("ZM")
    )


@pytest.mark.parametrize(
    "geometry_type", ["", "Unknown (any)", "GeometryCollection", "NoGeometry", "CircularString"]
)
def test_a_geometry_this_cannot_place_is_placed_nowhere(geometry_type):
    """No family, rather than a default one.

    "Unknown (any)" is what OGR reports for a mixed layer, and a mixed layer has no single
    destination -- picking one would send the part that does not match to a collection that
    refuses it feature by feature. Everything downstream reads the empty string as "refuse
    this layer and say so".
    """
    assert routing.geometry_family(geometry_type) == ""


# --- resolving routes against what the backend lists ------------------------


def test_three_typed_collections_are_recognised_by_their_geometry_word():
    routes = routing.build_routes(TYPED + NEIGHBOURS)
    assert routes.collection_for("MultiPolygon") == "label_polygon"
    assert routes.collection_for("Point") == "label_point"
    assert routes.collection_for("MultiLineString") == "label_line"


def test_the_neighbouring_collections_are_not_mistaken_for_typed_ones():
    """``labeled_extent`` contains the letters of "line" and must not read as one.

    The geometry word is matched as a whole token, not as a substring. A substring match
    would route every powerline into the survey-extent collection, where the rows would be
    accepted -- ``labeled_extent.geom`` is a MultiPolygon column, so most would be refused,
    and any that landed would become supervised-background claims nobody made.
    """
    routes = routing.build_routes(TYPED + NEIGHBOURS)
    assert set(routes.targets()) == set(TYPED)


def test_a_deployment_may_name_its_collections_anything():
    # The ids are the backend's to choose; see the module docstring. Nothing in the plugin
    # may require the ones being deployed today.
    routes = routing.build_routes(["annotation-Points", "annotation-Polygons"])
    assert routes.collection_for("Point") == "annotation-Points"
    assert routes.collection_for("MultiPolygon") == "annotation-Polygons"


def test_a_preference_stored_before_the_split_still_finds_the_split_collections():
    """The stored setting says ``label``; the collections are now ``label_*``.

    Matching by stem rather than by id is what makes the backend change require no
    migration of anyone's settings and no re-prompt on the first publish after it. Getting
    this wrong is not a crash -- it is a dialog asking a question the user already answered,
    at the exact moment they are about to publish 1,246 irreversible features.
    """
    routes = routing.build_routes(TYPED, preferred="label")
    assert routes.stem == "label"
    assert routes.collection_for("Point") == "label_point"


def test_a_preference_naming_one_of_the_typed_collections_selects_its_siblings():
    # Somebody who published once before the routing existed has "label_point" stored.
    # That names the group as well as any other member of it does.
    routes = routing.build_routes(TYPED, preferred="label_point")
    assert routes.collection_for("MultiPolygon") == "label_polygon"


def test_a_backend_still_serving_one_untyped_collection_keeps_working():
    """The pre-split behaviour exactly, and it has to survive.

    The plugin ships independently of the backend: a user on today's release will point it
    at a deployment that has not been migrated yet, and "everything into the one collection
    the setting names" is both correct there and what they already had.
    """
    routes = routing.build_routes(["label", "labeled_extent"], preferred="label")
    assert routes.untyped == "label"
    assert routes.collection_for("Point") == "label"
    assert routes.collection_for("MultiPolygon") == "label"
    # Including a geometry nothing can place: an untyped column accepts it, so refusing it
    # here would take away a capability the deployment actually has.
    assert routes.collection_for("Unknown (any)") == "label"


def test_an_untyped_collection_alongside_the_typed_ones_takes_what_they_cannot():
    # A transitional deployment serving both. The typed collections take what they are
    # typed for; the untyped one is the honest home for a mixed layer, and having it means
    # the transition does not remove a capability.
    routes = routing.build_routes([*TYPED, "label"], preferred="label")
    assert routes.collection_for("Point") == "label_point"
    assert routes.collection_for("GeometryCollection") == "label"


def test_two_unrelated_typed_groups_resolve_nothing_and_say_which():
    """Degrade honestly rather than tie-break.

    Two sets of geometry-typed collections and nothing saying which holds labels is a
    question about the deployment. Answering it with a rule here -- first listed, shortest
    stem, alphabetical -- would put the founding dataset in another dataset's collections,
    permanently, with the server assigning the identities that make it unfindable.
    """
    routes = routing.build_routes([*TYPED, "capture_point", "capture_polygon"])
    assert not routes
    assert routes.ambiguous == ("capture", "label")
    assert routes.collection_for("Point") == ""


def test_a_stored_preference_settles_a_deployment_that_would_otherwise_be_ambiguous():
    routes = routing.build_routes([*TYPED, "capture_point", "capture_polygon"], preferred="label")
    assert routes.collection_for("Point") == "label_point"


def test_a_collection_naming_two_families_is_not_used_for_either():
    # "label_point_polygon" says nothing about which of the two it stores, and choosing
    # between them here would be a coin toss whose result cannot be undone.
    routes = routing.build_routes(["label_point_polygon"])
    assert not routes
    assert routes.collection_for("Point") == ""


def test_the_first_listing_wins_when_two_collections_claim_one_family():
    # Not an expected shape, but silence is the wrong response to it: taking the later one
    # would make the choice invisible in the one flow whose writes cannot be undone.
    routes = routing.build_routes(["label_point", "label_points"])
    assert routes.collection_for("Point") == "label_point"


def test_separators_and_case_do_not_change_the_answer():
    routes = routing.build_routes(["Label-Polygon", "LABEL.point"], preferred="label")
    assert routes.collection_for("Polygon") == "Label-Polygon"
    assert routes.collection_for("Point") == "LABEL.point"


def test_an_empty_collection_list_resolves_nothing():
    # A backend that listed nothing, or a panel that never connected. Falsy, so the caller
    # asks rather than sends.
    assert not routing.build_routes([])


# --- refusing, in a sentence somebody can act on ----------------------------


def test_a_mixed_geometry_layer_is_refused_by_name_and_by_type():
    """Never silently sent to whichever collection happened to be configured.

    The layer name is in the sentence because the preview lists seven layers and a
    complaint that names none of them cannot be acted on. The geometry type is in it
    because that is the thing the analyst has to go and change.
    """
    routes = routing.build_routes(TYPED)
    refusal = routes.refusal("Everything", "Unknown (any)")
    assert "Everything" in refusal
    assert "Unknown (any)" in refusal
    assert "split" in refusal


def test_a_family_the_backend_does_not_offer_is_refused_rather_than_approximated():
    """A powerline layer against a backend with no line collection.

    The nearest collection is not a fallback: app.label_check() compares ST_GeometryType
    against the column, so all 23 features would be refused one at a time and the report
    would read as a server fault rather than as a missing collection.
    """
    routes = routing.build_routes(["label_point", "label_polygon"])
    refusal = routes.refusal("Powerlines", "MultiLineString")
    assert "Powerlines" in refusal and "MultiLineString" in refusal
    assert routes.collection_for("MultiLineString") == ""


def test_a_routable_layer_is_not_refused():
    routes = routing.build_routes(TYPED)
    assert routes.refusal("Compounds", "MultiPolygon") == ""


def test_the_refusal_on_an_ambiguous_deployment_names_the_groups_it_saw():
    # The message has to start an investigation, not end one: what the plugin saw is the
    # only thing that distinguishes "the backend is wrong" from "choose one".
    routes = routing.build_routes([*TYPED, "capture_point", "capture_polygon"])
    refusal = routes.refusal("Compounds", "MultiPolygon")
    assert "capture" in refusal and "label" in refusal


def test_the_refusal_with_nothing_listed_says_what_was_offered():
    refusal = routing.build_routes(["labeled_extent"]).refusal("Compounds", "MultiPolygon")
    assert "labeled_extent" in refusal


# --- what the flow around this reads ----------------------------------------


def test_the_single_collection_helper_is_the_pre_split_behaviour():
    # What the plugin falls back to after asking which collection holds the labels. It has
    # to route everything, including a geometry nothing can place, or the fallback would be
    # narrower than the behaviour it replaces.
    routes = routing.single("whatever_they_called_it")
    assert routes
    assert routes.collection_for("Point") == "whatever_they_called_it"
    assert routes.collection_for("Unknown (any)") == "whatever_they_called_it"


def test_describe_says_where_things_go_in_one_line():
    # Logged before the publish starts, so a support thread has the routing decision in it
    # without anybody having to reproduce the dialog.
    described = routing.build_routes(TYPED).describe()
    for collection_id in TYPED:
        assert collection_id in described


def test_the_stem_of_an_id_drops_only_the_geometry_word():
    assert routing.stem_of("label_point") == "label"
    assert routing.stem_of("label") == "label"
    assert routing.stem_of("labeled_extent") == "labeled_extent"
