"""Exact public captures from the file-based live run; no network or model.

The gzip fixtures contain only public supplier HTML/text and capture provenance,
not owner jobs, request specifications, credentials or the production database.
Additional synthetic markup checks reject old/hidden/related/from/variant prices.
"""
from copy import deepcopy
from decimal import Decimal
import gzip
import hashlib
import json
from pathlib import Path
import unittest

from quotelens.acquisition import _PageParser, _space
from quotelens.engine import parse_request
from quotelens.extraction import extract_product_offer
from quotelens.price_context import primary_price_contexts
from quotelens.selection import finalize, validate_candidate


FIXTURES = Path(__file__).parent / 'fixtures/live-price-scopes'
CAPTURES = {
    'delo': '08af4c57efa42da78d4c50fe', 'paneco': '19f9f6388252de4d8152ed69',
    'il2': '344edb055f329e27d095f93d', 'lider': '89bf1582b4065e7b2df360a0',
    'il7': 'd3291d98b3795716a942fcae', 'format': 'eb585bfd89ea78ccc6ccc63c',
    'kancmag': 'f054506b0ab2e2e31d403ba7', 'il21': 'fcb9004d51e072002b351b2d',
}


def capture(name):
    return json.loads(gzip.decompress((FIXTURES / (CAPTURES[name] + '.json.gz')).read_bytes()))


def mass_request(name, mass=None):
    description = name + ' рекомбинантный белок'
    request = parse_request(description)
    request['product'] = name
    request['requirements'] = [] if mass is None else [{'key': 'mass_mcg', 'label': 'Масса одной единицы', 'op': 'gte', 'value': mass, 'unit': 'мкг'}]
    request['missing_critical_fields'] = []
    return request


def qa_document(body, title='3D принтер FixtureD600', before=''):
    markup = before + '<main itemscope itemtype="https://schema.org/Product"><h1 itemprop="name">' + title + '</h1>' + body + '</main>'
    parser = _PageParser();parser.feed(markup)
    return {'url': 'https://qa.example/product/', 'title': title, 'primary_title': title,
            'html': markup, 'text': _space(' '.join(parser.parts)), 'document_id': 'qa-live-scope',
            'sha256': hashlib.sha256(markup.encode()).hexdigest(), 'retrieved_at': '2026-11-12T00:00:00Z'}


class ExactLivePriceScopeTests(unittest.TestCase):
    def test_explicit_foreign_table_product_name_never_becomes_primary_price(self):
        for tag in ['td','th']:
          for price in ['29 977', '<span class="price">29 977 RUB</span>']:
            with self.subTest(price=price,tag=tag):
                header='<tr>'+''.join('<'+tag+'>'+label+'</'+tag+'>' for label in ['Артикул','Наименование','Вес','Цена, руб.'])+'</tr>'
                document=qa_document('<table>'+header+
                    '<tr><td>ABC-12</td><td>rhIL-2 Интерлейкин-2 человека</td><td>10 мкг</td><td>'+price+'</td></tr></table>', title='rhIL-7 Интерлейкин-7 человека')
                request=mass_request('rhIL-7',10)
                offer=extract_product_offer(document,request)
                self.assertIsNone(offer['price'])
                self.assertTrue(offer['source_issues'])
                candidate={'title':document['title'],'price':29977,'currency':'RUB','attributes':{},'evidence':{'price':{'quote':'29 977'}}}
                self.assertIsNone(validate_candidate(candidate,document,request)['price'])

    def test_explicit_matching_table_product_name_can_bind_price(self):
        for tag in ['td','th']:
            with self.subTest(tag=tag):
                header='<tr>'+''.join('<'+tag+'>'+label+'</'+tag+'>' for label in ['Артикул','Наименование','Вес','Цена, руб.'])+'</tr>'
                document=qa_document('<table>'+header+'<tr><td>ABC-12</td><td>rhIL-7</td><td>10 мкг</td><td>33 573</td></tr></table>',title='rhIL-7 Интерлейкин-7 человека')
                offer=extract_product_offer(document,mass_request('rhIL-7',10))
                self.assertEqual(offer['price'],33573)
                self.assertFalse(offer.get('source_issues'))

    def test_explicit_primary_paper_catalogue_unit_is_preserved(self):
        document = qa_document('<div class="price">650 ₽</div><div>Единица измерения: пач</div><div>Количество листов в пачке: 500</div>', title='Бумага А4 65 г/м2')
        offer = extract_product_offer(document, parse_request('Бумага А4 65 г/м2 500 листов', 50))
        self.assertEqual(offer['unit'], 'пачка')
        self.assertEqual(offer['evidence']['unit']['quote'], 'Единица измерения: пач')

    def test_inventory_pack_count_does_not_set_paper_sale_unit(self):
        document = qa_document('<div class="price">650 ₽</div><div>В упаковке 5</div>', title='Бумага А4 65 г/м2')
        offer = extract_product_offer(document, parse_request('Бумага А4 65 г/м2', 50))
        self.assertIsNone(offer['unit'])

    def test_capture_provenance_is_complete(self):
        for name in CAPTURES:
            with self.subTest(name=name):
                document = capture(name)
                self.assertTrue(document['url'].startswith('https://'))
                self.assertEqual(len(document['sha256']), 64)
                self.assertTrue(document['retrieved_at'])
                self.assertIn('<', document['html'])

    def test_lider_definite_primary_quote_survives_from_banner_and_foreign_fixed_header(self):
        document = capture('lider')
        before = deepcopy(document)
        offer = extract_product_offer(document, parse_request('3D принтер CreatBot D600 PRO'))
        self.assertEqual(offer['price'], 1250000)
        self.assertEqual(offer['unit'], 'шт')
        self.assertEqual({block['amount'] for block in primary_price_contexts(document)}, {Decimal('1250000')})
        self.assertNotIn('5 549 000', offer['evidence']['price']['quote'])
        self.assertEqual(document, before)

    def test_sibling_h1_and_product_purchase_root_bind_3d_format_price(self):
        offer = extract_product_offer(capture('format'), parse_request('3D принтер CreatBot D600 PRO'))
        self.assertEqual(offer['price'], 1100000)
        self.assertIsNone(offer['unit'])

    def test_schema_bound_unknown_paper_survives_earlier_navigation_cut(self):
        document = capture('delo')
        offer = extract_product_offer(document, parse_request('Бумага А4 65 г/м2 500листов в пачке', 50))
        self.assertEqual(offer['price'], 579.01)
        self.assertEqual(offer['attributes']['format'], 'A4')
        self.assertEqual(offer['attributes']['density_gsm'], 65)
        self.assertEqual(offer['attributes']['sheets_per_pack'], 500)
        self.assertNotIn('model', offer['attributes'])

    def test_current_a3_price_and_cyrillic_format_have_unmodified_quote(self):
        offer = extract_product_offer(capture('kancmag'), parse_request('Бумага А3 80г/м2 500листов в пачке', 25))
        self.assertEqual(offer['price'], 895.32)
        self.assertEqual(offer['attributes']['format'], 'A3')
        self.assertRegex(offer['evidence']['attributes.format']['quote'], '[АA]3')
        self.assertIn(offer['evidence']['attributes.format']['quote'], capture('kancmag')['text'])
        self.assertNotEqual(offer['price'], 1061.12)
        self.assertIsNone(offer['unit'])

    def test_paneco_current_price_is_detected_but_10_50_mass_conflict_remains_explicit(self):
        document = capture('paneco')
        self.assertEqual({block['amount'] for block in primary_price_contexts(document)}, {Decimal('19035.00')})
        offer = extract_product_offer(document, mass_request('rhTGFbeta1', 10))
        self.assertIsNone(offer['price'])
        self.assertTrue(offer['source_issues'])
        self.assertIn('10 мкг', offer['validation']['price_reason'])
        self.assertIn('50мкг', offer['validation']['price_reason'])

    def test_visible_table_price_uses_currency_header_but_never_turns_10_mcg_into_100(self):
        for name, product, expected_price in [('il2', 'rhIL-2', 29977), ('il21', 'rhIL-21', 32914)]:
            with self.subTest(name=name):
                request = mass_request(product, 100)
                offer = extract_product_offer(capture(name), request)
                self.assertEqual(offer['price'], expected_price)
                self.assertEqual(offer['currency'], 'RUB')
                self.assertNotIn('=', offer['evidence']['price']['quote'])
                self.assertIn('руб', offer['evidence']['currency']['quote'])
                self.assertEqual(offer['price_variant']['mass_mcg'], 10)
                self.assertEqual(offer['relevance']['verdict'], 'mismatch')
                self.assertIsNone(offer['unit'])
                result = finalize(request, [offer], {'min_suppliers': 1})
                self.assertEqual(result['offers'][0]['status'], 'excluded')
                self.assertIsNone(result['calculation']['provisional']['total'])
                self.assertTrue(result['offers'][0]['reasons'][0].startswith('Сравнение фасовки по источнику'))

    def test_multirow_table_requires_an_exact_pack_target_without_cheapest_price_guess(self):
        document = capture('il7')
        small = extract_product_offer(document, mass_request('rhIL-7', 10))
        large = extract_product_offer(document, mass_request('rhIL-7', 50))
        self.assertEqual(small['price'], 33573)
        self.assertEqual(small['price_variant']['mass_mcg'], 10)
        self.assertEqual(large['price'], 116571)
        self.assertEqual(large['price_variant']['mass_mcg'], 50)
        ambiguous = extract_product_offer(document, mass_request('rhIL-7'))
        self.assertIsNone(ambiguous['price'])
        self.assertIn('фасовок', ambiguous['validation']['price_reason'])

    def test_model_match_cannot_overwrite_source_variant_mismatch(self):
        document = capture('il2')
        candidate = {'title': document['primary_title'], 'price': 29977, 'currency': 'RUB',
                     'evidence': {'price': {'quote': '29 977'}, 'currency': {'quote': 'Цена, руб.'}},
                     'relevance': {'verdict': 'match', 'reason': 'Неподтверждённое предположение модели'}}
        offer = validate_candidate(candidate, document, mass_request('rhIL-2', 100))
        self.assertEqual(offer['price'], 29977)
        self.assertEqual(offer['relevance']['verdict'], 'mismatch')
        self.assertEqual(offer['relevance']['basis'], 'source_variant_comparison')
        self.assertEqual(offer['price_variant']['mass_mcg'], 10)

    def test_model_short_quote_cannot_choose_an_unspecified_pack_from_multirow_table(self):
        document = capture('il7')
        candidate = {'title': document['primary_title'], 'price': 33573,
                     'evidence': {'price': {'quote': '33 573'}}}
        offer = validate_candidate(candidate, document, mass_request('rhIL-7'))
        self.assertIsNone(offer['price'])
        self.assertIn('фасовок', offer['validation']['price_reason'])


class GenericScopeGuardTests(unittest.TestCase):
    def test_font_weight_bold_is_not_an_old_price_class(self):
        document = qa_document('<div class="price-block"><span class="fn-price font-weight-bold">19 035 ₽</span><span class="old_price">0 ₽</span></div>')
        self.assertEqual(extract_product_offer(document, parse_request('3D принтер FixtureD600'))['price'], 19035)

    def test_from_old_hidden_and_recommended_only_prices_are_not_accepted(self):
        for markup in ['<div class="price">от 1 200 ₽</div>', '<div class="price-old">1 200 ₽</div>',
                       '<div class="price hide">1 200 ₽</div>', '<div class="product-card"><span class="price">1 200 ₽</span></div>']:
            with self.subTest(markup=markup):
                offer = extract_product_offer(qa_document(markup), parse_request('3D принтер FixtureD600'))
                self.assertTrue(offer is None or offer['price'] is None)

    def test_same_amount_from_banner_does_not_veto_independently_bound_definite_price(self):
        title = '3D принтер FixtureD600'
        before = '<header>' + title + ' от 1 200 ₽ Сопутствующие товары</header>'
        document = qa_document('<div class="price-block"><span class="price-current">1 200 ₽</span> за 1 шт.</div>', before=before)
        offer = extract_product_offer(document, parse_request(title))
        self.assertEqual(offer['price'], 1200)
        self.assertEqual(offer['unit'], 'шт')

    def test_foreign_local_title_cannot_supply_a_price_through_flat_text_fallback(self):
        document = qa_document('<div class="price-block">1 200 ₽ за 1 шт.</div>'
                               '<div><div class="title-block">Другой прибор Other200</div><div class="price-block"><span class="price-current">9 900 ₽</span></div></div>')
        request = parse_request('3D принтер FixtureD600')
        self.assertEqual(extract_product_offer(document, request)['price'], 1200)
        candidate = {'title': document['title'], 'price': 9900, 'evidence': {'price': {'quote': '9 900 ₽'}}}
        self.assertIsNone(validate_candidate(candidate, document, request)['price'])

    def test_hidden_currency_value_is_not_a_visible_bare_table_price(self):
        document = qa_document('<div class="pay_block"><table><tr><td colspan="2">Артикул</td><td>Цена, руб.</td></tr>'
                               '<tr><td>SKU-10</td><td>10 мкг</td><td><span style="display:none">29 977,= RUB</span><span>По запросу</span></td></tr></table></div>')
        offer = extract_product_offer(document, mass_request('FixtureD600', 10))
        self.assertIsNone(offer['price'])

    def test_equal_prices_do_not_hide_different_table_pack_sizes(self):
        document = qa_document('<div class="pay_block"><table><tr><td colspan="2">Артикул</td><td>Цена, руб.</td></tr>'
                               '<tr><td>SKU-10</td><td>10 мкг</td><td>10 000</td></tr>'
                               '<tr><td>SKU-50</td><td>50 мкг</td><td>10 000</td></tr></table></div>')
        ambiguous = extract_product_offer(document, mass_request('FixtureD600'))
        self.assertIsNone(ambiguous['price'])
        exact = extract_product_offer(document, mass_request('FixtureD600', 50))
        self.assertEqual(exact['price'], 10000)
        self.assertEqual(exact['price_variant']['mass_mcg'], 50)

    def test_body_colspan_cannot_shift_a_number_under_the_wrong_currency_header(self):
        document = qa_document('<div class="pay_block"><table><tr><td>Артикул</td><td>Цена, руб.</td><td>Примечание</td></tr>'
                               '<tr><td colspan="2">SKU-10, 10 мкг</td><td>99 000</td></tr></table></div>')
        self.assertIsNone(extract_product_offer(document, mass_request('FixtureD600', 10))['price'])

    def test_warranty_terms_remain_readable_without_accepting_their_addon_price(self):
        document = qa_document('<div class="product-top"><div class="price-block"><span class="price-current">1 200 ₽</span></div>'
                               '<div class="warranty">Гарантия производителя 12 месяцев. Дополнительная гарантия <span class="price">900 ₽</span></div></div>')
        request = parse_request('3D принтер FixtureD600')
        request['requirements'] = [{'key': 'warranty_months', 'label': 'Гарантия', 'op': 'gte', 'value': 12}]
        candidate = {'title': document['title'], 'attributes': {'warranty_months': 12},
                     'evidence': {'attributes.warranty_months': {'quote': 'Гарантия производителя 12 месяцев'}}}
        self.assertEqual(validate_candidate(candidate, document, request)['attributes']['warranty_months'], 12)
        self.assertEqual(extract_product_offer(document, request)['price'], 1200)


if __name__ == '__main__':
    unittest.main()
