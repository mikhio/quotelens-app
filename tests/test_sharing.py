from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from quotelens.sharing import sanitize_snapshot, reader_html
from quotelens.storage import Store
from quotelens.review import RevisionConflict
from tests.test_review_acceptances import fixture, JOB


class SharingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name) / "qa.sqlite3")
        self.result, self.payload, self.identifier = fixture()
        self.store.create_job({"id": JOB, "status": "complete", "result": self.result}, self.payload)
        self.store.add_document(JOB, {"url": "https://example.com/goods", "title": "Source", "text": "<script>evil()</script> fixed source", "retrieved_at": "2026-01-01T00:00:00Z"})

    def test_immutable_snapshot_and_safe_reader_exclude_owner_data(self):
        result = deepcopy(self.result)
        result.update(events=[{"private": "owner-event"}], input_documents=[{"text": "private attachment"}], provider_key="private-key")
        result["search"].update(model="private model", budget={"actual_usage": {"cost": 88}}, resume_state={"private": "resume secret"})
        snapshot, docs = sanitize_snapshot(result, self.store.list_documents(JOB), ["Synthetic query"])
        raw = json.dumps(snapshot)
        for private in ("owner-event", "private attachment", "private-key", "private model", "resume secret", "actual_usage", "base_result"):
            self.assertNotIn(private, raw)
        receipt = self.store.create_share(JOB, 0)
        self.assertEqual(len(receipt["token"]), 43)
        first = self.store.get_share(receipt["token"])
        self.store.apply_review(JOB, self.identifier, "accept_deviation", "QA acceptance after sharing", fields={"attributes.channels": True}, expected_revision=0)
        self.assertEqual(self.store.get_share(receipt["token"]), first)
        html = reader_html(first["result"], first["documents"], revision=0, created_at=receipt["created_at"])
        self.assertIn("&lt;script&gt;evil()&lt;/script&gt;", html)
        self.assertNotIn("<script", html)
        self.assertNotIn(JOB, html)
        self.assertNotIn("/api/jobs", html)
        self.assertNotIn("<form", html)
        self.assertNotIn("actual_usage", html)
        self.assertIn("SHA256", html)
        self.assertIn("Отброшено", html)

    def test_share_requires_current_revision_and_terminal_result(self):
        with self.assertRaises(RevisionConflict): self.store.create_share(JOB, 1)
        self.store.begin_continuation(JOB, 20, 0)
        with self.assertRaises(ValueError): self.store.create_share(JOB, 1)
        self.assertIsNone(self.store.get_share("missing"))

    def test_reader_preserves_fractional_unit_price_and_source_currency(self):
        result = deepcopy(self.result)
        for offer in result["items"][0]["offers"]:
            offer["price"] = .0004
        snapshot, docs = sanitize_snapshot(result)
        page = reader_html(snapshot)
        self.assertIn("0,0004 ₽", page)
        self.assertNotIn("<strong>0,00 ₽</strong>", page)
        result["items"][0]["offers"][0]["currency"] = "USD"
        result["items"][0]["offers"][1]["currency"] = None
        snapshot, _ = sanitize_snapshot(result)
        page = reader_html(snapshot)
        self.assertIn("0,0004 USD", page)
        self.assertIn("0,0004 валюта не подтверждена", page)


if __name__ == "__main__": unittest.main()
