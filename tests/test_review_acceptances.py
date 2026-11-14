from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from quotelens.procurement import aggregate_results
from quotelens.scenarios import load_scenario
from quotelens.selection import finalize
from quotelens.storage import Store
from quotelens.review import RevisionConflict

JOB = "d" * 24


def fixture():
    sample = load_scenario("synthetic-comparable")
    request, offers = deepcopy(sample["request"]), deepcopy(sample["offers"][:3])
    offers[0]["attributes"]["channels"] = 2
    line = finalize(request, offers)
    line["search"] = {"budget": {"model_calls": 0, "actual_cost_known": True, "actual_usage": {}}, "source_statuses": []}
    item = {"id": "a", "title": "QA осциллограф", "description": request["description"], "quantity": request["quantity"], "unit": request["unit"]}
    payload = {"mode": "live", "quantity": 1, "description": "Synthetic acceptance QA", "items": [item], "search_policy": {"mode": "target", "max_seconds": 600, "max_offers": 3}}
    result = aggregate_results(payload, [item], {"a": line}, stop="source_exhausted")
    return result, payload, "a:" + offers[0]["id"]


class AcceptanceStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name) / "qa.sqlite3")
        self.result, self.payload, self.identifier = fixture()
        self.store.create_job({"id": JOB, "status": "complete", "created_at": "2026-01-01T00:00:00Z", "result": self.result}, self.payload)

    def test_accept_withdraw_and_reload_keep_source_and_raw_mismatch(self):
        base = deepcopy(self.result["items"][0]["base_result"])
        self.store.apply_review(JOB, self.identifier, "accept_deviation", "Два канала достаточны для этой задачи", fields={"attributes.channels": True}, expected_revision=0)
        job = Store(self.store.path).get_job(JOB)
        offer = job["result"]["offers"][0]
        self.assertEqual(offer["attributes"]["channels"], 2)
        self.assertEqual(offer["status"], "included")
        self.assertEqual(offer["requirement_checks"][0]["status"], "mismatch")
        self.assertTrue(offer["requirement_checks"][0]["acceptance"])
        self.assertEqual(job["result"]["items"][0]["base_result"], base)
        self.assertEqual(job["result"]["items"][0]["target_group"]["manual_count"], 1)
        self.store.apply_review(JOB, self.identifier, "accept_deviation", "Отзываю решение", fields={"attributes.channels": False}, expected_revision=1)
        withdrawn = self.store.get_job(JOB)
        self.assertEqual(withdrawn["result"]["offers"][0]["status"], "excluded")
        self.assertFalse(withdrawn["manual_acceptances"][self.identifier])
        self.assertEqual(len(withdrawn["reviews"]), 2)

    def test_commercial_waiver_and_stale_decision_leave_database_unchanged(self):
        before = self.store.get_job(JOB)
        for field in ("price", "tax", "unit", "attributes.unknown"):
            with self.assertRaises(ValueError):
                self.store.apply_review(JOB, self.identifier, "accept_deviation", "QA forbidden", fields={field: True}, expected_revision=0)
        with self.assertRaises(RevisionConflict):
            self.store.apply_review(JOB, self.identifier, "accept_deviation", "QA stale", fields={"attributes.channels": True}, expected_revision=1)
        self.assertEqual(self.store.get_job(JOB), before)

    def test_reset_removes_decisions_and_continuation_preserves_history(self):
        self.store.apply_review(JOB, self.identifier, "accept_deviation", "QA accepted", fields={"attributes.channels": True}, expected_revision=0)
        payload = self.store.begin_continuation(JOB, 45, 1)
        self.assertIn("manual_acceptances", payload["_resume_result"]["offers"][0])
        job = self.store.get_job(JOB)
        self.assertEqual(job["status"], "running")
        self.assertEqual(job["revision"], 2)
        with self.assertRaises(ValueError):
            self.store.apply_review(JOB, self.identifier, "reset", "QA during search", expected_revision=2)
        with self.assertRaises(ValueError):
            self.store.begin_continuation(JOB, 45, 2)

    def test_every_publication_keeps_exclusion_with_mixed_or_no_other_decisions(self):
        for companion in (None, "accept_deviation", "update"):
            with self.subTest(companion=companion):
                store = Store(":memory:")
                result, payload, identifier = fixture()
                store.create_job({"id": JOB, "status": "complete", "result": result}, payload)
                revision = 0
                if companion:
                    fields = {"attributes.channels": True} if companion == "accept_deviation" else {"price": result["offers"][0]["price"]}
                    store.apply_review(JOB, identifier, companion, "QA companion decision", fields=fields, expected_revision=revision)
                    revision += 1
                excluded = result["offers"][1]["id"]
                store.apply_review(JOB, excluded, "exclude", "QA exclude before continuation", expected_revision=revision)
                revision += 1
                resumed = store.begin_continuation(JOB, 20, revision)
                prior = resumed["_resume_result"]
                raw = aggregate_results(resumed, resumed["items"], {line["id"]: line for line in prior["items"]}, stop="source_exhausted")
                self.assertNotIn("review", raw)  # historically missing root exclusions
                for _ in range(2):
                    store.save_result(JOB, raw)
                    current = store.get_job(JOB)
                    offer = next(o for o in current["result"]["offers"] if o["id"] == excluded)
                    self.assertEqual(offer["status"], "excluded")
                    self.assertTrue(offer["manual_excluded"])
                    self.assertIn(excluded, current["result"]["review"]["excluded_ids"])
                    self.assertFalse(current["result"]["items"][0]["target_group"]["ready"])
                    self.assertEqual(current["result"]["items"][0]["base_result"], prior["items"][0]["base_result"])

    def test_legacy_payload_restores_all_structured_lines_and_exact_query_link_evidence(self):
        result, payload, _ = fixture()
        line = result["items"][0]
        items = [{**payload["items"][0], "id": key} for key in ("a", "b", "c")]
        result = aggregate_results({**payload, "items": items}, items, {key: deepcopy(line) for key in ("a", "b", "c")}, stop="source_exhausted")
        store = Store(":memory:")
        old_payload = {key: value for key, value in payload.items() if key not in {"items", "search_policy"}}
        store.create_job({"id": JOB, "status": "complete", "result": result}, old_payload)
        query = "QA Legacy exact query"
        store.add_step(JOB, {"kind": "tool", "name": "search_web", "item_id": "a", "arguments": {"query": query}})
        did = store.add_document(JOB, {"url": "https://example.com/old", "text": "Old captured product", "links": [{"url": "https://example.com/new", "title": "Unvisited product"}]})
        store.add_step(JOB, {"kind": "tool", "name": "open_page", "item_id": "a", "output": {"document_id": did}})
        resumed = store.begin_continuation(JOB, 30, 0)
        self.assertEqual([i["id"] for i in resumed["items"]], ["a", "b", "c"])
        self.assertEqual(resumed["_resume_queries"], [{"item_id": "a", "query": query}])
        self.assertEqual(resumed["_resume_documents"][0]["item_ids"], ["a"])
        self.assertEqual(resumed["_resume_documents"][0]["links"][0]["url"], "https://example.com/new")
        self.assertNotIn("items", old_payload)  # original historical form is preserved


if __name__ == "__main__": unittest.main()
