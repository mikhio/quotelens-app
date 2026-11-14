"""Explicit QA doubles verify orchestration, never represent market prices."""
import tempfile
import threading
import unittest
from pathlib import Path

from quotelens.agent import Research
from quotelens.storage import Store


class ToolLoopTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name) / "data.sqlite3")
        self.store.create_job({"id": "qa-job", "status": "running", "events": []}, {})
        self.payload = {"description": "Осциллограф 4 канала полоса пропускания 100 МГц", "quantity": 2}
        self.research = Research(self.store, "qa-job", self.payload, threading.Event(), lambda *a: None)

    def test_open_requires_an_actually_discovered_link(self):
        self.assertIn("error", self.research.execute_tool("open_page", {"url": "https://example.org/unknown"}))
        self.assertEqual(self.research.pages, 0)

    def test_paid_reservations_are_durable_and_block_before_network(self):
        from unittest.mock import patch
        from quotelens.agent import BudgetStop
        with patch.dict("os.environ", {"APP_MODEL_CALLS_TOTAL": "1"}):
            self.research.reserve_call([{"role": "user", "content": "QA"}])
            with self.assertRaises(BudgetStop):
                self.research.reserve_call([{"role": "user", "content": "QA"}])
        reopened = Store(Path(self.tmp.name) / "data.sqlite3")
        self.assertEqual(reopened.quota_usage("model_calls", period="all"), 1)

    def test_cancel_prevents_tools(self):
        from quotelens.agent import BudgetStop
        self.research.cancel.set()
        with self.assertRaises(BudgetStop):
            self.research.execute_tool("search_web", {"query": "QA"})
        self.assertEqual(self.research.queries, 0)

    def test_metered_reply_releases_only_the_unused_reserve_and_keeps_call_count(self):
        self.research.reserve_call([{"role": "user", "content": "QA"}])
        self.research.settle_call({"prompt_tokens": 100, "completion_tokens": 20})
        self.assertEqual(self.store.quota_usage("model_tokens", period="all"), 120)
        self.assertEqual(self.store.quota_usage("model_micro_usd", period="all"), 200)
        self.assertEqual(self.store.quota_usage("model_calls", period="all"), 1)

    def test_unknown_or_invalid_usage_never_refunds_a_paid_reserve(self):
        self.research.reserve_call([{"role": "user", "content": "QA"}])
        before = self.store.quota_usage("model_tokens", period="all")
        self.research.settle_call({})
        self.research.settle_call({"prompt_tokens": -1, "completion_tokens": 20})
        self.assertEqual(self.store.quota_usage("model_tokens", period="all"), before)

    def test_inconsistent_total_keeps_reserve_and_stops_future_paid_calls(self):
        from quotelens.agent import BudgetStop
        self.research.reserve_call([{"role": "user", "content": "QA"}])
        before = self.store.quota_usage("model_tokens", period="all")
        self.research.settle_call({"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 12000, "cost": .015})
        self.assertEqual(self.store.quota_usage("model_tokens", period="all"), before)
        with self.assertRaises(BudgetStop):
            self.research.reserve_call([{"role": "user", "content": "QA"}])

    def test_reported_cost_is_a_floor_and_nonfinite_cost_never_refunds(self):
        self.research.reserve_call([{"role": "user", "content": "QA"}])
        self.research.settle_call({"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120, "cost": .015})
        self.assertEqual(self.store.quota_usage("model_micro_usd", period="all"), 15000)
        self.research.reserve_call([{"role": "user", "content": "QA"}])
        before = self.store.quota_usage("model_micro_usd", period="all")
        self.research.settle_call({"prompt_tokens": 100, "completion_tokens": 20, "cost": float("nan")})
        self.assertEqual(self.store.quota_usage("model_micro_usd", period="all"), before)

    def test_heading_excerpt_has_real_offset_and_full_document_retained(self):
        doc = {"text": "QA product menu " + "header " * 700 + "QA product price 100", "title": "QA product"}
        brief = self.research.brief_document(doc)
        self.assertGreater(brief["offset"], 4000)
        self.assertIn("price 100", brief["text"])
        self.assertEqual(brief["text"], doc["text"][brief["offset"]:brief["offset"] + 5500])

    def test_missing_provider_does_not_replay_a_saved_catalog(self):
        from unittest.mock import patch
        with patch.dict("os.environ", {}, clear=True):
            research = Research(self.store, "qa-job", self.payload, threading.Event(), lambda *a: None)
            result = research.run()
        self.assertEqual(result["offers"], [])
        self.assertEqual(result["search"]["stop"]["code"], "provider_unavailable")
        self.assertEqual(result["search"]["budget"]["model_calls"], 0)


if __name__ == "__main__":
    unittest.main()
