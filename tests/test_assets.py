"""Signed imagery URLs.

Every URL in this file is fabricated. There is no real bucket name, no real object path
and no real signature here, and there must never be: this repository is public and the
imagery is licensed.
"""

from __future__ import annotations

from datetime import timezone

import pytest

from qgis_label_client.core.assets import (
    RasterLayerRef,
    SignedAsset,
    gcs_object_key,
    parse_signed_assets,
    plan_rewrites,
    redact,
    vsicurl_source,
)
from qgis_label_client.core.errors import BackendError

SIGNED = "https://storage.googleapis.com/example-bucket/cog/scene_visual.tif?X-Goog-Signature=deadbeef&X-Goog-Expires=3600"


# --- redaction --------------------------------------------------------------


def test_redaction_keeps_the_path_and_drops_the_signature():
    result = redact(SIGNED)
    assert "example-bucket/cog/scene_visual.tif" in result
    assert "deadbeef" not in result
    assert "X-Goog-Signature" not in result


def test_redaction_leaves_unsigned_urls_alone():
    assert redact("https://host/a.tif") == "https://host/a.tif"


# --- object keys ------------------------------------------------------------


@pytest.mark.parametrize(
    "reference",
    [
        "gs://example-bucket/cog/scene_visual.tif",
        "https://storage.googleapis.com/example-bucket/cog/scene_visual.tif",
        SIGNED,
        "https://example-bucket.storage.googleapis.com/cog/scene_visual.tif",
        "/vsicurl/" + SIGNED,
        "/vsicurl?url=https%3A%2F%2Fstorage.googleapis.com%2Fexample-bucket%2Fcog%2Fscene_visual.tif",
    ],
)
def test_every_form_of_the_same_object_reduces_to_one_key(reference):
    assert gcs_object_key(reference) == "example-bucket/cog/scene_visual.tif"


def test_percent_encoded_paths_are_decoded():
    assert (
        gcs_object_key("gs://example-bucket/cog/scene%20one.tif")
        == "example-bucket/cog/scene one.tif"
    )


@pytest.mark.parametrize(
    "reference",
    [None, "", "/local/path/file.tif", "https://example.org/tiles/1.tif", "gs://bucket"],
)
def test_non_gcs_references_are_not_claimed(reference):
    # A basemap or a local DEM must never be mistaken for imagery and rewritten.
    assert gcs_object_key(reference) is None


def test_vsicurl_wrapping_is_idempotent():
    once = vsicurl_source(SIGNED)
    assert once.startswith("/vsicurl/https://")
    assert vsicurl_source(once) == once


# --- parsing ----------------------------------------------------------------

RESPONSE = {
    "expires_at": "2026-08-23T18:00:00Z",
    "assets": [
        {
            "capture_id": "11111111-1111-1111-1111-111111111111",
            "stac_id": "26APR21034014-S2AS-EXAMPLE_01_P001",
            "asset": "visual",
            "url": SIGNED,
            "gs_uri": "gs://example-bucket/cog/scene_visual.tif",
        },
        {
            "capture_id": "11111111-1111-1111-1111-111111111111",
            "stac_id": "26APR21034014-S2AS-EXAMPLE_01_P001",
            "asset": "analysis",
            "url": "https://storage.googleapis.com/example-bucket/cog/scene_analysis.tif?sig=x",
            "expires_at": "2026-08-23T17:00:00Z",
        },
    ],
}


def test_parsing_returns_assets_and_the_earliest_expiry():
    assets, expiry = parse_signed_assets(RESPONSE)
    assert [a.role for a in assets] == ["visual", "analysis"]
    # The session is only good until the first URL dies, not the last.
    assert expiry.astimezone(timezone.utc).isoformat() == "2026-08-23T17:00:00+00:00"


def test_asset_key_pairs_scene_and_derivative():
    assets, _ = parse_signed_assets(RESPONSE)
    assert assets[0].key == "26APR21034014-S2AS-EXAMPLE_01_P001:visual"


def test_bare_array_response_is_accepted():
    assets, expiry = parse_signed_assets(RESPONSE["assets"])
    assert len(assets) == 2
    assert expiry is not None


def test_entries_without_a_url_are_skipped():
    assets, _ = parse_signed_assets({"assets": [{"asset": "visual"}, RESPONSE["assets"][0]]})
    assert len(assets) == 1


@pytest.mark.parametrize("document", ["nope", {"assets": []}, {}, {"assets": "x"}])
def test_unusable_responses_raise(document):
    with pytest.raises(BackendError):
        parse_signed_assets(document)


# --- matching ---------------------------------------------------------------


def _assets():
    return parse_signed_assets(RESPONSE)[0]


def test_explicit_asset_key_wins():
    assets = _assets()
    layer = RasterLayerRef(
        layer_id="L1",
        name="Visual",
        source="/vsicurl/https://storage.googleapis.com/example-bucket/cog/scene_analysis.tif?old=1",
        asset_key="26APR21034014-S2AS-EXAMPLE_01_P001:visual",
    )
    rewrites, unmatched = plan_rewrites([layer], assets)
    assert not unmatched
    assert rewrites[0].matched_by == "asset_key"
    assert rewrites[0].asset.role == "visual"


def test_stale_url_matches_by_object_path_ignoring_the_signature():
    assets = _assets()
    stale = "/vsicurl/https://storage.googleapis.com/example-bucket/cog/scene_visual.tif?X-Goog-Signature=expired"
    rewrites, unmatched = plan_rewrites(
        [RasterLayerRef(layer_id="L1", name="Visual", source=stale)], assets
    )
    assert not unmatched
    assert rewrites[0].matched_by == "object_key"
    assert rewrites[0].new_source.startswith("/vsicurl/https://")


def test_unmatched_layers_are_reported_not_silently_skipped():
    assets = _assets()
    orphan = RasterLayerRef(layer_id="L9", name="Something else", source="gs://other-bucket/x.tif")
    rewrites, unmatched = plan_rewrites([orphan], assets)
    assert not rewrites
    assert unmatched == [orphan]


def test_asset_key_that_matches_nothing_falls_back_to_the_url():
    assets = _assets()
    layer = RasterLayerRef(
        layer_id="L1",
        name="Visual",
        source="gs://example-bucket/cog/scene_visual.tif",
        asset_key="a-scene-that-no-longer-exists:visual",
    )
    rewrites, _ = plan_rewrites([layer], assets)
    assert rewrites[0].matched_by == "object_key"


def test_asset_without_gs_uri_still_matches_by_its_signed_url_path():
    asset = SignedAsset(capture_id=None, stac_id="S", role="visual", url=SIGNED, gs_uri=None)
    assert asset.object_key == "example-bucket/cog/scene_visual.tif"
