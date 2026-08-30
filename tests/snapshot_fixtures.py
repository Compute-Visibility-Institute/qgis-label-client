"""The seven source layers and the seven seeded classes, as they actually are.

Every other fixture in this suite is invented, on purpose: the plugin must not know any
class or attribute name, and inventing them is how that gets verified. These are the one
deliberate exception, and they earn it.

The bootstrap publish exists to translate *this specific* vocabulary -- ``No. Cooler`` and
``No. Coolim`` being one concept under two DBF truncations, ``No. transf`` and
``No. Transf`` differing only in case, ``Name:ch`` and ``Name_en`` being names rather than
attributes -- onto *that specific* registry. A test using invented names would verify that
the matcher matches things, which is not the claim. The claim is that it gets these right.

Sources: ``docs/current-labeling-practice.md`` (the layer inventory and fill rates) and
``db/seed/010_classes.sql`` (the class registry as seeded). Nothing here is imported by the
plugin; the plugin reads the registry from the backend at runtime.
"""

from __future__ import annotations

from qgis_label_client.core.registry import parse_registry
from qgis_label_client.core.tracks import STATUS_ARCHIVED, Track

_STATUS = {
    "type": "string",
    "enum": ["operational", "under_construction", "planned", "decommissioned", "unknown"],
}

#: db/seed/010_classes.sql, reduced to what the mapping logic reads.
SEED_CLASSES = [
    {
        "class_id": "compound",
        "geom_type": "MultiPolygon",
        "label_en": "Compound",
        "label_zh": "园区",
        "sort_order": 10,
        "attr_schema": {
            "type": "object",
            "additionalProperties": True,
            "properties": {
                "operator": {"type": "string"},
                "cooling_unit_count": {"type": "integer", "minimum": 0},
                "transformer_count": {"type": "integer", "minimum": 0},
                "commissioned_year": {"type": "integer", "minimum": 1990, "maximum": 2100},
                "status": _STATUS,
                # The description is part of the seed and is load-bearing: it is the only
                # thing that tells a human the Area column they just mapped is wrong.
                "area_m2": {
                    "type": "number",
                    "minimum": 0,
                    "description": "Computed in a projected CRS. NEVER in EPSG:4326.",
                },
            },
        },
    },
    {
        "class_id": "datacenter_building",
        "geom_type": "MultiPolygon",
        "label_en": "Datacenter building",
        "label_zh": "数据中心建筑",
        "sort_order": 20,
        "attr_schema": {
            "type": "object",
            "additionalProperties": True,
            "properties": {
                "building_designator": {"type": "string"},
                "cooling_unit_count": {"type": "integer", "minimum": 0},
                "floors": {"type": "integer", "minimum": 0},
                "building_use": {
                    "type": "string",
                    "enum": ["compute", "substation", "office", "plant", "warehouse", "unknown"],
                },
                "status": _STATUS,
            },
        },
    },
    {
        "class_id": "substation",
        "geom_type": "MultiPolygon",
        "label_en": "Substation",
        "label_zh": "变电站",
        "sort_order": 30,
        "attr_schema": {
            "type": "object",
            "additionalProperties": True,
            "properties": {
                "transformer_count": {"type": "integer", "minimum": 0},
                "voltage_kv": {"type": "number", "minimum": 0},
                "status": _STATUS,
            },
        },
    },
    {
        "class_id": "backup_generator",
        "geom_type": "MultiPolygon",
        "label_en": "Backup generator",
        "label_zh": "备用发电机",
        "sort_order": 40,
        "attr_schema": {
            "type": "object",
            "additionalProperties": True,
            "properties": {
                "unit_count": {"type": "integer", "minimum": 0},
                "fuel": {"type": "string", "enum": ["diesel", "gas", "dual", "unknown"]},
            },
        },
    },
    {
        "class_id": "cooling_unit",
        "geom_type": "Point",
        "label_en": "Cooling unit",
        "label_zh": "冷却装置",
        "sort_order": 50,
        "attr_schema": {
            "type": "object",
            "additionalProperties": True,
            "properties": {
                "model": {"type": "string"},
                "cooling_type": {
                    "type": "string",
                    "enum": ["dry_cooler", "chiller", "cooling_tower", "crah", "unknown"],
                },
                "confidence": {
                    "type": "string",
                    "enum": ["certain", "probable", "uncertain"],
                },
            },
        },
    },
    {
        "class_id": "powerline",
        "geom_type": "MultiLineString",
        "label_en": "Powerline",
        "label_zh": "输电线路",
        "sort_order": 60,
        "attr_schema": {
            "type": "object",
            "additionalProperties": True,
            "properties": {
                "voltage_kv": {"type": "number", "minimum": 0},
                "circuits": {"type": "integer", "minimum": 1},
            },
        },
    },
    {
        "class_id": "administrative",
        "geom_type": "MultiPolygon",
        "label_en": "Administrative area",
        "label_zh": "行政区",
        "sort_order": 90,
        "attr_schema": {
            "type": "object",
            "additionalProperties": True,
            "properties": {
                "admin_level": {"type": "string"},
                "code": {"type": "string"},
            },
        },
    },
]

REGISTRY = parse_registry({"classes": SEED_CLASSES})

#: A history track for the tests that need one.
#:
#: The NAME is arbitrary and is deliberately unlike any deployment's. Tracks are data,
#: exactly as classes are, and a plausible-looking name here would be the beginning of a
#: second copy of the deployment's vocabulary living in the test suite.
TRACK = Track(
    name="fixture_track",
    track_id="44444444-4444-4444-8444-444444444444",
    label_en="Fixture track",
)

#: A second one, for the tests that are about the boundary between two.
OTHER_TRACK = Track(
    name="fixture_other_track",
    track_id="55555555-5555-4555-8555-555555555555",
    label_en="Other fixture track",
)

#: An archived track: readable forever, writable never.
ARCHIVED_TRACK = Track(
    name="fixture_archived_track",
    track_id="66666666-6666-4666-8666-666666666666",
    label_en="Archived fixture track",
    status=STATUS_ARCHIVED,
)

#: The seven shapefile layers, with the columns each actually carries.
#: docs/current-labeling-practice.md, "Inventory".
SNAPSHOT_LAYERS = {
    "Compounds": ["id", "Name:ch", "Name_en", "No. Cooler", "Year", "Area", "No. transf"],
    "Bld_Datacenters": ["id", "No. Coolim"],
    "Backup_Generators": ["id"],
    "Substation": ["id", "Name", "No. Transf"],
    "Powerlines": ["id"],
    "Administrative": ["id"],
    "CoolingUnits": ["id", "Model"],
}

#: Layer name -> the class it must be guessed as.
EXPECTED_CLASSES = {
    "Compounds": "compound",
    "Bld_Datacenters": "datacenter_building",
    "Backup_Generators": "backup_generator",
    "Substation": "substation",
    "Powerlines": "powerline",
    "Administrative": "administrative",
    "CoolingUnits": "cooling_unit",
}

#: Real ``Name:ch`` values whose final character was destroyed by the UTF-7 truncation,
#: with the English name that proves the damage. Table in finding 4 of the analysis.
DAMAGED_NAMES = (
    ("优刻得乌兰察布智算中fw", "UCloud Ulanqab Smart Computing Center"),
    ("快手智能云乌兰察布数据中X8", "Kuaishou Smart Cloud Ulanqab Data Center"),
    ("乌兰察布华为云数据中fw", "Ulanqab Huawei Cloud Data Center"),
    (
        "内蒙古中联亚信绿色智算中X8",
        "Inner Mongolia Zhonglian AsiaInfo Green Intelligent Computing Center",
    ),
    (
        "世纪互联乌兰察布云智算中心一号基XM",
        "Century Internet Ulanqab Cloud Computing Center Base No. 1",
    ),
)

#: Names that decode correctly, plus the shapes most likely to be mistaken for damage.
INTACT_NAMES = (
    "快手智能云数据中心",
    "阿里巴巴乌兰察布开发区数据中心",
    # A genuine building designator. One trailing letter after CJK is real data.
    "华为数据中心-B",
    "华为数据中心B",
    # Pure ASCII: every character is in the base64 alphabet and none of it is damage.
    "Ulanqab Huawei Cloud Data Center",
    "UCloud",
    "",
)
