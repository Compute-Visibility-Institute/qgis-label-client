"""History tracks: which dataset the annotator is working in.

WHAT A TRACK IS, AND WHY THE PLUGIN HAS TO CARE

A track is an isolated dataset sharing one deployment -- one for kicking the tyres, one
the analysts build for real. Labels drawn on one are invisible from the other, and the
isolation is enforced by the database rather than by anything in this plugin: row-level
security keyed on an ``app.track()`` session variable, which reaches the database as an
``X-Track`` header the auth edge translates.

So the plugin does not *implement* isolation and must never look like it does. What it
implements is three much smaller things, and each of them exists because getting it wrong
is silent:

1. **Saying which track you are on**, everywhere a claim is being made. A publish is
   irreversible -- the server assigns identity, so nothing can recognise the features
   afterwards -- and "which dataset did those 1,246 features land in?" has to be answered
   before the button, not after.
2. **Naming the track in the layer's own data source**, so a layer cannot be pointed at
   the wrong track by a stale setting, and so a project file reopens where it was saved.
3. **A canary.** Under row-level security the ``track_id`` clause :func:`canary_filter`
   builds is redundant. If the isolation ever silently stops applying, it is the
   difference between a layer that goes *empty* and a layer that quietly shows somebody
   else's polygons -- and empty-and-wrong is enormously better than populated-and-wrong,
   because somebody notices it.

NO TRACK NAME IS COMPILED IN, for the same reason no class name is. Tracks are data. The
deployment's own names arrive from ``/v1/tracks`` at runtime, and the only string this
module knows is the ``status`` vocabulary the schema itself defines.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .errors import BackendError
from .expressions import equals
from .fields import DEFAULT_FIELDS, CoreFields

#: Header naming the history track a request is for.
#:
#: Lives here, in the pure core, because three modules need it and none of them is the
#: obvious owner: :mod:`..network` sets it on the plugin's own requests, :mod:`..auth`
#: bakes it into the credential, and :mod:`..layers` puts it in the layer URI. One
#: definition, because a header name that differs between two of the three is a track that
#: silently stops travelling on one of the routes.
#:
#: The client *asks* with this. The auth edge resolves it and re-asserts it internally
#: under a reserved prefix a client cannot forge, so sending it is a request, not a claim.
TRACK_HEADER = "X-Track"

#: ``track.status`` values (db/migrations/009_track.sql). There is deliberately no
#: 'deleted': a track is archived, never dropped, because its label_history rows are the
#: proof of what happened and cannot be removed.
STATUS_ACTIVE = "active"
STATUS_ARCHIVED = "archived"


def _is_list(value: Any) -> bool:
    """True for a JSON array. A string is a Sequence, which is the trap."""
    return isinstance(value, Sequence) and not isinstance(value, (str, bytes))


@dataclass(frozen=True)
class Track:
    """One entry from ``/v1/tracks``.

    Every field except ``name`` is optional, because every field of the response is
    optional as far as this plugin is concerned: a backend that has not shipped a column
    yet must not make the panel unusable.
    """

    name: str
    #: The uuid ``label.track_id`` carries. Empty when the backend did not send one, in
    #: which case the canary in :func:`canary_filter` cannot be built -- see
    #: :meth:`can_be_verified`.
    track_id: str = ""
    label_en: str = ""
    label_zh: str | None = None
    description: str | None = None
    status: str = STATUS_ACTIVE
    is_default: bool = False
    sort_order: int = 100
    #: The earliest transaction-time instant this track has a record of, as the backend
    #: renders it. The floor for the historical-view picker (:mod:`.recorded`), and the
    #: fact the empty-layer message needs: "nothing was believed at that instant" is a
    #: sentence, "and the record only starts here" is an explanation. Empty when the
    #: backend has not shipped the field, in which case the picker simply has no floor --
    #: an instant before the data existed is a valid question with the answer "nothing".
    earliest_recorded: str = ""

    @property
    def display_name(self) -> str:
        return self.label_en or self.name

    @property
    def archived(self) -> bool:
        return self.status == STATUS_ARCHIVED

    @property
    def writable(self) -> bool:
        """False when the server will refuse every write to this track.

        Archived means readable forever and writable never: reads resolve through
        ``app.track_id()`` and every write verb through ``app.writable_track_id()``, which
        is the same query plus ``AND status = 'active'``. That single difference is the
        whole of what archiving does.
        """
        return not self.archived

    @property
    def can_be_verified(self) -> bool:
        """True when a layer loaded on this track can be checked against it.

        Both the canary filter and the loaded-layer check need the uuid, because
        ``label.track_id`` is a uuid and the name is not in the feature at all.
        """
        return bool(self.track_id)

    def describe(self) -> str:
        """One line for a combo box entry or a status label."""
        bits = [self.display_name]
        if self.display_name != self.name:
            bits.append(f"({self.name})")
        if self.archived:
            bits.append("- ARCHIVED, read-only")
        elif self.is_default:
            bits.append("- deployment default")
        return " ".join(bits)

    def warning(self) -> str:
        """What has to be said out loud about this track before anyone edits, or ``""``."""
        if self.archived:
            return (
                f"Track {self.name!r} is ARCHIVED. It is readable and every write to it "
                "is refused by the database, so an edit made here cannot be saved. "
                "Switch to an active track before drawing anything."
            )
        return ""


def parse_tracks(document: Any) -> list[Track]:
    """Parse a ``/v1/tracks`` response, sorted the way the deployment asked.

    Accepts either ``{"tracks": [...]}`` or a bare array. Not laxity for its own sake: the
    two endpoints this plugin already reads disagree on the wrapper, and a client that
    hard-fails on the shape turns a cosmetic backend change into "the plugin is broken".
    A response that is neither is an error, because the alternative is a silently empty
    track list, which reads as "this deployment has no tracks" and is much worse.
    """
    if isinstance(document, Mapping):
        raw_tracks = document.get("tracks")
    elif _is_list(document):
        raw_tracks = document
    else:
        raw_tracks = None

    if not _is_list(raw_tracks):
        raise BackendError(
            "Response is not a track list (no 'tracks' array). Check the tracks path "
            "setting -- it must point at the backend's own v1/ namespace, not at a "
            "collection, or the request reaches the feature service instead."
        )

    parsed: list[Track] = []
    for raw in raw_tracks:
        if not isinstance(raw, Mapping):
            continue
        name = raw.get("name") or raw.get("track")
        if not isinstance(name, str) or not name:
            continue
        status = raw.get("status")
        parsed.append(
            Track(
                name=name,
                track_id=str(raw.get("track_id") or raw.get("id") or ""),
                label_en=str(raw.get("label_en") or ""),
                label_zh=raw.get("label_zh") if isinstance(raw.get("label_zh"), str) else None,
                description=(
                    raw.get("description") if isinstance(raw.get("description"), str) else None
                ),
                status=status if isinstance(status, str) and status else STATUS_ACTIVE,
                is_default=bool(raw.get("is_default") or raw.get("default")),
                sort_order=_int(raw.get("sort_order"), 100),
                earliest_recorded=str(raw.get("earliest_recorded") or ""),
            )
        )
    parsed.sort(key=lambda t: (t.sort_order, t.display_name.lower(), t.name))
    return parsed


def _int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def find(tracks: Sequence[Track], name: str) -> Track | None:
    """The track with this name, or ``None``. Exact match: names are snake_case ids."""
    for track in tracks:
        if track.name == name:
            return track
    return None


def default_track(tracks: Sequence[Track]) -> Track | None:
    """The track a session with no opinion lands on, if the deployment declares one."""
    for track in tracks:
        if track.is_default and not track.archived:
            return track
    return None


def resolve(tracks: Sequence[Track], name: str) -> Track | None:
    """The track to work in, given a stored setting.

    An empty setting means "whatever the deployment defaults to", which is what a fresh
    profile has and what the API does with a read that names no track.

    A setting naming a track that is **not** in the list resolves to ``None``, never to
    the default. Silently answering a request for one track from another is the
    contamination failure in reverse: you would conclude the track you asked for was
    empty. The caller says so instead.
    """
    if not name:
        return default_track(tracks)
    return find(tracks, name)


def canary_filter(track: Track | None, fields: CoreFields = DEFAULT_FIELDS) -> str | None:
    """A QGIS expression pinning a layer to one track, or ``None``.

    Redundant under row-level security, and that is the entire point: it is a check on the
    mechanism rather than a second copy of it. If the ``X-Track`` header fails to reach
    the database -- a stale credential, a proxy that strips it, a read that escaped its
    transaction -- ``app.track()`` falls back to the deployment's *default* track, which
    is a silent wrong answer. With this clause the layer comes back empty instead.

    Only ever applied to layers that actually expose the column; see
    :func:`qgis_label_client.layers.track_filter_for`. A collection that is shared between
    tracks by design -- the class registry, imagery captures -- has no ``track_id``, and
    filtering one on a column it does not have would make the layer invalid.
    """
    if track is None or not track.can_be_verified:
        return None
    return equals(fields.track_id, track.track_id)


def mismatch(expected: Track | None, found: object) -> str:
    """Why a loaded feature's track disagrees with the requested one, or ``""``.

    The cheap half of the canary, run once against the first feature a layer returns. The
    filter above makes a failure show up as an empty layer; this makes it show up as a
    sentence, which is the difference between "the backend is down" and "you are looking
    at the wrong dataset".

    An absent value is not a mismatch: several collections have no ``track_id`` because
    they are shared between tracks on purpose.
    """
    if expected is None or not expected.can_be_verified:
        return ""
    value = "" if found is None else str(found).strip()
    if not value or value.lower() in ("null", "none"):
        return ""
    if value == expected.track_id:
        return ""
    return (
        f"This layer returned features from track {value}, but you asked for "
        f"{expected.name!r} ({expected.track_id}). Do NOT edit it: the isolation that is "
        "supposed to make this impossible is not in force, and an edit would land in a "
        "dataset you are not looking at. Reconnect, and report it."
    )
