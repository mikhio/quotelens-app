"""SQLite persistence checks; all documents and offers below are QA fixtures."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from quotelens.engine import parse_request
from quotelens.selection import finalize
from quotelens.storage import SCHEMA_VERSION, Store


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="quotelens-store-qa-")
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "store.sqlite3"
        self.store = Store(self.path)
        self.store.create_job({"id": "qa-job", "status": "running", "created_at": "2026-01-01T00:00:00Z", "events": []}, {"description": "QA"})

    def result(self):
        request = parse_request("Уборка помещений за м²", 10)
        offers = []
        for index, price in enumerate([10, 20, 30]):
            url = f"https://seller{index}.example/qa-service"
            offers.append({"id": f"qa-{index}", "title": "QA Уборка помещений", "supplier": f"QA продавец {index}",
                           "url": url, "retrieved_at": "2026-01-01T00:00:00Z", "provenance": "synthetic",
                           "price": price, "currency": "RUB", "unit": "м²", "tax": "included",
                           "document_id": "qa-doc", "source_hash": "a" * 64,
                           "attributes": {"service_type": "уборка помещений"},
                           "evidence": {field: {"quote": "QA fixture", "url": url} for field in ["price", "tax", "unit", "currency", "attributes.service_type"]}})
        return finalize(request, offers)

    def test_restart_preserves_jobs_events_steps_and_raw_document(self):
        self.store.add_event("qa-job", {"kind": "info", "message": "QA event"})
        step = self.store.add_step("qa-job", {"kind": "tool", "tool": "open", "arguments": {"url": "https://qa.example/item"}})
        document = {"url": "https://qa.example/item", "title": "QA товар", "text": "QA raw text чайник 2000 Вт", "html": "<h1>QA товар</h1>", "method": "http"}
        docid = self.store.add_document("qa-job", document)
        second = Store(self.path)
        self.assertEqual(second.get_job("qa-job")["events"][0]["message"], "QA event")
        saved = second.get_document("qa-job", docid)
        self.assertEqual(saved["text"], document["text"])
        self.assertEqual(saved["html"], document["html"])
        self.assertEqual(len(saved["sha256"]), 64)
        self.assertEqual(second.add_document("qa-job", saved), docid)
        meta = second.list_documents("qa-job")
        self.assertEqual(len(meta), 1)
        self.assertNotIn("text", meta[0])
        self.assertNotIn("html", meta[0])
        self.assertIsNone(second.get_document("different-job", docid))
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute("SELECT tool FROM steps WHERE id=?", (step,)).fetchone()[0], "open")
            self.assertEqual(db.execute("PRAGMA journal_mode").fetchone()[0], "wal")
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)

    def test_local_fts_is_job_scoped_bounded_and_handles_query_syntax(self):
        self.store.add_document("qa-job", {"url": "https://qa.example/one", "text": "QA Чайник мощность 2000 Вт", "title": "Чайник"})
        self.store.create_job({"id": "other-job", "status": "complete"}, {})
        self.store.add_document("other-job", {"url": "https://qa.example/two", "text": "секретный для другого задания чайник", "title": "Чайник"})
        matches = self.store.search_documents("qa-job", 'чайник OR ("мощность") NEAR()', limit=1)
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]["url"], "https://qa.example/one")
        self.assertIn("snippet", matches[0])
        self.assertEqual(self.store.search_documents("qa-job", "!<>:"), [])

    def test_save_result_normalizes_entities_and_keeps_replay_inputs(self):
        result = self.result()
        self.store.save_result("qa-job", result)
        self.assertEqual(Store(self.path).get_job("qa-job")["result"], result)
        with sqlite3.connect(self.path) as db:
            for table, expected in [("offers", 3), ("decisions", 3), ("evidence", 15), ("groups", 4), ("calculations", 1)]:
                self.assertEqual(db.execute("SELECT count(*) FROM " + table).fetchone()[0], expected, table)
            self.assertEqual(db.execute("SELECT n,total FROM calculations").fetchone(), (3, 200))
        self.store.save_result("qa-job", result)
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM evidence").fetchone()[0], 15)

    def test_malformed_offer_values_are_quarantined_without_becoming_prices(self):
        result = self.result()
        result["offers"][0]["price"] = "not a price"
        result["offers"][1]["evidence"]["price"] = {"quote": 15}
        result["offers"].append(["malformed QA"])
        self.store.save_result("qa-job", result)
        with sqlite3.connect(self.path) as db:
            self.assertIsNone(db.execute("SELECT price FROM offers WHERE id='qa-0'").fetchone()[0])
            self.assertEqual(db.execute("SELECT count(*) FROM quarantine").fetchone()[0], 3)
        self.assertEqual(len(self.store.get_job("qa-job")["result"]["offers"]), 4)

    def test_atomic_durable_quota_across_multiple_store_instances(self):
        stores = [Store(self.path) for _ in range(4)]
        with ThreadPoolExecutor(max_workers=8) as pool:
            reserved = list(pool.map(lambda index: stores[index % 4].quota_reserve("paid_calls", 1, 7), range(32)))
        self.assertEqual(sum(reserved), 7)
        self.assertEqual(Store(self.path).quota_usage("paid_calls"), 7)
        self.assertFalse(self.store.quota_reserve("paid_calls", 1, 7))
        self.assertTrue(self.store.quota_reserve("tokens", 30, 30, period="all"))
        self.assertEqual(Store(self.path).quota_usage("tokens", "lifetime"), 30)
        self.assertFalse(self.store.quota_reserve("tokens", 1, 30, "lifetime"))
        for bad in [-1, True, 0.5]:
            with self.assertRaises(ValueError):
                self.store.quota_reserve("bad", bad, 5)

    def test_nonfinite_and_structured_scalar_fields_are_quarantined(self):
        result = self.result()
        result["offers"][0].update(price=float("nan"), supplier={"bad": "QA"}, status=["invalid"])
        self.store.save_result("qa-job", result)
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute("SELECT price,supplier FROM offers WHERE id='qa-0'").fetchone(), (None, None))
            self.assertEqual(db.execute("SELECT status FROM decisions WHERE offer_id='qa-0'").fetchone()[0], "uncertain")
            self.assertEqual(db.execute("SELECT count(*) FROM quarantine").fetchone()[0], 3)

    def test_quota_release_returns_freed_amount_and_never_goes_below_zero(self):
        self.assertTrue(self.store.quota_reserve("tokens", 20, 30, "all"))
        self.assertEqual(self.store.quota_release("tokens", 7, "lifetime"), 7)
        self.assertEqual(Store(self.path).quota_usage("tokens", "all"), 13)
        self.assertEqual(self.store.quota_release("tokens", 99, "all"), 13)
        self.assertEqual(self.store.quota_release("tokens", 1, "all"), 0)
        self.assertEqual(self.store.quota_release("missing", 4), 0)
        self.assertEqual(self.store.quota_usage("tokens", "all"), 0)
        with self.assertRaises(ValueError):
            self.store.quota_release("tokens", -1)

    def test_concurrent_quota_reserve_and_release_do_not_lose_reservations(self):
        stores = [Store(self.path) for _ in range(4)]
        self.assertTrue(self.store.quota_reserve("qa-shared", 2, 5, "all"))
        def reserve_and_settle(index):
            store = stores[index % len(stores)]
            reserved = store.quota_reserve("qa-shared", 1, 5, "lifetime")
            released = store.quota_release("qa-shared", 1, "all") if reserved else 0
            return int(reserved), released
        with ThreadPoolExecutor(max_workers=8) as pool:
            operations = list(pool.map(reserve_and_settle, range(40)))
        self.assertEqual(sum(item[0] for item in operations), sum(item[1] for item in operations))
        self.assertEqual(Store(self.path).quota_usage("qa-shared", "all"), 2)

    def test_recovery_marks_running_jobs_interrupted_once(self):
        self.store.create_job({"id": "complete-job", "status": "complete"}, {})
        self.assertEqual(Store(self.path).recover_interrupted(), 1)
        job = self.store.get_job("qa-job")
        self.assertEqual(job["status"], "interrupted")
        self.assertIn("перезапуск", job["error"])
        self.assertEqual(self.store.get_job("complete-job")["status"], "complete")
        self.assertEqual(self.store.recover_interrupted(), 0)

    def test_review_audit_revision_survives_save_and_restart(self):
        self.store.save_result("qa-job", self.result())
        stale = self.store.get_job("qa-job")
        self.assertEqual(self.store.append_review("qa-job", "qa-0", "exclude", "QA review"), 1)
        self.store.save_job(stale)
        job = Store(self.path).get_job("qa-job")
        self.assertEqual(job["revision"], 1)
        self.assertEqual(job["excluded_ids"], ["qa-0"])
        self.assertEqual(self.store.append_review("qa-job", "qa-0", "reset", "QA reset"), 2)
        job = Store(self.path).get_job("qa-job")
        self.assertEqual(job["excluded_ids"], [])
        self.assertEqual(len(job["reviews"]), 2)
        with self.assertRaises(ValueError):
            self.store.append_review("qa-job", "missing", "exclude", "QA")
        with self.assertRaises(ValueError):
            self.store.append_review("qa-job", "qa-0", "include", "QA")

    def test_secret_fields_and_non_json_cancellation_objects_are_never_saved(self):
        import threading
        job = self.store.get_job("qa-job")
        job.update(cancel=threading.Event(), api_key="qa-secret", notes="Bearer qa.token.value", result={"api_key": "nested-secret"})
        self.store.save_job(job)
        restored = Store(self.path).get_job("qa-job")
        self.assertNotIn("cancel", restored)
        self.assertNotIn("api_key", restored)
        self.assertEqual(restored["notes"], "[redacted]")
        with sqlite3.connect(self.path) as db:
            raw = json.dumps(db.execute("SELECT job_json,result_json FROM jobs").fetchall())
        self.assertNotIn("qa-secret", raw)
        self.assertNotIn("nested-secret", raw)


if __name__ == "__main__":
    unittest.main()
