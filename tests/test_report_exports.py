"""Saved-result export parity, editable OOXML and network-free PDF checks."""
from copy import deepcopy
from io import BytesIO
import unittest
from xml.etree import ElementTree as ET
from zipfile import ZipFile

from quotelens.engine import analyze_offers, parse_request
from quotelens.reports import document_model, report_docx, report_html, report_pdf
from quotelens.selection import finalize
from test_provisional_calculation import observation, sheet_purchase


def result_fixture():
    result = analyze_offers(parse_request("Осциллограф 4 канала полоса 100 МГц", 3),
                            [observation("supplier-a", 100), observation("supplier-b", 200)])
    result["search"] = {"started_at": "2026-11-12T12:00:00Z", "finished_at": "2026-11-12T12:02:00Z",
                        "stop": {"code": "time_budget", "label": "Достигнуто выбранное время", "detail": "Найденное сохранено."},
                        "budget": {"pages_used": 8, "queries_used": 3, "model_calls": 2, "elapsed_seconds": 120,
                                   "actual_usage": {"prompt_tokens": 5000, "completion_tokens": 1000, "total_tokens": 6000, "cost": 0.0005}},
                        "policy": {"mode": "time", "max_seconds": 120}}
    result["review"] = {"revision": 4}
    return result


class ReportExportTests(unittest.TestCase):
    def test_html_contains_conditional_math_sources_revision_and_stop_reason(self):
        result = result_fixture()
        html = report_html(result)
        for text in ["Предварительная стоимость", "450,00", "100,00", "200,00", "Ревизия результата: 4",
                     "Достигнуто выбранное время", "https://supplier-a.example/product/", "Точная цитата", "НДС"]:
            self.assertIn(text, html)
        self.assertNotIn('<script', html)
        self.assertNotIn('public-feedback-widget', html)
        self.assertNotIn('<button', html)
        self.assertNotIn('Скачать', html)

    def test_report_html_escapes_supplier_markup_and_refuses_unsafe_links(self):
        result = result_fixture()
        result['request']['description'] = '<img src="https://private.invalid/a"><script>alert(1)</script>'
        result['offers'][0]['title'] = '<svg onload=alert(1)>'
        result['offers'][0]['url'] = 'javascript:alert(1)'
        html = report_html(result)
        self.assertIn('&lt;img', html)
        self.assertIn('&lt;svg', html)
        self.assertNotIn('<img', html)
        self.assertNotIn('<svg', html)
        self.assertNotIn('href="javascript:', html)
        self.assertNotIn('src="https://', html)

    def test_docx_is_editable_ooxml_with_same_visible_calculation(self):
        data = report_docx(result_fixture())
        with ZipFile(BytesIO(data)) as archive:
            self.assertEqual(archive.testzip(), None)
            names = archive.namelist()
            self.assertIn('word/document.xml', names)
            self.assertFalse(any('vba' in n.lower() or 'embedding' in n for n in names))
            document = ET.fromstring(archive.read('word/document.xml'))
            text = ' '.join(document.itertext())
            for expected in ['Предварительная стоимость', '450,00', 'Ревизия результата: 4', 'https://supplier-a.example/product/', 'НДС']:
                self.assertIn(expected, text)
            self.assertIn('{http://schemas.openxmlformats.org/wordprocessingml/2006/main}tbl', {node.tag for node in document.iter()})
            relationships = ET.fromstring(archive.read('word/_rels/document.xml.rels'))
            external = [node for node in relationships if node.get('TargetMode') == 'External']
            self.assertTrue(external)
            for relation in external:
                self.assertTrue(relation.get('Type', '').endswith('/hyperlink'))
                self.assertTrue(relation.get('Target', '').startswith('https://'))
        try:
            from docx import Document
        except ImportError:
            pass
        else:
            document = Document(BytesIO(data))
            self.assertGreater(len(document.tables), 0)
            self.assertIn('Расчёт и обоснование', document.paragraphs[0].text)

    def test_multi_item_partial_total_is_not_presented_as_whole_purchase(self):
        a = result_fixture()
        a.update(id='item-a', title='Первая позиция', quantity=3, unit='шт')
        b = {'id': 'item-b', 'title': 'Вторая позиция', 'quantity': 2, 'unit': 'пачка',
             'request': {'description': 'Бумага', 'quantity': 2, 'unit': 'пачка'}, 'offers': [],
             'calculation': {'status': 'insufficient_data', 'total': None, 'reason': 'Нет предложений',
                             'provisional': {'total': None, 'offer_ids': [], 'warnings': []}}}
        result = {'request': {'description': 'Две позиции'}, 'items': [a, b], 'offers': a['offers'],
                  'calculation': {'status': 'insufficient_data', 'total': None, 'reason': 'Не все позиции рассчитаны',
                                  'provisional': {'total': None, 'known_subtotal': 450, 'missing_item_ids': ['item-b']}}, 'search': {}}
        html = report_html(result)
        self.assertIn('Подытог только по позициям', html)
        self.assertIn('Это не сумма всей закупки', html)
        self.assertIn('Не рассчитаны позиции: Вторая позиция', html)
        self.assertIn('Вторая позиция', html)
        self.assertIn('Подтверждённые предложения для этой позиции не получены', html)

    def test_procurement_summary_adds_line_costs_without_averaging_different_goods(self):
        a=result_fixture();a.update(id='a',title='Прибор',quantity=3,unit='шт')
        b=deepcopy(a);b.update(id='b',title='Другой прибор',quantity=4)
        b['calculation']['provisional']['total']=600
        root={'request':{'description':'Два разных товара','quantity':1},'items':[a,b],
              'offers':a['offers']+b['offers'],'calculation':{'status':'insufficient_data','total':None,
              'provisional':{'total':1050,'n':4,'offer_ids':[o['id'] for o in a['offers']],
                             'normalized_prices':a['calculation']['provisional']['normalized_prices']}}}
        model=document_model(root);summary=' '.join(next(section for section in model['sections'] if section['title']=='Результат')['paragraphs'])
        self.assertIn('450,00 ₽ + 600,00 ₽ = 1 050,00 ₽',summary)
        self.assertNotIn(' / ',summary)
        for text in [report_html(root),ET.tostring(ET.fromstring(ZipFile(BytesIO(report_docx(root))).read('word/document.xml')),encoding='unicode')]:
            self.assertIn('Сумма стоимостей позиций',text)

    def test_single_uploaded_line_uses_line_quantity_in_its_own_formula(self):
        item=result_fixture();item.update(id='a',title='Прибор',quantity=3,unit='шт')
        root={'request':{'description':'Файл ТЗ','quantity':1},'items':[item],'offers':item['offers'],
              'calculation':deepcopy(item['calculation'])}
        model=document_model(root)
        self.assertNotIn(' / ',' '.join(next(section for section in model['sections'] if section['title']=='Результат')['paragraphs']))
        line=next(section for section in model['sections'] if section['title']=='Расчёт позиции')
        self.assertIn(' / 2 × 3 = 450,00 ₽',' '.join(line['paragraphs']))

    def test_unknown_purchase_terms_are_retained_in_report(self):
        result = result_fixture()
        result['request']['conditions'] = [{'key': 'delivery', 'label': 'Доставка', 'text': 'Доставка с разгрузкой включена в стоимость'}]
        model = document_model(result)
        text = str(model)
        self.assertIn('Доставка с разгрузкой включена', text)

    def test_input_unchanged_by_all_document_builders(self):
        result = result_fixture();before = deepcopy(result)
        report_html(result);report_docx(result)
        self.assertEqual(result, before)

    def test_untrusted_control_characters_cannot_break_docx_xml(self):
        result = result_fixture()
        result['request']['description'] = 'Осциллограф\x00\x0b\uffff'
        data = report_docx(result)
        with ZipFile(BytesIO(data)) as archive:
            ET.fromstring(archive.read('word/document.xml'))

    def test_actual_pdf_is_local_readable_and_contains_result(self):
        data = report_pdf(result_fixture())
        self.assertTrue(data.startswith(b'%PDF-'))
        self.assertGreater(len(data), 10000)
        try:
            from pypdf import PdfReader
        except ImportError:
            return
        pdf = PdfReader(BytesIO(data))
        text = '\n'.join(page.extract_text() for page in pdf.pages)
        self.assertIn('450,00', text)
        self.assertIn('Предварительная стоимость', text)
        self.assertIn('Достигнуто выбранное время', text)
        self.assertNotIn('Скачать', text)

    def test_fractional_sheet_formula_and_mean_remain_nonzero_in_all_exports(self):
        request, offers = sheet_purchase()
        result = finalize(request, offers)
        rendered = report_html(result)
        self.assertIn('(0,00246 ₽ + 0,00246 ₽ + 0,00246 ₽) / 3 × 1000 = 2,46 ₽', rendered)
        self.assertIn('Среднее: 0,00246 ₽', rendered)
        self.assertNotIn('(0,00 ₽ + 0,00 ₽', rendered)
        with ZipFile(BytesIO(report_docx(result))) as archive:
            document = ET.fromstring(archive.read('word/document.xml'))
            text = ' '.join(document.itertext())
        self.assertIn('0,00246 ₽', text)
        self.assertIn('2,46 ₽', text)
        from pypdf import PdfReader
        pdf = PdfReader(BytesIO(report_pdf(result)))
        pdf_text = '\n'.join(page.extract_text() for page in pdf.pages)
        self.assertIn('0,00246', pdf_text)
        self.assertIn('2,46', pdf_text)
        self.assertNotRegex(pdf_text, r'\(0,00\s*(?:₽|\+)')

    def test_shortened_repeating_unit_prices_have_approximation_and_rounding_note(self):
        request, offers = sheet_purchase(pack_size=3, price='1.00', quantity=3)
        result = finalize(request, offers)
        self.assertAlmostEqual(result['calculation']['unit_price'], 1 / 3, places=15)
        rendered = report_html(result)
        self.assertIn('0,333333333333 ₽', rendered)
        self.assertIn('≈ 1,00 ₽', rendered)
        self.assertIn('показаны с ограниченной точностью', rendered)
        self.assertIn('без промежуточного округления', rendered)

    def test_very_small_nonzero_unit_price_is_never_displayed_as_zero(self):
        request, offers = sheet_purchase(pack_size=500000000)
        rendered = report_html(finalize(request, offers))
        self.assertIn('0,00000000246 ₽', rendered)
        self.assertNotIn('Среднее: 0,00 ₽', rendered)




class ManualRequirementReportTests(unittest.TestCase):
    def test_user_requirement_edit_is_not_attributed_to_original_quote(self):
        from quotelens.reports import report_html, report_docx
        from zipfile import ZipFile
        from io import BytesIO
        result = {"request": {"description": "Синтетическая QA закупка", "quantity": 1,
                  "requirements": [{"key": "density_gsm", "label": "Плотность", "op": "eq", "value": 65,
                  "unit": "г/м²", "quote": "Плотность 80 г/м²", "manual_override": True}]},
                  "offers": [], "calculation": {"status": "insufficient_data", "total": None}, "search": {"scenario_kind": "synthetic"}}
        rendered = report_html(result)
        self.assertIn("изменено пользователем", rendered)
        self.assertIn("Исходный текст", rendered)
        self.assertIn("Плотность 80", rendered)
        with ZipFile(BytesIO(report_docx(result))) as archive:
            text = archive.read("word/document.xml").decode()
        self.assertIn("изменено пользователем", text)
        self.assertIn("Плотность 80", text)

    def test_reparsed_user_text_and_original_docx_quote_are_distinct_sources(self):
        result = {"request": {"description": "Бумага А4", "quantity": 1,
                  "requirements": [{"key": "density_gsm", "label": "Плотность", "op": "eq", "value": 65,
                  "unit": "г/м²", "quote": "Плотность 65 г/м²", "original_quote": "Плотность 80 г/м²",
                  "manual_override": True, "source": {"kind": "user_edit", "document_id": "edit-newtext", "document_name": "Правка пользователя.txt",
                  "original": {"document_id": "original-docx", "document_name": "paper.docx"}}}]},
                  "offers": [], "calculation": {"status": "insufficient_data", "total": None}}
        model = document_model(result)
        changes = next(section for section in model['sections'] if section['title'] == 'Правки требований')
        self.assertIn('Текст правки («Плотность»): Плотность 65 г/м²', changes['paragraphs'])
        self.assertIn('Исходная цитата ТЗ («Плотность»): Плотность 80 г/м²', changes['paragraphs'])
        self.assertIn('Источник правки: Правка пользователя.txt', changes['paragraphs'])
        self.assertIn('Первоначальный источник: paper.docx', changes['paragraphs'])
        self.assertNotIn('Исходный текст («Плотность»): Плотность 65 г/м²', changes['paragraphs'])
        with ZipFile(BytesIO(report_docx(result))) as archive:
            document = ET.fromstring(archive.read('word/document.xml'))
        text = ' '.join(document.itertext())
        self.assertIn('Текст правки', text)
        self.assertIn('Исходная цитата ТЗ', text)

if __name__ == '__main__':
    unittest.main()
