"""HTTP/Chromium checks for evidence and a higher, ordinary feedback layer.

The feedback host below is an explicitly synthetic interaction fixture. The real
hosted widget has a separate integration check; this file never posts feedback.
"""
from __future__ import annotations

import argparse
import os
import sys
import unittest

import ui_live_acceptance as live


class EvidenceWorkspaceAcceptance(unittest.TestCase):
    setUpClass = classmethod(live.LiveUiAcceptance.setUpClass.__func__)
    tearDownClass = classmethod(live.LiveUiAcceptance.tearDownClass.__func__)
    setUp = live.LiveUiAcceptance.setUp
    tearDown = live.LiveUiAcceptance.tearDown
    submit = live.LiveUiAcceptance.submit
    evidence = live.LiveUiAcceptance.evidence
    open_query_editor = live.LiveUiAcceptance.open_query_editor

    def assert_no_overflow(self):
        dimensions = self.page.evaluate("""() => {
          const panel = document.querySelector('#evidence-dialog');
          return {viewport:innerWidth,page:document.documentElement.scrollWidth,
                  panel:panel.clientWidth,content:panel.scrollWidth};
        }""")
        self.assertLessEqual(dimensions["page"], dimensions["viewport"] + 1, dimensions)
        self.assertLessEqual(dimensions["content"], dimensions["panel"] + 1, dimensions)

    def test_01_table_links_requirements_to_selected_source_quote(self):
        self.submit()
        self.evidence()
        self.assertGreater(self.page.locator("#evidence-dialog").bounding_box()["width"], 1000)
        self.assertEqual(self.page.locator(".evidence-table thead th").all_text_contents(),
                         ["Параметр / требуется", "Найдено", "Подтверждение"])
        row = self.page.locator('[data-fact-field="attributes.bandwidth_mhz"]')
        self.assertIn("≥ 100 МГц", row.inner_text())
        self.assertIn("Соответствует", row.inner_text())
        row.locator("button").click()
        self.assertIn("bandwidth_mhz = 100", self.page.locator("#field-proof").inner_text())
        self.assertEqual(row.locator("button").get_attribute("aria-pressed"), "true")
        self.assertEqual(self.page.locator("#field-proof a").get_attribute("href"),
                         "https://a.example.invalid/synthetic/a")
        self.assertFalse(any("/documents/" in call["path"] for call in self.fixture.calls))
        self.page.locator("[data-source-document] summary").click()
        self.page.locator("[data-source-document] pre").filter(has_text="Цена 69000").wait_for()
        self.assertEqual(self.page.locator("[data-source-document] script").count(), 0)
        self.assert_no_overflow()

    def test_02_navigation_and_missing_rejected_and_mismatched_fields_are_distinct(self):
        self.submit()
        self.evidence()
        self.assertEqual(self.page.locator("#evidence-position").inner_text(), "1 / 5")
        self.assertTrue(self.page.locator("#previous-evidence").is_disabled())
        self.page.locator("#next-evidence").click()
        self.assertEqual(self.page.locator("#evidence-position").inner_text(), "2 / 5")
        self.page.locator("#previous-evidence").click()
        self.assertIn("SYN-A", self.page.locator("#evidence-title").inner_text())
        self.page.locator("#close-evidence").click()
        self.evidence("synthetic-two-channels")
        self.assertIn("Не соответствует", self.page.locator('[data-fact-field="attributes.channels"]').inner_text())
        self.page.locator("#close-evidence").click()
        self.evidence("synthetic-no-band")
        row = self.page.locator('[data-fact-field="attributes.bandwidth_mhz"]')
        self.assertIn("Не найдено", row.inner_text())
        row.locator("button").click()
        self.assertIn("Подтверждённое значение не найдено", self.page.locator("#field-proof").inner_text())
        self.page.locator("#close-evidence").click()
        offer = self.fixture.jobs["qa-live-1"]["result"]["offers"][0]
        offer["price"] = None
        offer["normalized_price"] = None
        offer["validation"] = {"rejected_fields": ["price"]}
        offer["evidence"].pop("price")
        self.page.reload(wait_until="networkidle")
        self.evidence()
        self.assertIn("Отклонено проверкой", self.page.locator('[data-fact-field="price"]').inner_text())
        self.assertIn("Извлечённое значение отклонено", self.page.locator("#field-proof").inner_text())

    def test_03_observed_price_remains_visible_with_unknown_tax_and_incomplete_request(self):
        self.submit("Осциллограф")
        offer = self.fixture.jobs["qa-live-1"]["result"]["offers"][0]
        offer.update(tax="unknown", normalized_price=None, status="uncertain")
        offer["evidence"].pop("tax")
        self.page.reload(wait_until="networkidle")
        self.evidence()
        self.assertIn("69 000 RUB", self.page.locator(".evidence-price strong").inner_text().replace("\xa0", " "))
        self.assertNotIn("За 1", self.page.locator(".evidence-price").inner_text())
        self.assertIn("Уточните запрос", self.page.locator('[data-fact-field="attributes.channels"]').inner_text())
        self.assertIn("Не найдено", self.page.locator('[data-fact-field="tax"]').inner_text())
        self.assertNotEqual(self.page.locator(".total-price").inner_text(), "207 000 ₽")

    def test_04_mobile_switches_between_readable_facts_and_lazy_source(self):
        self.page.set_viewport_size({"width": 390, "height": 844})
        self.submit()
        self.evidence()
        self.assertTrue(self.page.locator(".evidence-facts").is_visible())
        self.assertFalse(self.page.locator(".evidence-source").is_visible())
        self.assert_no_overflow()
        self.page.locator('[data-proof-field="attributes.channels"]').click()
        self.assertTrue(self.page.locator(".evidence-source").is_visible())
        self.assertFalse(self.page.locator(".evidence-facts").is_visible())
        self.assertIn("channels = 4", self.page.locator("#field-proof").inner_text())
        self.page.locator("[data-source-document] summary").click()
        self.page.locator("[data-source-document] pre").filter(has_text="Цена 69000").wait_for()
        self.page.screenshot(path=str(self.output / "evidence-source-mobile-390.png"))
        self.page.locator('[data-evidence-pane="facts"]').click()
        self.page.locator("#review-reason").fill("Учебная проверка мобильного поля")
        self.assert_no_overflow()
        self.page.screenshot(path=str(self.output / "evidence-facts-mobile-390.png"))

    def test_05_nonmodal_feedback_launcher_form_and_area_picker_stay_reachable(self):
        self.submit()
        # Isolated stand-in for the shared widget's public shadow-host contract.
        self.page.evaluate("""() => {
          const host = document.createElement('public-feedback-widget');
          host.style.cssText = 'position:fixed;right:16px;bottom:16px;z-index:2147483646';
          const shadow = host.attachShadow({mode:'open'});
          shadow.innerHTML = `<style>
            button,input{font:14px sans-serif;padding:12px;border:1px solid #999;background:white}
            .panel{position:absolute;bottom:55px;right:0;width:280px;padding:14px;background:white;border:1px solid #999}
            .picker{position:fixed;inset:0;background:#17345115;cursor:crosshair;z-index:1}
            [hidden]{display:none!important}
            </style><button class="launcher">QA: замечание</button>
            <div class="panel" hidden><input aria-label="QA текст замечания"><button class="pick">QA область</button></div>
            <div class="picker" hidden></div>`;
          shadow.querySelector('.launcher').onclick = () => shadow.querySelector('.panel').hidden = false;
          shadow.querySelector('.pick').onclick = () => shadow.querySelector('.picker').hidden = false;
          shadow.querySelector('.picker').onclick = event => {
            window.qaSelectedEvidencePoint = {x:event.clientX,y:event.clientY};
            shadow.querySelector('.picker').hidden = true;
          };
          document.body.append(host);
        }""")
        self.evidence()
        self.assertFalse(self.page.locator("#evidence-dialog").evaluate("node => node.matches(':modal')"))
        self.assertFalse(self.page.locator("body").evaluate("node => node.inert"))
        host = self.page.locator("public-feedback-widget")
        host.locator(".launcher").click()
        host.get_by_label("QA текст замечания").fill("Учебное замечание поверх открытых доказательств")
        self.page.keyboard.press("Escape")
        self.assertTrue(self.page.locator("#evidence-dialog").is_visible())
        self.assertEqual(host.get_by_label("QA текст замечания").input_value(),
                         "Учебное замечание поверх открытых доказательств")
        host.locator(".pick").click()
        target = self.page.locator("#evidence-title").bounding_box()
        self.page.mouse.click(target["x"] + 20, target["y"] + 10)
        self.assertIsNotNone(self.page.evaluate("window.qaSelectedEvidencePoint"))
        self.assertTrue(self.page.locator("#evidence-dialog").is_visible())
        self.assertEqual(self.fixture.reviews, [])
        self.page.locator("#close-evidence").focus()
        self.page.keyboard.press("Escape")
        self.page.locator("#evidence-dialog").wait_for(state="hidden")
        self.assertEqual(self.page.locator("body").evaluate("node => node.style.overflow"), "")
        self.assertTrue(self.page.locator('[data-offer="synthetic-a"] [data-evidence]').evaluate(
            "node => node === document.activeElement"))

    def test_06_result_emphasizes_current_query_and_filters_open_on_request(self):
        self.submit()
        self.assertFalse(self.page.locator("#search-form").is_visible())
        self.assertIn(live.DESCRIPTION, self.page.locator("#query-heading").inner_text())
        self.assertFalse(self.page.locator("#status-filter").is_visible())
        self.assertFalse(self.page.locator("#source-filter").is_visible())
        self.page.locator("#result-filters summary").click()
        self.page.locator("#source-filter").select_option("group:retail")
        self.assertEqual(self.page.locator("#active-filter-count").inner_text(), "1")
        self.page.keyboard.press("Escape")
        self.assertFalse(self.page.locator("#source-filter").is_visible())
        self.page.locator("#edit-query").click()
        self.assertTrue(self.page.locator("#search-form").is_visible())
        self.assertEqual(self.page.locator("#description").input_value(), live.DESCRIPTION)
        self.page.locator("#edit-query").click()
        self.assertFalse(self.page.locator("#search-form").is_visible())

    def test_07_only_explicit_functional_mismatches_warn_on_cards(self):
        self.submit()
        card = self.page.locator('[data-offer="synthetic-no-band"]')
        self.assertEqual(card.locator(".offer-reason").count(), 0)
        mismatch = self.page.locator('[data-offer="synthetic-two-channels"]')
        self.assertEqual(mismatch.locator(".offer-reason li").count(), 1)
        self.assertIn("2", mismatch.locator(".offer-reason").inner_text())
        self.assertNotIn("НДС", mismatch.locator(".offer-reason").inner_text())
        offer = self.fixture.jobs["qa-live-1"]["result"]["offers"][0]
        offer.update(functional_status="matches", functional_reasons=[], tax="unknown", unit="шт", unit_inference=None)
        offer["evidence"]["unit"] = {"quote":"СИНТЕТИКА: цена за шт", "url":offer["url"]}
        self.page.reload(wait_until="networkidle")
        card = self.page.locator('[data-offer="synthetic-a"]')
        self.assertIn("Соответствует запросу", card.inner_text())
        self.assertIn("₽/шт", card.locator(".offer-price").inner_text())
        self.assertIn("НДС не указан", card.inner_text())
        self.assertNotIn("Доставка не указана", card.inner_text())
        self.assertNotIn("не учт", card.inner_text())

    def test_08_running_search_reveals_visited_sources_only_in_dropdown(self):
        self.submit()
        job = self.fixture.jobs["qa-live-1"]
        job["status"] = "running"
        job["search_progress"] = {"phase":"search", "sources":[
            {"domain":"qa.example.invalid", "url":"https://qa.example.invalid/product", "status":"visited", "verdict":"uncertain", "message":"Цена подтверждена исходной карточкой."},
            {"domain":"blocked.example.invalid", "status":"blocked", "message":"Доступ закрыт сайтом"},
        ]}
        self.page.reload(wait_until="networkidle")
        self.page.locator("#progress-screen").wait_for(state="visible")
        self.assertFalse(self.page.locator("#search-progress-list").is_visible())
        self.assertFalse(self.page.locator("#search-form").is_visible())
        self.page.locator("#search-progress-sources summary").click()
        self.assertEqual(self.page.locator("#search-progress-list li").count(), 2)
        self.assertIn("Цена подтверждена исходной карточкой.", self.page.locator("#search-progress-list").inner_text())
        self.assertIn("Доступ закрыт сайтом", self.page.locator("#search-progress-list").inner_text())
        self.assertEqual(self.page.locator("#search-progress-list a").get_attribute("href"),
                         "https://qa.example.invalid/product")
        self.assertTrue(self.page.locator("#search-progress-sources").get_attribute("open") == "")

    def test_09_real_broad_title_missing_request_rows_keep_table_geometry(self):
        self.submit("осцилограф")
        result = self.fixture.jobs["qa-live-1"]["result"]
        offer = result["offers"][0]
        # The title reproduces the actual hosted layout that exposed this bug.
        # This is still the visibly synthetic fixture, without network prices.
        title = "Настольный осциллограф Hantek DSO2C10 (2 канала, 100 МГц)"
        offer["title"] = title
        offer["attributes"].update(channels=2, bandwidth_mhz=100)
        offer["evidence"]["title"] = {"quote":title, "url":offer["url"]}
        offer["evidence"]["attributes.channels"] = {"quote":"СИНТЕТИКА: channels = 2", "url":offer["url"]}
        live.annotate_decisions(result)
        self.page.reload(wait_until="networkidle")
        self.evidence()
        self.assertIn("Hantek DSO2C10", self.page.locator("#evidence-title").inner_text())
        rows = self.page.locator('tr[data-fact-field="attributes.channels"], tr[data-fact-field="attributes.bandwidth_mhz"]')
        self.assertEqual(rows.count(), 2)
        for row in rows.all():
            self.assertIn("Уточните запрос", row.inner_text())
        for width in (1292, 390):
            self.page.set_viewport_size({"width":width, "height":844})
            geometry = rows.evaluate_all("""nodes => nodes.map(row => ({
              display:getComputedStyle(row).display,
              width:row.getBoundingClientRect().width,
              table:row.closest('table').getBoundingClientRect().width,
              cells:[...row.cells].map(cell => cell.getBoundingClientRect().width)
            }))""")
            for row in geometry:
                self.assertEqual(row["display"], "table-row", row)
                self.assertGreaterEqual(row["width"], row["table"] - 2, row)
                self.assertEqual(len(row["cells"]), 3)
                self.assertGreater(min(row["cells"]), 60 if width == 390 else 100, row)
            self.assert_no_overflow()
            self.page.screenshot(path=str(self.output / f"real-broad-missing-rows-{width}.png"))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", required=True)
    arguments, remaining = parser.parse_known_args()
    os.environ["QUOTELENS_QA_OUTPUT"] = arguments.output_dir
    unittest.main(argv=[sys.argv[0], *remaining], verbosity=2)
