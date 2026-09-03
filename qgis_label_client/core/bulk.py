"""The backend's atomic bulk create: whether to use it, and how to read its answers.

WHY A BATCH IS ACCEPTABLE AGAIN, HAVING BEEN REMOVED ONCE

The client-side batch this replaces POSTed a ``FeatureCollection`` to the ordinary items
endpoint and was removed for three reasons, all of them still true of that endpoint: a
save is not atomic, so the first refusal aborts the rest *after* the earlier rows are
committed; there is no ``ETag`` and no ``If-Match`` and identity is server-assigned, so
nothing here can ask "did that land?"; and the feature service's create handler takes a
single ``Feature``. Together they meant a partly-applied batch got re-sent whole and the
founding dataset gained duplicate rows nothing could tell apart.

A **server-side endpoint that inserts every feature in ONE database transaction** answers
all three. All-or-nothing removes partial application. The response states how many rows
were created and refuses with ``created: 0``, which is the answer to "did that land?"
stated as data rather than inferred. And it does not go through the single-Feature create
handler at all.

That is the only reason batching is permissible here, which is why :func:`parse_capabilities`
**declines a backend that does not declare** ``atomic``. A bulk endpoint without the
transaction is the design that made duplicates, and being offered one is not a reason to
use it.

WHAT REMAINS AMBIGUOUS, AND WHAT THE CLIENT DOES ABOUT IT

Atomicity removes partial application. It does not remove an ambiguous *response*: a
request that times out, or whose socket is aborted, may or may not have committed. Nothing
in the response can help, because there is no response.

So every chunk carries a ``reason`` unique to it -- :func:`chunk_reason` -- which the edge
binds as ``app.reason`` and the audit trigger writes onto the history row of every feature
in the batch. After an ambiguous failure the question "did that chunk land?" therefore has
an answer that costs one read: ask the history collection for that exact reason string.
Uniqueness is this side's job, which is why the run id is minted here and the chunk
ordinal is counted across the whole run rather than per layer.

The plugin does not run that read itself, and that is deliberate -- but NOT for the reason
first written here. That reason was that labels are stored one collection per geometry
family so history is too, and a lookup aimed at the wrong one answers "no rows" cleanly.
The hazard is real (asking ``label_history_polygon`` about a batch of points does answer
zero) and it is also avoidable: the deployment advertises a MIXED ``label_history``
alongside the three typed ones, and it answers correctly whatever family the chunk held.
Aiming the read there removes that failure mode entirely, so it cannot be what decides
this.

What decides it is a race the collection choice does not touch. The read is wanted exactly
when a request timed out or its socket was aborted -- and a request that timed out on this
side may still be committing on the other. A history lookup a moment later can therefore
answer "no rows" about a transaction that is about to commit, and the repair for a chunk
believed lost is a re-publish, which is the duplicate this whole design exists to prevent.
Waiting long enough to be sure is not something this client can bound.

So the read is left to a human, who can run it twice, minutes apart, and tell a slow commit
from an absent one. Reporting the ambiguity and handing over the exact reason string is the
honest move: a wrong automatic answer here is permanent, and a sentence in the report is
not.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .urls import BULK_PATH, COLLECTION_PLACEHOLDER

#: What one chunk request turned out to be. Every value maps to exactly one way of
#: counting the chunk's features, and there is deliberately no value meaning "some of
#: them": the endpoint is one transaction, so a chunk lands whole or not at all, and a
#: report that could say "412 of 500 landed" would bring back every argument that removed
#: the client-side batch.
CREATED = "created"
#: Nothing was created, and the server named ONE feature as the reason. ``at`` is its
#: index in the chunk.
REFUSED = "refused"
#: Nothing was created, and no single feature is responsible -- a request the endpoint
#: refused whole, or a credential it would not accept.
NOT_CREATED = "not-created"
#: The response did not establish what happened. May have landed; may not.
UNKNOWN = "unknown"
#: Never put on the wire, because the run was already stopping.
NOT_SENT = "not-sent"

#: Statuses that mean the request never reached the endpoint's transaction, whatever the
#: body does or does not say. An intermediary answering one of these has not touched the
#: database either, so "nothing was created" is safe to state rather than merely likely.
#:
#: Deliberately three, and deliberately not a general "4xx means nothing landed" rule: the
#: endpoint states ``created`` on every refusal it issues itself, so anything that arrives
#: without that field and is not one of these is a response this client cannot read, and
#: an unreadable response is :data:`UNKNOWN` rather than assumed harmless.
NEVER_ARRIVED_STATUSES = frozenset({401, 403, 404})

#: Smallest ``max_body_bytes`` worth believing. A document advertising less than this is
#: not describing a working endpoint, and chunking against it would produce single-feature
#: requests through the batch path -- slower than the fallback and stranger to debug.
MIN_BODY_BYTES = 4096


def new_run_id() -> str:
    """An identifier for one publish run, unique across runs and machines.

    Half of the per-chunk ``reason``. It has to be unique because the recovery read is an
    exact-match query: two runs sharing an id would make "did chunk 3 land?" ambiguous
    again, which is the one question the reason exists to answer.
    """
    return uuid.uuid4().hex


def chunk_reason(run_id: str, index: int) -> str:
    """The ``X-Edit-Reason`` one chunk travels under.

    Lands in ``label_history.reason`` on every row of the batch, so an ambiguous failure
    is resolved by one read for this exact string rather than by re-sending anything.
    """
    return f"bootstrap {run_id} chunk {index}"


def feature_collection(features: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """The request body one chunk is sent as."""
    return {"type": "FeatureCollection", "features": list(features)}


def encoded_size(value: Any) -> int:
    """Bytes `value` occupies in a request body.

    Measured with the same options :func:`~qgis_label_client.network.post_json` encodes
    with, because a size measured one way and a body encoded another is a byte budget
    that does not bind -- and the endpoint answers an oversized body with a 413 naming
    neither the layer nor the fix.
    """
    return len(json.dumps(value, ensure_ascii=False).encode("utf-8"))


#: Bytes a chunk costs before any feature is in it. Derived rather than written down, so
#: it cannot drift from the body :func:`feature_collection` actually produces.
ENVELOPE_BYTES = encoded_size(feature_collection([]))


@dataclass(frozen=True)
class BulkCapability:
    """What ``/v1/capabilities`` says this deployment's bulk endpoint will accept.

    Every number is the server's. Chunking against a locally chosen limit is how a
    founding import discovers a 413 on its first request, and the whole reason the
    capability document is a document rather than a boolean.
    """

    #: Path template, relative to the backend base URL, with ``{collectionId}`` where the
    #: collection goes.
    path: str
    #: Collections the endpoint will write to. Not every writable collection is on it --
    #: a survey extent is one row per layer per publish, so bulk buys it nothing.
    collections: tuple[str, ...]
    max_features: int
    max_body_bytes: int

    def serves(self, collection_id: str) -> bool:
        """True when this collection can be written through the bulk endpoint."""
        return bool(collection_id) and collection_id in self.collections

    def chunk_features(self, preferred: int = 0) -> int:
        """How many features one chunk carries, given what the plugin would like.

        The server's cap is a ceiling, never a target: a smaller chunk moves the progress
        bar more often and costs less when one row is refused, because a refusal is
        all-or-nothing and takes its whole chunk with it.
        """
        if preferred <= 0:
            return self.max_features
        return max(1, min(preferred, self.max_features))


def parse_capabilities(document: Any) -> BulkCapability | None:
    """Read ``/v1/capabilities``, or ``None`` when bulk must not be used.

    Never raises, and every uncertainty resolves to ``None``. A backend that predates the
    endpoint answers 404; one that answers something this cannot read is in the same
    position as far as the publish is concerned, and the fallback -- one feature per
    request -- is correct, only slower. Failing a founding import over a capability probe
    would make the optimisation less reliable than not having it.

    ``atomic`` is required rather than merely read. See the module docstring: the
    transaction is the entire argument for batching at all, and a bulk endpoint without
    one is the design that put duplicate rows in the founding dataset.
    """
    if not isinstance(document, Mapping):
        return None
    block = document.get("bulk_create")
    if not isinstance(block, Mapping):
        return None
    if block.get("atomic") is not True:
        return None

    max_features = _positive_int(block.get("max_features"))
    max_body_bytes = _positive_int(block.get("max_body_bytes"))
    if max_features is None or max_body_bytes is None or max_body_bytes < MIN_BODY_BYTES:
        return None

    raw_collections = block.get("collections")
    if not isinstance(raw_collections, Sequence) or isinstance(raw_collections, (str, bytes)):
        return None
    collections = tuple(name for name in raw_collections if isinstance(name, str) and name)
    if not collections:
        # An endpoint that will not say which collections it serves cannot be aimed. A
        # guess would spend a round trip per layer to be told 404, and "the endpoint
        # exists but not for you" is not a distinction worth discovering per layer.
        return None

    # The advertised path is honoured, exactly as the tracks and class-registry paths are
    # settings rather than constants: a deployment may mount its own namespace where it
    # likes. A template this client cannot substitute into is not usable, so the one it
    # was built against stands in.
    path = block.get("path")
    if not isinstance(path, str) or COLLECTION_PLACEHOLDER not in path:
        path = BULK_PATH

    return BulkCapability(
        path=path,
        collections=collections,
        max_features=max_features,
        max_body_bytes=max_body_bytes,
    )


def _positive_int(value: Any) -> int | None:
    """`value` as an int above zero, or ``None``. A bool is not an int here."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if value > 0 else None


@dataclass(frozen=True)
class ChunkVerdict:
    """What one chunk request did, in the terms the report has to count in."""

    state: str
    #: Rows the server says it created. Non-zero only when :attr:`state` is
    #: :data:`CREATED`, because the endpoint is one transaction.
    created: int = 0
    #: Index within the chunk of the one feature the server blamed, when it blamed one.
    #: Zero-based, and taken from the response rather than parsed out of its prose: a
    #: client re-chunking computes ``absolute = chunk_start + at`` and must not have to
    #: read English to do it.
    at: int | None = None
    #: The server's own sentence, or this client's account of why there is not one. Passed
    #: through verbatim wherever there is one: the database writes these for a person to
    #: read, so the wording IS the user interface.
    detail: str = ""


def read_success(payload: Any, expected: int) -> ChunkVerdict:
    """What a 2xx from the bulk endpoint actually established.

    A non-raising POST is not a publish. The response names what was created and that is
    what gets counted -- the previous batch path credited ``len(features)`` on faith, with
    nothing verifying the server had created that many, and that is half of why a partial
    run could not be reasoned about afterwards.
    """
    if not isinstance(payload, Mapping):
        return ChunkVerdict(
            state=UNKNOWN,
            detail=(
                "the server accepted the batch but its response did not say what was "
                "created, so these features cannot be counted as published"
            ),
        )

    created = payload.get("created")
    if isinstance(created, bool) or not isinstance(created, int):
        return ChunkVerdict(
            state=UNKNOWN,
            detail=(
                "the server accepted the batch but named no count of created features, "
                "so these cannot be counted as published"
            ),
        )

    listed = payload.get("features")
    listable = isinstance(listed, Sequence) and not isinstance(listed, (str, bytes))
    if listable and len(listed) != created:
        return ChunkVerdict(
            state=UNKNOWN,
            detail=(
                f"the server said it created {created} feature(s) and then listed "
                f"{len(listed)}, so what landed cannot be established from the response"
            ),
        )

    if created != expected:
        # The endpoint is one transaction, so this cannot happen against a backend that
        # honours its own contract -- which is exactly why it is reported rather than
        # reconciled. Crediting min(created, expected) here would invent an accounting
        # for a state the design says does not exist.
        return ChunkVerdict(
            state=UNKNOWN,
            detail=(
                f"{expected} feature(s) were sent in one atomic batch and the server said "
                f"it created {created}, which that endpoint cannot do; what landed is "
                "unknown"
            ),
        )
    return ChunkVerdict(state=CREATED, created=created)


def read_failure(
    status: int | None,
    payload: Any,
    message: str,
    expected: int,
) -> ChunkVerdict:
    """What a failed chunk request established, which is usually more than it looks.

    ``created`` is the most important field the endpoint emits and it is stated on every
    refusal, so "nothing landed" is read rather than assumed. Where it is absent this
    falls back to :data:`NEVER_ARRIVED_STATUSES` and then to :data:`UNKNOWN` -- never to
    optimism, because the repair for a chunk wrongly reported as lost is a re-publish, and
    that is the duplicate the whole design exists to prevent.
    """
    if isinstance(payload, Mapping):
        created = payload.get("created")
        if not isinstance(created, bool) and isinstance(created, int):
            # The endpoint's own sentence, in preference to the prose wrapped around it.
            # The database writes these for a person to read -- which class expects which
            # geometry, which attribute the schema refused -- and the wording IS the user
            # interface. What the network layer produces for an arbitrary error is that
            # same sentence inside 300 characters of quoted JSON.
            detail = payload.get("description")
            detail = detail if isinstance(detail, str) and detail.strip() else message
            if created:
                return ChunkVerdict(
                    state=UNKNOWN,
                    detail=(
                        f"the server refused the batch and reported {created} feature(s) "
                        f"created anyway, which an atomic endpoint cannot do: {detail}"
                    ),
                )
            at = payload.get("at")
            if not isinstance(at, bool) and isinstance(at, int) and 0 <= at < expected:
                return ChunkVerdict(state=REFUSED, at=at, detail=detail)
            return ChunkVerdict(state=NOT_CREATED, detail=detail)

    if status in NEVER_ARRIVED_STATUSES:
        return ChunkVerdict(state=NOT_CREATED, detail=message)
    return ChunkVerdict(state=UNKNOWN, detail=message)
