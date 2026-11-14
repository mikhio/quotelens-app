"""User-path acceptance for the procurement interface.

Run the revised app first, then::

    python3 tests/ui_rework_acceptance.py --base-url http://127.0.0.1:8000 \
        --output-dir /path/to/run/qa --real-api

The deterministic checks serve the actual source UI in a local HTTP thread and
route its API calls to the existing calculation engine and saved source evidence. They never install data or scenarios in the product.
``--real-api`` additionally exercises the preview's actual API and downloads.
For a source server, ``--snapshot-api`` changes only the test's outgoing search
POST to the dated real catalog so this acceptance run causes no new web search.
A dedicated Chrome instance and isolated browser contexts are used throughout.
"""
from __future__ import annotations

import argparse
import copy
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import sys
import threading
import time
import unittest
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from quotelens.engine import analyze_offers, parse_request
from quotelens.scenarios import load_scenario
from quotelens.server import annotate_decisions, report_html

DESCRIPTION = "Осциллограф цифровой АКИП-4131/1В, 4 канала, полоса не менее 100 МГц"
OLD_COPY = (
    "Цена, у которой есть основания", "ПРИНЦИП QUOTELENS",
    "Каждая цифра ведёт к своему источнику", "Доступен режим снимков",
    "От предложений — к решению", "Не просто среднее",
    "Доступность источников",
    "Сохранённый сценарий", "Снимок данных", "Режим исследования",
    "Ближе к первоисточнику", "Профиль исследования",
)


class HttpEvidenceFixture:
    """Explicit test-only HTTP responses, computed by the real price engine."""

    def __init__(self, scenario="real-oscilloscopes", extra_sources=False,
                 error=None, malicious=False, currency=None, grouped_sources=False, rejected_price=False):
        self.scenario = scenario
        self.extra_sources = extra_sources
        self.error = error
        self.malicious = malicious
        self.currency = currency
        self.grouped_sources = grouped_sources
        self.rejected_price = rejected_price
        self.payloads = []
        self.jobs = {}
        sample = load_scenario(scenario)
        ids = {"prist.ru": "prist", "souz-pribor.ru": "souz",
               "elizpribor.ru": "eliz", "aktakom.ru": "aktakom"}
        self.sources = [{"id": ids.get(o["domain"], "qa-source-" + str(index)), "name": o["supplier"],
                         "domain": o["domain"], "url": o["url"],
                         "kind": "catalog", "active": True,
                         "connected": True, "available": True,
                         "description": "Подключённый источник"}
                        for index, o in enumerate(sample["offers"])
                        if o["domain"] not in {prior["domain"] for prior in sample["offers"][:index]}]
        # A planned integration must not appear as a selectable source.
        self.sources.append({"id": "not-connected", "name": "Неподключённая интеграция",
                             "domain": "not-connected.example.invalid",
                             "url": "https://not-connected.example.invalid/",
                             "kind": "reference", "active": False,
                             "connected": False, "available": False,
                             "description": "Не реализован"})

    def result(self, payload):
        description = payload["description"]
        request = parse_request(description, payload["quantity"])
        if "несуществующий товар" in description.lower():
            sample = {"offers": [], "source_statuses": [], "kind": "synthetic"}
        else:
            sample = load_scenario(self.scenario)
        offers = copy.deepcopy(sample["offers"])
        scope = payload.get("source_scope", {})
        selected = payload.get("source_ids")
        domains = set(scope["domains"]) if scope.get("mode") == "domains" else None
        if domains is None and selected is not None:
            domains = {s["domain"] for s in self.sources if s["id"] in selected}
        if domains is not None:
            offers = [o for o in offers if o["domain"] in domains]
        if self.currency:
            # Currency display QA uses only explicitly synthetic offers.
            if any(o.get("provenance") != "synthetic" for o in offers):
                raise ValueError("Currency fixture must be visibly synthetic")
            for offer in offers:
                offer["currency"] = self.currency
                offer["evidence"]["price"]["quote"] = (
                    "СИНТЕТИКА: цена " + str(offer["price"]) + " " + self.currency)
        if self.malicious and offers:
            offers[0]["title"] = '<img src=x onerror="window.qaInjected=1">'
            offers[0]["evidence"]["price"]["quote"] = '<script>window.qaInjected=2</script>'
        result = annotate_decisions(analyze_offers(request, offers))
        if self.rejected_price:
            for offer in result["offers"]:
                if offer.get("price") is None:
                    offer["validation"] = {"rejected_fields": ["price"]}
            result["validation_notice"] = "Учебная проверка: страницы каталогов убраны из предложений."
            result["discovery_pages"] = [{"title": "Каталог QA, не товар", "url": "https://catalog.example.invalid/"}]
        statuses = copy.deepcopy(sample.get("source_statuses", []))
        if domains is not None:
            statuses = [s for s in statuses if s["domain"] in domains]
        kind = sample.get("kind", "synthetic")
        if self.extra_sources:
            kind = "synthetic"
            statuses.extend([
                {"name": "Учебный источник без предложений", "domain": "empty.example.invalid",
                 "status": "no_results", "message": "Предложений по запросу нет"},
                {"name": "Учебный недоступный источник", "domain": "failed.example.invalid",
                 "status": "failed", "message": "Источник не ответил"},
            ])
        if self.grouped_sources:
            kind = "synthetic"
            examples = [("radioelementy.ru", "РадиоЭлемент (QA)", "retail", "Магазины и поставщики"),
                        ("brand.example.invalid", "Учебный бренд", "manufacturers", "Сайты брендов и производителей"),
                        ("records.example.invalid", "Учебный реестр", "public_records", "Реестры договоров")]
            for offer, (domain, name, group, label) in zip(result["offers"], examples):
                offer.update(domain=domain, supplier=name, source_group=group, source_group_label=label)
            statuses = [{"name": "Посещённый учебный каталог", "domain": "catalog.example.invalid",
                         "status": "no_results", "source_group": "industry", "source_group_label": "Промышленные каталоги"}]
        result["search"] = {"mode": "snapshot", "scenario_kind": kind,
                            "captured_at": sample.get("captured_at"),
                            "started_at": "2026-11-11T19:35:00+00:00",
                            "finished_at": "2026-11-11T19:35:33+00:00",
                            "min_suppliers": 3, "source_statuses": statuses,
                            "completeness": "bounded_not_exhaustive",
                            "stop": {"code": "snapshot_complete", "label": "Поиск завершён",
                                     "detail": "Проверена ограниченная выборка"},
                            "budget": {"pages_used": len(offers), "max_pages": 20,
                                       "queries_used": 1, "max_queries": 6,
                                       "model_calls": 0, "max_model_calls": 8,
                                       "elapsed_seconds": 0, "max_seconds": 90}}
        return result

    def route(self, route):
        request = route.request
        path = urlsplit(request.url).path
        if path == "/api/config":
            return route.fulfill(json={"model": {"configured": False, "name": None},
                                       "search": {"configured": False},
                                       "defaults": {"min_suppliers": 3, "max_pages": 20,
                                                    "max_queries": 6, "max_seconds": 90,
                                                    "max_model_calls": 8}, "version": "0.1.0"})
        if path == "/api/sources":
            return route.fulfill(json=self.sources)
        if path == "/api/scenarios":
            return route.fulfill(json=[])
        if path == "/api/jobs":
            # These dated QA snapshots are outside the live search history.
            return route.fulfill(json={"items": [], "next_cursor": None, "total": 0})
        if path == "/api/search":
            payload = request.post_data_json
            self.payloads.append(payload)
            if self.error:
                return route.fulfill(status=503, json={"error": self.error})
            job_id = "qa-http-" + str(len(self.payloads))
            self.jobs[job_id] = {"id": job_id, "status": "complete", "events": [],
                                 "request": {key: copy.deepcopy(payload[key]) for key in
                                             ("description", "quantity", "source_ids", "source_scope") if key in payload},
                                 "result": self.result(payload)}
            return route.fulfill(status=202, json={"id": job_id, "status": "running",
                                                  "request": self.jobs[job_id]["request"]})
        match = re.fullmatch(r"/api/jobs/([^/]+)(?:/(report\.(?:html|json)))?", path)
        if match and match[1] in self.jobs:
            job = self.jobs[match[1]]
            if match[2]:
                # Chrome downloads need a real HTTP response, not route.fulfill.
                return route.continue_()
            return route.fulfill(json=job)
        return route.fulfill(status=404, json={"error": "Неизвестный тестовый HTTP endpoint"})


class QuietStaticHandler(SimpleHTTPRequestHandler):
    def do_GET(self):
        match = re.fullmatch(r"/api/jobs/([^/]+)/(report\.(?:html|json))", urlsplit(self.path).path)
        fixture = getattr(self.server, "fixture", None)
        if match and fixture is not None and match[1] in fixture.jobs:
            result = fixture.jobs[match[1]]["result"]
            if match[2] == "report.html":
                body = report_html(result).encode("utf-8")
                mime = "text/html; charset=utf-8"
            else:
                body = json.dumps(result, ensure_ascii=False).encode("utf-8")
                mime = "application/json; charset=utf-8"
            self.send_response(200)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Content-Disposition", 'attachment; filename="quotelens-' + match[2] + '"')
            self.end_headers()
            self.wfile.write(body)
            return
        return super().do_GET()

    def log_message(self, *args):
        pass

    def translate_path(self, path):
        # The source HTML uses its deployed /static/ URL prefix.
        if path.startswith("/static/"):
            path = path[len("/static"):]
        return super().translate_path(path)


class ReworkedUiAcceptance(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.base = os.environ.get("QUOTELENS_BASE_URL", "").rstrip("/")
        if not cls.base:
            raise unittest.SkipTest("Set QUOTELENS_BASE_URL or pass --base-url")
        if urlsplit(cls.base).scheme not in {"http", "https"}:
            raise ValueError("The preview URL must use HTTP(S)")
        from playwright.sync_api import sync_playwright
        cls.output = Path(os.environ["QUOTELENS_QA_OUTPUT"])
        cls.output.mkdir(parents=True, exist_ok=True)
        cls.fixture_server = ThreadingHTTPServer(
            ("127.0.0.1", 0), partial(QuietStaticHandler, directory=str(ROOT / "static")))
        cls.fixture_thread = threading.Thread(target=cls.fixture_server.serve_forever, daemon=True)
        cls.fixture_thread.start()
        cls.fixture_base = "http://127.0.0.1:" + str(cls.fixture_server.server_port)
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(
            executable_path="/usr/bin/google-chrome", headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"])

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()
        cls.fixture_server.shutdown()
        cls.fixture_server.server_close()
        cls.fixture_thread.join(timeout=2)

    def setUp(self):
        self.context = self.browser.new_context(viewport={"width": 1292, "height": 754},
                                                accept_downloads=True)
        self.page = self.context.new_page()
        self.page.set_default_timeout(6000)
        self.errors = []
        self.page.on("pageerror", lambda error: self.errors.append(str(error)))
        self.fixture = None

    def tearDown(self):
        # Always retain a rendered state; a failing path is therefore reviewable.
        self.page.screenshot(path=str(self.output / (self._testMethodName + ".png")),
                             full_page=True)
        (self.output / (self._testMethodName + ".txt")).write_text(
            self.page.locator("body").inner_text(), encoding="utf-8")
        self.context.close()
        self.assertEqual(self.errors, [], "Unexpected browser errors")

    def open_fixture(self, **options):
        self.fixture = HttpEvidenceFixture(**options)
        self.fixture_server.fixture = self.fixture
        self.page.route("**/api/**", self.fixture.route)
        self.page.goto(self.fixture_base, wait_until="networkidle")
        self.page.locator("#search-submit").wait_for()
        self.page.locator('#source-picker input[name="source_scope_mode"]').first.wait_for(state="attached")
        return self.fixture

    def open_query_editor(self):
        if not self.page.locator("#search-form").is_visible():
            self.page.locator("#edit-query").click()

    def select_filter(self, selector, value):
        if not self.page.locator("#result-filters").get_attribute("open") == "":
            self.page.locator("#result-filters summary").click()
        self.page.locator(selector).select_option(value)
        self.page.locator("#result-filters summary").click()

    def submit(self, description=DESCRIPTION, quantity=3):
        self.open_query_editor()
        self.page.locator("#description").fill(description)
        self.page.locator("#quantity").fill(str(quantity))
        self.page.locator("#search-submit").click()
        self.page.locator("#result-screen").wait_for(state="visible")
        # Polling and DOM rendering must have finished, not merely the HTTP POST.
        self.page.wait_for_function("() => document.querySelector('#status-filter') && !document.querySelector('#search-submit').disabled")

    def card_count(self):
        return self.page.locator("#offer-list .offer-card").count()

    def source_options(self):
        return self.page.locator("#source-filter option").evaluate_all(
            "nodes => nodes.map(n => ({value:n.value,text:n.textContent}))")

    def assert_source_domains(self, expected):
        values = {o["value"] for o in self.source_options() if o["value"] != "all" and not o["value"].startswith("group:")}
        self.assertEqual(values, set(expected))

    def assert_clean_product(self):
        visible = self.page.locator("body").inner_text().lower()
        for text in OLD_COPY:
            self.assertNotIn(text.lower(), visible)
        for name in ["Сценарии", "Параметры", "Диагностика источников", "FAQ"]:
            self.assertEqual(self.page.get_by_role("button", name=name, exact=True).count(), 0)
            self.assertEqual(self.page.get_by_role("link", name=name, exact=True).count(), 0)
        self.assertEqual(self.page.locator('input[name="mode"], select[name="mode"], #scenario-select').count(), 0)
        self.assertEqual(self.page.locator('input[type="password"]').count(), 0)

    def assert_no_overflow(self):
        size = self.page.evaluate("""() => ({viewport:innerWidth,
            root:document.documentElement.scrollWidth,body:document.body.scrollWidth})""")
        self.assertLessEqual(max(size["root"], size["body"]), size["viewport"] + 1, size)

    def test_01_compact_start_and_explicit_domain_scope(self):
        fixture = self.open_fixture()
        self.assert_clean_product()
        self.assertEqual(self.page.locator("#description").input_value(), "")
        self.assertEqual(self.page.locator('#source-picker input[name="source_scope_mode"]').count(), 2)
        self.assertEqual(self.page.locator('#source-picker input[type="checkbox"]').count(), 0)
        self.assertTrue(self.page.locator('#source-picker input[value="web"]').is_checked())
        self.assertEqual(self.page.locator('input[value="not-connected"]').count(), 0)
        box = self.page.locator("#search-form").bounding_box()
        self.assertLess(box["y"], 160, "The usable form is buried under introductory content")
        self.assertLess(box["y"] + box["height"], 500, "The initial query is not compact")
        self.open_query_editor()
        self.page.locator("#source-picker-summary").click()
        self.page.locator('#source-picker input[value="domains"]').check()
        self.page.locator('#source-domains').fill("WWW.PRIST.RU, souz-pribor.ru\nprist.ru")
        self.open_query_editor()
        self.page.locator("#source-picker-summary").click()
        self.submit()
        payload = fixture.payloads[-1]
        self.assertEqual(payload["mode"], "live")
        self.assertEqual(payload["source_scope"], {"mode": "domains", "domains": ["prist.ru", "souz-pribor.ru"]})
        self.assertNotIn("source_ids", payload)
        self.assertNotIn("scenario_id", payload)
        self.assertEqual(self.card_count(), 2)
        self.assert_clean_product()

    def test_02_result_is_immediate_compact_and_material_conditions_remain(self):
        self.open_fixture()
        self.submit()
        self.assert_clean_product()
        self.assertEqual(self.card_count(), 4)
        first = self.page.locator("#offer-list .offer-card").first.bounding_box()
        self.assertLessEqual(first["y"], 360, "The first offer is buried below the result controls")
        text = self.page.locator("#calculation-summary").inner_text().lower()
        self.assertRegex(text, r"207\s*195")
        self.assertRegex(text, r"69\s*065")
        self.assertRegex(text, r"3")
        self.assertNotIn("Доставка в итог не включена", text)
        self.assertRegex(self.page.locator("#result-date").inner_text(), r"(?:11[./]11[./]2026|11\s+ноябр[яь]\s+2026)")
        self.assertEqual(self.page.get_by_role("heading", name="Ограничения и необходимые уточнения").count(), 0)
        self.assertEqual(self.page.locator("#warnings-list, .warnings-list, .warning-list").count(), 0)
        self.assert_no_overflow()

    def test_03_status_source_facets_and_incompatible_selection_reconcile(self):
        self.open_fixture()
        self.submit()
        all_domains = {"prist.ru", "souz-pribor.ru", "elizpribor.ru", "aktakom.ru"}
        self.assert_source_domains(all_domains)
        self.select_filter("#source-filter", "aktakom.ru")
        self.assertEqual(self.card_count(), 1)
        self.assertIn("ADS-2031", self.page.locator("#offer-list").inner_text())
        self.select_filter("#status-filter", "included")
        self.assert_source_domains(all_domains - {"aktakom.ru"})
        self.assertEqual(self.page.locator("#source-filter").input_value(), "all")
        self.assertEqual(self.card_count(), 3)
        self.select_filter("#source-filter", "prist.ru")
        self.assertEqual(self.card_count(), 1)
        self.select_filter("#status-filter", "excluded")
        self.assert_source_domains({"aktakom.ru"})
        self.assertEqual(self.page.locator("#source-filter").input_value(), "all")
        self.assertEqual(self.card_count(), 1)
        self.select_filter("#status-filter", "uncertain")
        self.assert_source_domains(set())
        self.assertEqual(self.card_count(), 0)
        self.assertRegex(self.page.locator("#offer-list").inner_text().lower(), r"нет|найден|услови")
        self.select_filter("#status-filter", "all")
        self.assert_source_domains(all_domains)
        self.assertEqual(self.card_count(), 4)

    def test_04_visited_failed_and_no_result_sources_are_in_all_facet(self):
        self.open_fixture(extra_sources=True)
        self.submit()
        self.assert_source_domains({"prist.ru", "souz-pribor.ru", "elizpribor.ru",
                                    "aktakom.ru", "empty.example.invalid", "failed.example.invalid"})
        self.assertIn("Учебные данные", self.page.locator("body").inner_text())
        self.select_filter("#source-filter", "empty.example.invalid")
        self.assertEqual(self.card_count(), 0)
        self.assertRegex(self.page.locator("#offer-list").inner_text().lower(), r"нет|найден|предложен")
        self.select_filter("#status-filter", "included")
        self.assert_source_domains({"prist.ru", "souz-pribor.ru", "elizpribor.ru"})
        self.assertEqual(self.page.locator("#source-filter").input_value(), "all")
        self.assertEqual(self.card_count(), 3)

    def test_05_changed_query_quantity_and_empty_results_do_not_keep_old_cards(self):
        fixture = self.open_fixture()
        self.submit(quantity=3)
        self.submit(quantity=5)
        self.assertEqual(fixture.payloads[-1]["quantity"], 5)
        self.assertRegex(self.page.locator("#calculation-summary").inner_text(), r"345\s*325")
        query = "Несуществующий товар, учебная проверка пустого результата"
        self.submit(description=query, quantity=7)
        self.assertEqual(fixture.payloads[-1]["description"], query)
        self.assertEqual(fixture.payloads[-1]["quantity"], 7)
        self.assertEqual(self.page.locator("#description").input_value(), query)
        self.assertEqual(self.card_count(), 0)
        self.assertNotIn("АКИП-4131", self.page.locator("#offer-list").inner_text())
        self.assertRegex(self.page.locator("#offer-list").inner_text().lower(), r"нет|найден|предложен")
        self.assertNotRegex(self.page.locator("#calculation-summary").inner_text(), r"345\s*325")

    def test_06_evidence_dialog_keyboard_and_grid(self):
        self.open_fixture()
        self.submit()
        self.page.locator("#layout-grid").click()
        self.assertEqual(self.card_count(), 4)
        self.page.locator("#layout-list").click()
        self.assertEqual(self.card_count(), 4)
        button = self.page.locator("#offer-list [data-evidence]").first
        button.focus()
        self.page.keyboard.press("Enter")
        dialog = self.page.locator("#evidence-dialog")
        dialog.wait_for(state="visible")
        self.assertRegex(dialog.inner_text(), r"69\s*065")
        self.assertGreater(dialog.locator('a[href^="https://"]').count(), 0)
        self.assertRegex(dialog.inner_text().lower(), r"единиц|шт")
        self.page.keyboard.press("Escape")
        dialog.wait_for(state="hidden")
        self.assertTrue(button.evaluate("node => node === document.activeElement"))

    def test_07_synthetic_unknowns_are_labelled_and_not_rendered_as_zero(self):
        self.open_fixture(scenario="synthetic-missing")
        self.submit(description=load_scenario("synthetic-missing")["request"]["description"])
        self.assertIn("Учебные данные", self.page.locator("body").inner_text())
        self.assertEqual(self.card_count(), 3)
        self.select_filter("#status-filter", "uncertain")
        self.assertEqual(self.card_count(), 3)
        no_price = self.page.locator(".offer-card").filter(has_text="SYN-NO-PRICE")
        self.assertEqual(no_price.count(), 1)
        self.assertRegex(no_price.inner_text().lower(), r"не указ|нет цен|цен[аы].*неизвест|неизвест.*цен")
        self.assertNotRegex(no_price.inner_text(), r"(?:^|\s)0(?:[,.]00)?\s*₽")
        self.assertNotRegex(self.page.locator("#calculation-summary").inner_text(), r"(?:^|\s)0(?:[,.]00)?\s*₽")

    def test_08_description_title_and_evidence_are_escaped(self):
        self.open_fixture(scenario="synthetic-comparable", malicious=True)
        marker = '<svg onload="window.qaInjected=3">учебный запрос</svg>'
        self.submit(description=DESCRIPTION + " " + marker)
        self.assertIsNone(self.page.evaluate("window.qaInjected"))
        self.assertEqual(self.page.locator('#offer-list img[src="x"]').count(), 0)
        self.assertIn('<img src=x onerror="window.qaInjected=1">', self.page.locator("#offer-list").inner_text())
        self.page.locator("#offer-list [data-evidence]").first.click()
        dialog = self.page.locator("#evidence-dialog")
        dialog.wait_for(state="visible")
        self.assertIn('<script>window.qaInjected=2</script>', dialog.inner_text())
        self.assertIsNone(self.page.evaluate("window.qaInjected"))

    def test_09_empty_input_and_http_error_keep_a_recoverable_form(self):
        fixture = self.open_fixture(error="Источник временно недоступен, повторите поиск")
        self.page.locator("#description").fill("")
        self.page.locator("#search-submit").click()
        self.assertEqual(fixture.payloads, [])
        self.page.locator("#description").fill(DESCRIPTION)
        self.page.locator("#quantity").fill("0")
        self.page.locator("#search-submit").click()
        self.assertEqual(fixture.payloads, [])
        self.page.locator("#quantity").fill("3")
        self.page.locator("#search-submit").click()
        self.page.get_by_text("Источник временно недоступен, повторите поиск", exact=False).first.wait_for()
        self.assertEqual(len(fixture.payloads), 1)
        self.assertTrue(self.page.locator("#description").is_editable())
        self.assertTrue(self.page.locator("#search-submit").is_enabled())
        self.assertEqual(self.card_count(), 0)
        self.page.screenshot(path=str(self.output / "recoverable-http-error.png"), full_page=True)
        fixture.error = None
        self.submit()
        self.assertEqual(len(fixture.payloads), 2)
        self.assertEqual(self.card_count(), 4)
        self.assertTrue(self.page.locator("#global-error").is_hidden())

    def test_10_mobile_no_horizontal_overflow_in_form_results_and_evidence(self):
        self.page.set_viewport_size({"width": 375, "height": 812})
        self.open_fixture()
        self.assert_no_overflow()
        self.open_query_editor()
        self.page.locator("#source-picker-summary").click()
        self.page.locator('#source-picker input[value="domains"]').check()
        self.page.locator('#source-domains').fill("prist.ru, souz-pribor.ru, elizpribor.ru, aktakom.ru")
        self.assert_no_overflow()
        self.page.screenshot(path=str(self.output / "domain-scope-mobile.png"), full_page=True)
        self.open_query_editor()
        self.page.locator("#source-picker-summary").click()
        self.submit()
        self.assert_no_overflow()
        self.page.locator("#layout-grid").click()
        self.assert_no_overflow()
        self.page.locator("#offer-list [data-evidence]").first.click()
        self.page.locator("#evidence-dialog").wait_for(state="visible")
        self.assert_no_overflow()
        dialog_box = self.page.locator("#evidence-dialog").bounding_box()
        self.assertGreaterEqual(dialog_box["x"], 0)
        self.assertLessEqual(dialog_box["x"] + dialog_box["width"], 376)

    def download_report(self, selector, filename):
        with self.page.expect_download() as event:
            self.page.locator(selector).click()
        path = self.output / filename
        event.value.save_as(str(path))
        self.assertIsNone(event.value.failure())
        return path

    def assert_clean_download(self, path, expected_description):
        markup = path.read_text(encoding="utf-8")
        self.assertIn(expected_description, markup)
        self.assertNotRegex(markup.lower(), r"<(?:script|form|nav)\b")
        self.assertNotRegex(markup.lower(), r"<(?:script|link|img)\b[^>]*(?:src|href)=[\"']https?://")
        self.assertNotRegex(markup.lower(), r"review-shell|feedback-shell|отправить замечание|скачать этот файл")
        offline = self.browser.new_context(offline=True)
        try:
            page = offline.new_page()
            page.goto(path.as_uri(), wait_until="load")
            self.assertIn(expected_description, page.locator("body").inner_text())
            self.assertGreater(page.locator('a[href^="https://"]').count(), 0)
            page.screenshot(path=str(path.with_suffix(".offline.png")), full_page=True)
        finally:
            offline.close()

    def test_11_downloaded_html_and_json_are_clean_standalone_result(self):
        self.open_fixture()
        self.submit()
        html_path = self.download_report("#report-html", "fixture-report.html")
        json_path = self.download_report("#report-json", "fixture-report.json")
        self.assert_clean_download(html_path, DESCRIPTION)
        data = json.loads(json_path.read_text(encoding="utf-8"))
        self.assertEqual(data["request"]["quantity"], 3)
        self.assertEqual(data["calculation"]["total"], 207195)
        self.assertEqual(len(data["offers"]), 4)
        self.assertTrue(all(o["evidence"] for o in data["offers"]))

    def test_12_actual_preview_query_images_evidence_and_downloads(self):
        if os.environ.get("QUOTELENS_REAL_API") != "1":
            self.skipTest("Use --real-api for integrated preview/API check")
        # The source-server option changes only search POST provenance in this
        # test; job polling, photos, evidence and downloads use the actual API.
        outgoing = []
        if os.environ.get("QUOTELENS_SNAPSHOT_API") == "1":
            def dated_real_search(route):
                payload = route.request.post_data_json
                outgoing.append(copy.deepcopy(payload))
                payload.update(mode="snapshot", scenario_id="real-oscilloscopes")
                route.continue_(post_data=json.dumps(payload, ensure_ascii=False))
            self.page.route("**/api/search", dated_real_search)
        self.page.goto(self.base, wait_until="networkidle")
        self.page.locator("#search-submit").wait_for()
        self.page.wait_for_function("() => !document.querySelector('#search-submit').disabled")
        self.submit()
        if outgoing:
            self.assertEqual(outgoing[-1]["mode"], "live")
            self.assertNotIn("scenario_id", outgoing[-1])
        self.assert_clean_product()
        self.assertEqual(self.card_count(), 4)
        self.assertLessEqual(self.page.locator("#offer-list .offer-card").first.bounding_box()["y"], 360)
        self.assert_source_domains({"prist.ru", "souz-pribor.ru", "elizpribor.ru", "aktakom.ru"})
        self.select_filter("#status-filter", "included")
        self.assertEqual(self.card_count(), 3)
        self.assert_source_domains({"prist.ru", "souz-pribor.ru", "elizpribor.ru"})
        self.select_filter("#status-filter", "excluded")
        self.assertEqual(self.card_count(), 1)
        self.assert_source_domains({"aktakom.ru"})
        self.assertIn("ADS-2031", self.page.locator("#offer-list").inner_text())
        self.select_filter("#status-filter", "all")
        self.assertEqual(self.card_count(), 4)
        images = self.page.locator("#offer-list .offer-image")
        self.assertGreater(images.count(), 0, "No actual source product photo reached the result")
        self.page.wait_for_function("() => [...document.querySelectorAll('#offer-list img.offer-image')].every(n => n.complete && n.naturalWidth > 1)")
        self.assertTrue(images.evaluate_all("nodes => nodes.every(n => /^(data:image\\/|\\/|https?:)/.test(n.currentSrc))"))
        self.page.locator("#offer-list [data-evidence]").first.click()
        self.page.locator("#evidence-dialog").wait_for(state="visible")
        self.assertRegex(self.page.locator("#evidence-dialog").inner_text(), r"69\s*065")
        self.page.locator("#close-evidence").click()
        html_path = self.download_report("#report-html", "actual-report.html")
        json_path = self.download_report("#report-json", "actual-report.json")
        self.assert_clean_download(html_path, DESCRIPTION)
        data = json.loads(json_path.read_text(encoding="utf-8"))
        self.assertEqual(data["request"]["quantity"], 3)
        self.assertEqual(data["calculation"]["total"], 207195)
        self.assertEqual(len(data["offers"]), 4)
        self.assertFalse(any(o.get("provenance") == "synthetic" for o in data["offers"]))
        self.assertTrue(any(o.get("image") for o in data["offers"]), "Photo must be delivered from actual API evidence")
        pictured = [o for o in data["offers"] if o.get("image")]
        self.assertTrue(all(o.get("image_url", "").startswith("https://") and
                            o.get("image_source_url", "").startswith("https://") for o in pictured),
                        "Real product photos must keep source provenance")
        self.assert_no_overflow()
        self.page.screenshot(path=str(self.output / "actual-real-result.png"), full_page=True)
        self.submit(quantity=5)
        self.assertRegex(self.page.locator("#calculation-summary").inner_text(), r"345\s*325")
        if os.environ.get("QUOTELENS_SNAPSHOT_API") != "1":
            self.submit(quantity=0.001)
            self.assertRegex(self.page.locator("#calculation-summary").inner_text(), r"69[,.]07")
            self.assertIn("0,001", self.page.locator("#calculation-summary").inner_text())
            # Public dated catalog: source selection must actually change offers.
            self.open_query_editor()
            self.page.locator("#source-picker-summary").click()
            self.page.locator('#source-picker input[value="domains"]').check()
            self.page.locator('#source-domains').fill("prist.ru, souz-pribor.ru")
            self.open_query_editor()
            self.page.locator("#source-picker-summary").click()
            self.submit(quantity=5)
            self.assertEqual(self.card_count(), 2)
            self.assert_source_domains({"prist.ru", "souz-pribor.ru"})
            self.assertNotRegex(self.page.locator("#calculation-summary").inner_text(), r"345\s*325")
            self.open_query_editor()
            self.page.locator("#source-picker-summary").click()
            self.page.locator('#source-picker input[value="web"]').check()
            self.open_query_editor()
            self.page.locator("#source-picker-summary").click()
            self.submit(description="Несуществующий товар для проверки пустого результата", quantity=1)
            self.assertEqual(self.card_count(), 0, "An unrelated query reused the oscilloscope cards")
            self.assertNotRegex(self.page.locator("#calculation-summary").inner_text(), r"345\s*325")


    def test_13_foreign_currency_is_preserved_without_a_fabricated_ruble_price(self):
        self.open_fixture(scenario="synthetic-one", currency="USD")
        self.submit(description=load_scenario("synthetic-one")["request"]["description"])
        self.assertIn("Учебные данные", self.page.locator("body").inner_text())
        self.assertEqual(self.card_count(), 1)
        card = self.page.locator("#offer-list .offer-card").first
        self.assertRegex(card.inner_text(), r"100(?:[,.]00)?\s*(?:USD|\$)")
        self.assertNotRegex(card.inner_text(), r"100(?:[,.]00)?\s*₽")
        self.assertNotRegex(self.page.locator("#calculation-summary").inner_text(), r"100(?:[,.]00)?\s*₽")
        card.locator("[data-evidence]").click()
        dialog = self.page.locator("#evidence-dialog")
        dialog.wait_for(state="visible")
        self.assertRegex(dialog.inner_text(), r"100(?:[,.]00)?\s*(?:USD|\$)")
        self.assertNotRegex(dialog.inner_text(), r"100(?:[,.]00)?\s*₽")

    def test_14_scope_is_exclusive_and_invalid_domains_never_submit(self):
        fixture = self.open_fixture()
        self.page.locator("#description").fill(DESCRIPTION)
        self.open_query_editor()
        self.page.locator("#source-picker-summary").click()
        self.page.locator('#source-options input[value="domains"]').check()
        self.assertFalse(self.page.locator('#source-options input[value="web"]').is_checked())
        for value in ["", "https://prist.ru/catalog/", "127.0.0.1", "localhost", "user@radioelementy.ru", "radioelementy.ru/path", ",".join(f"site{index}.ru" for index in range(33))]:
            self.page.locator("#source-domains").fill(value)
            self.page.locator("#search-submit").click()
            self.assertTrue(self.page.locator("#source-error").is_visible(), value)
            self.assertEqual(fixture.payloads, [], value)
        self.page.locator('#source-options input[value="web"]').check()
        self.assertFalse(self.page.locator('#source-options input[value="domains"]').is_checked())
        self.assertTrue(self.page.locator("#source-domains").is_hidden())
        self.open_query_editor()
        self.page.locator("#source-picker-summary").click()
        self.submit()
        self.assertEqual(fixture.payloads[-1]["source_scope"], {"mode": "web", "domains": []})
        self.assertNotIn("source_ids", fixture.payloads[-1])
        self.assertEqual(self.card_count(), 4)

    def test_15_grouped_types_include_discovered_sites_and_status_facets_reconcile(self):
        fixture = self.open_fixture(scenario="synthetic-comparable", grouped_sources=True)
        self.assertFalse(any(source.get("domain") == "radioelementy.ru" for source in fixture.sources))
        self.submit(description=load_scenario("synthetic-comparable")["request"]["description"])
        options = self.source_options()
        self.assertIn("radioelementy.ru", {option["value"] for option in options})
        self.assertIn("radioelementy.ru", next(option["text"] for option in options if option["value"] == "radioelementy.ru"))
        labels = self.page.locator("#source-filter optgroup").evaluate_all("nodes => nodes.map(node => node.label)")
        self.assertIn("Магазины и поставщики", labels)
        self.assertIn("Сайты брендов и производителей", labels)
        self.assertIn("Промышленные каталоги", labels)
        self.assertIn("Другие открытые сайты", labels)
        self.select_filter("#source-filter", "group:retail")
        self.assertEqual(self.card_count(), 1)
        self.assertIn("РадиоЭлемент", self.page.locator("#offer-list").inner_text())
        self.select_filter("#source-filter", "group:industry")
        self.assertEqual(self.card_count(), 0)
        self.select_filter("#status-filter", "included")
        self.assertEqual(self.page.locator("#source-filter").input_value(), "all")
        self.assertNotIn("catalog.example.invalid", {option["value"] for option in self.source_options()})
        self.select_filter("#source-filter", "group:retail")
        self.assertEqual(self.card_count(), 1)
        self.select_filter("#status-filter", "excluded")
        self.assertEqual(self.page.locator("#source-filter").input_value(), "all")
        self.select_filter("#status-filter", "all")
        self.assertIn("catalog.example.invalid", {option["value"] for option in self.source_options()})

    def test_16_rejected_price_and_catalog_correction_notice_are_truthful(self):
        self.open_fixture(scenario="synthetic-missing", rejected_price=True)
        self.submit(description=load_scenario("synthetic-missing")["request"]["description"])
        card = self.page.locator(".offer-card").filter(has_text="SYN-NO-PRICE")
        self.assertEqual(card.locator(".offer-price").inner_text(), "Цена не подтверждена")
        self.assertIn("страницы каталогов убраны", self.page.locator("#validation-notice").inner_text())
        self.assertNotIn("Каталог QA, не товар", self.page.locator("#offer-list").inner_text())
        card.locator("[data-evidence]").click()
        self.assertIn("Цена в источнике: не подтверждена", self.page.locator("#evidence-dialog").inner_text())


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

    def addSkip(self, test, reason):
        super().addSkip(test, reason)
        self.records.append({"test": test.id(), "status": "skipped", "detail": reason})


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default=os.environ.get("QUOTELENS_BASE_URL"))
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--real-api", action="store_true")
    parser.add_argument("--snapshot-api", action="store_true",
                        help="Only in this test, use actual dated catalog API instead of a new network search")
    parser.add_argument("--test", action="append", help="Optional unittest method name; repeat for targeted checks")
    args = parser.parse_args()
    if args.base_url:
        os.environ["QUOTELENS_BASE_URL"] = args.base_url
    os.environ["QUOTELENS_QA_OUTPUT"] = args.output_dir
    if args.real_api:
        os.environ["QUOTELENS_REAL_API"] = "1"
    if args.snapshot_api:
        os.environ["QUOTELENS_SNAPSHOT_API"] = "1"
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(ReworkedUiAcceptance)
    if args.test:
        suite = unittest.TestSuite(ReworkedUiAcceptance(name) for name in args.test)
    started = time.monotonic()
    result = unittest.TextTestRunner(verbosity=2, resultclass=ArtifactResult).run(suite)
    output = Path(args.output_dir)
    output.mkdir(parents=True, exist_ok=True)
    (output / "acceptance-results.json").write_text(json.dumps({
        "base_url": args.base_url, "actual_api": args.real_api,
        "snapshot_api": args.snapshot_api,
        "duration_seconds": round(time.monotonic() - started, 2),
        "run": result.testsRun, "failures": len(result.failures), "errors": len(result.errors),
        "skipped": len(result.skipped), "checks": result.records,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    sys.exit(0 if result.wasSuccessful() else 1)
