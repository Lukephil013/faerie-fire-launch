import json
import os
import tempfile

import pytest

from livingpc.browser_assistant import (
    BrowserAssistant, BrowserPolicyError, BrowserTaskStore,
    EXTRACT_FORM_CONTROLS_JS, build_mapping_prompt, normalize_origin,
    parse_mapping_reply, validate_mappings,
)
from livingpc.config import Config


FIELDS = [
    {"field_id": "ff-scan-1", "label": "Professional title", "kind": "text",
     "required": True, "current": "", "options": []},
    {"field_id": "ff-scan-2", "label": "Availability", "kind": "select",
     "required": False, "current": "", "options": [
         {"label": "Full time", "value": "full"},
         {"label": "Part time", "value": "part"}]},
    {"field_id": "ff-scan-3", "label": "Open to work", "kind": "checkbox",
     "required": False, "current": False, "options": []},
]


class FakeController:
    def __init__(self):
        self.calls = []

    def call(self, operation, *args, **kwargs):
        self.calls.append((operation, args))
        if operation == "scan":
            return {"url": "https://forms.example/profile", "origin": "https://forms.example",
                    "fields": FIELDS}
        if operation == "fill":
            return {"filled": len(args[1]), "url": "https://forms.example/profile"}
        return True if operation in {"allow", "close"} else {"url": args[1]}

    def post(self, operation, *args):
        self.calls.append((operation, args))


def test_origin_validation_is_exact_secure_and_blocks_upwork():
    assert normalize_origin("https://Forms.Example:443/profile?step=1") == (
        "https://forms.example/profile?step=1", "https://forms.example")
    assert normalize_origin("http://localhost:8000/form")[1] == "http://localhost:8000"
    with pytest.raises(BrowserPolicyError):
        normalize_origin("http://forms.example/profile")
    with pytest.raises(BrowserPolicyError):
        normalize_origin("https://www.upwork.com/freelancers/settings")
    with pytest.raises(BrowserPolicyError):
        normalize_origin("https://account:secret@forms.example/profile")


def test_permissions_and_tasks_are_durable_and_private_by_default():
    with tempfile.TemporaryDirectory() as directory:
        store = BrowserTaskStore(os.path.join(directory, "memory.db"))
        task = store.create_task(
            "https://forms.example/profile", "Update profile", "Fill my public profile",
            "Private source material")
        assert task["status"] == "awaiting_domain_approval"
        store.approve("https://forms.example")
        assert store.is_approved("https://forms.example")
        assert BrowserTaskStore(store.db_path).permissions()[0]["origin"] == "https://forms.example"
        public = store.list_active()[0]
        assert "source_context" not in public
        store.revoke("https://forms.example")
        assert not store.is_approved("https://forms.example")


def test_model_mappings_are_constrained_to_snapshot_and_control_types():
    proposed = {"mappings": [
        {"field_id": "ff-scan-1", "value": "Automation Engineer", "reason": "Resume"},
        {"field_id": "ff-scan-2", "value": "Part time"},
        {"field_id": "ff-scan-3", "value": True},
        {"field_id": "ff-unknown", "value": "ignored"},
        {"field_id": "ff-scan-2", "value": "Not an option"},
    ]}
    mappings = validate_mappings(FIELDS, proposed)
    assert [item["field_id"] for item in mappings] == [
        "ff-scan-1", "ff-scan-2", "ff-scan-3"]
    assert mappings[1]["value"] == "part"
    assert parse_mapping_reply(json.dumps(proposed), FIELDS) == mappings


def test_mapping_prompt_contains_form_controls_but_no_surrounding_page():
    task = {"purpose": "Update a profile", "source_context": "Title: Automation Engineer"}
    prompt = build_mapping_prompt(task, FIELDS)
    assert "Professional title" in prompt
    assert "Title: Automation Engineer" in prompt
    assert "document.body" not in EXTRACT_FORM_CONTROLS_JS
    assert "input,textarea,select" in EXTRACT_FORM_CONTROLS_JS
    assert "password" in EXTRACT_FORM_CONTROLS_JS
    assert "submit" in EXTRACT_FORM_CONTROLS_JS


def test_guarded_workflow_approves_domain_previews_then_fills_without_submit():
    with tempfile.TemporaryDirectory() as directory:
        cfg = Config(memory_db_path=os.path.join(directory, "memory.db"),
                     browser_assistant_profile_dir=os.path.join(directory, "browser"))
        controller = FakeController()
        assistant = BrowserAssistant(cfg, controller=controller)
        task = assistant.store.create_task(
            "https://forms.example/profile", "Update profile", "Fill visible fields",
            "Professional title: Automation Engineer; Availability: Part time")

        opened = assistant.approve_domain(task["id"])
        assert opened["status"] == "browser_ready"
        planned = assistant.scan_and_plan(task["id"], lambda system, user: json.dumps({
            "mappings": [
                {"field_id": "ff-scan-1", "value": "Automation Engineer"},
                {"field_id": "ff-scan-2", "value": "Part time"},
            ]
        }))
        assert planned["status"] == "review_ready"
        filled = assistant.fill(task["id"])
        assert filled["status"] == "filled"
        operations = [item[0] for item in controller.calls]
        assert operations == ["allow", "open", "scan", "fill"]
        assert "click" not in operations and "submit" not in operations

        closed = assistant.close(task["id"])
        assert closed["status"] == "cancelled"
        assert controller.calls[-1][0] == "close"


def test_upwork_can_never_become_a_browser_task():
    with tempfile.TemporaryDirectory() as directory:
        store = BrowserTaskStore(os.path.join(directory, "memory.db"))
        with pytest.raises(BrowserPolicyError):
            store.create_task("https://upwork.com/nx/profile", "Profile", "Fill", "facts")
