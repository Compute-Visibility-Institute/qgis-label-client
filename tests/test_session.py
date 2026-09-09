"""Queued requests survive peer failures but never cross an account/session boundary."""

import pytest

from qgis_label_client.core.session import ReplayResult, SessionCoordinator


def test_failed_action_does_not_drop_later_requests():
    session = SessionCoordinator()
    calls, errors = [], []

    def fail():
        calls.append("failed")
        raise RuntimeError("task manager refused")

    session.defer(lambda: calls.append("first"))
    session.defer(fail)
    session.defer(lambda: calls.append("last"))
    assert session.resume(errors.append) == ReplayResult(completed=2, failed=1)
    assert calls == ["first", "failed", "last"]
    assert str(errors[0]) == "task manager refused"
    assert not session.resuming and session.pending_count == 0


def test_session_change_during_replay_cancels_remaining_old_requests():
    session = SessionCoordinator()
    calls = []
    session.defer(session.advance)
    session.defer(lambda: calls.append("old account request"))
    assert session.resume(calls.append) == ReplayResult(completed=1, cancelled=1)
    assert calls == []
    assert not session.resuming


@pytest.mark.parametrize("end_refresh", [False, True])
def test_cancellation_during_replay_stops_batch_without_invalidating_read_identity(end_refresh):
    session = SessionCoordinator()
    context = session.capture_read("server", "account", {})
    calls = []
    session.defer(
        (lambda: session.end_refresh(discard_pending=True))
        if end_refresh
        else session.cancel_pending
    )
    session.defer(lambda: calls.append("cancelled request"))
    assert session.resume(calls.append) == ReplayResult(completed=1, cancelled=1)
    assert calls == []
    assert session.allows_read(context, "server", "account", {})


def test_cancelled_batch_does_not_mark_new_requests_as_already_renewed():
    session = SessionCoordinator()
    guards = []

    def cancel():
        guards.append(session.resuming)
        session.cancel_pending()
        guards.append(session.resuming)

    session.defer(cancel)
    session.resume(lambda error: pytest.fail(str(error)))
    assert guards == [True, False]


def test_new_session_can_replay_without_inheriting_old_replay_guard():
    session = SessionCoordinator()
    calls = []

    def switch_account():
        session.advance()
        assert not session.resuming
        session.defer(lambda: calls.append("new account"))
        session.resume(calls.append)

    session.defer(switch_account)
    session.defer(lambda: calls.append("old account"))
    session.resume(calls.append)
    assert calls == ["new account"]
    assert not session.resuming


def test_nested_resume_does_not_duplicate_actions_or_drop_new_queue():
    session = SessionCoordinator()
    calls = []

    def action():
        session.defer(lambda: calls.append("later"))
        assert session.resume(calls.append) == ReplayResult()
        calls.append("first")

    session.defer(action)
    session.resume(calls.append)
    assert calls == ["first"]
    assert session.pending_count == 1
    session.resume(calls.append)
    assert calls == ["first", "later"]


def test_failed_refresh_releases_state_for_another_attempt():
    session = SessionCoordinator()
    session.refreshing = session.repairing = True
    session.defer(lambda: None)
    session.end_refresh(discard_pending=True)
    assert not session.refreshing and not session.repairing
    assert session.pending_count == 0


def test_renewal_may_add_tracks_without_invalidating_captured_read():
    session = SessionCoordinator()
    configs = {"dev": "existing"}
    context = session.capture_read("server", "account", configs)
    configs["prod"] = "new"
    assert session.allows_read(context, "server", "account", configs)
    configs["dev"] = "replacement"
    assert not session.allows_read(context, "server", "account", configs)


def test_old_read_is_rejected_after_account_server_or_session_change():
    session = SessionCoordinator()
    context = session.capture_read("server", "account", {})
    assert not session.allows_read(context, "other", "account", {})
    assert not session.allows_read(context, "server", "other", {})
    session.advance()
    assert not session.allows_read(context, "server", "account", {})
