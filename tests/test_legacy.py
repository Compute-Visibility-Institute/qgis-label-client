"""Translating the shapefile vocabulary onto the registry's.

The interesting assertions here are the ones using the real names from the analysis: the
same concept under two DBF truncations, the same concept in two cases, and name columns
that must not be filed as attributes. The plugin contains none of those names -- they
arrive from the registry -- so this is where the mapping is actually pinned down.

The other half is ambiguity. A wrong guess in a one-way bootstrap is expensive to unpick,
so every tie must produce *no* answer rather than an arbitrary one.
"""

from __future__ import annotations

from dataclasses import replace

import pytest
from snapshot_fixtures import EXPECTED_CLASSES, REGISTRY, SEED_CLASSES, SNAPSHOT_LAYERS

from qgis_label_client.core.legacy import (
    FieldRole,
    build_attrs,
    coerce,
    concept_of,
    guess_class,
    is_blank,
    map_field,
    map_fields,
    name_columns,
    schema_problem,
    tokenise,
)
from qgis_label_client.core.registry import parse_registry

COMPOUND = REGISTRY.get("compound")
BUILDING = REGISTRY.get("datacenter_building")
SUBSTATION = REGISTRY.get("substation")
COOLING_UNIT = REGISTRY.get("cooling_unit")


# --- reducing a name to a concept ------------------------------------------


@pytest.mark.parametrize(
    "name,tokens",
    [
        ("No. Cooler", ("no", "cooler")),
        ("No. Coolim", ("no", "coolim")),
        ("Bld_Datacenters", ("bld", "datacenters")),
        ("CoolingUnits", ("cooling", "units")),
        ("Name:ch", ("name", "ch")),
        ("cooling_unit_count", ("cooling", "unit", "count")),
        ("", ()),
    ],
)
def test_every_naming_convention_in_the_source_splits_the_same_way(name, tokens):
    # Punctuation, underscores, spaces and camel case, all from the same person.
    assert tokenise(name) == tokens


def test_the_two_truncations_reduce_to_the_same_concept():
    # "No. Cooler" (Compounds) and "No. Coolim" (Bld_Datacenters) are one concept under
    # two DBF ten-character truncations. That is the whole reason for the stem.
    assert concept_of("No. Cooler").stems == concept_of("No. Coolim").stems
    assert concept_of("No. Cooler").counts and concept_of("No. Coolim").counts


def test_case_is_not_a_distinction():
    assert concept_of("No. transf") == concept_of("No. Transf")


def test_a_quantity_marker_is_recorded_separately_from_the_content():
    counted = concept_of("No. Cooler")
    described = concept_of("Cooler")
    assert counted.stems == described.stems
    assert counted.counts and not described.counts


def test_a_language_marker_is_stripped_so_the_two_name_columns_match():
    assert concept_of("Name:ch").stems == concept_of("Name_en").stems == {"name"}
    assert concept_of("Name:ch").language == "zh"
    assert concept_of("Name_en").language == "en"
    assert concept_of("Name").language is None


# --- guessing the class ----------------------------------------------------


@pytest.mark.parametrize("layer_name,class_id", sorted(EXPECTED_CLASSES.items()))
def test_all_seven_source_layers_guess_their_class(layer_name, class_id):
    guess = guess_class(layer_name, REGISTRY)
    assert guess.class_id == class_id, guess.describe()
    assert guess.confident


def test_the_more_specific_class_wins_when_both_match_one_word():
    # "Datacenter_Substations" overlaps datacenter_building on one word out of two and
    # substation on its only word. The one with nothing left over is the closer fit.
    assert guess_class("Datacenter_Substations", REGISTRY).class_id == "substation"


def test_a_genuine_tie_produces_no_guess_at_all():
    # Both classes match one word each with nothing left over. Breaking that by registry
    # order would make the guess depend on something the user cannot see.
    guess = guess_class("Compound_Substation", REGISTRY)
    assert guess.class_id is None
    assert guess.tied_with == ("compound", "substation")
    assert "Ambiguous" in guess.describe()


def test_a_layer_resembling_nothing_gets_no_guess():
    guess = guess_class("Roads", REGISTRY)
    assert guess.class_id is None and guess.tied_with == ()
    assert "Choose one" in guess.describe()


def test_a_chinese_layer_name_matches_the_registry_chinese_label():
    # A Chinese layer name reduces to no ASCII stems at all, so the class's own label_zh
    # is matched as a substring rather than losing the signal entirely.
    assert guess_class("园区_2026", REGISTRY).class_id == "compound"


def test_retired_classes_are_never_guessed():
    # The database refuses new labels on a retired class, so guessing one would produce a
    # mapping that cannot be published.
    registry = parse_registry(
        {"classes": [dict(entry, active=entry["class_id"] != "compound") for entry in SEED_CLASSES]}
    )
    assert guess_class("Compounds", registry).class_id is None


def test_guessing_ignores_case_and_plurals():
    assert guess_class("compounds", REGISTRY).class_id == "compound"
    assert guess_class("COOLING_UNITS", REGISTRY).class_id == "cooling_unit"


# --- mapping the columns ---------------------------------------------------


@pytest.mark.parametrize(
    "column,label_class,target",
    [
        # The same concept, two truncations, two layers, two classes. One attribute.
        ("No. Cooler", "compound", "cooling_unit_count"),
        ("No. Coolim", "datacenter_building", "cooling_unit_count"),
        # The same concept, differing only in case.
        ("No. transf", "compound", "transformer_count"),
        ("No. Transf", "substation", "transformer_count"),
        # The two that are not counts.
        ("Year", "compound", "commissioned_year"),
        ("Area", "compound", "area_m2"),
        ("Model", "cooling_unit", "model"),
    ],
)
def test_the_legacy_columns_land_on_the_declared_attribute(column, label_class, target):
    mapping = map_field(column, REGISTRY.get(label_class))
    assert mapping.role is FieldRole.ATTRIBUTE
    assert mapping.target == target


def test_a_column_already_using_the_canonical_spelling_is_matched_exactly():
    mapping = map_field("cooling_unit_count", COMPOUND)
    assert mapping.role is FieldRole.ATTRIBUTE and mapping.target == "cooling_unit_count"


def test_a_description_does_not_land_on_a_count():
    # "Cooler" carries no quantity marker, so it is not the same fact as "No. Cooler".
    # Without that check an abbreviation would silently become a denormalised count.
    assert map_field("Cooler", COMPOUND).role is FieldRole.SOURCE_ATTRIBUTE


def test_an_abbreviation_may_say_less_than_the_canonical_name_but_never_more():
    assert map_field("No. Cooling Units", COMPOUND).target == "cooling_unit_count"
    # Extra content the class never declared means this is a different fact.
    assert map_field("No. Cooler Roof", COMPOUND).role is FieldRole.SOURCE_ATTRIBUTE


def test_an_ambiguous_column_is_reported_rather_than_guessed():
    registry = parse_registry(
        {
            "classes": [
                {
                    "class_id": "invented",
                    "geom_type": "Point",
                    "label_en": "Invented",
                    "attr_schema": {
                        "type": "object",
                        "properties": {
                            "cooling_unit_count": {"type": "integer"},
                            "cooling_tower_count": {"type": "integer"},
                        },
                    },
                }
            ]
        }
    )
    mapping = map_field("No. Cooler", registry.get("invented"))
    assert mapping.role is FieldRole.SOURCE_ATTRIBUTE
    assert mapping.tied_with == ("cooling_tower_count", "cooling_unit_count")
    assert "ambiguous" in mapping.describe()


@pytest.mark.parametrize(
    "column,language",
    [("Name:ch", "zh"), ("Name_en", "en"), ("Name", "auto")],
)
def test_name_columns_become_names_not_attributes(column, language):
    mapping = map_field(column, COMPOUND)
    assert mapping.role is FieldRole.NAME
    assert mapping.target == language


def test_an_unmarked_name_column_says_that_its_language_is_a_guess():
    # Substation.Name declares no language, so the key is decided per value from the
    # content -- and a pinyin transliteration in it will be filed under the English key.
    # The preview is where that is catchable.
    line = map_field("Name", SUBSTATION).describe()
    assert "does not say which language" in line


def test_a_column_naming_an_attribute_is_not_mistaken_for_the_feature_name():
    # "<something>_name" names an attribute of the feature, not the feature.
    assert map_field("operator_name", COMPOUND).role is not FieldRole.NAME


def test_the_registry_wins_when_it_declares_a_name_attribute():
    registry = parse_registry(
        {
            "classes": [
                {
                    "class_id": "invented",
                    "geom_type": "Point",
                    "label_en": "Invented",
                    "attr_schema": {"type": "object", "properties": {"name": {"type": "string"}}},
                }
            ]
        }
    )
    mapping = map_field("Name", registry.get("invented"))
    assert mapping.role is FieldRole.ATTRIBUTE and mapping.target == "name"


def test_source_id_is_an_attribute_not_server_identity():
    assert map_field("id", COMPOUND).role is FieldRole.SOURCE_ATTRIBUTE


def test_name_columns_are_found_without_a_class():
    # The damaged-name scan runs in the preview, before a class has been chosen.
    assert name_columns(SNAPSHOT_LAYERS["Compounds"]) == ("Name:ch", "Name_en")
    assert name_columns(SNAPSHOT_LAYERS["Substation"]) == ("Name",)
    assert name_columns(SNAPSHOT_LAYERS["CoolingUnits"]) == ()


def test_the_whole_compounds_layer_maps_as_expected():
    mappings = {m.source: m for m in map_fields(SNAPSHOT_LAYERS["Compounds"], COMPOUND)}
    assert mappings["Name:ch"].role is FieldRole.NAME
    assert mappings["No. Cooler"].target == "cooling_unit_count"
    assert mappings["No. transf"].target == "transformer_count"
    assert mappings["Year"].target == "commissioned_year"
    assert mappings["Area"].target == "area_m2"
    assert mappings["id"].role is FieldRole.SOURCE_ATTRIBUTE


# --- values ----------------------------------------------------------------


@pytest.mark.parametrize("value", [None, "", "   ", [], {}])
def test_blank_values_record_nothing(value):
    assert is_blank(value)


@pytest.mark.parametrize("value", [0, 0.0, False, "0", "x"])
def test_zero_and_false_are_facts_not_blanks(value):
    # A recorded count of zero is a fact. Treating it as absent would lose it.
    assert not is_blank(value)


@pytest.mark.parametrize("raw,expected", [("3", 3), (3.0, 3), (3, 3)])
def test_integers_are_coerced_from_whatever_the_dbf_held(raw, expected):
    value, problem = coerce(raw, COMPOUND.attribute("cooling_unit_count"))
    assert problem is None and value == expected


def test_a_fractional_value_for_an_integer_attribute_is_refused():
    value, problem = coerce(3.5, COMPOUND.attribute("cooling_unit_count"))
    assert value is None and "not a whole number" in problem


def test_an_unparseable_value_is_refused_with_its_own_text():
    value, problem = coerce("many", COMPOUND.attribute("cooling_unit_count"))
    assert value is None and "cannot be read as integer" in problem


def test_an_undeclared_attribute_passes_through_untouched():
    # Every seeded class sets additionalProperties true: capture first, formalise later.
    value, problem = coerce("whatever", COMPOUND.attribute("invented_attribute"))
    assert problem is None and value == "whatever"


class _Unserialisable:
    """Stands in for a QDate or a QByteArray: what a DBF Date or Binary column hands back."""

    def __repr__(self) -> str:
        return "QDate(2020, 1, 1)"


def test_a_value_with_no_json_form_is_reported_rather_than_passed_on():
    # It would reach json.dumps and raise TypeError -- not a BackendError, so it would
    # escape the per-feature handler AND the task, taking down a run that had already
    # published 900 features and losing the report saying which.
    value, problem = coerce(_Unserialisable(), COMPOUND.attribute("attribute_with_no_type"))
    assert value is None
    assert "no JSON form" in problem


def test_a_non_finite_number_is_refused_before_it_poisons_a_batch():
    # json.dumps writes a bare NaN, which is not valid JSON, so a strict server refuses
    # the whole FeatureCollection it travelled in rather than the one feature.
    value, problem = coerce(float("nan"), COMPOUND.attribute("area_m2"))
    assert value is None and "finite" in problem
    assert coerce(float("inf"), COMPOUND.attribute("area_m2"))[0] is None


def test_a_value_with_no_json_form_never_reaches_attrs():
    # A declared attribute stating no `type` is legal JSON Schema, and is what "adding an
    # attribute is an UPDATE to label_class" produces in a hurry.
    label_class = parse_registry(
        {
            "classes": [
                {
                    "class_id": "widget",
                    "geom_type": "Polygon",
                    "label_en": "Widget",
                    "attr_schema": {
                        "type": "object",
                        "properties": {"surveyed": {}, "tally": {"type": "integer"}},
                    },
                }
            ]
        }
    ).get("widget")
    values = {"Surveyed": _Unserialisable(), "Tally": 6}
    result = build_attrs(values, map_fields(list(values), label_class), label_class)

    # Refuse before drafting if the original-value archive would lose a source value.
    assert result.attrs == {}
    assert any("JSON" in issue and "Surveyed" in issue for issue in result.issues)
    assert result.blocking_issues


#: A class declaring the three types a DBF column arrives in the wrong shape for. Built
#: here rather than taken from the seed because the point is the *type*, and the registry
#: is free to add an attribute of any of them with an UPDATE and no plugin release.
TYPED = parse_registry(
    {
        "classes": [
            {
                "class_id": "widget",
                "geom_type": "Polygon",
                "label_en": "Widget",
                "attr_schema": {
                    "type": "object",
                    "properties": {
                        "flag": {"type": "boolean"},
                        "reference": {"type": "string"},
                        "short_note": {"type": "string", "maxLength": 4},
                        "serial": {"type": "integer"},
                    },
                },
            }
        ]
    }
).get("widget")


@pytest.mark.parametrize("raw", ["false", "F", "f", "0", "no", "N", "off", 0, False])
def test_the_spellings_a_shapefile_uses_for_false_are_read_as_false(raw):
    # Python truthiness reads every one of these as True, the server's validator sees a
    # valid boolean, and the founding dataset records the opposite of the truth.
    value, problem = coerce(raw, TYPED.attribute("flag"))
    assert problem is None and value is False


@pytest.mark.parametrize("raw", ["true", "T", "Y", "1", "on", 1, True])
def test_the_spellings_a_shapefile_uses_for_true_are_read_as_true(raw):
    value, problem = coerce(raw, TYPED.attribute("flag"))
    assert problem is None and value is True


def test_a_value_that_is_not_recognisably_a_boolean_is_refused_rather_than_guessed():
    value, problem = coerce("maybe", TYPED.attribute("flag"))
    assert value is None and "guess" in problem


def test_a_dbf_numeric_column_becomes_the_string_the_code_actually_is():
    # An official administrative code stored as a double arrives as 150000.0, and
    # "150000.0" joins against nothing.
    value, problem = coerce(150000.0, TYPED.attribute("reference"))
    assert problem is None and value == "150000"


def test_an_object_with_no_text_form_never_becomes_its_python_repr():
    # str() is a valid JSON string for every Python object, which is what makes this
    # branch dangerous: the server's validator accepts it and nothing reports it.
    value, problem = coerce(_Unserialisable(), TYPED.attribute("reference"))
    assert value is None and "repr" in problem


def test_a_boolean_offered_to_a_string_attribute_is_refused():
    value, problem = coerce(True, TYPED.attribute("reference"))
    assert value is None and "boolean" in problem


def test_a_long_registration_number_survives_exactly():
    # int(float(x)) is exact only below 2^53, and jsonb stores arbitrary-precision
    # numerics, so a rounded value would differ from the source with nothing objecting.
    number = 913100001000012345
    assert coerce(number, TYPED.attribute("serial")) == (number, None)
    assert coerce(str(number), TYPED.attribute("serial")) == (number, None)


def test_the_length_keyword_the_server_enforces_is_checked_here_too():
    # Otherwise the rejection arrives as a bare HTML 500 naming neither the attribute nor
    # the rule, because the feature service does not catch the trigger's exception.
    assert schema_problem("ok", TYPED.attribute("short_note")) is None
    problem = schema_problem("far too long", TYPED.attribute("short_note"))
    assert problem is not None and "maximum of 4" in problem


def test_padding_is_trimmed_before_a_string_attribute_is_stored():
    # A field padded with NUL rather than with spaces cannot be stored in jsonb at all.
    value, problem = coerce("ULQB-01\x00\x00", TYPED.attribute("reference"))
    assert problem is None and value == "ULQB-01"
    assert is_blank("\x00 \x00")


def test_the_schema_keywords_the_server_enforces_are_checked_first():
    assert schema_problem(1899, COMPOUND.attribute("commissioned_year")) is not None
    assert schema_problem(2020, COMPOUND.attribute("commissioned_year")) is None
    assert schema_problem("nonsense", COMPOUND.attribute("status")) is not None
    assert schema_problem("operational", COMPOUND.attribute("status")) is None


def test_attributes_preserve_empty_source_columns_without_claiming_canonical_values():
    mappings = map_fields(SNAPSHOT_LAYERS["Compounds"], COMPOUND)
    values = {
        "id": None,
        "Name:ch": "云汇数据中心",
        "Name_en": "",
        "No. Cooler": None,
        "Year": "   ",
        "Area": None,
        "No. transf": 4,
    }
    result = build_attrs(values, mappings, COMPOUND)
    assert result.attrs == {
        "source_attributes": values,
        "transformer_count": 4, "id": None, "No. Cooler": None, "Year": "   ", "Area": None,
    }


def test_a_recorded_zero_survives():
    mappings = map_fields(["No. Cooler"], COMPOUND)
    assert build_attrs({"No. Cooler": 0}, mappings, COMPOUND).attrs == {
        "cooling_unit_count": 0, "source_attributes": {"No. Cooler": 0},
    }


def test_an_unmapped_column_keeps_its_original_key_and_value():
    mappings = map_fields(["Mystery"], COMPOUND)
    result = build_attrs({"Mystery": "something"}, mappings, COMPOUND)
    assert result.attrs == {"Mystery": "something", "source_attributes": {"Mystery": "something"}}
    assert result.issues == ()


def test_an_unmapped_column_that_is_empty_produces_no_noise():
    mappings = map_fields(["Mystery"], COMPOUND)
    assert build_attrs({"Mystery": None}, mappings, COMPOUND).issues == ()


def test_a_value_the_canonical_schema_would_reject_is_reported_and_preserved():
    mappings = map_fields(["Year"], COMPOUND)
    result = build_attrs({"Year": 1200}, mappings, COMPOUND)
    assert result.attrs == {"Year": 1200, "source_attributes": {"Year": 1200}}
    assert "minimum" in result.issues[0]


def test_updated_campus_columns_preserve_extra_attributes_nulls_zero_and_units():
    values = {
        "id": None, "Name_Ch": "示例园区", "Name_En": "Example Campus",
        "Company": "Example operator", "Location": "Example city",
        "No. Cooler": None, "Year": None, "Area_sqm": 12345.67, "No. transf": 0,
    }
    result = build_attrs(values, map_fields(values, COMPOUND), COMPOUND)
    assert result.attrs == {
        "source_attributes": values,
        "id": None, "Company": "Example operator", "Location": "Example city",
        "No. Cooler": None, "Year": None, "Area_sqm": 12345.67, "transformer_count": 0,
    }
    assert result.issues == ()
    assert result.blocking_issues == ()


@pytest.mark.parametrize("reverse", [False, True])
def test_colliding_alias_keeps_both_values_regardless_of_source_field_order(reverse):
    values = {"No. Cooler": 3, "cooling_unit_count": 9}
    sources = list(reversed(values)) if reverse else list(values)
    result = build_attrs(values, map_fields(sources, COMPOUND), COMPOUND)
    assert result.attrs == {"cooling_unit_count": 9, "No. Cooler": 3, "source_attributes": values}
    assert any("conflicts" in issue for issue in result.issues)
    assert result.blocking_issues == ()


def test_conflicting_source_key_stays_in_the_archive_without_overwriting_canonical_data():
    values = {"No. Cooler": 3, "cooling_unit_count": "unreadable"}
    result = build_attrs(values, map_fields(values, COMPOUND), COMPOUND)
    assert result.attrs == {"cooling_unit_count": 3, "source_attributes": values}
    assert any("source key conflicts" in issue for issue in result.issues)
    assert result.blocking_issues == ()


def test_closed_schema_blocks_unmapped_source_values_including_null():
    closed = replace(COMPOUND, attr_schema={**COMPOUND.attr_schema, "additionalProperties": False})
    values = {"Company": "Example operator", "id": None}
    result = build_attrs(values, map_fields(values, closed), closed)
    assert result.blocking_issues
    assert "source_attributes" in result.blocking_issues[0]
    assert result.attrs == {}


def test_unserializable_source_value_blocks_instead_of_escaping_json_encoding():
    values = {"survey_date": object()}
    result = build_attrs(values, map_fields(values, COMPOUND), COMPOUND)
    assert result.blocking_issues


def test_a_source_column_named_source_attributes_is_nested_without_replacing_the_archive():
    values = {"source_attributes": "Original table text", "Company": "  Example  "}
    result = build_attrs(values, map_fields(values, COMPOUND), COMPOUND)
    assert result.attrs["source_attributes"] == values
    assert result.attrs["Company"] == "  Example  "
    assert result.blocking_issues == ()


def test_a_closed_schema_can_explicitly_allow_the_original_value_archive():
    closed = replace(COMPOUND, attr_schema={
        "type": "object", "additionalProperties": False,
        "properties": {"source_attributes": {"type": "object"}},
    })
    values = {"Company": "Example", "id": None}
    result = build_attrs(values, map_fields(values, closed), closed)
    assert result.attrs == {"source_attributes": values}
    assert result.blocking_issues == ()


def test_the_original_value_archive_never_overwrites_a_declared_nonobject_attribute():
    incompatible = replace(COMPOUND, attr_schema={
        **COMPOUND.attr_schema,
        "properties": {"source_attributes": {"type": "string"}},
    })
    values = {"Company": "Example"}
    result = build_attrs(values, map_fields(values, incompatible), incompatible)
    assert result.attrs == {}
    assert result.blocking_issues


def test_a_canonical_alias_cannot_replace_the_original_value_archive():
    declared = replace(COMPOUND, attr_schema={
        **COMPOUND.attr_schema,
        "properties": {"source_attributes": {"type": "object"}},
    })
    values = {"Source Attributes": {"original": "value"}}
    result = build_attrs(values, map_fields(values, declared), declared)
    assert result.attrs["source_attributes"] == values
    assert result.blocking_issues


def test_name_columns_reach_only_the_original_values_archive_inside_attrs():
    mappings = map_fields(SNAPSHOT_LAYERS["Substation"], SUBSTATION)
    result = build_attrs({"Name": "变电站", "No. Transf": 2}, mappings, SUBSTATION)
    assert result.attrs == {
        "transformer_count": 2, "source_attributes": {"Name": "变电站", "No. Transf": 2},
    }


def test_the_cooling_unit_model_column_maps_even_though_it_is_always_empty():
    # Model is 0% populated today. The mapping still has to exist, or the first value
    # anyone enters would be reported as unmapped.
    assert map_field("Model", COOLING_UNIT).target == "model"
    assert BUILDING is not None
