"""Address conversations, directed investigations, and canonical deduplication."""
import os
import sqlite3
import tempfile

from livingpc.config import Config
from livingpc import crypto
from livingpc.inference import InferenceStore, concept_similarity
from livingpc.inference_inquiry import (StubInquiryModel, reply_to_inquiry,
                                        start_inquiry)
from livingpc.memory import MemoryStore


def _stores(folder):
    path = os.path.join(folder, "memory.db")
    return path, InferenceStore(path), MemoryStore(path)


def test_address_is_persistent_idempotent_and_encrypted():
    with tempfile.TemporaryDirectory() as folder:
        path, inf, mem = _stores(folder)
        try:
            cid = inf.add_candidate("focus", "You focus through self-observation.",
                                    confidence=0.9)
            cfg = Config(memory_db_path=path, inference_backend="stub")
            first = start_inquiry(cfg, inf, mem, kind="address",
                                  prompt="You focus through self-observation.",
                                  inference_id=cid, model=StubInquiryModel())
            second = start_inquiry(cfg, inf, mem, kind="address",
                                   prompt="You focus through self-observation.",
                                   inference_id=cid, model=StubInquiryModel())
            assert first["id"] == second["id"]
            assert first["messages"][0]["content"].endswith("?")
            check = sqlite3.connect(path)
            try:
                raw = check.execute(
                    "SELECT prompt FROM inference_inquiry WHERE id=?", (first["id"],)
                ).fetchone()[0]
            finally:
                check.close()
            if crypto.enabled():
                assert "self-observation" not in raw
            else:
                assert raw == "You focus through self-observation."
        finally:
            mem.close(); inf.close()


def test_reply_updates_persistent_conversation():
    with tempfile.TemporaryDirectory() as folder:
        path, inf, mem = _stores(folder)
        try:
            cfg = Config(memory_db_path=path, inference_backend="stub")
            inquiry = start_inquiry(cfg, inf, mem, kind="directed",
                                    prompt="Why do I avoid starting important work?",
                                    model=StubInquiryModel())
            updated = reply_to_inquiry(cfg, inf, mem, inquiry["id"],
                                       "I start once the outcome feels reversible.",
                                       model=StubInquiryModel())
            assert [m["role"] for m in updated["messages"]] == [
                "assistant", "user", "assistant"]
            assert updated["messages"][-1]["content"].endswith("?")
        finally:
            mem.close(); inf.close()


def test_accept_creates_canonical_belief_and_absorbs_repeats():
    with tempfile.TemporaryDirectory() as folder:
        path, inf, mem = _stores(folder)
        try:
            source = inf.add_candidate(
                "focus", "You sustain focus through recursive self observation.",
                confidence=0.91)
            duplicate = inf.add_candidate(
                "focus", "Your sustained focus comes from recursive self observation.",
                confidence=0.88)
            cfg = Config(memory_db_path=path, inference_backend="stub")
            inquiry = start_inquiry(
                cfg, inf, mem, kind="address",
                prompt="You sustain focus through recursive self observation.",
                inference_id=source, model=StubInquiryModel())
            canonical = inf.resolve_inquiry(
                inquiry["id"], "accepted",
                "I sustain focus when recursive self observation makes progress visible.")
            belief = inf._dict(inf.get(canonical))
            assert belief["resolution_status"] == "accepted"
            assert belief["is_core_belief"] is True
            assert inf.get(source)["status"] == "retired"
            assert inf.get(duplicate)["status"] == "retired"
            assert inf.get(duplicate)["absorbed_by_id"] == canonical
            assert inf.to_review() == []
        finally:
            mem.close(); inf.close()


def test_needs_evidence_parks_claim_without_approving_it():
    with tempfile.TemporaryDirectory() as folder:
        path, inf, mem = _stores(folder)
        try:
            source = inf.add_candidate("identity", "You fear visible failure.",
                                       confidence=0.9)
            cfg = Config(memory_db_path=path, inference_backend="stub")
            inquiry = start_inquiry(cfg, inf, mem, kind="address",
                                    prompt="You fear visible failure.", inference_id=source,
                                    model=StubInquiryModel())
            assert inf.resolve_inquiry(inquiry["id"], "awaiting_evidence") is None
            assert inf.get(source)["status"] == "retired"
            assert inf.confirmed() == []
            assert inf.inquiry(inquiry["id"])["status"] == "awaiting_evidence"
        finally:
            mem.close(); inf.close()


def test_future_rewording_attaches_to_canonical_belief_not_review_stack():
    with tempfile.TemporaryDirectory() as folder:
        path, inf, mem = _stores(folder)
        try:
            source = inf.add_candidate(
                "focus", "You sustain focus through recursive self observation.",
                confidence=0.9)
            inquiry_id = inf.start_inquiry(
                "address", "You sustain focus through recursive self observation.",
                inference_id=source)
            inf.update_inquiry_draft(
                inquiry_id, "You sustain focus through recursive self observation.", 0.9)
            canonical = inf.resolve_inquiry(inquiry_id, "accepted")
            returned = inf.upsert_claim(
                "focus", "Your sustained focus comes from recursive self observation.", 0.92)
            assert returned == canonical
            assert inf.to_review(min_confidence=0.0) == []
        finally:
            mem.close(); inf.close()


def test_concept_similarity_is_conservative_but_catches_light_rewording():
    assert concept_similarity(
        "You sustain focus through recursive self observation",
        "Your sustained focus comes from recursive self observation") >= 0.58
    assert concept_similarity("You like Korean grammar", "You avoid social conflict") < 0.3


def test_gui_bridge_directed_investigation_roundtrip():
    with tempfile.TemporaryDirectory() as folder:
        cfg = Config(memory_db_path=os.path.join(folder, "memory.db"),
                     db_path=os.path.join(folder, "events.db"),
                     inference_backend="stub")
        from gui import GuiApi
        api = GuiApi(cfg)
        started = api.inference_inquiry_start("Why do I avoid starting X?", None)
        assert started["ok"]
        inquiry_id = started["inquiry"]["id"]
        replied = api.inference_inquiry_reply(inquiry_id, "Mostly when stakes feel permanent.")
        assert replied["ok"] and len(replied["inquiry"]["messages"]) == 3
        resolved = api.inference_inquiry_resolve(
            inquiry_id, "tentative", "I delay when decisions feel irreversible.")
        assert resolved["ok"] and resolved["canonical_id"]
        beliefs = api.state()["beliefs"]
        assert any(b["resolution_status"] == "tentative" for b in beliefs)
