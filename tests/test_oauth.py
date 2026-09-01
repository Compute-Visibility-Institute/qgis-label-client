"""Google sign-in: the parts where a mistake is silent.

Three of these are security properties rather than behaviour:

* the **PKCE challenge** is what stops another process on the machine redeeming an
  intercepted authorization code, and a wrong challenge does not fail loudly -- Google
  simply refuses the exchange later, which reads as "sign-in is broken";
* the **``state`` comparison** is what stops this plugin accepting a code minted for
  somebody else's account. Without it an analyst signs in as the wrong person and every
  label they then draw is attributed to that person, in an append-only table;
* the **expiry arithmetic** decides whether the token is renewed before a request or after
  a 401. After is not a fixable state: the failing request was made by QGIS's own OAPIF
  provider, and no plugin code is in its path to retry it.

The client secret's *absence* is asserted too. It is not an oversight to be tidied up
later: this client ships in a public GPL repository, so a secret in it would be public,
and PKCE is what actually binds the exchange.
"""

from __future__ import annotations

import base64
import json

import pytest

from qgis_label_client.core.oauth import (
    CLIENT_ID,
    CLIENT_SECRET,
    REFRESH_SKEW_SECONDS,
    Credential,
    SignInCancelledError,
    SignInError,
    SignInExpiredError,
    authorization_url,
    callback_page,
    challenge,
    claims,
    credential_from_token_response,
    describe_session,
    encode_form,
    needs_refresh,
    new_state,
    new_verifier,
    parse_callback,
    raise_for_token_error,
    redirect_uri,
    refresh_request,
    request_target,
    revocation_request,
    seconds_until_refresh,
    token_request,
)


def _jwt(payload: dict) -> str:
    """A JWT with a real payload and a meaningless signature.

    Meaningless on purpose: the plugin must never verify one, so a test that needed a
    valid signature would be testing something the plugin is not allowed to do.
    """
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return f"eyJhbGciOiJSUzI1NiJ9.{body}.not-a-real-signature"


# --- PKCE --------------------------------------------------------------------


def test_the_s256_challenge_matches_the_published_rfc_7636_vector():
    """Against RFC 7636 Appendix B, not against our own implementation.

    A challenge derived by a subtly wrong recipe -- padded base64, standard rather than
    URL-safe alphabet, hex instead of base64 -- produces a sign-in that fails only at the
    token exchange, with an error from Google that names none of those causes.
    """
    verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
    assert challenge(verifier) == "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"


def test_a_verifier_is_url_safe_and_inside_the_length_rfc_7636_requires():
    # 43-128 characters of unreserved ASCII. A verifier outside that range is rejected by
    # the authorization server, and padding characters would be re-encoded in the URL.
    verifier = new_verifier()
    assert 43 <= len(verifier) <= 128
    assert "=" not in verifier and "+" not in verifier and "/" not in verifier


def test_two_verifiers_are_never_the_same():
    # A reused verifier means a code intercepted once can be redeemed forever.
    assert new_verifier() != new_verifier()
    assert new_state() != new_state()


# --- the authorization request ------------------------------------------------


def test_the_authorization_url_asks_for_everything_the_renewal_depends_on():
    """``access_type=offline`` plus ``prompt=consent`` is what returns a refresh token.

    Without both, Google returns an ID token and nothing to renew it with, and the session
    dies silently an hour later -- which presents as "the plugin stopped working" rather
    than as an expired sign-in.
    """
    url = authorization_url(
        redirect=redirect_uri(51234), state="st", verifier="v" * 43, login_hint=""
    )
    assert url.startswith("https://accounts.google.com/o/oauth2/v2/auth?")
    assert "access_type=offline" in url
    assert "prompt=consent" in url
    assert "code_challenge_method=S256" in url
    assert "response_type=code" in url
    assert "scope=openid+email+profile" in url
    assert f"code_challenge={challenge('v' * 43)}" in url


def test_the_authorization_url_carries_no_secret_and_names_the_desktop_client():
    # The client id identifies the APP, never the bearer. It is public by construction;
    # a secret beside it would not be, and would suggest a protection nobody has.
    url = authorization_url(redirect=redirect_uri(1), state="st", verifier="v" * 43)
    assert CLIENT_ID in url
    assert "client_secret" not in url


def test_a_login_hint_is_only_sent_when_there_is_one():
    # An empty hint would pre-select an account named "", which Google answers by refusing
    # the whole authorization.
    assert "login_hint" not in authorization_url(
        redirect="http://127.0.0.1:1/", state="s", verifier="v" * 43
    )
    hinted = authorization_url(
        redirect="http://127.0.0.1:1/", state="s", verifier="v" * 43, login_hint="a@example.org"
    )
    assert "login_hint=a%40example.org" in hinted


def test_the_redirect_is_loopback_only():
    # Any other host would send the authorization code off this machine.
    assert redirect_uri(49152) == "http://127.0.0.1:49152/"


# --- the redirect back --------------------------------------------------------


def test_a_matching_state_yields_the_code():
    assert parse_callback("/?code=abc123&state=s3cr3t", "s3cr3t") == "abc123"


def test_a_mismatched_state_is_refused_and_says_nothing_was_stored():
    """The security check in this module.

    Anything on the machine can connect to the loopback port and present a code. Accepting
    one would sign the analyst in as whoever minted it, and every label drawn afterwards
    would carry that identity into an append-only history.
    """
    with pytest.raises(SignInError) as caught:
        parse_callback("/?code=abc123&state=someone-elses", "s3cr3t")
    assert "Nothing was stored" in str(caught.value)


def test_a_missing_state_is_refused_rather_than_treated_as_a_match():
    # Two empty strings compare equal, which would turn "no state at all" into "verified".
    with pytest.raises(SignInError):
        parse_callback("/?code=abc123", "s3cr3t")
    with pytest.raises(SignInError):
        parse_callback("/?code=abc123&state=", "")


def test_refusing_consent_is_reported_as_a_cancellation_not_a_failure():
    # Closing the consent tab is a decision. Reporting it in red trains people to ignore
    # red, and the next real failure is the one they ignore.
    with pytest.raises(SignInCancelledError):
        parse_callback("/?error=access_denied&state=s", "s")


def test_any_other_google_error_keeps_google_s_own_words():
    with pytest.raises(SignInError) as caught:
        parse_callback("/?error=invalid_scope&error_description=bad+scope&state=s", "s")
    assert "invalid_scope" in str(caught.value)
    assert "bad scope" in str(caught.value)


def test_a_reply_with_neither_code_nor_error_is_still_a_failure():
    with pytest.raises(SignInError):
        parse_callback("/?state=s", "s")


def test_only_a_get_request_line_is_treated_as_the_browser_coming_back():
    """Port scanners and other local software do connect to loopback ports.

    Parsing whatever they send as a redirect would mean comparing ``state`` against
    nothing at all, which is the check above defeated by an accident.
    """
    assert request_target("GET /?code=a&state=b HTTP/1.1") == "/?code=a&state=b"
    assert request_target("POST /?code=a HTTP/1.1") == ""
    assert request_target("garbage") == ""
    assert request_target("") == ""


def test_the_browser_page_escapes_what_it_is_handed_and_declares_its_length():
    # The message can carry a server's own words. An unescaped '<' would break the page,
    # and a wrong Content-Length leaves the tab spinning after a sign-in that worked.
    page = callback_page("Signed in as <a@example.org>")
    head, _, body = page.partition(b"\r\n\r\n")
    assert b"&lt;a@example.org&gt;" in body
    assert b"<a@example.org>" not in body
    assert f"Content-Length: {len(body)}".encode() in head


# --- the token exchange -------------------------------------------------------


def test_the_code_exchange_sends_both_the_verifier_and_the_secret():
    """PKCE AND a secret. This test used to assert the opposite, and was wrong.

    The old reasoning: the verifier proves this is the same client that made the
    authorization request, and a secret shipped in a public repository proves nothing
    about anybody. Both clauses are true, and Google refuses the request regardless --

        invalid_request: client_secret is missing

    -- because it requires the field as a client IDENTIFIER, not as a protection. PKCE
    is additional to it, never a substitute. Asserting its absence made a passing suite
    out of a flow that could not complete a single sign-in.
    """
    fields = token_request(
        "the-code", "the-verifier", "http://127.0.0.1:1/", client_secret="s3cr3t"
    )
    assert fields["code_verifier"] == "the-verifier"
    assert fields["grant_type"] == "authorization_code"
    assert fields["redirect_uri"] == "http://127.0.0.1:1/"
    assert fields["client_secret"] == "s3cr3t"


def test_the_renewal_carries_the_secret_too():
    """Same endpoint, same client, same requirement.

    Omitting it here would have been the nastier half of the bug: sign-in works, and the
    silent renewal fails an hour later with a layer going 401 mid-edit and nothing about
    the symptom pointing back at this request body.
    """
    assert refresh_request("r3fr3sh", client_secret="s3cr3t") == {
        "client_id": CLIENT_ID,
        "client_secret": "s3cr3t",
        "grant_type": "refresh_token",
        "refresh_token": "r3fr3sh",
    }


def test_revocation_names_the_token_to_destroy():
    # Signing out has to be true on Google's side too, or "signed out" describes nothing.
    assert revocation_request("r3fr3sh") == {"token": "r3fr3sh"}


def test_the_form_body_is_url_encoded_ascii():
    assert encode_form({"b": "two words", "a": "x/y"}) == b"a=x%2Fy&b=two+words"


def test_a_dead_refresh_token_is_reported_as_expired_rather_than_as_an_outage():
    """``invalid_grant`` is the one error whose repair is a click, not a retry.

    Reported as a backend failure it produces a support thread and a wait; reported as an
    expired sign-in it produces one browser round trip.
    """
    with pytest.raises(SignInExpiredError) as caught:
        raise_for_token_error({"error": "invalid_grant", "error_description": "Token revoked."})
    assert "Sign in with Google" in str(caught.value)
    # And it says what does NOT fix itself, because a renewed credential cannot un-fail a
    # request QGIS's own provider already made.
    assert "reload" in str(caught.value).lower()


def test_any_other_token_error_is_an_ordinary_failure():
    with pytest.raises(SignInError) as caught:
        raise_for_token_error({"error": "invalid_request"})
    assert not isinstance(caught.value, SignInExpiredError)


def test_a_reply_with_no_error_raises_nothing():
    raise_for_token_error({"id_token": "x"})
    raise_for_token_error("not a mapping at all")


# --- reading the ID token -----------------------------------------------------


def test_the_payload_is_decoded_without_being_verified():
    # The plugin must never verify a signature: the server has Google's keys and the
    # audience it accepts, and a second, weaker verifier here would look like a check.
    decoded = claims(_jwt({"email": "a@example.org", "exp": 1750000000}))
    assert decoded["email"] == "a@example.org"
    assert decoded["exp"] == 1750000000


def test_an_unreadable_token_decodes_to_nothing_rather_than_raising():
    """A token this cannot parse may still be one the server accepts.

    Refusing the sign-in over a payload the *display code* could not read would turn a
    cosmetic problem into a lockout.
    """
    assert claims("") == {}
    assert claims("not.a.jwt") == {}
    assert claims("only-one-part") == {}
    assert claims(_jwt([1, 2, 3])) == {}


def test_expiry_comes_from_the_id_token_not_from_expires_in():
    """They usually agree, and when they do not, ``expires_in`` is about another token.

    ``expires_in`` describes the *access token*, which this plugin never sends anywhere.
    Scheduling the renewal off it is how a token expires before the timer meant to replace
    it fires.
    """
    payload = {
        "id_token": _jwt({"email": "a@example.org", "exp": 1_700_003_600}),
        "expires_in": 60,
        "refresh_token": "r3fr3sh",
    }
    credential = credential_from_token_response(payload, now=1_700_000_000)
    assert credential.expires_at == 1_700_003_600
    assert credential.email == "a@example.org"
    assert credential.refresh_token == "r3fr3sh"


def test_a_token_with_no_readable_exp_falls_back_to_the_replys_own_lifetime():
    # Earlier than the truth, never later, so the fallback can only renew too soon.
    payload = {"id_token": "unreadable", "expires_in": 120}
    assert credential_from_token_response(payload, now=1000).expires_at == 1120


def test_a_reply_with_no_id_token_is_refused_and_names_the_likely_cause():
    with pytest.raises(SignInError) as caught:
        credential_from_token_response({"access_token": "a"}, now=0)
    assert "openid" in str(caught.value)


def test_a_renewal_keeps_the_refresh_token_it_was_given():
    """Google returns no ``refresh_token`` on a renewal, and that is not a loss.

    Treating the absence as "the refresh token is gone" would make every session die at
    its first renewal -- an hour in, every time.
    """
    payload = {"id_token": _jwt({"exp": 2000})}
    credential = credential_from_token_response(payload, now=1000, refresh_token="kept")
    assert credential.refresh_token == "kept"


# --- expiry arithmetic --------------------------------------------------------


def test_a_token_inside_the_renewal_window_needs_renewing():
    # The window is five minutes: a token that expires while a QGIS OAPIF request is in
    # flight produces a 401 nothing can retry.
    expires_at = 10_000
    assert needs_refresh(expires_at, now=expires_at - REFRESH_SKEW_SECONDS) is True
    assert needs_refresh(expires_at, now=expires_at - REFRESH_SKEW_SECONDS - 1) is False


def test_an_already_expired_token_needs_renewing():
    """The case the timer cannot cover: a laptop suspended over lunch.

    The renewal timer fires late or not at all across a suspend, so the pre-flight check is
    the only thing standing between waking up and a layer full of 401s.
    """
    assert needs_refresh(10_000, now=99_999) is True


def test_no_google_session_is_never_reported_as_needing_a_renewal():
    # Zero means a fresh profile, or one still holding a hand-pasted token. There is
    # nothing to renew, and nagging about it would be noise on every action.
    assert needs_refresh(0, now=99_999) is False
    assert seconds_until_refresh(0, now=99_999) == 0.0


def test_the_timer_delay_is_never_negative():
    # A negative interval is not a timer that fires immediately, it is a timer that never
    # fires -- and the session it was meant to renew simply ends.
    assert seconds_until_refresh(10_000, now=1_000) == 10_000 - REFRESH_SKEW_SECONDS - 1_000
    assert seconds_until_refresh(10_000, now=99_999) == 0.0


def test_the_credential_answers_the_same_question_as_the_free_function():
    credential = Credential(id_token="t", refresh_token="r", email="a@example.org", expires_at=1000)
    assert credential.needs_refresh(now=999) is True
    assert credential.needs_refresh(now=0) is False


def test_the_panel_line_says_who_and_for_how_much_longer():
    """Remaining time rather than an absolute instant.

    The question a person actually asks is "do I have time to finish this", and a
    timestamp in an unstated zone is the classic way to be read off by an hour.
    """
    assert "a@example.org" in describe_session("a@example.org", 4_000, now=1_000)
    assert "50 min" in describe_session("a@example.org", 4_000, now=1_000)
    assert "expired" in describe_session("a@example.org", 1_000, now=4_000)
    # No address stored is not an error state; it is a session signed in before the panel
    # learned to record one.
    assert describe_session("", 0, now=0) == "Signed in with Google"


# ── the client_secret that PKCE does not replace ─────────────────────────────


def test_the_token_exchange_carries_the_client_secret() -> None:
    """Google refuses the exchange without it, AFTER the analyst has consented.

    The reasoning that omitted it was sound and still wrong: a desktop client's secret
    is not confidential, PKCE's verifier is what binds the exchange -- and Google
    requires the field anyway, as a client identifier rather than as a protection.
    Observed as `invalid_request: client_secret is missing` at the worst possible
    moment, with a browser tab already saying "Signed in".
    """
    fields = token_request("CODE", "VERIFIER", "http://127.0.0.1:9/", client_secret="s3cr3t")
    assert fields["client_secret"] == "s3cr3t"
    assert fields["code_verifier"] == "VERIFIER", (
        "PKCE is additional to the secret, not replaced by it"
    )


def test_the_refresh_carries_it_too() -> None:
    """Otherwise sign-in works and the silent renewal fails an hour later.

    That is the hardest failure to attribute, because nothing about a layer going 401
    mid-session points back at the refresh body.
    """
    assert refresh_request("RT", client_secret="s3cr3t")["client_secret"] == "s3cr3t"


def test_an_absent_secret_is_omitted_rather_than_sent_empty() -> None:
    """An empty client_secret is a different Google error from a missing one, and the
    missing-field message is the one that names what to do about it.

    Reachable only by asking for it: the default is the embedded CLIENT_SECRET, so a
    deployment that overrides it with a blank setting gets the clearer failure.
    """
    assert "client_secret" not in token_request("C", "V", "http://127.0.0.1:9/", client_secret="")
    assert "client_secret" not in refresh_request("RT", client_secret="")


def test_a_release_build_needs_no_pasting() -> None:
    """Onboarding cost: install, sign in. Not "install, then paste two values".

    The constant is the seam the release workflow writes into. Substituting a value there
    must reach the token request without any other change, which is the whole reason the
    default is the constant rather than the empty string literal.
    """
    import qgis_label_client.core.oauth as oauth_module

    original = oauth_module.CLIENT_SECRET
    try:
        oauth_module.CLIENT_SECRET = "released-value"
        fields = oauth_module.token_request("C", "V", "http://127.0.0.1:9/")
        assert fields["client_secret"] == "released-value"
    finally:
        oauth_module.CLIENT_SECRET = original


def test_the_source_tree_carries_no_client_secret() -> None:
    """The release workflow substitutes it; git must never hold it.

    Not because publishing it would be dangerous -- Google documents an installed app's
    secret as not confidential, and the allowlist is what actually grants access. Because
    a value in git history can only be rotated by rewriting history, and GitHub's push
    protection blocks the push regardless of that argument.

    This asserts the seam the workflow greps for. If someone hardcodes a value here, the
    substitution step fails loudly at release rather than silently producing a zip with
    the wrong secret in it.
    """
    assert CLIENT_SECRET == "", (
        "core.oauth.CLIENT_SECRET must stay empty in source; "
        ".github/workflows/release.yml substitutes it into the published zip"
    )
