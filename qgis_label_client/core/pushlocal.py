"""Content comparisons and a durable journal for incremental local feature uploads."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from pathlib import Path


def _ring(points):
    points = tuple(tuple(round(float(v), 9) for v in p[:2]) for p in points)
    if points and points[0] == points[-1]:
        points = points[:-1]
    if not points:
        return ()
    # Ring starts and winding are representational differences, not new polygons.
    variants = []
    for sequence in (points, tuple(reversed(points))):
        smallest = min(sequence)
        variants.extend(
            sequence[i:] + sequence[:i] for i, point in enumerate(sequence) if point == smallest
        )
    return min(variants)


def geometry_key(geometry):
    kind, coordinates = geometry.get("type"), geometry.get("coordinates")

    def point(position):
        return tuple(round(float(value), 9) for value in position[:2])

    if kind in {"Point", "MultiPoint"}:
        points = [coordinates] if kind == "Point" else coordinates
        return "points", tuple(sorted(point(p) for p in points))
    if kind in {"LineString", "MultiLineString"}:
        lines = [coordinates] if kind == "LineString" else coordinates
        # Preserve line direction; reversing a directed line may change its meaning.
        return "lines", tuple(sorted(tuple(point(p) for p in line) for line in lines))
    if kind == "Polygon":
        coordinates = [coordinates]
    elif kind != "MultiPolygon":
        raise ValueError("Push all local accepts point, line and polygon geometries only.")
    polygons = []
    for polygon in coordinates:
        if not polygon:
            raise ValueError("An empty polygon cannot be compared safely.")
        polygons.append((_ring(polygon[0]), tuple(sorted(_ring(ring) for ring in polygon[1:]))))
    return "polygons", tuple(sorted(polygons))


def _object(value):
    if isinstance(value, str):
        value = json.loads(value)
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError("Server names and attributes must be JSON objects.")
    return value


def _numbers(value):
    if isinstance(value, dict):
        return {key: _numbers(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_numbers(item) for item in value]
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


def fingerprint(feature, fields):
    properties = feature.get("properties") or {}
    document = {
        "geometry": geometry_key(feature.get("geometry") or {}),
        "class": properties.get(fields.class_id),
        "names": _object(properties.get(fields.names)),
        "attrs": _object(properties.get(fields.attrs)),
    }
    encoded = json.dumps(_numbers(document), sort_keys=True, ensure_ascii=False, allow_nan=False)
    return hashlib.sha256(encoded.encode()).hexdigest()


def spatial_fingerprint(feature, fields):
    document = [
        (feature.get("properties") or {}).get(fields.class_id),
        geometry_key(feature.get("geometry") or {}),
    ]
    return hashlib.sha256(json.dumps(_numbers(document), sort_keys=True).encode()).hexdigest()


class UploadLedger:
    """Journal before sending; an interrupted request is uncertain until reconciled.

    The database stores no tokens or geometry/attribute payloads. The scope includes
    account, backend and track, so one destination's result cannot authorize another.
    A confirmed journal row is retained even if the feature disappears on the server;
    reconnect must not silently recreate deliberately deleted server data.
    """

    def __init__(self, path, backend, email, track):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(path, timeout=5)
        os.chmod(path, 0o600)
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS uploads ("
            "scope TEXT, collection TEXT, fingerprint TEXT, state TEXT, reason TEXT, "
            "PRIMARY KEY(scope, collection, fingerprint))"
        )
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS sources (scope TEXT, collection TEXT, source TEXT, "
            "fingerprint TEXT, PRIMARY KEY(scope, collection, source))"
        )
        self.scope = json.dumps([backend.rstrip("/"), email.casefold(), track])
        self.db.commit()

    def state(self, collection, key):
        row = self.db.execute(
            "SELECT state FROM uploads WHERE scope=? AND collection=? AND fingerprint=?",
            (self.scope, collection, key),
        ).fetchone()
        return row[0] if row else ""

    def source_fingerprint(self, collection, source):
        row = self.db.execute(
            "SELECT fingerprint FROM sources WHERE scope=? AND collection=? AND source=?",
            (self.scope, collection, source),
        ).fetchone()
        return row[0] if row else ""

    def reserve(self, collection, keys, reason, sources=()):
        # A competing QGIS instance must not publish a fingerprint we just reserved.
        with self.db:
            self.db.executemany(
                "INSERT INTO uploads VALUES (?, ?, ?, 'uncertain', ?)",
                [(self.scope, collection, key, reason) for key in keys],
            )
            self.db.executemany(
                "INSERT INTO sources VALUES (?, ?, ?, ?)",
                [(self.scope, collection, source, key) for source, key in sources],
            )

    def finish(self, collection, keys, state):
        with self.db:
            for key in keys:
                if state == "confirmed":
                    self.db.execute(
                        "UPDATE uploads SET state='confirmed' "
                        "WHERE scope=? AND collection=? AND fingerprint=?",
                        (self.scope, collection, key),
                    )
                elif state == "not-created":
                    self.db.execute(
                        "DELETE FROM uploads WHERE scope=? AND collection=? AND fingerprint=?",
                        (self.scope, collection, key),
                    )
                    self.db.execute(
                        "DELETE FROM sources WHERE scope=? AND collection=? AND fingerprint=?",
                        (self.scope, collection, key),
                    )

    def observe(self, collection, key, source=""):
        with self.db:
            self.db.execute(
                "INSERT INTO uploads VALUES (?, ?, ?, 'confirmed', 'observed on server') "
                "ON CONFLICT(scope, collection, fingerprint) DO UPDATE SET state='confirmed'",
                (self.scope, collection, key),
            )
            if source:
                self.db.execute(
                    "INSERT INTO sources VALUES (?, ?, ?, ?) "
                    "ON CONFLICT(scope, collection, source) DO UPDATE SET fingerprint=excluded.fingerprint",
                    (self.scope, collection, source, key),
                )

    def close(self):
        self.db.close()
