"""Real-browser acceptance for durable live jobs and manual review.

Uses an isolated HTTP fixture with explicitly synthetic offers, the real price
engine and actual report downloads. No network search, model spend or owner data.
Run: python3 tests/ui_live_acceptance.py --output-dir /managed/run/ui-live
"""
from __future__ import annotations

import argparse
import copy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import mimetypes
import os
from pathlib import Path
import re
import sys
import threading
import time
import unittest
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from quotelens.engine import analyze_offers, parse_request
from quotelens.scenarios import load_scenario
from quotelens.server import annotate_decisions, report_html

DESCRIPTION = "Осциллограф цифровой, 4 канала, полоса не менее 100 МГц"
PREFIX = "/qa-uid/"


class LiveFixture:
    """Test-only live protocol; all product observations remain labelled QA."""

    def __init__(self):
        self.jobs = {}
        self.originals = {}
        self.calls = []
        self.reviews = []
        self.payloads = []
        self.fail_review = False
        self.sources = [{"id": "web", "name": "Открытые источники",
                         "kind": "web", "connected": True},
                        {"id": "prist", "name": "ПриСТ", "domain": "prist.ru", "kind": "retail", "connected": True}]

    def new_job(self, payload):
        self.payloads.append(copy.deepcopy(payload))
        job_id = "qa-live-" + str(len(self.jobs) + 1)
        sample = load_scenario("synthetic-comparable")
        offers = copy.deepcopy(sample["offers"][:4])
        offers.append(copy.deepcopy(load_scenario("synthetic-missing")["offers"][0]))
        offers[0]["document_id"] = "qa-document"
        self.originals[job_id] = (parse_request(payload["description"], payload["quantity"]), offers)
        self.jobs[job_id] = {"id": job_id, "status": "complete", "events": [],
                             "created_at": "2026-11-11T23:00:00Z", "revision": 0,
                             "request": {key: copy.deepcopy(payload[key]) for key in
                                         ("description", "quantity", "source_ids", "source_scope") if key in payload}}
        self.recalculate(job_id, [])
        return self.jobs[job_id]

    def recalculate(self, job_id, excluded_ids):
        request, offers = self.originals[job_id]
        original = analyze_offers(request, offers, {"min_suppliers": 2})
        recalculated = analyze_offers(request, [o for o in offers if o["id"] not in excluded_ids],
                                      {"min_suppliers": 2})
        by_id = {o["id"]: o for o in recalculated["offers"]}
        for offer in original["offers"]:
            if offer["id"] in excluded_ids:
                offer.update(status="excluded", manual_excluded=True,
                             reasons=["Исключено пользователем: проверка QA"])
            else:
                offer.update(by_id[offer["id"]])
        original["calculation"] = recalculated["calculation"]
        original["search"] = {"mode": "live", "scenario_kind": "synthetic",
                               "finished_at": "2026-11-11T23:01:00Z",
                               "policy": {"min_suppliers": 2}, "source_statuses": []}
        original["review"] = {"excluded_ids": excluded_ids,
                               "revision": self.jobs[job_id]["revision"]}
        for offer in original["offers"]:
            offer.update(source_group="retail", source_group_label="Магазины и поставщики")
        self.jobs[job_id]["result"] = annotate_decisions(original)


class LiveHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def send_body(self, value, mime="application/json; charset=utf-8", status=200, download=None):
        body = json.dumps(value, ensure_ascii=False).encode() if isinstance(value, (dict, list)) else value
        if isinstance(body, str):
            body = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(body)))
        if download:
            self.send_header("Content-Disposition", f'attachment; filename="{download}"')
        self.end_headers()
        self.wfile.write(body)

    def dispatch(self):
        fixture = self.server.fixture
        path = unquote(urlsplit(self.path).path)
        fixture.calls.append({"method": self.command, "path": path})
        if not path.startswith(PREFIX):
            return self.send_body({"error": "Request escaped UID prefix"}, status=404)
        route = "/" + path[len(PREFIX):]
        payload = None
        if self.command == "POST":
            payload = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
        if route == "/api/config":
            return self.send_body({"model": {"configured": True}, "defaults": {"min_suppliers": 2}})
        if route == "/api/sources":
            return self.send_body(fixture.sources)
        if route == "/api/jobs":
            items = [{"id": job["id"], "status": job["status"],
                      "created_at": job["created_at"], "revision": job["revision"],
                      "request": job["request"], "description": job["request"]["description"],
                      "quantity": job["request"]["quantity"],
                      "offers_count": len(job["result"]["offers"])}
                     for job in sorted(fixture.jobs.values(), key=lambda job: job["id"], reverse=True)]
            return self.send_body({"items": items, "next_cursor": None, "total": len(items)})
        if route == "/api/search" and payload:
            job = fixture.new_job(payload)
            return self.send_body({"id": job["id"], "status": "running",
                                   "request": job["request"], "created_at": job["created_at"]}, status=202)
        match = re.fullmatch(r"/api/jobs/([^/]+)(?:/(review|report\.(html|json)|documents/([^/]+)))?", route)
        if match and match[1] in fixture.jobs:
            job = fixture.jobs[match[1]]
            if match[2] == "review":
                if fixture.fail_review:
                    return self.send_body({"error": "Учебная ошибка сохранения. Повторите попытку."}, status=503)
                fixture.reviews.append(copy.deepcopy(payload))
                excluded = set(job["result"]["review"]["excluded_ids"])
                if payload["action"] == "exclude":
                    excluded.add(payload["offer_id"])
                else:
                    excluded.discard(payload["offer_id"])
                job["revision"] += 1
                fixture.recalculate(job["id"], sorted(excluded))
            elif match[4]:
                return self.send_body({"id": match[4], "text": "QA: сохранённый текст <script>window.qaInjected=1</script> Цена 69000 рублей."})
            elif match[3] == "html":
                return self.send_body(report_html(job["result"]), "text/html; charset=utf-8", download="qa-report.html")
            elif match[3] == "json":
                return self.send_body(job["result"], download="qa-report.json")
            return self.send_body(job)
        if route.startswith("/api/"):
            return self.send_body({"error": "Сохранённый поиск не найден."}, status=404)
        filename = "index.html" if route == "/" else route.removeprefix("/static/")
        if filename not in {"index.html", "app.js", "style.css"}:
            return self.send_body({"error": "Not found"}, status=404)
        return self.send_body((ROOT / "static" / filename).read_bytes(), mimetypes.guess_type(filename)[0])

    do_GET = dispatch
    do_POST = dispatch


class LiveUiAcceptance(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright
        cls.output = Path(os.environ["QUOTELENS_QA_OUTPUT"])
        cls.output.mkdir(parents=True, exist_ok=True)
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), LiveHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}{PREFIX}"
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(executable_path="/usr/bin/google-chrome", headless=True,
                                                     args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"])

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def setUp(self):
        self.fixture = self.server.fixture = LiveFixture()
        self.context = self.browser.new_context(viewport={"width": 1292, "height": 754}, accept_downloads=True)
        self.page = self.context.new_page()
        self.page.set_default_timeout(6500)
        self.errors = []
        self.page.on("pageerror", lambda error: self.errors.append(str(error)))
        self.page.goto(self.base, wait_until="networkidle")

    def tearDown(self):
        self.page.screenshot(path=str(self.output / (self._testMethodName + ".png")), full_page=True)
        (self.output / (self._testMethodName + ".txt")).write_text(self.page.locator("body").inner_text())
        self.context.close()
        self.assertEqual(self.errors, [], "Browser exception")

    def open_query_editor(self):
        if not self.page.locator("#search-form").is_visible():
            self.page.locator("#edit-query").click()

    def select_filter(self, selector, value):
        if not self.page.locator("#result-filters").get_attribute("open") == "":
            self.page.locator("#result-filters summary").click()
        self.page.locator(selector).select_option(value)
        self.page.locator("#result-filters summary").click()

    def submit(self, description=DESCRIPTION):
        self.open_query_editor()
        self.page.locator("#description").fill(description)
        self.page.locator("#quantity").fill("3")
        self.page.locator("#search-submit").click()
        self.page.locator("#result-screen").wait_for(state="visible")
        self.page.wait_for_function("() => !document.querySelector('#search-submit').disabled")

    def evidence(self, offer_id="synthetic-a"):
        self.page.locator(f'[data-offer="{offer_id}"] [data-evidence]').click()
        self.page.locator("#evidence-dialog").wait_for(state="visible")

    def review(self, reason=""):
        self.page.locator("#review-reason").fill(reason)
        self.page.locator("#review-submit").click()
        self.page.get_by_text("Решение сохранено. Расчёт и отчёт обновлены.", exact=True).wait_for()

    def download(self, selector, name):
        with self.page.expect_download() as event:
            self.page.locator(selector).click()
        path = self.output / name
        event.value.save_as(path)
        self.assertIsNone(event.value.failure())
        return path

    def test_01_exclude_reset_updates_summary_revision_and_report(self):
        self.submit()
        original = self.page.locator(".total-price").inner_text()
        self.assertRegex(original, r"216\s*000")
        self.select_filter("#status-filter", "included")
        self.select_filter("#source-filter", "group:retail")
        self.evidence()
        self.review("Цена требует проверки QA")
        self.assertEqual(self.fixture.reviews[-1], {"offer_id": "synthetic-a", "action": "exclude", "reason": "Цена требует проверки QA"})
        self.assertRegex(self.page.locator(".total-price").inner_text(), r"220\s*500")
        self.assertEqual(self.page.locator("#status-filter").input_value(), "included")
        self.assertEqual(self.page.locator("#source-filter").input_value(), "group:retail")
        self.assertEqual(self.page.locator(".offer-card").count(), 2)
        self.assertIn("revision=1", self.page.locator("#report-json").get_attribute("href"))
        self.assertEqual(self.page.locator("#review-submit").inner_text(), "Вернуть исходное решение")
        self.review()
        self.assertEqual(self.fixture.reviews[-1]["action"], "reset")
        self.assertEqual(self.fixture.reviews[-1]["reason"], "Проверка пользователем")
        self.assertEqual(self.page.locator(".total-price").inner_text(), original)
        self.page.locator("#close-evidence").click()
        self.assertEqual(self.page.locator(".offer-card").count(), 3)
        self.assertTrue(self.page.locator('[data-offer="synthetic-a"] [data-evidence]').evaluate("node => node === document.activeElement"))
        self.assertLessEqual(self.page.locator(".offer-card").first.bounding_box()["y"], 360)

    def test_02_reset_does_not_force_uncertain_or_rejected_offer_into_calculation(self):
        self.submit()
        for offer_id, expected in [("synthetic-no-band", "Нужны уточнения"), ("synthetic-two-channels", "Вне расчёта")]:
            self.evidence(offer_id)
            self.review()
            self.review()
            self.assertEqual(self.page.locator("#evidence-body .status-pill").inner_text(), expected)
            self.page.locator("#close-evidence").click()
        self.assertEqual(self.fixture.jobs["qa-live-1"]["result"]["calculation"]["n"], 3)

    def test_03_hash_reload_history_and_uid_prefix(self):
        self.submit()
        self.assertEqual(urlsplit(self.page.url).fragment, "job=qa-live-1")
        self.page.reload(wait_until="networkidle")
        self.page.locator("#result-screen").wait_for(state="visible")
        self.assertEqual(len(self.fixture.jobs), 1)
        self.assertEqual(self.page.locator("#description").input_value(), DESCRIPTION)
        self.assertEqual(self.page.locator(".offer-card").count(), 5)
        for index in range(6):
            self.submit(DESCRIPTION + f"; QA {index + 2}")
        self.page.locator("#all-searches").click()
        self.page.wait_for_function("() => document.querySelectorAll('#history-list a').length === 7")
        links = self.page.locator("#history-list a")
        self.assertEqual(links.count(), 7)
        self.assertIn("#job=qa-live-1", links.evaluate_all("nodes => nodes.map(n => n.hash)"))
        self.page.locator('#history-list a[href="#job=qa-live-3"]').click()
        self.page.wait_for_url("**/#job=qa-live-3")
        self.page.wait_for_function("() => document.querySelector('#description').value.endsWith('QA 3')")
        self.assertTrue(all(c["path"].startswith(PREFIX) for c in self.fixture.calls if c["path"] != "/favicon.ico"), self.fixture.calls)
        self.assertIn(PREFIX + "api/jobs/qa-live-3/report.html", self.page.locator("#report-html").get_attribute("href"))

    def test_04_lazy_document_is_captured_text_and_not_executed(self):
        self.submit()
        self.evidence()
        self.assertFalse(any("/documents/" in call["path"] for call in self.fixture.calls))
        self.page.locator("[data-source-document] summary").click()
        self.page.locator("[data-source-document] pre").filter(has_text="Цена 69000").wait_for()
        self.assertIn("<script>window.qaInjected=1</script>", self.page.locator("[data-source-document] pre").inner_text())
        self.assertIsNone(self.page.evaluate("window.qaInjected"))
        self.assertEqual(sum("/documents/" in call["path"] for call in self.fixture.calls), 1)

    def test_05_review_error_is_retryable_and_keeps_result(self):
        self.submit()
        self.fixture.fail_review = True
        self.evidence()
        self.page.locator("#review-submit").click()
        self.page.get_by_text("Учебная ошибка сохранения. Повторите попытку.", exact=True).wait_for()
        self.assertTrue(self.page.locator("#review-submit").is_enabled())
        self.assertEqual(self.fixture.jobs["qa-live-1"]["revision"], 0)
        self.assertRegex(self.page.locator(".total-price").inner_text(), r"216\s*000")
        self.fixture.fail_review = False
        self.review()
        self.assertEqual(self.fixture.jobs["qa-live-1"]["revision"], 1)

    def test_06_interrupted_cancelled_failed_and_missing_job_are_distinct(self):
        self.submit()
        for status, expected in [("interrupted", "Поиск прерван после перезапуска"), ("cancelled", "Поиск остановлен"), ("failed", "Не удалось завершить поиск")]:
            self.fixture.jobs["qa-live-1"]["status"] = status
            self.page.reload(wait_until="networkidle")
            self.page.locator("#global-error").filter(has_text=expected).wait_for()
            self.assertTrue(self.page.locator("#description").is_editable())
            self.assertTrue(self.page.locator("#result-screen").is_visible())
        self.page.goto(self.base + "#job=missing", wait_until="networkidle")
        self.page.locator("#global-error").filter(has_text="Сохранённый поиск не найден").wait_for()
        self.assertTrue(self.page.locator("#search-submit").is_enabled())
        self.assertTrue(self.page.locator("#result-screen").is_hidden())

    def test_07_mobile_review_download_and_offline_clean_report(self):
        self.page.set_viewport_size({"width": 375, "height": 812})
        self.submit()
        self.evidence()
        self.review()
        self.assertFalse(self.page.evaluate("document.documentElement.scrollWidth > innerWidth"))
        box = self.page.locator("#evidence-dialog").bounding_box()
        self.assertGreaterEqual(box["x"], 0)
        self.assertLessEqual(box["x"] + box["width"], 376)
        self.page.locator("#close-evidence").click()
        data = json.loads(self.download("#report-json", "reviewed-report.json").read_text())
        self.assertEqual(data["review"], {"excluded_ids": ["synthetic-a"], "revision": 1})
        self.assertEqual(data["calculation"]["total"], 220500)
        report = self.download("#report-html", "reviewed-report.html")
        markup = report.read_text()
        self.assertIn("Исключено пользователем", markup)
        self.assertNotRegex(markup.lower(), r"<(?:script|form|nav)\b|offer-review-form|review-submit|отправить замечание|скачать этот файл")
        offline = self.browser.new_context(offline=True)
        try:
            page = offline.new_page()
            page.goto(report.as_uri(), wait_until="load")
            self.assertIn(DESCRIPTION, page.locator("body").inner_text())
            self.assertRegex(page.locator("body").inner_text(), r"220\s*500")
        finally:
            offline.close()

    def test_08_snapshot_adapter_has_no_manual_review_controls(self):
        self.submit()
        job = self.fixture.jobs["qa-live-1"]
        job["result"]["search"]["mode"] = "snapshot"
        self.page.reload(wait_until="networkidle")
        self.evidence()
        self.assertEqual(self.page.locator("#offer-review-form").count(), 0)
        self.assertIn("недоступна для сохранённого примера", self.page.locator("#evidence-body").inner_text())

    def test_09_scope_restores_on_reload_history_and_legacy_requests(self):
        self.open_query_editor()
        self.page.locator("#source-picker-summary").click()
        self.page.locator('#source-options input[value="domains"]').check()
        self.page.locator("#source-domains").fill("radioelementy.ru, terrapribor.ru")
        self.open_query_editor()
        self.page.locator("#source-picker-summary").click()
        self.submit()
        expected = {"mode": "domains", "domains": ["radioelementy.ru", "terrapribor.ru"]}
        self.assertEqual(self.fixture.payloads[-1]["source_scope"], expected)
        self.assertNotIn("source_ids", self.fixture.payloads[-1])
        self.page.reload(wait_until="networkidle")
        self.page.locator("#result-screen").wait_for(state="visible")
        self.assertTrue(self.page.locator('#source-options input[value="domains"]').is_checked())
        self.assertEqual(self.page.locator("#source-domains").input_value(), "radioelementy.ru, terrapribor.ru")
        self.page.locator("#all-searches").click()
        self.page.locator('#history-list a[href="#job=qa-live-1"]').click()
        self.page.locator("#result-screen").wait_for(state="visible")
        self.assertEqual(self.page.locator("#source-domains").input_value(), "radioelementy.ru, terrapribor.ru")
        # Old selected-source requests remain editable; web + a site was broad.
        for source_ids, mode, domains in [(["prist"], "domains", "prist.ru"), (["web", "prist"], "web", ""), (None, "web", "")]:
            legacy = self.fixture.new_job({"description": DESCRIPTION, "quantity": 4, "source_ids": source_ids})
            self.page.goto(self.base + "#job=" + legacy["id"], wait_until="networkidle")
            self.page.locator("#result-screen").wait_for(state="visible")
            self.assertTrue(self.page.locator(f'#source-options input[value="{mode}"]').is_checked())
            self.assertEqual(self.page.locator("#source-domains").input_value(), domains)
        self.assertIn("весь интернет", self.page.locator("#source-picker-summary").inner_text())
        self.assertEqual(sum(call["method"] == "POST" and call["path"].endswith("/api/search") for call in self.fixture.calls), 1)


class ArtifactResult(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.records = []

    def addSuccess(self, test):
        super().addSuccess(test)
        self.records.append({"test": test.id(), "status": "passed"})

    def addFailure(self, test, err):
        super().addFailure(test, err)
        self.records.append({"test": test.id(), "status": "failed", "detail": self._exc_info_to_string(err, test)})

    def addError(self, test, err):
        super().addError(test, err)
        self.records.append({"test": test.id(), "status": "failed", "detail": self._exc_info_to_string(err, test)})


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--test", action="append")
    args = parser.parse_args()
    os.environ["QUOTELENS_QA_OUTPUT"] = args.output_dir
    suite = unittest.TestSuite(LiveUiAcceptance(name) for name in args.test) if args.test else unittest.defaultTestLoader.loadTestsFromTestCase(LiveUiAcceptance)
    started = time.monotonic()
    result = unittest.TextTestRunner(verbosity=2, resultclass=ArtifactResult).run(suite)
    (Path(args.output_dir) / "acceptance-results.json").write_text(json.dumps({"fixture": "explicit_synthetic_http", "duration_seconds": round(time.monotonic() - started, 2), "run": result.testsRun, "failures": len(result.failures), "errors": len(result.errors), "checks": result.records}, ensure_ascii=False, indent=2))
    sys.exit(0 if result.wasSuccessful() else 1)
