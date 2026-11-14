"""Cold saved-job reads and supplier evidence, using fabricated captures only.

Run in a managed test workspace. These tests do not read production data or
perform public fetches, paid searches, model calls or source recapture.
"""
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import hashlib
import threading
import time
import unittest
from unittest.mock import patch
from urllib.parse import quote

from quotelens import server, supplier
from tests.test_history_api import DESCRIPTION, HistoryServerFixture


class SavedJobReadTests(unittest.TestCase):
    def setUp(self):
        cache = patch.object(server, "SUPPLIER_CACHE", OrderedDict())
        cache.start()
        self.addCleanup(cache.stop)
        self.http = HistoryServerFixture()
        self.addCleanup(self.http.close)
        self.job_id = self.http.seed(901, description=DESCRIPTION)
        captured = {
            "url": "https://supplier.example/product/qa-scope/",
            "title": "Учебный осциллограф",
            "html": '<main>Учебный осциллограф</main><footer>Реквизиты: ООО «QA Поставщик». '
                    'ИНН 1234567890. Email: sales@supplier.example.</footer>',
            "text": 'Учебный осциллограф Реквизиты: ООО «QA Поставщик». '
                    'ИНН 1234567890. Email: sales@supplier.example.',
            "retrieved_at": "2026-01-02T03:04:05Z",
        }
        self.document_id = self.http.db.add_document(self.job_id, captured)
        self.document = self.http.db.get_document(self.job_id, self.document_id)
        job = self.http.db.get_job(self.job_id)
        result = job["result"]
        for offer in result["offers"]:
            offer["supplier_details"] = {}
        offer = result["offers"][0]
        self.offer_id = offer["id"]
        offer.pop("supplier_details")
        offer.update(document_id=self.document_id, source_hash=self.document["sha256"])
        self.http.db.save_result(self.job_id, result)
        self.path = "/api/jobs/" + self.job_id
        self.supplier_path = self.path + "/suppliers/" + quote(self.offer_id, safe="")

    def persistent_snapshot(self):
        """All persistent table rows, including quotas/audit, not WAL bookkeeping."""
        with self.http.db._connection() as db:
            names = [row[0] for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
            return {name: [tuple(row) for row in db.execute(
                'SELECT * FROM "' + name.replace('"', '""') + '"')]
                for name in names}

    def test_cold_job_and_json_report_do_not_parse_html_or_change_persistent_state(self):
        before = self.persistent_snapshot()
        original = self.http.db.get_job(self.job_id)
        with patch.object(supplier, "supplier_details", side_effect=AssertionError("Bulk HTML parse on status GET")), \
                patch.object(server, "run_search", side_effect=AssertionError("Search on GET")):
            for path in (self.path, self.path + "/report.json"):
                status, body, _ = self.http.request(path)
                self.assertEqual(status, 200)
                result = body["result"] if path == self.path else body
                self.assertEqual(result["calculation"], original["result"]["calculation"])
                self.assertNotIn("supplier_details", result["offers"][0])
                self.assertEqual(result.get("base_result"), original["result"].get("base_result"))
                self.assertEqual([(item.get("id"), item.get("base_result")) for item in result.get("items", [])],
                                 [(item.get("id"), item.get("base_result")) for item in original["result"].get("items", [])])
        self.assertEqual(self.persistent_snapshot(), before)
        self.assertEqual(self.http.count("POST", "/api/search"), 0)

    def test_selected_supplier_uses_only_its_capture_and_cached_facts_are_detached(self):
        before = self.persistent_snapshot()
        parse = supplier.supplier_details
        with patch.object(supplier, "supplier_details", wraps=parse) as calls, \
                patch.object(server, "run_search", side_effect=AssertionError("Search on supplier read")):
            status, first, _ = self.http.request(self.supplier_path)
            self.assertEqual(status, 200)
            self.assertEqual(first["offer_id"], self.offer_id)
            details = first["supplier_details"]
            self.assertEqual(details["inn"], "1234567890")
            self.assertEqual(details["legal_name"], "ООО «QA Поставщик»")
            for proof in details["evidence"].values():
                self.assertIn(proof["quote"], self.document["text"])
                self.assertEqual(proof["source_hash"], self.document["sha256"])
                self.assertEqual(proof["document_id"], self.document_id)
                self.assertEqual(proof["retrieved_at"], self.document["retrieved_at"])
            first["supplier_details"]["inn"] = "changed in client"
            status, second, _ = self.http.request(self.supplier_path)
            self.assertEqual(status, 200)
            self.assertEqual(second["supplier_details"]["inn"], "1234567890")
            self.assertEqual(calls.call_count, 1)
        self.assertEqual(self.persistent_snapshot(), before)
        self.assertEqual(self.http.db.get_document(self.job_id, self.document_id), self.document)

    def test_slow_supplier_does_not_block_history_active_status_events_or_cancellation(self):
        active = self.http.submit("QA: отдельный управляемый поиск " + DESCRIPTION)
        entered, release = threading.Event(), threading.Event()
        actual = supplier.supplier_details
        original = self.http.db.get_job(self.job_id)
        original_audit = self.http.db.search_audit(self.job_id)

        def blocked(document):
            entered.set()
            if not release.wait(12):
                raise AssertionError("Test did not release supplier extraction")
            return actual(document)

        with patch.object(supplier, "supplier_details", side_effect=blocked):
            with ThreadPoolExecutor(max_workers=1) as pool:
                reading = pool.submit(self.http.request, self.supplier_path)
                try:
                    self.assertTrue(entered.wait(3), "Supplier read did not start")
                    start = time.monotonic()
                    self.assertEqual(self.http.request("/api/jobs")[0], 200)
                    status, current, _ = self.http.request("/api/jobs/" + active["id"])
                    self.assertEqual(status, 200)
                    self.assertEqual(current["status"], "running")
                    with server.LOCK:
                        event = {"time": "2026-01-01T00:00:00Z", "kind": "info", "message": "QA: event during supplier parsing"}
                        server.JOBS[active["id"]]["events"].append(event)
                        self.http.db.add_event(active["id"], event)
                    self.assertEqual(self.http.request("/api/jobs/" + active["id"] + "/cancel", {})[0], 200)
                    self.assertTrue(self.http.controls[active["id"]]["cancelled"].is_set())
                    self.assertLess(time.monotonic() - start, 2.5,
                                    "Unrelated history/status/control waited for HTML enrichment")
                finally:
                    release.set()
                self.assertEqual(reading.result(timeout=3)[0], 200)
        self.assertEqual(self.http.db.get_job(self.job_id), original)
        self.assertEqual(self.http.db.search_audit(self.job_id), original_audit)
        self.assertEqual(self.http.count("POST", "/api/search"), 1)

    def test_supplier_ids_are_bound_to_job_and_query_cannot_select_a_url(self):
        with patch.object(supplier, "supplier_details", side_effect=AssertionError("Unexpected source parse")):
            self.assertEqual(self.http.request(self.path + "/suppliers/not-saved-offer")[0], 404)
            self.assertEqual(self.http.request("/api/jobs/" + "f" * 24 + "/suppliers/" + quote(self.offer_id, safe=""))[0], 404)
            self.assertEqual(self.http.request(self.supplier_path + "?url=https://other.example/")[0], 400)

    def test_rejected_offer_is_still_available_to_lazy_supplier_read(self):
        job = self.http.db.get_job(self.job_id)
        job["result"]["offers"][0]["status"] = "excluded"
        self.http.db.save_result(self.job_id, job["result"])
        before = self.persistent_snapshot()
        status, body, _ = self.http.request(self.supplier_path)
        self.assertEqual(status, 200)
        self.assertEqual(body["supplier_details"]["inn"], "1234567890")
        self.assertEqual(self.persistent_snapshot(), before)


class SupplierViewCacheTests(unittest.TestCase):
    def setUp(self):
        cache = patch.object(server, "SUPPLIER_CACHE", OrderedDict())
        cache.start()
        self.addCleanup(cache.stop)

    @staticmethod
    def document(inn="1234567890"):
        text = 'Реквизиты: ООО «QA Поставщик». ИНН ' + inn + '.'
        raw = '<footer>' + text + '</footer>'
        return {"id": "legacy-document", "url": "https://supplier.example/product/",
                "text": text, "html": raw, "sha256": hashlib.sha256(raw.encode()).hexdigest(),
                "retrieved_at": "2026-01-01T00:00:00Z"}

    def test_cache_never_crosses_storage_and_parallel_reads_parse_one_capture_once(self):
        class Captures:
            def __init__(self, path, document):
                self.path, self.document = path, document

            def get_document(self, job_id, identifier):
                return deepcopy(self.document)

        first = Captures("qa-store-one", self.document())
        second = Captures("qa-store-two", self.document("9876543210"))
        entered, release = threading.Event(), threading.Event()
        actual = supplier.supplier_details

        def held(document):
            entered.set()
            release.wait(3)
            return actual(document)

        with patch.object(supplier, "supplier_details", side_effect=held) as calls:
            with ThreadPoolExecutor(max_workers=2) as pool:
                pending = [pool.submit(server._captured_supplier_view, first, "same-job", "same-doc", None) for _ in range(2)]
                self.assertTrue(entered.wait(2))
                release.set()
                views = [future.result(timeout=3) for future in pending]
            self.assertEqual(calls.call_count, 1)
        views[0]["inn"] = "mutated"
        self.assertEqual(views[1]["inn"], "1234567890")
        self.assertEqual(server._captured_supplier_view(second, "same-job", "same-doc", None)["inn"], "9876543210")

    def test_nested_contact_wrappers_do_not_duplicate_the_full_capture_per_ancestor(self):
        document = self.document()
        document["html"] = '<div class="contact-details">' * 70 + document["html"] + '</div>' * 70
        parser = supplier._Contacts()
        parser.feed(document["html"])
        blocks = parser.finish()
        self.assertEqual(sum(len(value) for _, value in blocks), len(document["text"]))
        self.assertEqual(supplier.supplier_details(document)["inn"], "1234567890")

    def test_export_enriches_only_used_captures_without_removing_any_offer(self):
        class Captures:
            path = "qa-selected-export"

            def __init__(self, document):
                self.document, self.reads = document, []

            def get_document(self, job_id, identifier):
                self.reads.append(identifier)
                return {**deepcopy(self.document), "id": identifier}

        database = Captures(self.document())
        native = [{"id": "strict", "status": "included", "document_id": "capture-strict"},
                  {"id": "strict-two", "status": "included", "document_id": "capture-strict-two"},
                  {"id": "strict-three", "status": "included", "document_id": "capture-strict-three"},
                  {"id": "estimate", "status": "uncertain", "document_id": "capture-estimate"},
                  {"id": "rejected", "status": "excluded", "document_id": "capture-rejected"},
                  {"id": "known", "status": "excluded", "document_id": "capture-known",
                   "supplier_details": {"inn": "9876543210", "evidence": {"inn": {"quote": "QA known source"}}}}]
        result = {"offers": [{**deepcopy(o), "id": "line:" + o["id"]} for o in native],
                  "calculation": {"provisional": {"offer_ids": ["line:estimate"]}},
                  "items": [{"id": "line", "offers": deepcopy(native),
                             "calculation": {"provisional": {"offer_ids": ["estimate"]}}}]}
        original_ids = [o["id"] for o in result["offers"]]
        known = deepcopy(result["offers"][-1]["supplier_details"])
        with patch.object(supplier, "supplier_details", wraps=supplier.supplier_details) as calls:
            server.captured_supplier_details("saved-job", result, database=database, used_only=True)
            self.assertEqual(calls.call_count, 4)
        self.assertCountEqual(database.reads, ["capture-strict", "capture-strict-two", "capture-strict-three", "capture-estimate"])
        self.assertEqual([o["id"] for o in result["offers"]], original_ids)
        self.assertEqual(len(result["items"][0]["offers"]), len(native))
        self.assertNotIn("supplier_details", next(o for o in result["offers"] if o["id"] == "line:rejected"))
        self.assertEqual(result["offers"][-1]["supplier_details"], known)

    def test_missing_export_contacts_do_not_claim_the_source_contains_no_details(self):
        from quotelens.reports import report_html
        from tests.test_history_api import synthetic_result
        result = synthetic_result({"description": DESCRIPTION, "quantity": 1})
        for offer in result["offers"]:
            offer.pop("supplier_details", None)
        rendered = report_html(result)
        self.assertIn("Нет подтверждённых сведений", rendered)
        self.assertNotIn("Не указан в сохранённом источнике", rendered)


if __name__ == "__main__":
    unittest.main()
