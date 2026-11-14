"""Manual corrections on isolated synthetic data; no research/model/network I/O."""
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
from http.server import ThreadingHTTPServer
from io import BytesIO
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from zipfile import ZipFile

from quotelens import server
from quotelens.engine import _has_evidence, _field_confirmed, _requirement_match, parse_request
from quotelens.procurement import aggregate_results, request_for_item
from quotelens.reports import report_docx, report_html, report_pdf
from quotelens.review import RevisionConflict, annotate_review_fields, validate_fields
from quotelens.scenarios import load_scenario
from quotelens.selection import finalize
from quotelens.storage import SCHEMA_VERSION, Store


JOB = "a" * 24
DESCRIPTION = "Осциллограф 4 канала полоса пропускания 100 МГц"


def synthetic_result(multi=False):
    offers = [deepcopy(o) for o in load_scenario("synthetic-comparable")["offers"] if o["id"] != "synthetic-a-mirror"]
    # Native IDs deliberately contain a colon and are shared between lines.
    offers[0]["id"] = "native:one"
    offers[0]["price"] = None
    offers[0]["evidence"].pop("price", None)
    offers[0]["tax"] = "unknown"
    offers[0]["evidence"].pop("tax", None)
    request = parse_request(DESCRIPTION, 3)
    result = finalize(request, offers, {"min_suppliers": 3})
    result["search"] = {"stop": {"code": "source_exhausted"}, "budget": {"model_calls": 0}, "started_at": "2026-01-01T00:00:00Z"}
    if multi:
        items = [{"id": key, "title": "Синтетический QA осциллограф", "description": DESCRIPTION, "quantity": 3, "unit": "шт"} for key in ("line-a", "line-b")]
        by_item = {item["id"]: deepcopy(result) for item in items}
        return aggregate_results({"description": "Синтетическая QA закупка", "items": items, "quantity": 1}, items, by_item, stop="source_exhausted")
    return result


class ManualStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "qa.sqlite3"
        self.store = Store(self.path)
        self.result = synthetic_result()
        self.store.create_job({"id": JOB, "status": "complete", "result": self.result}, {"description": DESCRIPTION, "quantity": 3})
        self.store.quota_reserve("model_calls", 7, 20, "all")
        self.capture_id = self.store.add_document(JOB, {"document_id": "qa-capture", "url": "https://example.com/qa", "text": "Synthetic QA source", "retrieved_at": "2026-01-01T00:00:00Z"})

    def update(self, **kwargs):
        return self.store.apply_review(JOB, "native:one", "update", "Уточнено пользователем", fields={"price": 30000.25, "tax": "included"}, expected_revision=0, **kwargs)

    def test_save_reload_recalculate_reset_preserves_source_and_captures(self):
        baseline = deepcopy(self.result["base_result"])
        original_total = self.result["calculation"]["total"]
        self.update()
        job = Store(self.path).get_job(JOB)
        result = job["result"]
        offer = next(o for o in result["offers"] if o["id"] == "native:one")
        self.assertEqual(result["base_result"], baseline)
        self.assertEqual(result["calculation"]["status"], "ready")
        self.assertNotEqual(result["calculation"]["total"], original_total)
        self.assertNotIn("price", offer["evidence"])
        self.assertTrue(_field_confirmed(offer, "price"))
        self.assertFalse(_has_evidence(offer, "price"))
        self.assertEqual(next(f for f in offer["review_fields"] if f["key"] == "price")["status"], "manual_confirmed")
        self.assertEqual(job["reviews"][0]["fields"]["price"]["original_value"], None)
        self.assertEqual(self.store.get_document(JOB, self.capture_id)["text"], "Synthetic QA source")
        self.assertEqual(self.store.quota_usage("model_calls", "all"), 7)
        self.store.apply_review(JOB, "native:one", "reset", "Вернуть исходное", expected_revision=1)
        reset = Store(self.path).get_job(JOB)
        self.assertEqual(reset["result"]["base_result"], baseline)
        self.assertEqual(reset["result"]["calculation"], self.result["calculation"])
        self.assertFalse(reset["manual_overrides"])
        self.assertFalse(next(o for o in reset["result"]["offers"] if o["id"] == "native:one").get("manual_confirmations"))
        self.assertEqual(len(reset["reviews"]), 2)

    def test_conflict_and_bad_fields_leave_entire_transaction_unchanged(self):
        self.update()
        before = self.store.get_job(JOB)
        with self.assertRaises(RevisionConflict):
            self.store.apply_review(JOB, "native:one", "update", "QA stale", fields={"price": 500}, expected_revision=0)
        for fields in ({"url": "https://example.org"}, {"price": True}, {"price": float("nan")}, {"price": -1}, {"tax": "yes"}, {"attributes.other": "made up"}, {"unit": {}}, {}):
            with self.assertRaises(ValueError, msg=str(fields)):
                self.store.apply_review(JOB, "native:one", "update", "QA invalid", fields=fields, expected_revision=1)
        self.assertEqual(self.store.get_job(JOB), before)
        with self.assertRaises(ValueError):
            self.store.apply_review(JOB, "native:one", "update", "QA missing revision", fields={"price": 1})

    def test_recalculation_failure_rolls_back_override_and_audit_together(self):
        before = self.store.get_job(JOB)
        with patch("quotelens.review.rebuild_result", side_effect=ValueError("QA failed replay")):
            with self.assertRaises(ValueError):
                self.update()
        self.assertEqual(self.store.get_job(JOB), before)

    def test_populated_v1_schema_migration_preserves_data_and_is_idempotent(self):
        self.store.append_review(JOB, "native:one", "exclude", "QA legacy review")
        before = self.store.get_job(JOB)
        document = self.store.get_document(JOB, self.capture_id)
        # Remove exactly the additive v2 objects to reproduce the populated
        # original v1 schema, including its original six-column review table.
        with sqlite3.connect(self.path) as db:
            db.executescript("DROP TABLE review_overrides; ALTER TABLE reviews DROP COLUMN fields_json; PRAGMA user_version=1;")
        migrated = Store(self.path)
        self.assertEqual(migrated.get_job(JOB), before)
        self.assertEqual(migrated.get_document(JOB, self.capture_id), document)
        self.assertEqual(migrated.quota_usage("model_calls", "all"), 7)
        self.assertEqual(Store(self.path).get_job(JOB), before)
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)
            self.assertEqual(db.execute("SELECT fields_json FROM reviews").fetchone()[0], None)

    def test_manual_update_preserves_exclusion_and_reset_clears_both(self):
        self.store.apply_review(JOB, "native:one", "exclude", "QA exclude")
        self.store.apply_review(JOB, "native:one", "update", "QA edit excluded", fields={"price": 10, "tax": "included"}, expected_revision=1)
        job = self.store.get_job(JOB)
        self.assertEqual(job["excluded_ids"], ["native:one"])
        self.assertTrue(job["manual_overrides"])
        self.assertTrue(next(o for o in job["result"]["offers"] if o["id"] == "native:one")["manual_excluded"])
        self.store.apply_review(JOB, "native:one", "reset", "QA reset", expected_revision=2)
        self.assertEqual(self.store.get_job(JOB)["excluded_ids"], [])
        self.assertEqual(self.store.get_job(JOB)["manual_overrides"], {})

    def test_legacy_saved_exclusion_without_review_rows_is_preserved(self):
        result = deepcopy(self.result)
        result["review"] = {"excluded_ids": ["synthetic-b"]}
        self.store.save_result(JOB, result)
        self.update()
        saved = self.store.get_job(JOB)
        self.assertIn("synthetic-b", saved["excluded_ids"])
        self.assertTrue(next(o for o in saved["result"]["offers"] if o["id"] == "synthetic-b")["manual_excluded"])

    def test_multi_item_totals_native_ids_and_untouched_line(self):
        result = synthetic_result(multi=True)
        self.store.save_result(JOB, result)
        unchanged = deepcopy(result["items"][1]["calculation"])
        self.store.apply_review(JOB, "line-a:native:one", "update", "QA first line", fields={"price": 30000.25, "tax": "included"}, expected_revision=0)
        saved = self.store.get_job(JOB)["result"]
        self.assertEqual(saved["request"], result["request"])
        self.assertEqual(saved["items"][1]["calculation"], unchanged)
        self.assertEqual(saved["items"][0]["offers"][0]["id"], "native:one")
        self.assertTrue(saved["items"][0]["offers"][0]["manual_confirmations"])
        self.assertFalse(saved["items"][1]["offers"][0].get("manual_confirmations"))
        self.assertIsNone(saved["calculation"]["total"])
        self.store.apply_review(JOB, "line-b:native:one", "update", "QA second line", fields={"price": 40000.5, "tax": "included"}, expected_revision=1)
        saved = self.store.get_job(JOB)["result"]
        self.assertEqual(saved["calculation"]["total"], round(sum(i["calculation"]["total"] for i in saved["items"]), 2))

    def test_specific_mismatch_stays_excluded_until_corrected_fact(self):
        result = deepcopy(self.result)
        result["base_result"]["offers"][0]["attributes"]["channels"] = 2
        self.store.save_result(JOB, result)
        self.update()
        offer = self.store.get_job(JOB)["result"]["offers"][0]
        self.assertEqual(offer["status"], "excluded")
        self.store.apply_review(JOB, "native:one", "update", "QA corrected channels", fields={"attributes.channels": 4}, expected_revision=1)
        offer = self.store.get_job(JOB)["result"]["offers"][0]
        self.assertEqual(offer["status"], "included")
        self.assertEqual(offer["evidence"]["attributes.channels"], self.result["offers"][0]["evidence"]["attributes.channels"])
        self.assertFalse(_has_evidence(offer, "attributes.channels"))

    def test_manual_conditions_do_not_become_source_quotes(self):
        result = deepcopy(self.result)
        result["base_result"]["request"]["conditions"] = [{"key": "warranty", "label": "Гарантия", "value": 12, "op": "gte"}]
        self.store.save_result(JOB, result)
        self.store.apply_review(JOB, "native:one", "update", "QA conditions", fields={"price": 30000, "tax": "included", "conditions.warranty": 24}, expected_revision=0)
        offer = self.store.get_job(JOB)["result"]["offers"][0]
        check = offer["condition_checks"][0]
        self.assertEqual(check["status"], "matches")
        self.assertIsNone(check["evidence"])
        self.assertEqual(check["confirmation"]["kind"], "manual")

    def test_source_conflict_and_unknown_request_remain_blockers(self):
        result = deepcopy(self.result)
        result["base_result"]["offers"][0]["source_issues"] = ["QA conflicting variant"]
        result["base_result"]["offers"][0]["relevance"] = {"verdict": "mismatch", "basis": "source_variant_comparison", "reason": "QA variant mismatch"}
        self.store.save_result(JOB, result)
        self.update()
        offer = self.store.get_job(JOB)["result"]["offers"][0]
        self.assertEqual(offer["status"], "excluded")
        self.assertIn("QA conflicting variant", offer["review_blockers"])

    def test_reports_name_manual_confirmation_and_keep_original_quotes(self):
        self.update()
        result = self.store.get_job(JOB)["result"]
        html = report_html(result)
        self.assertIn("Ручные подтверждения", html)
        self.assertIn("не являются цитатами сайта", html)
        with ZipFile(BytesIO(report_docx(result))) as archive:
            xml = archive.read("word/document.xml").decode()
        self.assertIn("Ручные подтверждения", xml)
        self.assertNotIn("review-widget", xml)
        from pypdf import PdfReader
        pdf = PdfReader(BytesIO(report_pdf(result)))
        pdf_text = "\n".join(page.extract_text() for page in pdf.pages)
        self.assertIn("Ручные подтверждения", pdf_text)
        self.assertIn("не являются цитатами сайта", pdf_text)
        self.assertNotIn("Скачать", pdf_text)
        self.assertEqual(result["base_result"], self.result["base_result"])

    def test_dom_image_proof_is_preserved_without_inventing_a_quote(self):
        result = deepcopy(self.result)
        proof = {"basis": "primary_product_dom", "locator": "main .gallery img", "attribute": "src", "raw_value": "/qa.png", "url": "https://example.org/qa", "document_id": "qa-document", "source_hash": "a" * 64}
        result["offers"][0]["evidence"]["image_url"] = proof
        self.store.save_result(JOB, result)
        with self.store._connection() as db:
            row = db.execute("SELECT quote,evidence_json FROM evidence WHERE job_id=? AND offer_id=? AND field='image_url'", (JOB, "native:one")).fetchone()
        self.assertEqual(row["quote"], "")
        self.assertEqual(json.loads(row["evidence_json"]), proof)
        self.assertFalse(_has_evidence(self.store.get_job(JOB)["result"]["offers"][0], "image_url"))

    def test_presence_requirement_is_exact_token_not_fuzzy(self):
        self.assertTrue(_requirement_match("USB (Кабель), USB (Флеш-накопитель)", "USB - наличие", "eq", "spec_usb"))
        self.assertFalse(_requirement_match("microUSB", "USB - наличие", "eq", "spec_usb"))
        for actual in ("USB нет", "Без USB", "USB: отсутствует", "Отсутствует USB", "не поддерживает USB", "USB не поддерживается", "No USB", "Without USB", "USB not supported"):
            self.assertFalse(_requirement_match(actual, "USB - наличие", "eq", "spec_usb"), actual)


class ManualHttpTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.result = synthetic_result()
        self.store.create_job({"id": JOB, "status": "complete", "result": self.result}, {})
        self.patch_store = patch.object(server, "store", return_value=self.store)
        self.patch_store.start(); self.addCleanup(self.patch_store.stop)
        self.http = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        threading.Thread(target=self.http.serve_forever, daemon=True).start()
        self.addCleanup(self.http.server_close); self.addCleanup(self.http.shutdown)
        self.base = "http://127.0.0.1:" + str(self.http.server_address[1]) + "/api/jobs/" + JOB

    def request(self, suffix, payload=None):
        data = json.dumps(payload).encode() if payload is not None else None
        request = Request(self.base + suffix, data=data, headers={"Content-Type": "application/json"})
        try: response = urlopen(request, timeout=5)
        except HTTPError as error: response = error
        with response:
            raw = response.read()
            return response.status, json.loads(raw) if "application/json" in response.headers.get("Content-Type", "") else raw

    def test_save_reload_conflict_and_malformed_http(self):
        payload = {"offer_id": "native:one", "action": "update", "fields": {"price": 30000.25, "tax": "included"}, "reason": "QA manual", "expected_revision": 0}
        status, job = self.request("/review", payload)
        self.assertEqual(status, 200, job)
        self.assertEqual(job["revision"], 1)
        self.assertEqual(self.request("")[1]["result"]["calculation"], job["result"]["calculation"])
        status, conflict = self.request("/review", payload)
        self.assertEqual(status, 409)
        self.assertEqual(conflict["code"], "revision_conflict")
        self.assertEqual(conflict["current_revision"], 1)
        status, error = self.request("/review", {**payload, "expected_revision": 1, "fields": {"id": "forbidden"}})
        self.assertEqual(status, 400)
        missing_reason = {key: value for key, value in payload.items() if key != "reason"}
        self.assertEqual(self.request("/review", {**missing_reason, "expected_revision": 1})[0], 400)
        self.assertEqual(self.store.get_job(JOB)["revision"], 1)

    def test_same_origin_image_only_uses_stored_url_and_encoded_id(self):
        result = deepcopy(self.result)
        result["offers"][0]["image_url"] = "https://example.org/qa-image.png"
        self.store.save_result(JOB, result)
        body = b"QA mocked raster transport"
        with patch("quotelens.acquisition.acquire_image", return_value={"body": body, "mime": "image/png", "url": "https://example.org/qa-image.png", "sha256": "qa"}) as acquire:
            with patch.object(server, "IMAGE_CACHE", {}):
                # The real cache is ordered; cache-hit path requires move_to_end.
                from collections import OrderedDict
                with patch.object(server, "IMAGE_CACHE", OrderedDict()):
                    status, image = self.request("/images/native%3Aone")
                    self.assertEqual(status, 200)
                    self.assertEqual(image, body)
                    self.assertEqual(self.request("/images/native%3Aone")[0], 200)
                    acquire.assert_called_once_with("https://example.org/qa-image.png", timeout=8)
                    self.assertEqual(self.request("/images/native%3Aone?url=https://attacker.test")[0], 400)
                    self.assertEqual(self.request("/images/unknown")[0], 404)

    def test_concurrent_thumbnails_wait_with_bounded_upstream_requests(self):
        from collections import OrderedDict
        result=deepcopy(self.result);template=result['offers'][0];result['offers']=[]
        for index in range(3):
            offer=deepcopy(template);offer.update(id='concurrent:'+str(index),image_url='https://example.org/image'+str(index)+'.png');result['offers'].append(offer)
        self.store.save_result(JOB,result)
        lock=threading.Lock();two_started=threading.Event();active=0;maximum=0
        def slow_image(url,timeout=8):
            nonlocal active,maximum
            with lock:
                active+=1;maximum=max(maximum,active)
                if active==2:two_started.set()
            two_started.wait(1);time.sleep(.1)
            with lock:active-=1
            return {'body':b'QA raster '+url.encode(),'mime':'image/png','url':url,'sha256':'qa'}
        with patch.object(server,'IMAGE_CACHE',OrderedDict()),patch.object(server,'IMAGE_ACTIVE',threading.BoundedSemaphore(2)),patch.object(server,'IMAGE_PENDING',threading.BoundedSemaphore(12)),patch('quotelens.acquisition.acquire_image',side_effect=slow_image) as acquire:
            with ThreadPoolExecutor(max_workers=3) as pool:
                responses=list(pool.map(lambda n:self.request('/images/concurrent%3A'+str(n)),range(3)))
            self.assertEqual([status for status,_ in responses],[200,200,200])
            self.assertEqual(maximum,2)
            self.assertEqual(acquire.call_count,3)

    def test_full_image_queue_is_bounded_and_never_opens_a_source(self):
        result=deepcopy(self.result);result['offers'][0]['image_url']='https://example.org/image.png';self.store.save_result(JOB,result)
        with patch.object(server,'IMAGE_PENDING',threading.BoundedSemaphore(0)),patch('quotelens.acquisition.acquire_image') as acquire:
            self.assertEqual(self.request('/images/native%3Aone')[0],429)
            acquire.assert_not_called()


if __name__ == "__main__":
    unittest.main()
