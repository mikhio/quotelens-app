"""HTTP acceptance checks for the public contract; no third-party packages.

Run against an explicitly started server with QUOTELENS_BASE_URL. The default
unittest discovery skips these checks, rather than treating an absent server as
a product failure or claiming that integration was tested.
"""

import json
import os
import re
import time
import unittest
from decimal import Decimal
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


class ApiAcceptance(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.base = os.environ.get("QUOTELENS_BASE_URL", "").rstrip("/")
        if not cls.base:
            raise unittest.SkipTest("Set QUOTELENS_BASE_URL to test a running API")
        if urlsplit(cls.base).scheme not in ("http", "https"):
            raise ValueError("QUOTELENS_BASE_URL must be an HTTP(S) URL")
        cls.config = cls.request_json("/api/config")[1]
        cls.scenarios = cls.request_json("/api/scenarios")[1]

    @classmethod
    def request(cls, path, payload=None):
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        req = Request(cls.base + path, data=data,
                      headers={"Content-Type": "application/json"} if data else {})
        try:
            with urlopen(req, timeout=8) as response:
                return response.status, response.read(), response.headers
        except HTTPError as error:
            return error.code, error.read(), error.headers

    @classmethod
    def request_json(cls, path, payload=None):
        status, body, headers = cls.request(path, payload)
        return status, json.loads(body.decode("utf-8")), headers

    def scenario(self, *, kind=None, category=None):
        for scenario in self.scenarios:
            if kind is not None and scenario["kind"] != kind:
                continue
            if category is not None and scenario["category"] != category:
                continue
            return scenario
        self.fail("Required declared scenario is missing: %r/%r" % (kind, category))

    def scenario_by_id(self, scenario_id):
        for scenario in self.scenarios:
            if scenario["id"] == scenario_id:
                return scenario
        self.fail("Required regression scenario is missing: " + scenario_id)

    def run_snapshot(self, scenario, **extra):
        descriptions = {
            "oscilloscope": "Осциллограф цифровой, не менее 4 каналов, не менее 100 МГц",
            "paper": "Бумага А4 80 г/м², 500 листов в пачке",
            "service": "Уборка помещений, цена за м²",
        }
        payload = {"description": descriptions[scenario["category"]], "quantity": 1,
                   "mode": "snapshot", "scenario_id": scenario["id"]}
        payload.update(extra)
        status, started, _ = self.request_json("/api/search", payload)
        self.assertEqual(status, 202, started)
        self.assertEqual(started["status"], "running")
        self.assertTrue(started["id"])
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            status, job, _ = self.request_json("/api/jobs/" + started["id"])
            self.assertEqual(status, 200, job)
            if job["status"] != "running":
                self.assertEqual(job["status"], "complete", job)
                return job
            time.sleep(0.03)
        self.fail("Snapshot job failed to reach a terminal status within 12 seconds")

    def test_config_exposes_status_and_limits_without_credentials(self):
        self.assertIsInstance(self.config["model"]["configured"], bool)
        self.assertIsInstance(self.config["search"]["configured"], bool)
        self.assertIn("name", self.config["model"])
        for name, expected in {"min_suppliers": 3, "max_pages": 16,
                               "max_queries": 6, "max_seconds": 240,
                               "max_model_calls": 10}.items():
            self.assertEqual(self.config["defaults"][name], expected)
        def check_keys(value):
            if isinstance(value, dict):
                for key, child in value.items():
                    self.assertNotIn(key.lower(), {"api_key", "apikey", "token",
                                                   "authorization", "secret"})
                    check_keys(child)
            elif isinstance(value, list):
                for child in value:
                    check_keys(child)
        check_keys(self.config)

    def test_scenarios_declare_provenance_and_distinct_categories(self):
        self.assertTrue(self.scenarios)
        self.assertEqual(len({s["id"] for s in self.scenarios}), len(self.scenarios))
        for scenario in self.scenarios:
            for key in ("id", "title", "description", "kind", "expected", "category"):
                self.assertTrue(scenario[key], (key, scenario))
        self.assertIn("synthetic", {s["kind"] for s in self.scenarios})
        self.assertTrue({s["kind"] for s in self.scenarios} & {"real_snapshot", "snapshot"})
        self.assertGreaterEqual(len({s["category"] for s in self.scenarios}), 3)

    def test_sources_are_actual_linked_source_descriptions(self):
        status, sources, _ = self.request_json("/api/sources")
        self.assertEqual(status, 200)
        self.assertTrue(sources)
        for source in sources:
            for key in ("id", "name", "url", "kind", "description"):
                self.assertTrue(source[key], (key, source))
            if source["kind"] == "web":
                self.assertEqual(source["id"], "web")
                self.assertTrue(source["connected"])
            else:
                self.assertTrue(source["domain"])
            self.assertIn(urlsplit(source["url"]).scheme, ("http", "https"))

    def test_empty_request_is_a_json_validation_error(self):
        status, body, _ = self.request_json("/api/search", {
            "description": " ", "quantity": 1, "mode": "live"})
        self.assertGreaterEqual(status, 400)
        self.assertLess(status, 500)
        self.assertTrue(body.get("error"))

    def test_snapshot_requires_scenario_id(self):
        status, body, _ = self.request_json("/api/search", {
            "description": "Бумага А4 80 г/м²", "quantity": 1, "mode": "snapshot"})
        self.assertGreaterEqual(status, 400)
        self.assertLess(status, 500)
        self.assertTrue(body.get("error"))

    def test_unknown_scenario_is_not_replaced_with_canned_result(self):
        status, body, _ = self.request_json("/api/search", {
            "description": "Неизвестная закупка", "quantity": 1,
            "mode": "snapshot", "scenario_id": "not-a-scenario-acceptance"})
        self.assertGreaterEqual(status, 400)
        self.assertLess(status, 500)
        self.assertTrue(body.get("error"))

    def test_unknown_job_returns_json_error(self):
        status, body, _ = self.request_json("/api/jobs/not-a-job-acceptance")
        self.assertEqual(status, 404)
        self.assertTrue(body.get("error"))

    def test_snapshot_retains_evidence_and_finite_search_metadata(self):
        job = self.run_snapshot(self.scenario(kind="synthetic"))
        result = job["result"]
        search = result["search"]
        self.assertEqual(search["mode"], "snapshot")
        self.assertEqual(search["completeness"], "bounded_not_exhaustive")
        self.assertEqual(search["scenario_kind"], "synthetic")
        for key in ("started_at", "finished_at", "source_statuses"):
            self.assertIn(key, search)
        for key in ("code", "label", "detail"):
            self.assertTrue(search["stop"][key])
        budget = search["budget"]
        for used, maximum in (("pages_used", "max_pages"),
                              ("queries_used", "max_queries"),
                              ("model_calls", "max_model_calls"),
                              ("elapsed_seconds", "max_seconds")):
            self.assertGreaterEqual(budget[used], 0)
            self.assertLessEqual(budget[used], budget[maximum])
        self.assertTrue(result["offers"])
        for offer in result["offers"]:
            self.assertEqual(offer["provenance"], "synthetic")
            self.assertIn(offer["status"], ("included", "excluded", "uncertain"))
            self.assertTrue(offer["reasons"])
            self.assertTrue(offer["url"])
            self.assertTrue(offer["retrieved_at"])
            if offer["status"] == "included":
                self.assertIsNotNone(offer["normalized_price"])
                self.assertTrue(offer["evidence"]["price"]["quote"])
                self.assertTrue(offer["evidence"]["price"]["url"])

    def test_real_snapshot_keeps_real_urls_and_historical_provenance(self):
        scenario = next((s for s in self.scenarios
                         if s["kind"] in ("real_snapshot", "snapshot")), None)
        self.assertIsNotNone(scenario)
        result = self.run_snapshot(scenario)["result"]
        self.assertTrue(result["offers"])
        for offer in result["offers"]:
            self.assertEqual(offer["provenance"], "snapshot")
            host = urlsplit(offer["url"]).hostname or ""
            self.assertTrue(host)
            self.assertFalse(host.endswith(".invalid"))
            self.assertTrue(offer["retrieved_at"])
        self.assertTrue(any(o["status"] == "excluded" for o in result["offers"]),
                        "The real comparison must preserve its incompatible candidate")

    def test_report_json_reproduces_the_completed_result(self):
        job = self.run_snapshot(self.scenario(kind="synthetic"))
        status, report, headers = self.request_json(
            "/api/jobs/" + job["id"] + "/report.json")
        self.assertEqual(status, 200)
        self.assertIn("application/json", headers.get("Content-Type", ""))
        self.assertEqual(report["calculation"], job["result"]["calculation"])
        self.assertEqual(report["offers"], job["result"]["offers"])
        self.assertEqual(report["request"], job["result"]["request"])
        self.assertIn("search", report)

    def test_report_html_is_standalone_and_escapes_input(self):
        marker = '<img src=x onerror="window.acceptanceInjected=1">'
        job = self.run_snapshot(self.scenario(kind="synthetic"), description=marker)
        status, body, headers = self.request(
            "/api/jobs/" + job["id"] + "/report.html")
        self.assertEqual(status, 200)
        self.assertIn("text/html", headers.get("Content-Type", ""))
        html = body.decode("utf-8")
        self.assertIn("<html", html.lower())
        self.assertNotIn(marker, html)
        self.assertIn("&lt;img", html)
        self.assertNotRegex(html.lower(), r'<(?:script|link|img)\b[^>]*(?:src|href)=[\"\']https?://')
        self.assertNotIn("<form", html.lower())
        self.assertNotIn("/api/jobs/", html)
        self.assertNotRegex(html.lower(), r'<input\b[^>]*(?:password|api.?key)')
        self.assertTrue(re.search(r'synthetic|синтетич', html, re.I))

    def test_paper_and_service_preserve_the_requested_category(self):
        for category in ("paper", "service"):
            with self.subTest(category=category):
                scenario = self.scenario(kind="synthetic", category=category)
                result = self.run_snapshot(scenario)["result"]
                self.assertEqual(result["request"]["category"], category)
                self.assertTrue(result["offers"])
                self.assertTrue(all("осциллограф" not in o["title"].lower()
                                    for o in result["offers"]))

    def test_single_supplier_cannot_produce_a_ready_price_or_defined_cv(self):
        result = self.run_snapshot(self.scenario_by_id("synthetic-one"))["result"]
        calculation = result["calculation"]
        self.assertEqual(calculation["status"], "insufficient_data")
        self.assertEqual(calculation["n"], 1)
        self.assertEqual(calculation["independent_suppliers"], 1)
        self.assertIsNone(calculation["total"])
        self.assertIsNone(calculation["unit_price"])
        self.assertIsNone(calculation["stddev"])
        self.assertIsNone(calculation["cv"])
        self.assertTrue(calculation["reason"])

    def test_missing_required_evidence_never_enters_the_calculation(self):
        result = self.run_snapshot(self.scenario_by_id("synthetic-missing"))["result"]
        self.assertEqual(result["calculation"]["status"], "insufficient_data")
        self.assertEqual(result["calculation"]["n"], 0)
        self.assertIsNone(result["calculation"]["total"])
        self.assertTrue(result["offers"])
        self.assertTrue(all(o["status"] != "included" for o in result["offers"]))
        self.assertTrue(all(o["reasons"] for o in result["offers"]))

    def test_comparable_prices_are_reproducible_from_accepted_offers(self):
        result = self.run_snapshot(self.scenario_by_id("synthetic-comparable"))["result"]
        calculation = result["calculation"]
        included = [o for o in result["offers"] if o["status"] == "included"]
        self.assertEqual(calculation["status"], "ready")
        self.assertEqual(calculation["n"], len(included))
        self.assertEqual(len(included), 3)
        prices = [Decimal(str(o["normalized_price"])) for o in included]
        mean = sum(prices) / len(prices)
        self.assertEqual(Decimal(str(calculation["mean"])), mean)
        self.assertEqual(mean, Decimal("72000"))
        quantity = Decimal(str(result["request"]["quantity"]))
        self.assertEqual(Decimal(str(calculation["total"])),
                         (mean * quantity).quantize(Decimal("0.01")))
        self.assertTrue(calculation["rule"])
        self.assertTrue(any(o["status"] == "excluded" for o in result["offers"]))


if __name__ == "__main__":
    unittest.main()
