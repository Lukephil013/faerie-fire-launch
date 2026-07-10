"""Tests for the temporal memory graph + supersession."""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from livingpc.memory import MemoryStore, association_evidence  # noqa: E402


def test_add_and_active():
    with tempfile.TemporaryDirectory() as d:
        m = MemoryStore(os.path.join(d, "m.db"))
        mid = m.add("League of Legends", "champion pool", "Jinx, Jhin")
        active = m.active()
        assert len(active) == 1
        assert active[0]["value"] == "Jinx, Jhin"
        assert active[0]["status"] == "active"
        assert m.get(mid)["valid_to"] is None
        m.close()


def test_supersession_preserves_trajectory():
    with tempfile.TemporaryDirectory() as d:
        m = MemoryStore(os.path.join(d, "m.db"))
        old = m.add("League of Legends", "champion pool", "Jinx, Jhin")
        new = m.supersede(old, "Caitlyn, Tristana")

        # old is closed + linked; new is active
        old_row = m.get(old)
        new_row = m.get(new)
        assert old_row["status"] == "superseded"
        assert old_row["valid_to"] is not None
        assert new_row["status"] == "active"
        assert new_row["supersedes_id"] == old
        assert new_row["valid_to"] is None

        # only one active fact for that attribute
        active = m.active("League of Legends")
        assert len(active) == 1
        assert active[0]["value"] == "Caitlyn, Tristana"

        # but the full history shows both -> the trajectory is preserved
        hist = m.history("League of Legends", "champion pool")
        assert [h["value"] for h in hist] == ["Jinx, Jhin", "Caitlyn, Tristana"]
        m.close()


def test_categories_autocreated():
    with tempfile.TemporaryDirectory() as d:
        m = MemoryStore(os.path.join(d, "m.db"))
        m.add("Korean study", "resources", "Anki, TTMIK")
        names = [c["name"] for c in m.categories()]
        assert "Korean study" in names
        m.close()


def test_association_evidence_is_explainable_and_deterministic():
    left = {
        "id": 1, "category": "Korean study", "attribute": "study resources",
        "value": "Anki and TTMIK", "supersedes_id": None,
    }
    right = {
        "id": 2, "category": "Korean study", "attribute": "study routine",
        "value": "Use Anki every morning", "supersedes_id": None,
    }
    first = association_evidence(left, right)
    assert first == association_evidence(left, right)
    assert first["method"] == "deterministic-v1"
    assert first["components"]["same_category"] > 0
    assert first["components"]["attribute_overlap"] > 0
    assert first["components"]["value_overlap"] > 0
    assert 0 < first["strength"] <= 1


def test_proposed_associations_respect_review_decisions():
    with tempfile.TemporaryDirectory() as d:
        m = MemoryStore(os.path.join(d, "m.db"))
        first = m.add("Korean study", "study resources", "Anki and TTMIK")
        second = m.add("Korean study", "study routine", "Use Anki every morning")
        m.add("Cooking", "favorite meal", "Tomato soup")

        assert m.propose_associations(min_strength=0.30) == 1
        edge = m.list_associations()[0]
        assert {edge["source_id"], edge["target_id"]} == {first, second}
        assert edge["status"] == "proposed"

        m.update_association(edge["id"], status="rejected")
        assert m.propose_associations(min_strength=0.30) == 0
        assert m.list_associations()[0]["status"] == "rejected"
        m.close()


def test_graph_data_and_manual_association_round_trip():
    with tempfile.TemporaryDirectory() as d:
        m = MemoryStore(os.path.join(d, "m.db"))
        first = m.add("Work", "project", "Faerie Fire", confidence=0.9)
        second = m.add("Work", "priority", "Build memory graph", confidence=0.8)
        edge_id = m.add_association(
            first, second, relation_type="supports", strength=0.72,
            directed=True, status="approved",
        )
        graph = m.graph_data()
        assert len(graph["nodes"]) == 2
        assert graph["nodes"][0]["value"]
        assert graph["edges"] == [{
            "id": edge_id,
            "source_id": first,
            "target_id": second,
            "relation_type": "supports",
            "directed": True,
            "strength": 0.72,
            "status": "approved",
            "evidence": {"method": "manual"},
        }]
        m.close()


def test_directed_association_preserves_source_and_target_order():
    with tempfile.TemporaryDirectory() as d:
        m = MemoryStore(os.path.join(d, "m.db"))
        first = m.add("Project", "result", "A working graph")
        second = m.add("Project", "cause", "Approved associations")
        m.add_association(second, first, relation_type="causes", strength=0.8)
        edge = m.graph_data()["edges"][0]
        assert edge["source_id"] == second
        assert edge["target_id"] == first
        assert edge["directed"] is True
        m.close()


def test_core_profile_round_trip_and_retire():
    with tempfile.TemporaryDirectory() as d:
        m = MemoryStore(os.path.join(d, "m.db"))
        fact_id = m.upsert_core_profile_fact(
            "Current Reality",
            "current work situation",
            "I have a job and cannot treat income as optional.",
            priority=96,
            source_kind="soul_calibration",
        )
        assert m.core_profile_facts()[0]["id"] == fact_id
        assert m.core_profile_facts()[0]["value"] == (
            "I have a job and cannot treat income as optional.")
        assert "current work situation" in m.core_profile_block()

        same_id = m.upsert_core_profile_fact(
            "Current Reality",
            "current work situation",
            "I still have a job; proposals must respect replacement-income constraints.",
            priority=98,
        )
        facts = m.core_profile_facts()
        assert same_id == fact_id
        assert len(facts) == 1
        assert facts[0]["priority"] == 98
        assert "replacement-income" in facts[0]["value"]

        m.retire_core_profile_fact(fact_id)
        assert m.core_profile_facts() == []
        retired = m.core_profile_facts(active_only=False)
        assert retired[0]["status"] == "retired"
        m.close()


def test_core_profile_can_retire_by_key():
    with tempfile.TemporaryDirectory() as d:
        m = MemoryStore(os.path.join(d, "m.db"))
        m.upsert_core_profile_fact("Core Identity", "other essential context",
                                   "I need beauty and play.")
        assert m.retire_core_profile_fact_key(
            "Core Identity", "other essential context") == 1
        assert m.retire_core_profile_fact_key(
            "Core Identity", "other essential context") == 0
        assert m.core_profile_facts() == []
        m.close()


if __name__ == "__main__":
    import traceback
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    fails = 0
    for fn in fns:
        try:
            fn(); print(f"PASS {fn.__name__}")
        except Exception:
            fails += 1; print(f"FAIL {fn.__name__}"); traceback.print_exc()
    print(f"\n{len(fns)-fails}/{len(fns)} passed")
    sys.exit(1 if fails else 0)
