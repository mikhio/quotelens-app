"""Bureau end-to-end acceptance through the real Handler, SQLite and exports.

Research uses explicitly synthetic candidates; selected public-domain DNS is
fixed for the network-isolated fixture. URL/IP admission code remains active.
Preparation, validation, persistence, corrections, sharing and documents use
the production code. No paid calls, public network or owner data is involved.
"""
from copy import deepcopy
from http.server import ThreadingHTTPServer
from io import BytesIO
import json
import os
from pathlib import Path
import sys
import tempfile
import socket
import threading
import unittest
from unittest.mock import patch
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from quotelens import server
from quotelens.procurement import aggregate_results, request_for_item
from quotelens.scenarios import load_scenario
from quotelens.selection import finalize
from quotelens.storage import Store

JOB = 'b' * 24
DESCRIPTION = 'Осциллограф цифровой, 4 канала, полоса не менее 100 МГц.\nКоличество: 2 шт.'


def result_for(payload, count=3):
    items = deepcopy(payload['items'])
    lines = {}
    sample = load_scenario('synthetic-comparable')['offers'][0]
    for item in items:
        request = request_for_item(item)
        offers = []
        for n in range(count):
            offer = deepcopy(sample)
            url = f'https://qa-{n}.example.invalid/product'
            offer.update(id=f'qa-{n}', supplier=f'QA поставщик {n}', domain=f'qa-{n}.example.invalid',
                         url=url, title='QA ' + item['title'], price=69000+n*100,
                         unit=request.get('unit') or 'шт', tax='included', provenance='synthetic')
            offer['evidence'] = {key: {'quote': f'QA: {key} = {value}', 'url': url}
                                 for key, value in (('price', offer['price']), ('unit', offer['unit']), ('tax', 'НДС включён'))}
            for req in request.get('requirements', []):
                value = req['value']
                if req.get('op') == 'between' and isinstance(value, list):
                    value = sum(value) / len(value)
                offer['attributes'][req['key']] = value
                offer['evidence']['attributes.' + req['key']] = {'quote': f"QA: {req['label']} {value}", 'url': url}
            offers.append(offer)
        line = finalize(request, offers)
        line['search'] = {'scenario_kind': 'synthetic', 'budget': {'model_calls': 0}, 'source_statuses': []}
        lines[item['id']] = line
    result = aggregate_results(payload, items, lines, stop='source_exhausted')
    result['search'].update(mode='live_agent', scenario_kind='synthetic')
    return result


class FixtureResearch:
    def __init__(self, store, identifier, payload, cancelled, event):
        self.payload = payload

    def run(self):
        return result_for(self.payload)


class BureauBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright
        cls.out = Path(os.environ.get('BUREAU_UI_OUTPUT', '/evidence/bureau-ui'))
        cls.out.mkdir(parents=True, exist_ok=True)
        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch(executable_path=os.environ.get('BROWSER_EXECUTABLE_PATH', '/usr/bin/google-chrome'),
            headless=True, args=['--no-sandbox', '--disable-dev-shm-usage'])

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='bureau-ui-')
        self.db = Store(Path(self.tmp.name) / 'qa.sqlite3')
        original_resolve = socket.getaddrinfo
        def resolve(host, port, *args, **kwargs):
            if host in {'zakupki.gov.ru','gisp.gov.ru','prist.ru'}:
                return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, '', ('93.184.216.34', port))]
            return original_resolve(host, port, *args, **kwargs)
        self.patches = [patch('socket.getaddrinfo', side_effect=resolve), patch.object(server, 'store', return_value=self.db),
                        patch.object(server, 'JOBS', {}), patch.object(server, 'ACTIVE', threading.BoundedSemaphore(2)),
                        patch('quotelens.procurement.ProcurementResearch', FixtureResearch)]
        for item in self.patches:
            item.start()
        self.http = ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
        self.thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.thread.start()
        self.base = f'http://127.0.0.1:{self.http.server_port}/'
        self.context = self.browser.new_context(viewport={'width':1440, 'height':1000}, accept_downloads=True)
        self.page = self.context.new_page()
        self.page.set_default_timeout(10000)
        self.errors, self.searches, self.worker_errors = [], [], []
        self.original_thread_hook = threading.excepthook
        threading.excepthook = lambda args: self.worker_errors.append(str(args.exc_value))
        self.page.on('pageerror', lambda e: self.errors.append(str(e)))
        self.page.on('request', lambda req: self.searches.append(req.post_data_json)
                     if req.method == 'POST' and req.url.endswith('/api/search') else None)
        self.page.goto(self.base)
        self.page.wait_for_selector('#prepare-spec')

    def tearDown(self):
        for worker in list(threading.enumerate()):
            if getattr(worker, '_target', None) is server.execute:
                worker.join(timeout=5)
        self.page.screenshot(path=str(self.out / (self._testMethodName + '.png')), full_page=True)
        self.context.close()
        self.http.shutdown()
        self.http.server_close()
        self.thread.join(timeout=2)
        for item in reversed(self.patches):
            item.stop()
        self.tmp.cleanup()
        threading.excepthook = self.original_thread_hook
        self.assertEqual(self.errors, [])
        self.assertEqual(self.worker_errors, [])

    def no_overflow(self):
        self.assertFalse(self.page.evaluate('document.documentElement.scrollWidth > innerWidth + 1'))

    def prepare_text(self):
        self.page.locator('#description').fill(DESCRIPTION)
        self.page.locator('#prepare-spec').click()
        self.page.locator('#spec-items article:not([hidden])').wait_for()

    def seed_result(self, count=3):
        prepared = server.prepare_input(DESCRIPTION)
        first = prepared['items'][0]
        second = deepcopy(first)
        first.update(id='qa-a', title='QA позиция А')
        second.update(id='qa-b', title='QA позиция Б')
        payload = {'mode':'live', 'description':'Синтетическая QA закупка', 'quantity':1,
                   'items':[first, second], 'search_policy':{'mode':'target', 'max_seconds':600}}
        result = result_for(payload, count)
        if self.db.get_job(JOB) is None:
            self.db.create_job({'id':JOB, 'status':'complete', 'result':result}, payload)
        self.page.goto(self.base+'#job='+JOB)
        self.page.locator('#offer-list [data-offer]').first.wait_for()
        return result

    def test_01_text_review_edit_search_real_api(self):
        self.prepare_text()
        self.assertEqual(self.searches, [])
        self.assertEqual(self.page.locator('#procurement-stages [aria-current=step]').get_attribute('data-workflow-stage'), 'review')
        self.assertTrue(self.page.locator('[data-requirement-field=value]').first.is_visible())
        self.page.locator('[data-requirement-field=value]').first.fill('6')
        self.page.locator('#review-next').click()
        self.assertEqual(self.searches, [])
        self.assertTrue(self.page.locator('#research-settings').is_visible())
        self.assertGreater(self.page.locator('#stop-mode-cards').bounding_box()['width'], 150)
        self.page.locator('#search-submit').click()
        self.page.locator('#offer-list [data-offer]').first.wait_for()
        self.assertEqual(len(self.searches), 1)
        requirement = self.searches[0]['items'][0]['requirements'][0]
        self.assertEqual(requirement['value'], 6)
        self.assertTrue(requirement['manual_override'])
        self.assertTrue(requirement.get('quote'))
        identifier = self.page.url.split('job=')[1]
        self.assertEqual(self.db.get_job(identifier)['status'], 'complete')
        self.assertEqual(self.db.quota_usage('model_calls', 'all'), 0)
        self.no_overflow()

    def test_02_file_and_combined_input_navigation_and_known_sources(self):
        self.page.locator('#description').fill('Гарантия не менее 12 месяцев.')
        self.page.locator('#spec-files').set_input_files(str(ROOT/'tests/fixtures/specifications/paper.docx'))
        self.assertFalse(self.page.locator('#spec-preview').is_visible())
        self.page.locator('#prepare-spec').click()
        self.page.locator('#draft-item-navigation button').first.wait_for()
        self.assertEqual(self.page.locator('#draft-item-navigation button').count(), 3)
        self.page.locator('#draft-item-navigation button').nth(1).click()
        self.assertEqual(self.page.locator('#spec-items article:not([hidden])').get_attribute('data-item-index'), '1')
        self.page.locator('#review-next').click()
        self.page.locator('#source-picker-summary').click()
        self.page.locator('[name=source_scope_mode][value=domains]').check()
        for domain in ('zakupki.gov.ru','gisp.gov.ru','prist.ru'):
            self.page.locator(f'[data-source-domain="{domain}"]').check()
        self.page.locator('#stop-mode-cards input[value=offers]').check()
        self.assertTrue(self.page.locator('#offer-limit-field').is_visible())
        self.page.locator('#search-submit').click()
        try:
            self.page.wait_for_function('() => location.hash.includes("job=")')
        except Exception:
            print('START_FAILURE', json.dumps({'global':self.page.locator('#global-error').inner_text(),'source':self.page.locator('#source-error').inner_text(),'spec':self.page.locator('#spec-preparation').inner_text(),'searches':len(self.searches)},ensure_ascii=False))
            raise
        self.page.locator('#result-screen:not([hidden])').wait_for()
        self.assertEqual(set(self.searches[0]['source_scope']['domains']), {'zakupki.gov.ru','gisp.gov.ru','prist.ru'})
        self.assertEqual(len(self.searches[0]['items']), 3)
        self.assertGreaterEqual(len(self.searches[0]['input_documents']), 2)
        self.no_overflow()

    def test_03_pagination_above_offers_and_research_entry(self):
        self.seed_result(45)
        self.assertEqual(self.page.locator('#offer-list [data-offer]').count(), 20)
        self.assertLess(self.page.locator('#offer-pagination-top').bounding_box()['y'], self.page.locator('#offer-list').bounding_box()['y'])
        self.page.get_by_role('button', name='Следующая страница', exact=True).click()
        self.assertIn('21–40', self.page.locator('#visible-count').inner_text())
        self.page.locator('#page-size-top').select_option('100')
        self.assertEqual(self.page.locator('#offer-list [data-offer]').count(), 45)
        self.page.locator('[data-open-research]').click()
        self.assertTrue(self.page.locator('#research-audit').evaluate('(e)=>e.open'))
        self.no_overflow()

    def test_04_real_exports_and_share_keep_whole_procurement(self):
        self.seed_result()
        self.page.locator('[data-select-item="qa-b"]').first.click()
        for extension in ('docx','pdf'):
            with self.page.expect_download(timeout=40000) as event:
                self.page.locator('#report-'+extension).click()
            target = self.out / ('full-bureau.'+extension)
            event.value.save_as(target)
            if extension == 'docx':
                with ZipFile(target) as archive:
                    data = archive.read('word/document.xml').decode()
                    rels = archive.read('word/_rels/document.xml.rels').decode()
                self.assertIn('QA позиция А', data)
                self.assertIn('QA позиция Б', data)
                self.assertIn('/hyperlink', rels)
                self.assertNotIn('public-feedback-widget', data)
            else:
                from pypdf import PdfReader
                data = '\n'.join(page.extract_text() for page in PdfReader(target).pages)
                self.assertIn('QA позиция А', data)
                self.assertIn('QA позиция Б', data)
        self.page.locator('#share-result').click()
        self.page.locator('#share-url').wait_for()
        url = self.page.locator('#share-url').input_value()
        self.assertTrue(url.startswith(self.base))
        reader = self.context.new_page()
        reader.goto(url)
        self.assertIn('QA позиция А', reader.locator('body').inner_text())
        self.assertIn('QA позиция Б', reader.locator('body').inner_text())
        self.assertEqual(reader.locator('form,script,public-feedback-widget').count(), 0)
        reader.close()

    def test_06_edited_combined_note_preserves_file_and_manual_requirements(self):
        for reattach in (False, True):
            with self.subTest(reattach=reattach):
                self.page.goto(self.base)
                self.page.locator('#description').fill('Гарантия не менее 12 месяцев.')
                self.page.locator('#spec-files').set_input_files(str(ROOT/'tests/fixtures/specifications/paper.docx'))
                self.page.locator('#prepare-spec').click()
                self.page.locator('#draft-item-navigation button').first.wait_for()
                self.assertEqual(self.page.locator('#draft-item-navigation button').count(), 3)
                article = self.page.locator('#spec-items article:not([hidden])')
                field = article.locator('[data-requirement-field=value]').first
                field.fill('Учебный изготовитель вручную')
                article.locator('[data-item-field=quantity]').fill('2501')
                self.page.locator('#review-back').click()
                self.page.locator('#description').fill('Гарантия не менее 24 месяцев.')
                if reattach:
                    self.page.locator('#spec-files').set_input_files(str(ROOT/'tests/fixtures/specifications/paper.docx'))
                self.page.locator('#prepare-spec').click()
                self.page.locator('#draft-item-navigation button').first.wait_for()
                self.assertEqual(self.page.locator('#draft-item-navigation button').count(), 3)
                article = self.page.locator('#spec-items article:not([hidden])')
                self.assertEqual(article.locator('[data-requirement-field=value]').first.input_value(), 'Учебный изготовитель вручную')
                self.assertEqual(article.locator('[data-item-field=quantity]').input_value(), '2501')
                self.page.locator('#review-next').click()
                self.page.locator('#search-submit').click()
                self.page.locator('#result-screen:not([hidden])').wait_for()
                sent = self.searches[-1]
                self.assertEqual(len(sent['items']), 3)
                for item in sent['items']:
                    notes = [v for v in item.get('conditions', []) if v.get('label') == 'Дополнение к ТЗ']
                    self.assertEqual([v['text'] for v in notes], ['Гарантия не менее 24 месяцев.'])
                first = sent['items'][0]
                self.assertEqual(first['quantity'], 2501)
                self.assertTrue(first['requirements'][0]['manual_override'])
                self.assertEqual(first['requirements'][0]['value'], 'Учебный изготовитель вручную')
                self.assertIn('Светогорский', first['requirements'][0]['quote'])
                notes = [v for v in sent['input_documents'] if v.get('name') == 'Текст запроса']
                self.assertEqual([v['text'] for v in notes], ['Гарантия не менее 24 месяцев.'])

    def test_05_mobile_views_fonts_and_evidence(self):
        for width in (390,375):
            self.page.set_viewport_size({'width':width,'height':844})
            self.prepare_text()
            self.no_overflow()
            self.page.screenshot(path=str(self.out/f'review-{width}.png'), full_page=True)
            self.page.locator('#review-next').click()
            self.no_overflow()
            self.seed_result()
            self.no_overflow()
            self.page.locator('[data-evidence]').first.click()
            self.page.locator('#evidence-body').wait_for()
            self.no_overflow()
            self.page.screenshot(path=str(self.out/f'evidence-{width}.png'), full_page=True)
            self.page.locator('#close-evidence').click()
            self.page.locator('.brand').click()


if __name__ == '__main__':
    unittest.main(verbosity=2)
