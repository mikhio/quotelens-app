"""Browser acceptance against the real Handler, Store and background workers.

All data are synthetic, SQLite is temporary, and Research cannot call a model.
Run: TMPDIR=/managed/run python3 tests/ui_background_history.py --output-dir /managed/run/ui-history
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import threading
import unittest
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from test_history_api import DESCRIPTION, HistoryServerFixture


class BackgroundHistoryUiAcceptance(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright
        cls.output = Path(os.environ["QUOTELENS_HISTORY_QA_OUTPUT"])
        cls.output.mkdir(parents=True, exist_ok=True)
        cls.playwright = sync_playwright().start()

    @classmethod
    def tearDownClass(cls):
        cls.playwright.stop()

    def setUp(self):
        self.fixture = HistoryServerFixture()
        self.errors = []
        self.observations = {}
        self.browser = self.context = self.page = None
        self.new_browser()

    def tearDown(self):
        try:
            if self.page and not self.page.is_closed():
                self.page.screenshot(path=str(self.output / (self._testMethodName + ".png")), full_page=True)
                (self.output / (self._testMethodName + ".txt")).write_text(self.page.locator("body").inner_text())
            self.observations.update({
                "http_calls": self.fixture.calls,
                "search_quota_used": self.fixture.db.quota_usage("search_runs"),
                "accepted_durable_ids": [job["id"] for job in self.fixture.accepted_durable if job],
                "browser_errors": self.errors,
            })
            (self.output / (self._testMethodName + ".json")).write_text(
                json.dumps(self.observations, ensure_ascii=False, indent=2))
        finally:
            if self.browser:
                self.browser.close()
            self.fixture.close()
        self.assertEqual(self.errors, [], "Uncaught browser exception")

    def new_browser(self, *, storage_disabled=False, width=1292):
        self.browser = self.playwright.chromium.launch(
            executable_path=os.environ.get("QUOTELENS_CHROME", "/usr/bin/google-chrome"),
            headless=True, args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
            ignore_default_args=["--disable-back-forward-cache"])
        self.context = self.browser.new_context(viewport={"width": width, "height": 850}, accept_downloads=True)
        if storage_disabled:
            self.context.add_init_script("""for (const key of ['localStorage','sessionStorage']) {
              Object.defineProperty(window,key,{configurable:true,get() {
                throw new DOMException('QA: browser storage is disabled','SecurityError');
              }});
            }""")
        self.page = self.context.new_page()
        self.page.set_default_timeout(9000)
        self.page.on("pageerror", lambda error: self.errors.append(str(error)))

    def close_entire_browser(self):
        self.browser.close()
        self.browser = self.context = self.page = None

    def home(self):
        self.page.goto(self.fixture.base + "/", wait_until="networkidle")
        self.page.locator("#search-history").wait_for(state="visible")

    def wait_rows(self, total):
        self.page.wait_for_function(
            "count => document.querySelectorAll('#history-list li[data-job-id]').length === count", arg=total)

    def row(self, identifier):
        return self.page.locator(f'#history-list li[data-job-id="{identifier}"]')

    def submit(self, description=DESCRIPTION, quantity=7, source_domains=None):
        self.page.locator("#description").fill(description)
        self.page.locator("#quantity").fill(str(quantity))
        self.page.locator("#source-picker-summary").click()
        scope_mode = "domains" if source_domains else "web"
        self.page.locator(f'#source-options input[value="{scope_mode}"]').check()
        if source_domains:
            self.page.locator("#source-domains").fill(source_domains)
        self.page.locator("#search-submit").click()
        self.page.wait_for_function("() => /^#job=[a-f0-9]{24}$/.test(location.hash)")
        identifier = urlsplit(self.page.url).fragment.removeprefix("job=")
        self.fixture.wait(lambda: identifier in self.fixture.controls)
        self.page.locator("#progress-screen").wait_for(state="visible")
        return identifier

    def wait_restored(self, description, quantity):
        self.page.wait_for_function("""([description,quantity]) =>
            document.querySelector('#description').value === description &&
            Number(document.querySelector('#quantity').value) === quantity""", arg=[description, quantity])

    def assert_home(self):
        self.page.locator("#search-history").wait_for(state="visible")
        self.assertEqual(urlsplit(self.page.url).fragment, "")
        self.assertFalse(self.page.locator("#result-screen").is_visible())

    def assert_no_automatic_posts(self, searches):
        self.assertEqual(self.fixture.count("POST", "/api/search"), searches)
        self.assertEqual(self.fixture.db.quota_usage("search_runs"), searches)
        self.assertFalse(any(call["method"] == "POST" and call["path"].endswith("/cancel")
                             for call in self.fixture.calls))

    def assert_no_overflow(self):
        width = self.page.evaluate("({viewport:innerWidth,document:document.documentElement.scrollWidth})")
        self.assertLessEqual(width["document"], width["viewport"] + 1, width)

    def test_01_reload_running_then_whole_browser_close_and_storage_disabled_home_recovery(self):
        self.home()
        first = self.submit(quantity=3.25)
        self.assertEqual(self.fixture.db.get_job(first)["status"], "running")
        self.page.reload(wait_until="networkidle")
        self.page.locator("#progress-screen").wait_for(state="visible")
        self.assertEqual(self.page.locator("#description").input_value(), DESCRIPTION)
        self.assertEqual(float(self.page.locator("#quantity").input_value()), 3.25)
        checked = self.page.locator('#source-options input:checked').evaluate_all("nodes => nodes.map(node => node.value)")
        self.assertEqual(checked, ["web"])
        self.assertEqual(urlsplit(self.page.url).fragment, "job=" + first)
        self.page.locator("#all-searches").click()
        self.assert_home()
        self.row(first).wait_for(state="visible")
        self.assertTrue(self.row(first).locator(".history-status").inner_text())
        second = self.submit(description=DESCRIPTION + "; второй фоновый запрос", quantity=2)
        self.page.locator("#all-searches").click()
        self.assert_home()
        self.wait_rows(2)
        self.close_entire_browser()
        for identifier in [first, second]:
            self.assertFalse(self.fixture.controls[identifier]["cancelled"].is_set())
            self.assertEqual(self.fixture.finish(identifier)["status"], "complete")
        self.new_browser(storage_disabled=True)
        self.home()  # Fresh profile, no hash, and storage getters always throw.
        self.wait_rows(2)
        self.row(first).locator("a").first.click()
        self.page.locator("#result-screen").wait_for(state="visible")
        self.assertEqual(urlsplit(self.page.url).fragment, "job=" + first)
        self.assertEqual(self.page.locator("#description").input_value(), DESCRIPTION)
        self.assertEqual(float(self.page.locator("#quantity").input_value()), 3.25)
        self.assertGreater(self.page.locator(".offer-card").count(), 0)
        self.assertTrue(self.page.locator("#synthetic-label").is_visible())
        self.assert_no_overflow()
        with self.page.expect_download() as event:
            self.page.locator("#report-html").click()
        report = self.output / "background-recovered-report.html"
        event.value.save_as(report)
        self.assertIsNone(event.value.failure())
        content = report.read_text()
        self.assertIn("Учебные данные", content)
        self.assertNotIn("<form", content)
        self.assertNotIn("search-history", content)
        self.assert_no_automatic_posts(2)

    def test_02_accepted_request_with_lost_202_is_discoverable_without_hash(self):
        self.home()
        self.fixture.hold_search_response = True
        self.page.locator("#description").fill(DESCRIPTION)
        self.page.locator("#quantity").fill("11")
        self.page.locator("#search-submit").click()
        self.assertTrue(self.fixture.search_response_ready.wait(3))
        self.assertEqual(urlsplit(self.page.url).fragment, "")
        identifier = self.fixture.accepted_durable[0]["id"]
        self.assertEqual(self.fixture.accepted_durable[0]["status"], "running")
        self.close_entire_browser()  # The 202 is still blocked in the real server.
        self.fixture.send_search_response.set()
        self.fixture.wait(lambda: identifier in self.fixture.controls)
        self.assertFalse(self.fixture.controls[identifier]["cancelled"].is_set())
        self.assertEqual(self.fixture.finish(identifier)["status"], "complete")
        self.new_browser(storage_disabled=True)
        self.home()
        self.row(identifier).wait_for(state="visible")
        self.assertEqual(urlsplit(self.page.url).fragment, "")
        self.row(identifier).locator("a").first.click()
        self.page.locator("#result-screen").wait_for(state="visible")
        self.assertEqual(float(self.page.locator("#quantity").input_value()), 11)
        self.assert_no_automatic_posts(1)

    def test_03_all_statuses_more_than_five_pagination_safe_text_and_mobile_layout(self):
        statuses = ["running", "complete", "failed", "cancelled", "interrupted"]
        identifiers = [self.fixture.seed(i, statuses[(i - 1) % 5]) for i in range(1, 28)]
        snapshot = self.fixture.seed(100, mode="snapshot")
        malicious = '<img src=x onerror="window.qaHistoryInjected=1">' + "ДлинноеНазвание" * 24
        attack = self.fixture.seed(99, description=malicious, status="failed")
        self.home()
        self.wait_rows(20)
        self.assertRegex(self.page.locator("#history-total").inner_text(), r"28")
        self.assertTrue(self.page.locator("#history-more").is_visible())
        self.page.locator("#history-more").click()
        self.wait_rows(28)
        found = self.page.locator('#history-list li[data-job-id]').evaluate_all("nodes => nodes.map(node => node.dataset.jobId)")
        self.assertEqual(set(found), set(identifiers + [attack]))
        self.assertNotIn(snapshot, found)
        self.assertEqual(len(found), len(set(found)))
        self.assertFalse(self.page.locator("#history-more").is_visible())
        self.assertIn('<img src=x', self.row(attack).inner_text())
        self.assertEqual(self.row(attack).locator("img,script").count(), 0)
        self.assertIsNone(self.page.evaluate("window.qaHistoryInjected"))
        for identifier in identifiers:
            self.assertTrue(self.row(identifier).locator(".history-status").inner_text().strip())
        self.assert_no_overflow()
        self.page.set_viewport_size({"width": 390, "height": 844})
        self.assert_no_overflow()
        self.page.screenshot(path=str(self.output / "history-mobile-390.png"), full_page=True)
        # A failed/interrupted request remains selectable and keeps its form.
        interrupted = identifiers[4]
        self.row(interrupted).locator("a").first.click()
        self.page.locator("#progress-screen").wait_for(state="visible")
        self.wait_restored("QA: учебная закупка 5", 5.5)
        self.assertEqual(urlsplit(self.page.url).fragment, "job=" + interrupted)
        self.assertEqual(self.page.locator("#description").input_value(), "QA: учебная закупка 5")
        self.assert_no_overflow()
        self.page.locator("#all-searches").click()
        self.assert_home()
        self.assert_no_automatic_posts(0)

    def test_04_transient_history_and_selected_job_errors_retry_without_losing_job(self):
        identifier = self.fixture.submit(quantity=4)["id"]
        self.fixture.failed_paths.add("/api/jobs")
        self.home()
        self.page.locator("#history-error").wait_for(state="visible")
        self.fixture.failed_paths.remove("/api/jobs")
        self.page.locator("#history-error button").click()
        self.row(identifier).wait_for(state="visible")
        # Refresh failure must preserve rows already shown.
        self.fixture.failed_paths.add("/api/jobs")
        self.page.locator("#history-refresh").click()
        self.page.locator("#history-error").wait_for(state="visible")
        self.assertTrue(self.row(identifier).is_visible())
        self.fixture.failed_paths.remove("/api/jobs")
        self.page.locator("#history-error button").click()
        self.page.locator("#history-error").wait_for(state="hidden")
        self.fixture.failed_paths.add("/api/jobs/" + identifier)
        self.row(identifier).locator("a").first.click()
        self.page.locator("#global-error").wait_for(state="visible")
        self.assertEqual(urlsplit(self.page.url).fragment, "job=" + identifier)
        self.assertNotIn("не найден", self.page.locator("#global-error").inner_text().lower())
        self.fixture.failed_paths.remove("/api/jobs/" + identifier)
        self.page.locator("#global-error button").click()
        self.page.locator("#progress-screen").wait_for(state="visible")
        self.wait_restored(DESCRIPTION, 4)
        self.assertEqual(self.page.locator("#description").input_value(), DESCRIPTION)
        self.assertEqual(float(self.page.locator("#quantity").input_value()), 4)
        self.assertEqual(urlsplit(self.page.url).fragment, "job=" + identifier)
        self.assert_no_automatic_posts(1)

    def test_05_late_job_response_cannot_reopen_result_after_back_home(self):
        identifier = self.fixture.seed(1)
        self.home()
        started, release = threading.Event(), threading.Event()
        self.fixture.delayed_gets["/api/jobs/" + identifier] = (started, release)
        self.row(identifier).locator("a").first.click()
        self.assertTrue(started.wait(3))
        self.page.locator("#all-searches").click()
        self.assert_home()
        release.set()
        self.page.wait_for_load_state("networkidle")
        self.assert_home()
        self.assert_no_automatic_posts(0)

    def test_06_focus_pageshow_visibility_and_actual_back_navigation_resync_home(self):
        self.home()
        self.wait_rows(0)
        events = [
            "window.dispatchEvent(new Event('focus'))",
            "window.dispatchEvent(new PageTransitionEvent('pageshow',{persisted:true}))",
            "Object.defineProperty(document,'hidden',{configurable:true,get:()=>true}); Object.defineProperty(document,'visibilityState',{configurable:true,get:()=> 'hidden'}); document.dispatchEvent(new Event('visibilitychange')); Object.defineProperty(document,'hidden',{configurable:true,get:()=>false}); Object.defineProperty(document,'visibilityState',{configurable:true,get:()=> 'visible'}); document.dispatchEvent(new Event('visibilitychange'))",
        ]
        for index, event in enumerate(events, start=1):
            # The user may return well after the previous lifecycle notification.
            self.page.wait_for_timeout(1100)
            identifier = self.fixture.seed(index)
            before = self.fixture.count("GET", "/api/jobs")
            self.page.evaluate(event)
            self.row(identifier).wait_for(state="visible")
            self.assertGreater(self.fixture.count("GET", "/api/jobs"), before)
        self.page.evaluate("window.qaPageShows=[]; addEventListener('pageshow',event=>qaPageShows.push(event.persisted))")
        self.page.goto(self.fixture.base + "/health", wait_until="load")
        identifier = self.fixture.seed(4)
        self.page.go_back(wait_until="commit")
        self.row(identifier).wait_for(state="visible")
        self.observations["actual_back_pageshow_persisted"] = self.page.evaluate("window.qaPageShows || []")
        self.assert_home()
        self.assert_no_automatic_posts(0)

    def test_07_hidden_selected_job_resyncs_on_visibility_return_without_restart(self):
        identifier = self.fixture.submit(quantity=6)["id"]
        self.page.goto(self.fixture.base + "/#job=" + identifier, wait_until="networkidle")
        self.page.locator("#progress-screen").wait_for(state="visible")
        self.wait_restored(DESCRIPTION, 6)
        self.page.evaluate("""Object.defineProperty(document,'hidden',{configurable:true,get:()=>true});
          Object.defineProperty(document,'visibilityState',{configurable:true,get:()=> 'hidden'});
          document.dispatchEvent(new Event('visibilitychange'));""")
        self.assertEqual(self.fixture.finish(identifier)["status"], "complete")
        before = self.fixture.count("GET", "/api/jobs/" + identifier)
        self.assertFalse(self.page.locator("#result-screen").is_visible())
        self.page.evaluate("""Object.defineProperty(document,'hidden',{configurable:true,get:()=>false});
          Object.defineProperty(document,'visibilityState',{configurable:true,get:()=> 'visible'});
          document.dispatchEvent(new Event('visibilitychange'));""")
        self.page.locator("#result-screen").wait_for(state="visible")
        self.assertGreater(self.fixture.count("GET", "/api/jobs/" + identifier), before)
        self.assertEqual(urlsplit(self.page.url).fragment, "job=" + identifier)
        self.assert_no_automatic_posts(1)

    def test_08_real_request_wrapper_keeps_hash_and_server_history(self):
        self.home()
        self.page.evaluate("""() => { const original = window.QuoteLens.request;
            window.QuoteLens.request = (...args) => original(...args); }""")
        identifier = self.submit(quantity=12)
        self.assertEqual(urlsplit(self.page.url).fragment, "job=" + identifier)
        self.page.locator("#all-searches").click()
        self.assert_home()
        self.row(identifier).wait_for(state="visible")
        self.fixture.finish(identifier)
        self.row(identifier).locator("a").first.click()
        self.page.locator("#result-screen").wait_for(state="visible")
        self.assertEqual(urlsplit(self.page.url).fragment, "job=" + identifier)
        self.assert_no_automatic_posts(1)

    def test_09_stale_empty_history_response_cannot_erase_accepted_running_search(self):
        self.fixture.hold_next_history_response = True
        self.page.goto(self.fixture.base + "/", wait_until="domcontentloaded")
        self.page.locator("#search-history").wait_for(state="visible")
        self.assertTrue(self.fixture.history_response_ready.wait(3))
        self.assertEqual(self.fixture.history_response_snapshot,
                         {"items": [], "next_cursor": None, "total": 0})
        self.assertEqual(self.fixture.count("GET", "/api/jobs"), 1)
        self.page.locator('#source-options input[value="web"]').wait_for(state="attached")
        identifier = self.submit(quantity=13)
        self.assertEqual(urlsplit(self.page.url).fragment, "job=" + identifier)
        self.page.locator("#all-searches").click()
        self.assert_home()
        self.row(identifier).wait_for(state="visible")
        self.assertTrue(self.row(identifier).locator(".history-status.running").is_visible())
        self.assertEqual(self.fixture.db.get_job(identifier)["status"], "running")
        self.page.evaluate("""identifier => {
            window.qaAcceptedRowDisappearances = [];
            const list = document.querySelector('#history-list');
            window.qaAcceptedRowObserver = new MutationObserver(() => {
                if (![...list.querySelectorAll('[data-job-id]')].some(node => node.dataset.jobId === identifier)) {
                    window.qaAcceptedRowDisappearances.push(performance.now());
                }
            });
            window.qaAcceptedRowObserver.observe(list,{childList:true});
        }""", identifier)
        self.fixture.send_history_response.set()
        self.page.wait_for_load_state("networkidle")
        self.assertGreaterEqual(self.fixture.count("GET", "/api/jobs"), 2,
                                "An obsolete history response must trigger a fresh server read")
        self.row(identifier).wait_for(state="visible")
        self.assertTrue(self.row(identifier).locator(".history-status.running").is_visible())
        self.assertFalse(self.page.locator("#history-empty").is_visible())
        self.assertEqual(self.page.evaluate("window.qaAcceptedRowDisappearances"), [],
                         "Accepted search disappeared while the stale history response settled")
        self.assert_home()
        self.assert_no_automatic_posts(1)
        self.observations["delayed_history_snapshot"] = self.fixture.history_response_snapshot
        self.observations["visible_history_ids_after_release"] = self.page.locator(
            '#history-list li[data-job-id]').evaluate_all("nodes => nodes.map(node => node.dataset.jobId)")
        self.observations["accepted_row_disappearances"] = self.page.evaluate("window.qaAcceptedRowDisappearances")

    def test_10_domain_scope_is_durable_after_reload_and_fresh_browser_history(self):
        self.home()
        identifier = self.submit(quantity=8, source_domains="radioelementy.ru, prist.ru")
        expected = {"mode": "domains", "domains": ["radioelementy.ru", "prist.ru"]}
        self.assertEqual(self.fixture.db.get_job(identifier)["request"]["source_scope"], expected)
        self.page.reload(wait_until="networkidle")
        self.page.locator("#progress-screen").wait_for(state="visible")
        self.assertTrue(self.page.locator('#source-options input[value="domains"]').is_checked())
        self.assertEqual(self.page.locator("#source-domains").input_value(), "radioelementy.ru, prist.ru")
        self.close_entire_browser()
        self.assertEqual(self.fixture.finish(identifier)["status"], "complete")
        self.fixture.reopen()
        status, history, _ = self.fixture.request("/api/jobs")
        self.assertEqual(status, 200)
        self.assertEqual(next(item for item in history["items"] if item["id"] == identifier)["request"]["source_scope"], expected)
        self.new_browser(storage_disabled=True, width=390)
        self.home()
        self.row(identifier).locator("a").first.click()
        self.page.locator("#result-screen").wait_for(state="visible")
        self.assertTrue(self.page.locator('#source-options input[value="domains"]').is_checked())
        self.assertEqual(self.page.locator("#source-domains").input_value(), "radioelementy.ru, prist.ru")
        self.wait_restored(DESCRIPTION, 8)
        self.assert_no_overflow()
        self.assert_no_automatic_posts(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", required=True)
    arguments, remaining = parser.parse_known_args()
    os.environ["QUOTELENS_HISTORY_QA_OUTPUT"] = arguments.output_dir
    unittest.main(argv=[sys.argv[0], *remaining])
