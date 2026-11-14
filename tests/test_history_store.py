"""Durable history checks with synthetic jobs; no source or model calls."""
import base64
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from quotelens.storage import Store


class HistoryStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="quotelens-history-qa-")
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "history.sqlite3"
        self.store = Store(self.path)

    def create(self, identifier, status="running", created_at="2026-01-01T00:00:00Z", mode="live", **payload):
        job = {"id": identifier, "status": status, "created_at": created_at, "events": []}
        self.store.create_job(job, {"mode": mode, "description": "QA " + identifier,
                                    "quantity": 3, "source_ids": ["prist"], **payload})
        return job

    def test_pending_original_form_is_durable_and_safe(self):
        self.create("qa-pending", description="QA исходный запрос", quantity=2.5,
                    source_ids=["prist", "web"], api_key="qa-secret", source_urls=["https://qa.example/private"])
        job = Store(self.path).get_job("qa-pending")
        self.assertEqual(job["request"], {"description": "QA исходный запрос", "quantity": 2.5,
                                          "source_ids": ["prist", "web"]})
        item = Store(self.path).job_history()["items"][0]
        self.assertEqual(item["request"], job["request"])
        self.assertEqual(item["description"], "QA исходный запрос")
        self.assertEqual(item["quantity"], 2.5)
        self.assertEqual(item["offers_count"], 0)
        self.assertIsNone(item["calculation_status"])
        self.assertIsNone(item["total_price"])
        self.assertIsNone(item["latest_event"])
        self.assertIsNone(item["error"])
        self.assertNotIn("qa-secret", json.dumps(item))
        self.assertNotIn("source_urls", json.dumps(item))

    def test_payload_wins_over_derived_request_and_legacy_request_can_fall_back(self):
        self.create("qa-original", quantity=4, source_ids=None)
        result = {"request": {"description": "QA derived description", "quantity": 90, "source_ids": ["souz"],
                              "requirements": [{"model_input": "QA private"}]}, "offers": []}
        self.store.save_result("qa-original", result)
        expected = {"description": "QA qa-original", "quantity": 4, "source_ids": None}
        self.assertEqual(self.store.get_job("qa-original")["request"], expected)
        self.store.create_job({"id": "qa-legacy", "status": "complete"}, {"mode": "live"})
        self.store.save_result("qa-legacy", result)
        legacy = {key: result["request"][key] for key in ("description", "quantity", "source_ids")}
        history = {item["id"]: item for item in self.store.job_history()["items"]}
        self.assertEqual(history["qa-original"]["request"], expected)
        self.assertEqual(history["qa-legacy"]["request"], legacy)
        self.assertEqual(Store(self.path).get_job("qa-legacy")["request"], legacy)

    def test_all_live_statuses_visible_and_internal_snapshot_rows_are_excluded(self):
        statuses = ["running", "complete", "cancelled", "failed", "interrupted"]
        for index, status in enumerate(statuses):
            job = self.create(f"qa-{index}", status)
            if status == "failed":
                job["error"] = "QA ошибка"
                self.store.save_job(job)
        self.create("qa-snapshot", mode="snapshot")
        self.create("qa-discovery", mode="discovery")
        self.store.create_job({"id": "qa-bookkeeping", "status": "complete"}, {})
        history = Store(self.path).job_history()
        self.assertEqual(history["total"], len(statuses))
        self.assertEqual([item["id"] for item in history["items"]], [f"qa-{index}" for index in range(4, -1, -1)])
        self.assertEqual({item["status"] for item in history["items"]}, set(statuses))
        self.assertIsNone(history["next_cursor"])
        failed = next(item for item in history["items"] if item["status"] == "failed")
        self.assertEqual(failed["error"], "QA ошибка")

    def test_keyset_pagination_retains_all_tied_rows_when_new_jobs_arrive(self):
        for index in range(9):
            self.create(f"qa-{index:02d}")
        self.create("qa-older", created_at="2025-12-31T23:59:59Z")
        first = self.store.job_history(limit=3)
        self.assertEqual(first["total"], 10)
        self.assertEqual([item["id"] for item in first["items"]], ["qa-08", "qa-07", "qa-06"])
        self.assertIsInstance(first["next_cursor"], str)
        self.create("qa-newer", created_at="2026-01-02T00:00:00Z")
        self.create("qa-99")  # A new row at the same timestamp also belongs ahead of the cursor.
        seen = [item["id"] for item in first["items"]]
        cursor = first["next_cursor"]
        while cursor is not None:
            page = Store(self.path).job_history(limit=3, cursor=cursor)
            self.assertEqual(page["total"], 12)
            seen.extend(item["id"] for item in page["items"])
            cursor = page["next_cursor"]
        self.assertEqual(seen, [f"qa-{index:02d}" for index in range(8, -1, -1)] + ["qa-older"])
        self.assertEqual(len(seen), len(set(seen)))
        refreshed = self.store.job_history(limit=2)
        self.assertEqual([item["id"] for item in refreshed["items"]], ["qa-newer", "qa-99"])

    def test_summary_uses_no_full_job_or_result_reads(self):
        job = self.create("qa-result", status="complete")
        huge = "QA-private-evidence-" * 20_000
        result = {"request": {"description": "QA derived", "quantity": 99},
                  "offers": [{"id": "qa-offer", "title": "QA synthetic offer", "price": 25,
                              "html": huge, "image": huge,
                              "evidence": {"price": {"quote": huge, "url": "https://qa.example/item"}}}],
                  "calculation": {"status": "ready", "n": 1, "total": 75}, "model_inputs": huge}
        job.update(result=result, error=None)
        self.store.save_job(job)
        self.store.add_event("qa-result", {"time": "2026-01-01T00:00:01Z", "kind": "info", "message": "QA first"})
        self.store.add_event("qa-result", {"time": "2026-01-01T00:00:02Z", "kind": "success", "message": "QA finished",
                                            "html": huge, "image": huge, "evidence": huge})
        self.store.append_review("qa-result", "qa-offer", "exclude", "QA synthetic review")
        original_loads = json.loads

        def bounded_loads(value, *args, **kwargs):
            # History may decode a cursor or source_ids, never a persisted replay.
            self.assertLess(len(value), 1000)
            return original_loads(value, *args, **kwargs)

        with patch.object(self.store, "get_job", side_effect=AssertionError("full get_job read")), \
                patch("quotelens.storage.json.loads", side_effect=bounded_loads):
            item = self.store.job_history()["items"][0]
        self.assertEqual(item["offers_count"], 1)
        self.assertEqual(item["calculation_status"], "ready")
        self.assertEqual(item["total_price"], 75)
        self.assertEqual(item["revision"], 1)
        self.assertEqual(item["latest_event"], {"time": "2026-01-01T00:00:02Z", "message": "QA finished", "kind": "success"})
        self.assertEqual(set(item), {"id", "status", "created_at", "updated_at", "revision", "request", "description",
                                     "quantity", "offers_count", "calculation_status", "total_price", "latest_event", "error"})
        self.assertLess(len(json.dumps(item)), 1500)
        self.assertNotIn("QA-private-evidence", json.dumps(item))

    def test_history_read_does_not_reserve_quota_and_recovery_is_visible(self):
        self.create("qa-interrupted")
        self.assertTrue(self.store.quota_reserve("search_runs", 1, 3))
        self.assertEqual(Store(self.path).recover_interrupted(), 1)
        for _ in range(2):
            item = Store(self.path).job_history()["items"][0]
            self.assertEqual(item["status"], "interrupted")
            self.assertIn("перезапуск", item["error"])
            self.assertIn("перезапуск", item["latest_event"]["message"])
        self.assertEqual(self.store.quota_usage("search_runs"), 1)

    def test_empty_pages_and_invalid_pagination(self):
        self.assertEqual(self.store.job_history(), {"items": [], "next_cursor": None, "total": 0})
        for limit in [0, -1, 51, True, 2.5, "3", None]:
            with self.subTest(limit=limit), self.assertRaises(ValueError):
                self.store.job_history(limit=limit)
        invalid = ["", "not-base64!", "====", "a", "a" * 4097, False, 1]
        invalid.append(base64.urlsafe_b64encode(("[" * 1200 + "]" * 1200).encode()).rstrip(b"=").decode())
        for value in [None, {}, ["2026-01-01T00:00:00Z"], ["invalid-date", "qa"],
                      ["2026-01-01T00:00:00Z", ""], ["2026-01-01T00:00:00Z", 1],
                      ["2026-01-01T00:00:00Z", "qa", "extra"]]:
            invalid.append(base64.urlsafe_b64encode(json.dumps(value).encode()).rstrip(b"=").decode())
        for cursor in invalid:
            with self.subTest(cursor=cursor), self.assertRaises(ValueError):
                self.store.job_history(cursor=cursor)

    def test_existing_schema_gains_non_destructive_history_index(self):
        self.create("qa-kept")
        with sqlite3.connect(self.path) as db:
            db.execute("DROP INDEX jobs_live_history")
        reopened = Store(self.path)
        self.assertEqual(reopened.get_job("qa-kept")["request"]["quantity"], 3)
        with sqlite3.connect(self.path) as db:
            indexes = {row[1] for row in db.execute("PRAGMA index_list(jobs)")}
            plan = db.execute("EXPLAIN QUERY PLAN SELECT id FROM jobs WHERE json_extract(payload_json,'$.mode')='live' "
                              "AND (created_at,id)<(?,?) ORDER BY created_at DESC,id DESC LIMIT 3",
                              ("2026-01-01T00:00:00Z", "qa-kept")).fetchall()
        self.assertIn("jobs_live_history", indexes)
        self.assertTrue(any("jobs_live_history" in row[3] for row in plan), plan)


if __name__ == "__main__":
    unittest.main()
