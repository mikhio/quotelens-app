"""One-click confirmations use isolated synthetic captures and no upstream I/O."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from quotelens import server
from quotelens.engine import _field_confirmed, _has_evidence
from quotelens.procurement import aggregate_results
from quotelens.review import RevisionConflict, annotate_review_fields
from quotelens.scenarios import load_scenario
from quotelens.selection import finalize
from quotelens.storage import Store


JOB = "e" * 24
OFFER = "qa:native:one"
COUNTRY = "attributes.country"
COLOR = "attributes.color"
QUOTE = "Синтетическая QA бумага производится в России."


def fixture(*, country="Россия", numeric=None, multi=False):
    sample = load_scenario("synthetic-comparable")
    request, offers = deepcopy(sample["request"]), deepcopy(sample["offers"][:3])
    request["requirements"].extend([
        {"key": "country", "label": "Страна", "value": "Россия", "op": "eq"},
        {"key": "color", "label": "Цвет", "value": "белый", "op": "eq"},
    ])
    offers[0]["id"] = OFFER
    source = {"quote": QUOTE, "url": "https://supplier.invalid/qa-product", "document_id": "qa-capture",
              "source_hash": "a" * 64, "retrieved_at": "2026-11-13T00:00:00Z", "basis": "qualified_source_context",
              "locator": "primary-description", "confirmed_for_product": False, "reason": "Синтетический QA контекст",
              "private_token": "qa-secret-must-not-leak", "headers": {"Authorization": "qa-private"}}
    for offer in offers:
        for key, value in (("country", "Россия"), ("color", "белый")):
            offer["attributes"][key] = value
            offer["evidence"]["attributes." + key] = {"quote": "Синтетический QA факт " + value,
                                                       "url": source["url"]}
    offers[0]["attributes"].update(country=None, color=None)
    for key in (COUNTRY, COLOR):
        offers[0]["evidence"].pop(key, None)
    offers[0]["attribute_hints"] = {
        "country": [{**source, "value": country}, {**source, "value": "Китай"}],
        "color": [{**source, "value": "белый", "quote": "Синтетическая QA белизна CIE 161",
                   "basis": "inferred_from_CIE_whiteness", "inferred": True, "related_field": "attributes.whiteness_cie"}],
    }
    if numeric is not None:
        request["requirements"].append({"key": "density", "label": "Плотность", "value": 80, "unit": "г/м²", "op": "eq"})
        offers[0]["attributes"]["density"] = None
        offers[0]["attribute_hints"]["density"] = [{**source, **numeric}]
    line = finalize(request, offers)
    payload = {"mode": "live", "description": "Синтетическая QA проверка подсказки", "quantity": 1}
    if multi:
        items = [{"id": key, "title": "Синтетический QA осциллограф", "description": request["description"],
                  "quantity": request["quantity"], "unit": request["unit"], "requirements": deepcopy(request["requirements"])} for key in ("line-a", "line-b")]
        payload["items"] = items
        line = aggregate_results(payload, items, {item["id"]: deepcopy(line) for item in items}, stop="source_exhausted")
    return line, payload


class ShownValueStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "synthetic.sqlite3"
        self.store = Store(self.path)
        self.result, self.payload = fixture()
        self.store.create_job({"id": JOB, "status": "complete", "result": self.result}, self.payload)
        self.store.quota_reserve("model_calls", 7, 20, "all")
        self.capture_id = self.store.add_document(JOB, {"url": "https://supplier.invalid/qa-product",
                                                       "text": QUOTE, "retrieved_at": "2026-11-13T00:00:00Z"})

    def confirm(self, key=COUNTRY, revision=0, identifier=OFFER):
        return self.store.apply_review(JOB, identifier, "confirm_value", fields={key: True}, expected_revision=revision)

    def test_confirm_reload_audit_then_restore_original_field_without_changing_source(self):
        original = deepcopy(self.store.get_job(JOB)["result"]["base_result"])
        self.confirm()
        job = Store(self.path).get_job(JOB)
        offer = job["result"]["offers"][0]
        confirmation = job["manual_overrides"][OFFER][COUNTRY]
        self.assertEqual(offer["attributes"]["country"], "Россия")
        self.assertEqual(confirmation["kind"], "manual")
        self.assertEqual(confirmation["origin"], "accepted_suggestion")
        self.assertEqual(confirmation["reason"], "Показанное значение подтверждено пользователем")
        self.assertEqual(confirmation["supporting_source"]["quote"], QUOTE)
        self.assertEqual(confirmation["supporting_source"]["document_id"], "qa-capture")
        self.assertNotIn("private_token", confirmation["supporting_source"])
        self.assertNotIn("headers", confirmation["supporting_source"])
        self.assertIsNone(confirmation["original_value"])
        self.assertTrue(_field_confirmed(offer, COUNTRY))
        self.assertFalse(_has_evidence(offer, COUNTRY))
        self.assertNotIn(COUNTRY, offer["evidence"])
        self.assertEqual(job["result"]["base_result"], original)
        self.assertEqual(job["reviews"][0]["fields"][COUNTRY], confirmation)
        self.assertEqual(self.store.get_document(JOB, self.capture_id)["text"], QUOTE)
        self.assertEqual(self.store.quota_usage("model_calls", "all"), 7)
        self.store.apply_review(JOB, OFFER, "reset_field", fields={COUNTRY: True}, expected_revision=1)
        restored = Store(self.path).get_job(JOB)
        self.assertFalse(restored["manual_overrides"])
        self.assertEqual(restored["result"]["base_result"], original)
        self.assertEqual(restored["result"]["calculation"], self.result["calculation"])
        self.assertIsNone(restored["result"]["offers"][0]["attributes"]["country"])
        self.assertEqual(restored["reviews"][1]["fields"][COUNTRY]["removed_confirmation"], confirmation)

    def test_field_undo_preserves_other_confirmations_manual_edit_exclusion_and_waiver(self):
        self.store.apply_review(JOB, OFFER, "update", "QA ручная цена", fields={"price": 12345}, expected_revision=0)
        self.confirm(revision=1)
        self.confirm(COLOR, revision=2)
        self.store.apply_review(JOB, OFFER, "accept_deviation", "QA прежнее решение", fields={"attributes.channels": True}, expected_revision=3)
        self.store.apply_review(JOB, OFFER, "exclude", "QA исключение", expected_revision=4)
        before = self.store.get_job(JOB)
        self.store.apply_review(JOB, OFFER, "reset_field", fields={COUNTRY: True}, expected_revision=5)
        job = Store(self.path).get_job(JOB)
        self.assertEqual(set(job["manual_overrides"][OFFER]), {"price", COLOR})
        self.assertEqual(job["manual_overrides"][OFFER]["price"], before["manual_overrides"][OFFER]["price"])
        self.assertEqual(job["manual_acceptances"], before["manual_acceptances"])
        self.assertEqual(job["result"]["review"]["excluded_ids"], [OFFER])
        self.assertTrue(job["result"]["offers"][0]["manual_excluded"])
        self.assertEqual(job["manual_overrides"][OFFER][COLOR]["supporting_source"]["basis"], "inferred_from_CIE_whiteness")
        self.assertTrue(job["manual_overrides"][OFFER][COLOR]["supporting_source"]["inferred"])
        self.assertNotIn(COLOR, job["result"]["offers"][0]["evidence"])

    def test_confirmation_never_hides_a_true_mismatch(self):
        result, _ = fixture(country="Китай")
        self.store.save_result(JOB, result)
        self.confirm()
        offer = self.store.get_job(JOB)["result"]["offers"][0]
        row = next(row for row in offer["review_fields"] if row["key"] == COUNTRY)
        self.assertEqual(row["status"], "mismatch")
        self.assertEqual(row["value"], "Китай")
        self.assertNotEqual(offer["status"], "included")
        self.assertFalse(offer.get("manual_acceptances"))

    def test_invalid_selection_missing_hint_and_missing_revision_do_not_write(self):
        before = self.store.get_job(JOB)
        for fields in ({}, {COUNTRY: False}, {COUNTRY: 1}, {COUNTRY: "Россия"}, {COUNTRY: True, COLOR: True},
                       {"price": True}, {"attributes.unknown": True}, {"conditions.unknown": True}, {"attributes.channels": True}):
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                self.store.apply_review(JOB, OFFER, "confirm_value", fields=fields, expected_revision=0)
        for revision in (None, True, -1, "0"):
            with self.subTest(revision=revision), self.assertRaises(ValueError):
                self.store.apply_review(JOB, OFFER, "confirm_value", fields={COUNTRY: True}, expected_revision=revision)
        with self.assertRaises(ValueError):
            self.store.apply_review(JOB, OFFER, "reset_field", fields={COUNTRY: True}, expected_revision=0)
        self.assertEqual(self.store.get_job(JOB), before)

    def test_first_hint_only_and_invalid_primitive_are_rejected(self):
        for invalid in (True, None, {}, [], [80, 90], "", "x" * 1001):
            with self.subTest(invalid=invalid):
                result, _ = fixture(country=invalid)
                self.store.save_result(JOB, result)
                before = self.store.get_job(JOB)
                with self.assertRaises(ValueError):
                    self.confirm()
                self.assertEqual(self.store.get_job(JOB), before)
        result, _ = fixture()
        result["offers"][0]["attribute_hints"]["country"][0]["quote"] = ""
        self.store.save_result(JOB, result)
        with self.assertRaises(ValueError):
            self.confirm()

    def test_numeric_unknown_unit_ranges_overlap_and_incompatible_units_cannot_be_accepted(self):
        variants = [{"value": value, **metadata} for value, metadata in (
            (80, {"unit_reason": "source_unit_not_stated"}),
            (80, {}), (80, {"source_unit": "мм"}),
            ([70, 90], {"source_unit": "г/м²"}),
            (True, {"source_unit": "г/м²"}),
        )]
        for hint in variants:
            with self.subTest(hint=hint):
                result, _ = fixture(numeric=hint)
                self.store.save_result(JOB, result)
                before = self.store.get_job(JOB)
                with self.assertRaises(ValueError):
                    self.confirm("attributes.density")
                self.assertEqual(self.store.get_job(JOB), before)
                descriptor = next(row for row in annotate_review_fields(result)["offers"][0]["review_fields"] if row["key"] == "attributes.density")
                self.assertNotIn("confirmable_suggestion", descriptor)
        result, _ = fixture(numeric={"value": 79, "normalized_unit": "г/м²", "source_unit": "г/м²"})
        self.store.save_result(JOB, result)
        self.confirm("attributes.density")
        density = next(row for row in self.store.get_job(JOB)["result"]["offers"][0]["review_fields"] if row["key"] == "attributes.density")
        self.assertEqual(density["status"], "mismatch")

    def test_canonical_range_and_existing_manual_edit_cannot_be_replaced_by_hint(self):
        result, _ = fixture(numeric={"value": 80, "source_unit": "г/м²"})
        result["offers"][0]["attributes"]["density"] = [70, 90]
        self.store.save_result(JOB, result)
        with self.assertRaises(ValueError):
            self.confirm("attributes.density")
        self.store.apply_review(JOB, OFFER, "update", "QA owner edit", fields={COUNTRY: "Китай"}, expected_revision=0)
        before = self.store.get_job(JOB)
        with self.assertRaises(ValueError):
            self.confirm(revision=1)
        self.assertEqual(self.store.get_job(JOB), before)

    def test_rollback_and_stale_cas_keep_override_audit_and_result_together(self):
        before = self.store.get_job(JOB)
        with patch("quotelens.review.rebuild_result", side_effect=ValueError("Synthetic QA replay failure")):
            with self.assertRaises(ValueError):
                self.confirm()
        self.assertEqual(self.store.get_job(JOB), before)
        self.confirm()
        before = self.store.get_job(JOB)
        with self.assertRaises(RevisionConflict):
            self.confirm(COLOR)
        with self.assertRaises(RevisionConflict):
            self.store.apply_review(JOB, OFFER, "reset_field", fields={COUNTRY: True}, expected_revision=0)
        self.assertEqual(self.store.get_job(JOB), before)

    def test_two_concurrent_field_confirms_have_one_cas_winner(self):
        barrier = threading.Barrier(2)
        def attempt(field):
            instance = Store(self.path)
            barrier.wait(timeout=5)
            try:
                instance.apply_review(JOB, OFFER, "confirm_value", fields={field: True}, expected_revision=0)
                return "saved"
            except RevisionConflict:
                return "conflict"
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(attempt, (COUNTRY, COLOR)))
        self.assertCountEqual(outcomes, ["saved", "conflict"])
        job = self.store.get_job(JOB)
        self.assertEqual(job["revision"], 1)
        self.assertEqual(len(job["reviews"]), 1)
        self.assertEqual(len(job["manual_overrides"][OFFER]), 1)

    def test_multi_line_native_ids_only_change_selected_line_and_replay_persists_it(self):
        result, _ = fixture(multi=True)
        self.store.save_result(JOB, result)
        original = deepcopy(self.store.get_job(JOB)["result"]["items"][1]["base_result"])
        identifier = "line-b:" + OFFER
        self.confirm(identifier=identifier)
        job = self.store.get_job(JOB)
        lines = job["result"]["items"]
        self.assertIsNone(lines[0]["offers"][0]["attributes"]["country"])
        self.assertEqual(lines[1]["offers"][0]["attributes"]["country"], "Россия")
        self.assertEqual(lines[1]["base_result"], original)
        # Simulate the next saved search publication with the same captures.
        with self.store._connection(immediate=True) as db:
            db.execute("UPDATE jobs SET status='running' WHERE id=?", (JOB,))
        self.store.save_result(JOB, result)
        published = Store(self.path).get_job(JOB)
        self.assertEqual(published["result"]["items"][1]["offers"][0]["attributes"]["country"], "Россия")
        self.assertEqual(published["manual_overrides"][identifier], job["manual_overrides"][identifier])


class ShownValueHttpTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        result, payload = fixture()
        self.store.create_job({"id": JOB, "status": "complete", "result": result}, payload)
        patched = patch.object(server, "store", return_value=self.store)
        patched.start()
        self.addCleanup(patched.stop)
        self.http = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        threading.Thread(target=self.http.serve_forever, daemon=True).start()
        self.addCleanup(self.http.server_close)
        self.addCleanup(self.http.shutdown)
        self.base = "http://127.0.0.1:" + str(self.http.server_address[1]) + "/api/jobs/" + JOB

    def request(self, payload=None):
        req = Request(self.base + ("/review" if payload is not None else ""),
                      data=json.dumps(payload).encode() if payload is not None else None,
                      headers={"Content-Type": "application/json"})
        try:
            response = urlopen(req, timeout=5)
        except HTTPError as error:
            response = error
        with response:
            return response.status, json.loads(response.read())

    def test_no_reason_one_click_then_reload_and_individual_undo(self):
        descriptor = next(row for row in self.request()[1]["result"]["offers"][0]["review_fields"] if row["key"] == COUNTRY)
        self.assertEqual(descriptor["confirmable_suggestion"]["value"], "Россия")
        self.assertNotIn("private_token", descriptor["confirmable_suggestion"])
        payload = {"action": "confirm_value", "offer_id": OFFER, "fields": {COUNTRY: True}, "expected_revision": 0}
        status, confirmed = self.request(payload)
        self.assertEqual(status, 200, confirmed)
        self.assertEqual(confirmed["revision"], 1)
        self.assertEqual(self.request()[1]["result"]["offers"][0]["attributes"]["country"], "Россия")
        status, stale = self.request(payload)
        self.assertEqual(status, 409)
        self.assertEqual(stale["code"], "revision_conflict")
        self.assertEqual(stale["current_revision"], 1)
        status, undone = self.request({**payload, "action": "reset_field", "expected_revision": 1})
        self.assertEqual(status, 200, undone)
        self.assertIsNone(undone["result"]["offers"][0]["attributes"]["country"])
        self.assertEqual(undone["revision"], 2)

    def test_client_cannot_supply_value_proof_or_bypass_cas_and_running_job(self):
        payload = {"action": "confirm_value", "offer_id": OFFER, "fields": {COUNTRY: True}, "expected_revision": 0}
        invalid = [{**payload, "value": "Китай"}, {**payload, "supporting_source": {"quote": "invented"}},
                   {**payload, "fields": {COUNTRY: "Китай"}}, {key: value for key, value in payload.items() if key != "expected_revision"}]
        for attempt in invalid:
            with self.subTest(attempt=attempt):
                self.assertEqual(self.request(attempt)[0], 400)
        self.assertEqual(self.store.get_job(JOB)["revision"], 0)
        with self.store._connection(immediate=True) as db:
            db.execute("UPDATE jobs SET status='running' WHERE id=?", (JOB,))
        self.assertEqual(self.request(payload)[0], 409)
        self.assertEqual(self.request({**payload, "action": "reset_field"})[0], 409)


if __name__ == "__main__":
    unittest.main()
