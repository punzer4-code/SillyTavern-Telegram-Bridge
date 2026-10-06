"""Native edits obey manual group turns at ingress and at durable execution."""

from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from application_test_setup import (
    ensure_application_extensions,
    make_test_memory_service,
    make_test_npc_service,
    make_test_persona_service,
    make_test_provider_port,
    make_test_rag_service,
    make_test_session_service,
)
from settings_test_support import SettingsTestCase, make_test_settings

import bridge.edit_messages as _owner_edit_messages
import bridge.metadata as _owner_metadata
import bridge.session_core as _owner_session_core
import bridge.sqlite_store as _sqlite_store
from bridge import group_core, update_message_routing, worker_orchestration
from bridge.job_service import DurableJob
from bridge.session_naming import create_session
from bridge.session_service import SessionService
from bridge.sqlite_store import db_connect

CHAT = "-100|topic:7"
DENIED = "It is not your turn in manual group mode."


@pytest.fixture
def case(tmp_path):
    config = make_test_settings(
        home=tmp_path,
        bot_token="test-token",
        default_model="test::model",
        db_file=tmp_path / "state.sqlite3",
        allowed_users=frozenset({"100", "200"}),
    )
    db = db_connect(app_settings=config)
    session = create_session(db, CHAT, "target::model", session_id="target", app_settings=config)
    db.execute(
        "INSERT INTO messages(chat_id,session_id,role,content,telegram_message_id,created_at) VALUES(?,?,?,?,?,?)",
        (CHAT, "target", "user", "original", "77", 1.0),
    )
    db.commit()
    group_core.save_group_state(
        db,
        CHAT,
        "target",
        dict(
            group_core.group_state(db, CHAT, "target"),
            enabled=True,
            mode="manual",
            turn_user_id="100",
            turn_users=["100", "200"],
            members=["a.png", "b.png"],
        ),
    )
    sent = []
    services = SimpleNamespace(
        config=config,
        jobs=Mock(),
        telegram=SimpleNamespace(send_text=lambda token, chat, text: sent.append(text)),
        group=SimpleNamespace(user_turn_allowed=group_core.group_user_turn_allowed),
        db_factory=lambda: db_connect(app_settings=config),
        provider=object(),
        memory=object(),
        npc=object(),
        persona=object(),
        delivery=object(),
        session=make_test_session_service(app_settings=config),
        rag=make_test_rag_service(),
    )
    services.jobs.start.return_value = True
    services.jobs.enqueue.return_value = 91
    services.jobs.actor_id.return_value = "100"
    yield SimpleNamespace(db=db, services=services, sent=sent, session=session)
    db.close()


def edited(sender="200"):
    return {
        "from": {"id": sender},
        "chat": {"id": "-100"},
        "message_thread_id": 7,
        "message_id": 77,
        "text": "replacement",
    }


@pytest.mark.parametrize("kind", ["ordinary", "edited"])
def test_other_manual_participant_is_rejected_before_enqueue(case, monkeypatch, kind):
    monkeypatch.setattr(update_message_routing, "send_help_command", lambda *args, **kw: False)
    if kind == "ordinary":
        update_message_routing.route_message_update(case.services, case.db, {}, edited(), 90, frozenset({"100", "200"}))
    else:
        update_message_routing.route_edited_message_update(
            case.services, case.db, edited(), 90, frozenset({"100", "200"})
        )
    case.services.jobs.enqueue.assert_not_called()
    case.services.jobs.submit.assert_not_called()
    assert case.sent == [DENIED]
    assert case.db.execute("SELECT content FROM messages").fetchone()[0] == "original"


@pytest.mark.parametrize(
    "mode,enabled,sender", [("manual", True, "100"), ("round_robin", True, "200"), ("manual", False, "200")]
)
def test_valid_edits_still_queue_and_preserve_actor_and_topic(case, mode, enabled, sender):
    state = group_core.group_state(case.db, CHAT, "target")
    group_core.save_group_state(case.db, CHAT, "target", dict(state, mode=mode, enabled=enabled))
    update_message_routing.route_edited_message_update(
        case.services, case.db, edited(sender), 90, frozenset({"100", "200"})
    )
    args = case.services.jobs.enqueue.call_args.args
    assert args[2:6] == (CHAT, "target", 77, "edit")
    assert args[6]["actor_id"] == sender
    assert args[6]["model"] == "target::model"
    assert case.services.jobs.submit.call_count == 1


def test_unpermitted_edit_returns_before_session_or_group_state(case, monkeypatch):
    monkeypatch.setattr(SessionService, "ensure", lambda *a, **k: pytest.fail("unauthorized state read"))
    case.services.group = SimpleNamespace(user_turn_allowed=lambda *a: pytest.fail("unauthorized group read"))
    update_message_routing.route_edited_message_update(case.services, case.db, edited("300"), 90, frozenset({"100"}))
    case.services.jobs.enqueue.assert_not_called()


def test_edit_checks_original_message_session_not_new_active_session(case):
    other = create_session(
        case.db, CHAT, case.services.config.default_model, session_id="new", app_settings=case.services.config
    )
    assert other["session_id"] == "new"
    update_message_routing.route_edited_message_update(case.services, case.db, edited(), 90, frozenset({"100", "200"}))
    case.services.jobs.enqueue.assert_not_called()
    assert case.sent == [DENIED]


def test_deleted_original_session_is_rejected_without_default_fallback(case, monkeypatch):
    def missing_session(*_args, **_kwargs):
        raise ValueError("queued session no longer exists")

    monkeypatch.setattr(SessionService, "load", missing_session)
    update_message_routing.route_edited_message_update(
        case.services, case.db, edited("100"), 90, frozenset({"100", "200"})
    )
    case.services.jobs.enqueue.assert_not_called()
    case.services.jobs.submit.assert_not_called()
    assert case.sent == ["Edited message was not found."]


@pytest.mark.parametrize("actor", ["200", ""])
def test_worker_rechecks_persisted_actor_before_editing(case, monkeypatch, actor):
    case.services.jobs.actor_id.return_value = actor
    edit = Mock()
    monkeypatch.setattr(worker_orchestration, "edit_telegram_user_message", edit)
    worker_orchestration.process_edit_job(case.services, CHAT, 77, "replacement", job_id=91)
    edit.assert_not_called()
    case.services.jobs.complete.assert_called_once()
    case.services.jobs.fail.assert_not_called()
    assert case.sent == [DENIED]


def test_turn_can_change_between_queue_and_recovery(case, monkeypatch):
    update_message_routing.route_edited_message_update(
        case.services, case.db, edited("100"), 90, frozenset({"100", "200"})
    )
    state = group_core.group_state(case.db, CHAT, "target")
    group_core.save_group_state(case.db, CHAT, "target", dict(state, turn_user_id="200"))
    job = DurableJob(91, CHAT, "target", 77, "edit", {"text": "replacement", "model": "test::model", "actor_id": "100"})
    submission = worker_orchestration.resolve_recovered_job_submission(case.services, {}, job)
    edit = Mock()
    monkeypatch.setattr(worker_orchestration, "edit_telegram_user_message", edit)
    submission.worker(*submission.args, job.job_id)
    edit.assert_not_called()
    assert case.sent[-1] == DENIED


def test_current_turn_owner_is_allowed_through_worker(case, monkeypatch):
    edit = Mock()
    monkeypatch.setattr(worker_orchestration, "edit_telegram_user_message", edit)
    worker_orchestration.process_edit_job(case.services, CHAT, 77, "replacement", job_id=91)
    edit.assert_called_once()
    assert edit.call_args.args[3:6] == (CHAT, 77, "replacement")


ensure_application_extensions()


class NativeEditedMessageSessionTests(SettingsTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = _sqlite_store.db_connect(
            Path(self.tmp.name) / "edit.sqlite3", app_settings=self.app_settings_builder.build()
        )
        self.model = "provider::model"

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def test_edit_uses_message_owning_session_without_switching_active_session(self):
        session_a = _owner_session_core.create_session(
            self.db,
            "chat",
            self.model,
            session_id="session-a",
            title="A",
            app_settings=self.app_settings_builder.build(),
        )
        _owner_session_core.update_session(
            self.db,
            "chat",
            session_a["session_id"],
            character_file="a.png",
        )
        now = time.time()
        cursor = self.db.execute(
            "INSERT INTO messages(chat_id,session_id,role,content,telegram_message_id,created_at) VALUES(?,?,?,?,?,?)",
            ("chat", "session-a", "user", "original", "77", now),
        )
        self.db.commit()
        user_rowid = int(cursor.lastrowid)

        session_b = _owner_session_core.create_session(
            self.db,
            "chat",
            self.model,
            session_id="session-b",
            title="B",
            app_settings=self.app_settings_builder.build(),
        )
        _owner_session_core.update_session(
            self.db,
            "chat",
            session_b["session_id"],
            character_file="b.png",
        )
        self.assertEqual(
            _owner_metadata.get_meta(self.db, "active_session:chat", ""),
            "session-b",
        )

        captured = {}
        sent = []

        def fake_card_fields(filename, *, app_settings=None):
            captured["character_file"] = filename
            return {"name": "Mira"}

        def fake_regenerate(
            _db,
            _token,
            _api_key,
            session,
            fields,
            _chat_id,
            rowid,
            new_text,
            *,
            app_settings=None,
            **kwargs,
        ):
            captured.update(
                {
                    "session_id": session["session_id"],
                    "fields": fields,
                    "rowid": rowid,
                    "new_text": new_text,
                    "provider_port": kwargs["provider_port"],
                    "memory_service": kwargs["memory_service"],
                    "npc_service": kwargs["npc_service"],
                    "persona_service": kwargs["persona_service"],
                }
            )

        provider = make_test_provider_port()
        memory = make_test_memory_service()
        npc = make_test_npc_service()
        persona = make_test_persona_service()
        with (
            patch.object(
                _owner_edit_messages,
                "card_fields_from_file",
                side_effect=fake_card_fields,
            ),
            patch.object(
                _owner_edit_messages,
                "regenerate_edited_turn",
                side_effect=fake_regenerate,
            ),
            patch.object(
                _owner_edit_messages,
                "send_text",
                side_effect=lambda _token, _chat_id, text: sent.append(text),
            ),
        ):
            _owner_edit_messages.edit_telegram_user_message(
                self.db,
                "token",
                "key",
                "chat",
                77,
                "replacement",
                self.model,
                provider_port=provider,
                memory_service=memory,
                npc_service=npc,
                persona_service=persona,
                app_settings=self.app_settings_builder.build(),
                rag_service=make_test_rag_service(),
            )

        self.assertEqual(captured["session_id"], "session-a")
        self.assertEqual(captured["character_file"], "a.png")
        self.assertEqual(captured["rowid"], user_rowid)
        self.assertEqual(captured["new_text"], "replacement")
        self.assertIs(captured["provider_port"], provider)
        self.assertIs(captured["memory_service"], memory)
        self.assertIs(captured["npc_service"], npc)
        self.assertIs(captured["persona_service"], persona)
        self.assertEqual(sent, [])
        self.assertEqual(
            _owner_metadata.get_meta(self.db, "active_session:chat", ""),
            "session-b",
        )


if __name__ == "__main__":
    unittest.main()

if __name__ == "__main__":
    unittest.main()
