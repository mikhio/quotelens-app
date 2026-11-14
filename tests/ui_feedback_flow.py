"""Synthetic Chromium acceptance for the procurement workspace.

Uses the real local HTTP review, storage and report code. All jobs are explicitly
synthetic; no model/search, owner data or feedback submission is used.
"""
from __future__ import annotations

from copy import deepcopy
from http.server import ThreadingHTTPServer
from pathlib import Path
import json
import os
import sys
import threading
import unittest
from unittest.mock import patch
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from quotelens import server
from quotelens.engine import parse_request
from quotelens.selection import finalize
from quotelens.scenarios import load_scenario
from quotelens.storage import Store
from test_manual_corrections import synthetic_result

JOB = 'b' * 24


class FeedbackWorkspace(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright
        cls.output = Path(os.environ.get('QUOTELENS_FEEDBACK_UI_OUTPUT', 'test-results/feedback-ui'))
        cls.output.mkdir(parents=True, exist_ok=True)
        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch(executable_path='/usr/bin/google-chrome', headless=True,
            args=['--no-sandbox', '--disable-dev-shm-usage'])

    @classmethod
    def tearDownClass(cls):
        cls.browser.close(); cls.pw.stop()

    def setUp(self):
        self.store = Store(':memory:')
        self.result = synthetic_result(multi=True)
        for index,label in enumerate(('QA Осциллограф для лаборатории','QA Осциллограф для сервиса')):
            self.result['items'][index]['title'] = label
            self.result['request']['items'][index]['title'] = label
        self.store.create_job({'id': JOB, 'status': 'complete', 'result': self.result}, self.result['request'])
        self.store_patch = patch.object(server, 'store', return_value=self.store)
        self.store_patch.start()
        self.http = ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
        self.thread = threading.Thread(target=self.http.serve_forever, daemon=True); self.thread.start()
        self.base = f'http://127.0.0.1:{self.http.server_port}/'
        self.context = self.browser.new_context(viewport={'width':1292, 'height':754}, accept_downloads=True)
        self.page = self.context.new_page(); self.page.set_default_timeout(12000)
        self.errors = []; self.page.on('pageerror', lambda e: self.errors.append(str(e)))
        self.open()

    def tearDown(self):
        self.page.screenshot(path=str(self.output / (self._testMethodName + '.png')))
        self.context.close(); self.http.shutdown(); self.http.server_close(); self.thread.join(timeout=2); self.store_patch.stop()
        self.assertEqual(self.errors, [])

    def open(self):
        self.page.goto(self.base + '#job=' + JOB, wait_until='networkidle')
        self.page.locator('#report-pdf').wait_for()

    def no_overflow(self):
        self.assertFalse(self.page.evaluate('document.documentElement.scrollWidth > innerWidth'))

    def test_01_desktop_item_navigation_and_visible_report(self):
        self.assertEqual(self.page.locator('[data-select-item="line-a"][aria-current=true]').count(), 1)
        self.assertTrue(all(o.startswith('line-a:') for o in self.page.locator('[data-offer]').evaluate_all('(els) => els.map(e => e.dataset.offer)')))
        self.page.locator('[data-select-item="line-b"]').first.click()
        self.assertTrue(all(o.startswith('line-b:') for o in self.page.locator('[data-offer]').evaluate_all('(els) => els.map(e => e.dataset.offer)')))
        self.page.locator('[data-select-item="all"]').click()
        self.assertEqual(self.page.locator('[data-offer]').count(), len(self.result['offers']))
        for selector in ('#report-pdf', '#report-docx'):
            box = self.page.locator(selector).bounding_box(); self.assertLess(box['y'] + box['height'], 754)
        self.assertFalse(self.page.locator('#search-form').is_visible())
        self.assertFalse(self.page.locator('.calculation-details').evaluate('(e) => e.open'))
        self.no_overflow()

    def test_02_pagination_page_size_and_filter_reset(self):
        request = parse_request('Осциллограф 4 канала полоса пропускания 100 МГц', 1)
        template = load_scenario('synthetic-comparable')['offers'][0]
        offers = []
        for index in range(45):
            offer = deepcopy(template); offer.update(id=f'qa-{index:02}', supplier=f'QA {index}', domain=f'qa-{index}.example.invalid', url=f'https://qa-{index}.example.invalid/product')
            offers.append(offer)
        result = finalize(request, offers, {'min_suppliers':3}); result['search'] = {'scenario_kind':'synthetic'}
        self.store.save_result(JOB, result); self.page.reload(wait_until='networkidle')
        self.assertEqual(self.page.locator('[data-offer]').count(), 20)
        self.assertIn('1–20 из 45', self.page.locator('#visible-count').inner_text())
        self.page.get_by_role('button', name='Следующая страница').click()
        self.assertIn('21–40 из 45', self.page.locator('#visible-count').inner_text())
        self.page.get_by_role('button', name='Следующая страница').click()
        self.assertEqual(self.page.locator('[data-offer]').count(), 5)
        self.page.locator('#page-size').select_option('50')
        self.assertEqual(self.page.locator('[data-offer]').count(), 45)
        self.page.locator('#page-size').select_option('20')
        self.page.get_by_role('button', name='Следующая страница').click()
        self.page.locator('#result-filters summary').click()
        self.page.locator('#status-filter').select_option('uncertain')
        self.assertIn('Страница 1 из', self.page.locator('#offer-pagination').inner_text())
        self.no_overflow()

    def test_03_mobile_report_precedes_items_and_offers(self):
        self.page.set_viewport_size({'width':390, 'height':844})
        for selector in ('#report-pdf', '#report-docx'):
            box = self.page.locator(selector).bounding_box(); self.assertLess(box['y'] + box['height'], 844)
            self.assertLess(box['y'], self.page.locator('#item-navigation').bounding_box()['y'])
        self.page.locator('[data-select-item="line-b"]').first.click()
        self.assertIn('line-b:', self.page.locator('[data-offer]').first.get_attribute('data-offer'))
        self.no_overflow()

    def test_04_typed_correction_save_reload_and_reset_uses_real_api(self):
        self.page.locator('[data-offer="line-a:native:one"] .verify-action').click()
        self.assertIn('Цена', self.page.locator('#field-proof').inner_text())
        self.assertEqual(self.page.locator('#correction-value').get_attribute('type'), 'number')
        self.page.locator('#field-edit > summary').click()
        self.page.locator('#correction-value').fill('30000.25')
        self.page.get_by_role('button', name='Сохранить уточнение').click()
        self.page.get_by_text('Сохранено. Расчёт и отчёт обновлены.', exact=True).wait_for()
        saved = self.store.get_job(JOB)
        self.assertEqual(saved['revision'], 1)
        self.assertEqual(saved['manual_overrides']['line-a:native:one']['price']['value'], 30000.25)
        self.assertIn('Ручное подтверждение', self.page.locator('#field-proof').inner_text())
        self.assertIn('Не указано', self.page.locator('.field-comparison').inner_text())
        self.assertIn('Цена в источнике: не указана', self.page.locator('.evidence-price').inner_text())
        self.assertIn('Цена для расчёта · уточнено вручную', self.page.locator('.evidence-price').inner_text())
        self.page.screenshot(path=str(self.output/'manual-price-provenance-desktop.png'))
        self.page.set_viewport_size({'width':390,'height':844})
        self.page.screenshot(path=str(self.output/'manual-price-provenance-mobile.png'))
        self.no_overflow()
        self.page.set_viewport_size({'width':1292,'height':754})
        self.page.locator('[data-proof-field="tax"]').click()
        self.page.locator('#field-edit > summary').click()
        self.page.locator('#correction-value').select_option('included')
        self.page.get_by_role('button', name='Сохранить уточнение').click()
        self.page.get_by_text('Сохранено. Расчёт и отчёт обновлены.', exact=True).wait_for()
        self.assertEqual(self.store.get_job(JOB)['revision'], 2)
        self.page.reload(wait_until='networkidle')
        self.page.locator('[data-offer="line-a:native:one"] [data-evidence]').last.click()
        self.page.locator('[data-proof-field="price"]').click()
        self.assertEqual(self.page.locator('#correction-value').input_value(), '30000.25')
        self.page.locator('#field-edit > summary').click()
        self.page.get_by_role('button', name='Вернуть исходные данные').click()
        self.page.get_by_text('Сохранено. Расчёт и отчёт обновлены.', exact=True).wait_for()
        self.assertEqual(self.store.get_job(JOB)['revision'], 3)
        self.assertFalse(self.store.get_job(JOB)['manual_overrides'])

    def test_05_conflict_is_visible_and_requires_reload(self):
        self.page.locator('[data-offer="line-a:native:one"] .verify-action').click()
        self.store.apply_review(JOB, 'line-a:native:one', 'update', 'QA parallel correction', fields={'price':31000}, expected_revision=0)
        self.page.locator('#field-edit > summary').click()
        self.page.locator('#correction-value').fill('32000')
        self.page.get_by_role('button', name='Сохранить уточнение').click()
        self.page.get_by_role('button', name='Загрузить актуальные данные').wait_for()
        self.assertEqual(self.store.get_job(JOB)['revision'], 1)
        self.page.get_by_role('button', name='Загрузить актуальные данные').click()
        self.page.wait_for_function("() => document.querySelector('#correction-value').value === '31000'")
        self.assertEqual(self.store.get_job(JOB)['manual_overrides']['line-a:native:one']['price']['value'], 31000)

    def test_06_feedback_shadow_host_and_area_mode_preserve_filters(self):
        # Explicit synthetic replica of the real widget's observable DOM contract.
        self.page.evaluate("""() => {
          const host = document.createElement('public-feedback-widget');
          host.style='position:fixed;bottom:10px;right:10px;z-index:9999';
          host.attachShadow({mode:'open'}).innerHTML='<button id="launch">QA feedback</button><button id="area">QA area</button><div class="picker" style="display:none;position:fixed;inset:0;pointer-events:none"></div>';
          host.shadowRoot.querySelector('#area').onclick=()=>host.shadowRoot.querySelector('.picker').style.display='block';
          document.body.append(host);
        }""")
        self.page.locator('#result-filters summary').click()
        self.page.locator('public-feedback-widget #launch').click()
        self.assertTrue(self.page.locator('#result-filters').evaluate('(e) => e.open'))
        self.page.locator('public-feedback-widget #area').click()
        self.page.locator('#selected-item-heading').click()
        self.assertTrue(self.page.locator('#result-filters').evaluate('(e) => e.open'))
        self.page.evaluate("document.querySelector('public-feedback-widget').remove()")
        self.page.locator('#selected-item-heading').click()
        self.assertFalse(self.page.locator('#result-filters').evaluate('(e) => e.open'))

    def test_07_exact_unit_suffix_display_preserves_original_and_edits(self):
        request = deepcopy(self.result['request'])
        request['items'][0]['requirements'] = [
            {'key':'size','label':'Размер','op':'eq','value':'600х600х600 мм','unit':'мм','quote':'QA original 600х600х600 мм'},
            {'key':'nozzles','label':'Сопла','op':'eq','value':'0.3/0.4/0.6/0.8/1.0мм','unit':'мм'},
            {'key':'power','label':'Питание','op':'eq','value':'230 В, 50 Гц','unit':'Гц'}]
        self.store.create_job({'id':'c'*24,'status':'complete','result':{**deepcopy(self.result),'request':request}},request)
        self.page.goto(self.base+'#job='+'c'*24,wait_until='networkidle')
        self.page.locator('#edit-query').click()
        first = self.page.locator('[data-item-index="0"]'); first.locator('summary').click()
        values = first.locator('[data-requirement-field="value"]')
        self.assertEqual(values.nth(0).input_value(),'600х600х600')
        self.assertEqual(values.nth(1).input_value(),'0.3/0.4/0.6/0.8/1.0')
        self.assertEqual(values.nth(2).input_value(),'230 В, 50 Гц')
        captured=[]
        self.page.route('**/api/search',lambda route:(captured.append(route.request.post_data_json),route.fulfill(status=400,json={'error':'QA intentional stop; no research'})))
        values.nth(1).fill('0.4/0.6')
        self.page.locator('#search-submit').click()
        self.page.wait_for_function("() => document.querySelector('#global-error').textContent.includes('QA intentional')")
        reqs = captured[0]['items'][0]['requirements']
        self.assertEqual(reqs[0]['value'],'600х600х600 мм')
        self.assertEqual(reqs[0]['quote'],'QA original 600х600х600 мм')
        self.assertEqual(reqs[1]['value'],'0.4/0.6')
        self.assertTrue(reqs[1]['manual_override'])
        self.assertEqual(reqs[2]['value'],'230 В, 50 Гц')

    def test_08_downloads_contain_full_result_with_one_selected_item(self):
        with self.page.expect_download() as event:
            self.page.locator('#report-docx').click()
        path=self.output/'synthetic-full-report.docx';event.value.save_as(path)
        with ZipFile(path) as archive:
            xml=archive.read('word/document.xml').decode()
        self.assertTrue('QA Осциллограф для лаборатории' in xml and 'QA Осциллограф для сервиса' in xml, 'DOCX must include both items while the UI selects only one')
        self.assertNotIn('public-feedback-widget',xml)
        self.assertIn('Учебные',xml)
        with self.page.expect_download() as event:
            self.page.locator('#report-pdf').click()
        pdf=self.output/'synthetic-full-report.pdf';event.value.save_as(pdf)
        self.assertTrue(pdf.read_bytes().startswith(b'%PDF-'), 'PDF button must deliver an actual PDF')


    def test_10_comparison_units_and_action_target_are_unambiguous(self):
        result=deepcopy(self.result)
        line=result['items'][0]
        requirement={'key':'dimension','label':'Размер','op':'eq','value':'600x600x600 мм','unit':'мм'}
        line['request']['requirements'].append(requirement)
        line['base_result']['request']['requirements'].append(deepcopy(requirement))
        result['validation_notice']='Служебная перепроверка сохранённого примера.'
        self.store.save_result(JOB,result);self.page.reload(wait_until='networkidle')
        self.assertFalse(self.page.locator('#validation-notice').is_visible())
        self.page.locator('[data-offer="line-a:native:one"] .verify-action').click()
        self.assertIn('Размер',self.page.locator('#field-proof h3').inner_text())
        requested=self.page.locator('[data-fact-field="attributes.dimension"] .fact-request').inner_text()
        self.assertEqual(requested.count('мм'),1)
        self.assertEqual(self.page.locator('.field-comparison dd').first.inner_text().count('мм'),1)
        self.page.locator('#close-evidence').click()
        self.page.get_by_text('О поиске',exact=True).click()
        self.assertTrue(self.page.get_by_text(result['validation_notice'],exact=True).last.is_visible())

    def test_09_manual_mismatch_keeps_source_and_manual_provenance(self):
        self.page.locator('[data-offer="line-a:native:one"] .verify-action').click()
        self.page.locator('[data-proof-field="attributes.channels"]').click()
        self.page.locator('#field-edit > summary').click()
        self.page.locator('#correction-value').fill('2')
        self.page.get_by_role('button', name='Сохранить уточнение').click()
        self.page.get_by_text('Сохранено. Расчёт и отчёт обновлены.', exact=True).wait_for()
        self.assertIn('Ручное подтверждение', self.page.locator('#field-proof').inner_text())
        self.assertIn('не соответствует требованию', self.page.locator('#field-proof').inner_text())
        comparison=self.page.locator('.field-comparison').inner_text()
        self.assertIn('В карточке\n4',comparison)
        self.assertIn('Уточнено\n2',comparison)
        self.assertIn('Уточнено · не соответствует', self.page.locator('[data-fact-field="attributes.channels"]').inner_text())
        self.page.screenshot(path=str(self.output/'manual-mismatch-provenance-desktop.png'))

    def test_11_dom_photo_and_optional_empty_fields_are_not_missing_facts(self):
        result=deepcopy(self.result);line=result['items'][0];offer=line['offers'][0]
        image_proof={'basis':'primary_product_dom','attribute':'src','raw_value':'https://qa.example.invalid/photo.jpg','url':offer['url'],'source_hash':'a'*64,'document_id':'qa-doc'}
        flat=next(o for o in result['offers'] if o['id']=='line-a:native:one')
        views=[offer,line['base_result']['offers'][0],flat]
        for source in views:
            source['evidence']['image_url']=deepcopy(image_proof)
            source.pop('pack_size',None);source.pop('delivery',None)
            source['evidence'].pop('pack_size',None);source['evidence'].pop('delivery',None)
        self.store.save_result(JOB,result);self.page.reload(wait_until='networkidle')
        self.page.locator('[data-offer="line-a:native:one"] [data-evidence]').last.click()
        for field in ['image_url','pack_size','delivery']:
            self.assertEqual(self.page.locator('[data-fact-field="'+field+'"]').count(),0,field)
        self.assertEqual(self.page.locator('[data-fact-field="tax"]').count(),1)
        self.assertEqual(self.store.get_job(JOB)['result']['items'][0]['offers'][0]['evidence']['image_url'],image_proof)
        self.page.locator('#close-evidence').click()
        for source in views:
            source['unit']='пачка';source.pop('pack_size',None);source.get('evidence',{}).pop('pack_size',None)
        self.store.save_result(JOB,result);self.page.reload(wait_until='networkidle')
        self.page.locator('[data-offer="line-a:native:one"] [data-evidence]').last.click()
        self.assertEqual(self.page.locator('[data-fact-field="pack_size"]').count(),1)
        self.page.locator('[data-proof-field="pack_size"]').click()
        self.page.locator('#field-edit > summary').click()
        self.assertTrue(self.page.get_by_role('button',name='Сохранить уточнение').is_visible())


if __name__ == '__main__':
    unittest.main()
