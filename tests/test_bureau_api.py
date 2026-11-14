"""Bureau delivery contracts: named scopes, recipient evidence and share base.

All fixtures are synthetic. No model, public website or production storage is
used by these tests.
"""
from copy import deepcopy
from io import BytesIO
import unittest
from unittest.mock import patch
from zipfile import ZipFile
from xml.etree import ElementTree as ET
from html import escape

from quotelens.reports import document_model, report_docx, report_html, report_pdf
from quotelens.sharing import reader_html, sanitize_snapshot
from quotelens.source_presets import source_presets
from quotelens.source_policy import source_metadata
from quotelens.supplier import supplier_details
from tests.test_history_api import HistoryServerFixture
from tests.test_review_acceptances import fixture, JOB


class BureauSourceTests(unittest.TestCase):
    def test_case_sites_are_selectable_without_claiming_live_connectors(self):
        http = HistoryServerFixture()
        self.addCleanup(http.close)
        status, rows, _ = http.request('/api/sources')
        self.assertEqual(status, 200)
        by_id = {row['id']: row for row in rows}
        for key in ('eis', 'gisp', 'sber_ast', 'rts', 'roseltorg', 'etp_gpb',
                    'zakaz_rf', 'tek_torg', 'fabrikant', 'rad', 'berezka',
                    'mos_suppliers', 'etm', 'chipdip', 'yandex_market',
                    'ozon', 'vseinstrumenti', 'komus'):
            with self.subTest(source=key):
                self.assertTrue(by_id[key]['selectable'])
                self.assertTrue(by_id[key]['domain'])
                self.assertTrue(by_id[key]['case_source'])
        self.assertFalse(by_id['eis']['connected'])
        self.assertFalse(by_id['gisp']['connected'])
        self.assertTrue(by_id['prist']['connected'])
        self.assertFalse(by_id['korus']['selectable'])
        self.assertEqual(by_id['korus']['domain'], '')
        self.assertEqual(len(rows), len(by_id))

    def test_source_choices_are_detached_from_their_definitions(self):
        first = source_presets()
        first[0]['domain'] = 'changed.invalid'
        self.assertNotEqual(source_presets()[0]['domain'], 'changed.invalid')

    def test_case_roles_match_native_policy_and_platform_footer_is_not_a_seller(self):
        for source in source_presets():
            if not source.get('domain'):
                continue
            domain = source['domain']
            with self.subTest(domain=domain):
                self.assertEqual(source_metadata(domain)['source_group'], source['source_group'])
                if source['source_group'] != 'platforms':
                    continue
                footer = 'ООО «Тестовый оператор» ИНН 1111111111 Контакты: office@example.invalid'
                captured = {'url': 'https://' + domain + '/product',
                    'html': '<main>Учебный товар</main><footer>' + footer + '</footer>',
                    'text': 'Учебный товар ' + footer}
                details = supplier_details(captured)
                self.assertIsNone(details['inn'])
                self.assertEqual(details['verification']['status'], 'conflict')
                seller = 'ООО «Учебный продавец» ИНН 2222222222 Контакты: sales@example.invalid'
                captured.update(html='<main>Учебный товар</main><section class="seller-info">' + seller + '</section><footer>' + footer + '</footer>',
                                text='Учебный товар ' + seller + ' ' + footer)
                self.assertEqual(supplier_details(captured)['inn'], '2222222222')

    def test_edited_context_note_is_canonical_and_not_a_new_item(self):
        http = HistoryServerFixture()
        self.addCleanup(http.close)
        status, updated, _ = http.request('/api/prepare',
            {'description': 'Гарантия не менее 24 месяцев.', 'files': [], 'context_only': True})
        self.assertEqual(status, 200)
        self.assertEqual(updated['items'], [])
        self.assertEqual(updated['documents'][0]['text'], 'Гарантия не менее 24 месяцев.')
        self.assertEqual(updated['conditions'][0]['source']['document_id'], updated['documents'][0]['id'])
        self.assertEqual(updated['conditions'][0]['label'], 'Дополнение к ТЗ')
        self.assertEqual(updated['conditions'][0]['quote'], 'Гарантия не менее 24 месяцев.')
        status, cleared, _ = http.request('/api/prepare', {'description': '', 'context_only': True})
        self.assertEqual(status, 200)
        self.assertEqual(cleared['documents'], [])
        self.assertEqual(cleared['conditions'], [])
        self.assertEqual(http.request('/api/prepare', {'description':'Note', 'context_only':'true'})[0], 400)
        self.assertEqual(http.request('/api/prepare', {'description':'Note', 'context_only':True,
            'files':[{'name':'not-accepted.txt','data':'bm90IHVzZWQ='}]})[0], 400)


class BureauRecipientTests(unittest.TestCase):
    def setUp(self):
        self.result, self.payload, self.identifier = fixture()
        self.offer = self.result['items'][0]['offers'][0]
        self.offer['url'] = 'https://supplier.example/product/qa-one'
        self.offer['attributes']['brand'] = 'QA Brand'
        self.offer['supplier_details'] = {
            'legal_name': 'ООО «Учебный поставщик»', 'inn': '1234567890',
            'email': 'sales@supplier.example', 'phone': '+7 000 000-00-00',
            'website': 'https://supplier.example/',
            'evidence': {'inn': {'quote': 'ИНН 1234567890',
                'url': 'https://supplier.example/contacts'}},
            'verification': {'status': 'format_valid', 'registry_url': 'https://egrul.nalog.ru/'}}

    def test_reports_keep_exact_offer_supplier_contacts_and_unknown_manufacturer(self):
        model = document_model(self.result)
        section = next(s for s in model['sections'] if s['title'] == 'Поставщик и товар 1.1')
        rows = dict(section['table']['rows'])
        self.assertEqual(rows['Карточка предложения'], self.offer['url'])
        self.assertEqual(rows['Email'], 'sales@supplier.example')
        self.assertEqual(rows['ИНН'], '1234567890')
        self.assertEqual(rows['Производитель'], 'Нет подтверждённых сведений')
        self.assertEqual(rows['Бренд'], 'QA Brand')
        html = report_html(self.result)
        self.assertIn('href="https://supplier.example/product/qa-one"', html)
        self.assertIn('sales@supplier.example', html)
        self.assertIn('Проверка организации по государственному реестру не выполнялась', html)
        self.assertNotIn('public-feedback-widget', html)
        self.assertNotIn('<script', html)
        with ZipFile(BytesIO(report_docx(self.result))) as archive:
            document = archive.read('word/document.xml').decode()
            relationships = archive.read('word/_rels/document.xml.rels').decode()
        self.assertIn('sales@supplier.example', document)
        self.assertIn('https://supplier.example/product/qa-one', relationships)

    def test_missing_supplier_fields_remain_explicit_in_reader_and_report(self):
        self.offer['supplier_details'] = {}
        model = document_model(self.result)
        section = next(s for s in model['sections'] if s['title'] == 'Поставщик и товар 1.1')
        rows = dict(section['table']['rows'])
        self.assertEqual(rows['ИНН'], 'Нет подтверждённых сведений')
        snapshot, _ = sanitize_snapshot(self.result)
        page = reader_html(snapshot)
        self.assertIn('<dt>Производитель</dt><dd>Нет подтверждённых сведений</dd>', page)
        self.assertIn('<dt>ИНН</dt><dd>Нет подтверждённых сведений</dd>', page)
        self.assertNotIn('Не указан в сохранённом источнике', page)
        self.assertIn('Поставщик и производитель', page)
        self.assertNotIn('<form', page)
        self.assertNotIn('<script', page)

    def test_distinct_manufacturer_proof_link_is_clickable_in_all_recipient_formats(self):
        url = 'https://manufacturer.example/products/qa-one?variant=A&lang=ru'
        self.offer['attributes']['manufacturer'] = 'Учебный изготовитель'
        self.offer['evidence']['attributes.manufacturer'] = {
            'quote':'Изготовитель: Учебный изготовитель', 'url':url}
        page = report_html(self.result)
        self.assertIn('href="' + escape(url, quote=True) + '"', page)
        snapshot, _ = sanitize_snapshot(self.result)
        self.assertIn('href="' + escape(url, quote=True) + '"', reader_html(snapshot))
        with ZipFile(BytesIO(report_docx(self.result))) as archive:
            relationships = ET.fromstring(archive.read('word/_rels/document.xml.rels'))
        self.assertTrue(any(r.get('Target') == url and r.get('Type', '').endswith('/hyperlink')
                            for r in relationships))
        from pypdf import PdfReader
        pdf = PdfReader(BytesIO(report_pdf(self.result)))
        targets = []
        for page in pdf.pages:
            for annotation in page.get('/Annots', []):
                action = annotation.get_object().get('/A', {})
                if action.get('/S') == '/URI':
                    targets.append(action.get('/URI'))
        self.assertIn(url, targets)

    def test_share_domain_is_configuration_and_snapshot_stays_immutable(self):
        http = HistoryServerFixture()
        self.addCleanup(http.close)
        http.db.create_job({'id': JOB, 'status': 'complete', 'result': self.result}, self.payload)
        with patch.dict('os.environ', {'APP_PUBLIC_SHARE_BASE': 'https://prices.example.org/procurement/'}):
            status, receipt, _ = http.request('/api/jobs/' + JOB + '/share', {'expected_revision': 0})
        self.assertEqual(status, 201)
        self.assertTrue(receipt['url'].startswith('https://prices.example.org/procurement/'))
        self.assertNotIn('mikhio', receipt['url'])
        token = receipt['url'].rstrip('/').rsplit('/', 1)[1]
        saved = deepcopy(http.db.get_share(token))
        self.assertIsNotNone(saved)
        http.db.apply_review(JOB, self.identifier, 'exclude', 'QA only', expected_revision=0)
        self.assertEqual(http.db.get_share(token), saved)


if __name__ == '__main__':
    unittest.main()
