#!/usr/bin/env python3
"""Native QGIS OAPIF acceptance using loopback fixtures and real API documents.

Run with QGIS's Python, for example:
  /Applications/QGIS.app/Contents/MacOS/python scripts/native-class-layers.py

The sibling labeling-platform checkout supplies the API contract through its
existing api/.venv. No cloud credentials, deployed services or real labels are
used. This proves provider compatibility, not database authorization or storage;
the API/DB suites own those checks.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

REPO = Path(__file__).resolve().parents[1]
API_SOURCE = REPO.parent / "labeling-platform" / "api"
CHINESE = "中国联通中卫云数据中心"
GEOMETRIES = {
    "point": [
        {"type": "Point", "coordinates": [105.31, 37.63]},
        {"type": "MultiPoint", "coordinates": [[105.32, 37.64], [105.33, 37.65]]},
    ],
    "line": [
        {"type": "LineString", "coordinates": [[105.31, 37.63], [105.32, 37.64]]},
        {
            "type": "MultiLineString",
            "coordinates": [
                [[105.31, 37.63], [105.32, 37.64]],
                [[105.33, 37.65], [105.34, 37.66]],
            ],
        },
    ],
    "polygon": [
        {
            "type": "Polygon",
            "coordinates": [
                [[105.31, 37.63], [105.32, 37.63], [105.32, 37.64], [105.31, 37.63]],
            ],
        },
        {
            "type": "MultiPolygon",
            "coordinates": [
                [
                    [[105.31, 37.63], [105.32, 37.63], [105.32, 37.64], [105.31, 37.63]],
                ],
                [
                    [[105.33, 37.65], [105.34, 37.65], [105.34, 37.66], [105.33, 37.65]],
                ],
            ],
        },
    ],
}


def export_contract(api_source: Path) -> None:
    """Use actual production builders; keep fixture data outside application code."""
    sys.path.insert(0, str(api_source))
    from edge import class_layers

    definitions = [
        {"name": key, "title": key, "type": kind, "read_only": key in class_layers.READ_ONLY}
        for key, kind in class_layers.CORE.items()
    ]
    for origin, key, kind in [
        ("src", "Name_Ch", "string"),
        ("src", "Company", "string"),
        ("src", "Year", "integer"),
        ("attr", "Area_sqm", "number"),
        ("attr", "confirmed", "boolean"),
    ]:
        definitions.append(
            {
                "name": class_layers.field_name(origin, key),
                "title": key,
                "source_name": key,
                "origin": origin,
                "type": kind,
                "read_only": False,
            }
        )
    documents = {}
    for family, geometries in GEOMETRIES.items():
        row = {
            "class_id": "native_" + family,
            "label_en": "Native " + family,
            "geom_type": "Any",
            "attr_schema": {},
        }
        spec = class_layers.descriptor(row, family)
        features = []
        for index, geometry in enumerate(geometries):
            feature_row = {
                "id": index + 1,
                "class_id": row["class_id"],
                "label_id": f"00000000-0000-4000-8000-{index + 1:012d}",
                "track_id": "00000000-0000-4000-8000-000000000099",
                "updated_at": "2026-09-22T00:00:00Z",
                "geometry": geometry,
                "names": {"en": "Native feature", "zh": CHINESE},
                "attrs": {
                    "Area_sqm": 388749.13,
                    "confirmed": False,
                    "source_attributes": {
                        "Name_Ch": CHINESE,
                        "Company": "China Unicom",
                        "Year": None,
                    },
                },
            }
            features.append(class_layers.feature(feature_row, definitions))
        documents[spec["id"]] = {
            "spec": spec,
            "fields": definitions,
            "schema": class_layers.schema_document(spec, definitions),
            "features": features,
        }
    print(
        json.dumps(
            {"collections": documents, "conformance": class_layers.conformance_document()},
            ensure_ascii=False,
        )
    )


class Fixture:
    def __init__(self, contract: dict):
        self.contract = contract
        self.records = {
            key: {feature["id"]: copy.deepcopy(feature) for feature in value["features"]}
            for key, value in contract["collections"].items()
        }
        self.requests = []
        self.root = ""
        self.created = 100
        self.history = copy.deepcopy(self.records)
        self.version = 1

    def description(self, collection: str) -> dict:
        spec = self.contract["collections"][collection]["spec"]
        url = self.root + "/collections/" + collection
        return {
            "id": collection,
            "title": spec["title"],
            "itemType": "feature",
            "links": [
                {"rel": "self", "href": url, "type": "application/json"},
                {"rel": "items", "href": url + "/items", "type": "application/geo+json"},
                {
                    "rel": "http://www.opengis.net/def/rel/ogc/1.0/schema",
                    "href": url + "/schema",
                    "type": "application/schema+json",
                },
            ],
        }

    def handle(self, method: str, path: str, body: dict | None) -> tuple[int, dict, dict]:
        parts = unquote(urlsplit(path).path).strip("/").split("/")
        self.requests.append({"method": method, "path": path, "body": body})
        if parts == ["class-layers"]:
            return (
                200,
                {
                    "links": [
                        {
                            "rel": "data",
                            "href": self.root + "/collections",
                            "type": "application/json",
                        },
                        {
                            "rel": "conformance",
                            "href": self.root + "/conformance",
                            "type": "application/json",
                        },
                        {
                            "rel": "service-desc",
                            "href": self.root + "/api",
                            "type": "application/vnd.oai.openapi+json;version=3.0",
                        },
                    ]
                },
                {},
            )
        if parts == ["class-layers", "conformance"]:
            return 200, self.contract["conformance"], {}
        if parts == ["class-layers", "api"]:
            return (
                200,
                {
                    "openapi": "3.0.3",
                    "info": {"title": "Native fixture", "version": "1"},
                    "paths": {},
                },
                {},
            )
        if parts == ["class-layers", "collections"]:
            return 200, {"collections": [self.description(key) for key in self.records]}, {}
        if len(parts) < 3 or parts[2] not in self.records:
            return 404, {"detail": "Unknown fixture path"}, {}
        collection = parts[2]
        records = self.records[collection]
        if len(parts) == 3:
            return 200, self.description(collection), {}
        if parts[3] in {"schema", "queryables"}:
            return (
                200,
                self.contract["collections"][collection]["schema"],
                {"Content-Type": "application/schema+json"},
            )
        if parts[3] != "items":
            return 404, {"detail": "Unknown fixture path"}, {}
        if method == "OPTIONS":
            return (
                204,
                {},
                {"Allow": "GET, OPTIONS, POST" if len(parts) == 4 else "GET, OPTIONS, PUT, DELETE"},
            )
        if len(parts) == 4:
            if method == "GET":
                features = list(records.values())
                return (
                    200,
                    {
                        "type": "FeatureCollection",
                        "features": features,
                        "numberMatched": len(features),
                        "numberReturned": len(features),
                        "links": [],
                    },
                    {},
                )
            if method == "POST":
                self.created += 1
                feature = copy.deepcopy(body)
                feature["id"] = f"{self.created}.{'a' * 24}"
                feature["properties"]["revision"] = "a" * 24
                records[feature["id"]] = feature
                return (
                    201,
                    feature,
                    {
                        "Location": self.root
                        + "/collections/"
                        + collection
                        + "/items/"
                        + feature["id"]
                    },
                )
        identifier = parts[4]
        baseline = self.history[collection].get(identifier) or records.get(identifier)
        serial = identifier.split(".")[0]
        current_key = next((key for key in records if key.split(".")[0] == serial), None)
        if baseline is None or current_key is None:
            return 409, {"detail": "Feature revision changed; reload before editing"}, {}
        if method == "GET":
            return 200, baseline, {}
        if method == "DELETE":
            if current_key != identifier:
                return 409, {"detail": "Feature revision changed; reload before deleting"}, {}
            del records[identifier]
            return 204, {}, {}
        if method == "PUT":
            current = records[current_key]
            feature = copy.deepcopy(current)
            # Storage is deliberately a fixture. API/DB tests separately prove
            # the real archived-baseline merge and authorization semantics.
            for key, value in body["properties"].items():
                if key in {"id", "label_id", "track_id", "class_id", "revision", "updated_at"}:
                    continue
                previous = baseline["properties"].get(key)
                actual = current["properties"].get(key)
                if value != previous:
                    if actual != previous and actual != value:
                        return 409, {"detail": "Concurrent field change"}, {}
                    feature["properties"][key] = value
            if body.get("geometry") != baseline.get("geometry"):
                if current.get("geometry") not in (baseline.get("geometry"), body.get("geometry")):
                    return 409, {"detail": "Concurrent geometry change"}, {}
                feature["geometry"] = body["geometry"]
            self.version += 1
            revision = f"{self.version:024x}"
            feature["id"] = f"{serial}.{revision}"
            feature["properties"]["revision"] = revision
            self.history[collection][current_key] = copy.deepcopy(current)
            del records[current_key]
            records[feature["id"]] = feature
            return (
                200,
                feature,
                {"Location": self.root + "/collections/" + collection + "/items/" + feature["id"]},
            )
        return 405, {"detail": "Unsupported fixture operation"}, {}


def run_native(contract: dict) -> dict:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from qgis.core import (
        NULL,
        Qgis,
        QgsApplication,
        QgsFeature,
        QgsGeometry,
        QgsVectorLayer,
        QgsWkbTypes,
    )
    from qgis.PyQt.QtCore import QVariant

    sys.path.insert(0, str(REPO))
    from qgis_label_client.core.uri import build_oapif_uri
    from qgis_label_client.layers import configure_class_columns, refresh_class_layer_after_commit

    fixture = Fixture(contract)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, _format, *_args):
            pass

        def serve(self):
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length)) if length else None
            status, payload, headers = fixture.handle(self.command, self.path, body)
            encoded = b"" if status == 204 else json.dumps(payload, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", headers.pop("Content-Type", "application/geo+json"))
            self.send_header("Content-Length", str(len(encoded)))
            self.send_header("Cache-Control", "no-store")
            for key, value in headers.items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(encoded)

        do_GET = serve
        do_OPTIONS = serve
        do_POST = serve
        do_PUT = serve
        do_DELETE = serve

    checks = []
    with tempfile.TemporaryDirectory(prefix="cvi-native-class-layers-") as profile:
        os.environ["QGIS_CUSTOM_CONFIG_PATH"] = profile
        app = QgsApplication([], False, profile)
        app.initQgis()
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        fixture.root = f"http://127.0.0.1:{server.server_port}/class-layers"
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        layers = []
        try:
            for collection, doc in contract["collections"].items():
                uri = build_oapif_uri(
                    landing_url=fixture.root + "?track=dev",
                    collection_id=collection,
                    restrict_to_request_bbox=False,
                )
                layer = QgsVectorLayer(uri, collection, "OAPIF")
                layers.append(layer)
                assert layer.isValid(), (collection, layer.error().summary(), fixture.requests)
                configure_class_columns(layer, {**doc["spec"], "fields": doc["fields"]})
                features = list(layer.getFeatures())
                assert len(features) == 2, (collection, len(features))
                assert QgsWkbTypes.isMultiType(layer.wkbType()), (collection, layer.wkbType())
                assert all(feature.geometry().isMultipart() for feature in features)
                names = {
                    field["source_name"]: field["name"]
                    for field in doc["fields"]
                    if "source_name" in field
                }
                for name, expected in (
                    ("Year", QVariant.LongLong),
                    ("Area_sqm", QVariant.Double),
                    ("confirmed", QVariant.Bool),
                ):
                    assert layer.fields().field(names[name]).type() == expected, name
                for feature in features:
                    assert feature[names["Name_Ch"]] == CHINESE
                    assert feature[names["Year"]] == NULL
                    assert feature[names["Area_sqm"]] == 388749.13
                    assert feature[names["confirmed"]] is False
                assert not layer.editFormConfig().readOnly(layer.fields().indexOf(names["Company"]))
                for key in ("class_id", "revision", "label_id", "track_id"):
                    assert layer.editFormConfig().readOnly(layer.fields().indexOf(key)), key
                checks.append(
                    collection
                    + ": multipart geometry, typed scalar values, Unicode/nulls and protected identity"
                )

                # A single Save invokes separate native attribute and geometry
                # PUT requests, both carrying the same cached opaque URL ID.
                provider = layer.dataProvider()
                first = features[0]
                old_id = doc["features"][0]["id"]
                company_index = layer.fields().indexOf(names["Company"])
                assert layer.startEditing()
                assert layer.changeAttributeValue(first.id(), company_index, "中国联通 updated")
                moved = QgsGeometry(first.geometry())
                moved.translate(0.001, 0.001)
                assert layer.changeGeometry(first.id(), moved)
                before = len(fixture.requests)
                assert layer.commitChanges(), layer.commitErrors()
                updates = [
                    request for request in fixture.requests[before:] if request["method"] == "PUT"
                ]
                assert len(updates) == 2, updates
                assert all(
                    urlsplit(update["path"]).path.endswith("/" + old_id) for update in updates
                )
                assert all(
                    update["body"]["properties"][names["Year"]] is None for update in updates
                )
                assert any(
                    update["body"]["properties"][names["Company"]] == "中国联通 updated"
                    for update in updates
                )
                checks.append(
                    collection
                    + ": combined native attribute/geometry commit uses baseline revision IDs"
                )

                # This is the same refresh helper the plugin schedules after a
                # fully acknowledged commit; reload is part of the implementation.
                assert refresh_class_layer_after_commit(layer)
                changed = next(
                    feature
                    for feature in layer.getFeatures()
                    if feature[names["Company"]] == "中国联通 updated"
                )
                assert layer.startEditing()
                assert layer.changeAttributeValue(changed.id(), company_index, "Second update")
                assert layer.commitChanges(), layer.commitErrors()
                assert refresh_class_layer_after_commit(layer)
                changed = next(
                    feature
                    for feature in layer.getFeatures()
                    if feature[names["Company"]] == "Second update"
                )
                assert layer.startEditing()
                assert layer.deleteFeature(changed.id())
                assert layer.commitChanges(), layer.commitErrors()
                checks.append(
                    collection + ": repeated edit/delete succeeds after plugin commit refresh"
                )

                addition = QgsFeature(layer.fields())
                addition.setGeometry(
                    QgsGeometry.fromWkt(
                        {
                            "point": "MultiPoint ((105.35 37.65))",
                            "line": "MultiLineString ((105.35 37.65, 105.36 37.66))",
                            "polygon": "MultiPolygon (((105.35 37.65, 105.36 37.65, 105.36 37.66, 105.35 37.65)))",
                        }[doc["spec"]["family"]]
                    )
                )
                addition.setAttribute("class_id", doc["spec"]["class_id"])
                addition.setAttribute(names["Company"], "新建公司")
                ok, created = provider.addFeatures([addition])
                assert ok and len(created) == 1, provider.errors()
                post = next(
                    request for request in reversed(fixture.requests) if request["method"] == "POST"
                )
                assert post["body"]["properties"][names["Company"]] == "新建公司"
                checks.append(collection + ": native POST sends Unicode scalar attributes")

                fixture.records[collection].clear()
                empty = QgsVectorLayer(uri, collection + " empty", "OAPIF")
                layers.append(empty)
                assert empty.isValid(), empty.error().summary()
                assert list(empty.getFeatures()) == []
                assert QgsWkbTypes.isMultiType(empty.wkbType())
                for name in (names["Year"], names["Area_sqm"], names["confirmed"]):
                    assert empty.fields().field(name).type() == layer.fields().field(name).type(), (
                        name
                    )
                assert (
                    empty.dataProvider().capabilities() & Qgis.VectorProviderCapability.AddFeatures
                )
                checks.append(
                    collection
                    + ": empty schema retains scalar/geometry types and create capability"
                )

            return {
                "qgis": Qgis.QGIS_VERSION,
                "checks": checks,
                "requests": fixture.requests,
                "scope": "Real QGIS provider with actual API schema/feature builders and local fixture storage only",
            }
        finally:
            layers.clear()
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
            # QgsVectorLayer/provider locals are destroyed when this function
            # returns. Tearing down the provider registry before that segfaults.


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-source", type=Path, default=API_SOURCE)
    parser.add_argument("--api-python", type=Path)
    parser.add_argument("--export-contract", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.export_contract:
        export_contract(args.api_source)
        return
    api_python = args.api_python or args.api_source / ".venv" / "bin" / "python"
    api_env = dict(os.environ)
    # The QGIS launcher pins its bundled interpreter. The API's existing venv
    # needs its own Python standard library and compiled extension paths.
    for key in ("PYTHONHOME", "PYTHONPATH", "DYLD_LIBRARY_PATH", "DYLD_FRAMEWORK_PATH"):
        api_env.pop(key, None)
    exported = subprocess.run(
        [
            str(api_python),
            str(Path(__file__).resolve()),
            "--export-contract",
            "--api-source",
            str(args.api_source),
        ],
        check=False,
        text=True,
        capture_output=True,
        env=api_env,
    )
    if exported.returncode:
        raise RuntimeError("API contract export failed: " + exported.stderr)
    report = run_native(json.loads(exported.stdout))
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(rendered + "\n", encoding="utf-8")
        print(
            json.dumps(
                {
                    "qgis": report["qgis"],
                    "checks": report["checks"],
                    "output": str(args.output),
                    "scope": report["scope"],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        print(rendered)


if __name__ == "__main__":
    main()
