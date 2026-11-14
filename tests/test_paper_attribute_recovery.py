"""Immutable public supplier captures and synthetic boundary requests.

The gzip files preserve the complete captured HTML/text and provenance. They
contain no owner request or job; all requested targets below are synthetic.
"""
from copy import deepcopy
from decimal import Decimal
import gzip
import hashlib
import json
from pathlib import Path
import unittest

from quotelens.acquisition import _PageParser, _space
from quotelens.price_context import (primary_attribute_rows, primary_price_contexts,
                                     primary_requirement_observations)


FIXTURES = Path(__file__).parent / 'fixtures/primary-product-pages'


def capture(name):
    return json.loads(gzip.decompress((FIXTURES / (name + '.json.gz')).read_bytes()))


def request():
    fields = [('maker', 'Производитель', None, 'Synthetic maker'),
              ('brand', 'Бренд', None, 'Synthetic brand'),
              ('country', 'Страна происхождения', None, 'Synthetic country'),
              ('format', 'Формат бумаги', None, 'A6'),
              ('grade', 'Марка бумаги', None, 'A'),
              ('sheets', 'Количество листов в пачке', 'листов', -1),
              ('color', 'Цвет', None, 'Synthetic color'),
              ('cie', 'Белизна CIE', '%', -1),
              ('density', 'Масса листа бумаги площадью 1 м2', 'г/м²', -1),
              ('thickness', 'Толщина', 'мкм', -1)]
    return {'category': 'paper', 'requirements': [
        {'key': key, 'label': label, 'unit': unit, 'value': value, 'op': 'eq'}
        for key, label, unit, value in fields]}


def document(body, *, root='main', visible=None, title='Бумага Fixture A3'):
    markup = '<' + root + '><h1>' + title + '</h1>' + body + '</' + root.split()[0] + '>'
    parser = _PageParser(); parser.feed(markup)
    return {'url': 'https://supplier.example/item', 'title': title, 'primary_title': title,
            'html': markup, 'text': visible if visible is not None else _space(' '.join(parser.parts)),
            'document_id': 'synthetic-source', 'sha256': hashlib.sha256(markup.encode()).hexdigest(),
            'retrieved_at': '2026-11-13T00:00:00Z'}


class PublicCaptureRecoveryTests(unittest.TestCase):
    def test_six_exact_primary_prices_exclude_recommendations_and_order_thresholds(self):
        prices = {'offmar': '740', 'ofisshop': '791', 'liner': '868',
                  'office': '1061', 'diapazon': '808.78', 'kancmag': '895.32'}
        for name, expected in prices.items():
            with self.subTest(source=name):
                source = capture(name); before = deepcopy(source)
                blocks = primary_price_contexts(source)
                self.assertEqual({block['amount'] for block in blocks}, {Decimal(expected)})
                for block in blocks:
                    self.assertIn(block['quote'], _space(source['text']))
                    self.assertIn(block['context'], _space(source['text']))
                self.assertEqual(source, before)
        office = primary_price_contexts(capture('office'))
        self.assertTrue(all('от 5 шт' in block['context'] for block in office))
        self.assertTrue(all('наценка: 80₽' in block['context'] for block in office))

    def test_ofisshop_structured_widgets_preserve_source_tolerances_and_label_units(self):
        source = capture('ofisshop'); facts = primary_requirement_observations(source, request())
        expected = {'format': 'A3', 'grade': 'C', 'sheets': 500,
                    'cie': [143, 149], 'density': [77, 83], 'thickness': [102, 106]}
        self.assertEqual({key: facts[key]['value'] for key in expected}, expected)
        self.assertEqual(facts['cie']['raw_value'], '146 +/- 3')
        self.assertEqual(facts['cie']['nominal'], 146)
        self.assertEqual(facts['cie']['tolerance'], 3)
        self.assertEqual(facts['cie']['source_unit'], 'CIE')
        self.assertEqual(facts['density']['source_unit'], 'г/м²')
        self.assertEqual(facts['density']['unit_basis'], 'row_label_and_value')
        self.assertEqual(facts['thickness']['source_unit'], 'мкм')
        self.assertEqual(facts['thickness']['unit_basis'], 'row_label')
        for fact in facts.values():
            self.assertIn(fact['quote'], _space(source['text']))
        self.assertNotIn('maker', facts)
        self.assertNotIn('color', facts)

    def test_complex_source_ranges_keep_outer_envelope_and_unknown_units(self):
        facts = primary_requirement_observations(capture('diapazon'), request())
        self.assertEqual(facts['maker']['value'], 'Sylvamo')
        self.assertEqual(facts['density']['value'], [69, 83])
        self.assertEqual(facts['density']['raw_value'], '72-80 +/- 2-3')
        self.assertEqual(facts['density']['nominal'], [72, 80])
        self.assertEqual(facts['density']['tolerance'], [2, 3])
        self.assertEqual(facts['density']['source_ambiguity'], 'range_with_tolerance')
        self.assertEqual(facts['density']['unit_reason'], 'source_unit_not_stated')
        self.assertIsNone(facts['density']['normalized_unit'])
        self.assertEqual(facts['cie']['value'], [137, 153])
        self.assertEqual(facts['thickness']['value'], [102, 106])
        self.assertEqual(facts['thickness']['raw_value'], '104 +/- 2')

    def test_declared_supplier_technical_tab_uses_distinct_html_evidence(self):
        source = capture('kancmag'); before = deepcopy(source)
        facts = primary_requirement_observations(source, request())
        self.assertEqual(facts['country']['value'], 'РОССИЯ')
        self.assertEqual(facts['cie']['value'], [137, 153])
        self.assertEqual(facts['density']['value'], [69, 83])
        self.assertEqual(facts['thickness']['value'], [102, 106])
        self.assertNotIn('maker', facts)
        self.assertEqual(facts['brand']['value'], 'Светогорский ЦБК')
        for key in ['country', 'cie', 'density', 'thickness']:
            fact = facts[key]
            self.assertNotIn(fact['quote'], _space(source['text']))
            self.assertEqual(fact['basis'], 'primary_product_dom_attribute')
            self.assertEqual(fact['visibility'], 'reachable_tab')
            self.assertEqual(fact['tab_id'], 'tab-features')
            self.assertEqual(fact['tab_label'], 'Характеристики')
            self.assertTrue(fact['locator'])
        self.assertEqual(source, before)

    def test_compound_area_density_is_not_mass_or_unit_exponent_data(self):
        facts = primary_requirement_observations(capture('liner'), request())
        self.assertEqual(facts['density']['value'], 80)
        self.assertEqual(facts['density']['raw_value'], '80 г/кв.м')
        self.assertEqual(facts['density']['source_unit'], 'г/м²')
        self.assertEqual(facts['sheets']['value'], 500)
        self.assertEqual(facts['grade']['value'], 'C')
        self.assertEqual(primary_requirement_observations(capture('offmar'), request())['maker']['value'],
                         'Светогорск, Сильвамо Корпорейшн Рус')

    def test_offmar_explicit_primary_title_facts_recover_grade_and_sheet_count(self):
        source = capture('offmar'); before = deepcopy(source)
        facts = primary_requirement_observations(source, request())
        self.assertEqual(facts['format']['value'], 'A3')
        self.assertEqual(facts['grade']['value'], 'C')
        self.assertEqual(facts['grade']['quote'], 'класс С')
        self.assertEqual(facts['sheets']['value'], 500)
        self.assertEqual(facts['sheets']['quote'], '500 л.')
        self.assertEqual(facts['sheets']['raw_label'], 'л.')
        self.assertEqual(facts['sheets']['source_unit'], 'листов')
        self.assertEqual(facts['sheets']['unit_basis'], 'paper_title_sheet_abbreviation')
        self.assertNotIn('cie', facts)  # The bare 146% has no stated scale.
        for key in ['format', 'grade', 'sheets']:
            self.assertEqual(facts[key]['basis'], 'primary_product_title_attribute')
            self.assertIn(facts[key]['quote'], _space(source['text']))
        self.assertEqual(source, before)


class AttributeBoundaryTests(unittest.TestCase):
    def test_title_fallback_never_overrules_conflicting_or_unusable_primary_rows(self):
        title = 'Бумага А3, класс С, 500 л.'
        source = document('<table><tr><td>Марка бумаги</td><td>A</td></tr>'
                          '<tr><td>Марка бумаги</td><td>B</td></tr></table>', title=title)
        self.assertNotIn('grade', primary_requirement_observations(source, request()))
        source = document('<table><tr><td>Марка бумаги</td><td>A / B</td></tr></table>', title=title)
        self.assertEqual(primary_requirement_observations(source, request())['grade']['value'], 'A / B')
        source = document('<table><tr><td>Количество листов</td><td>500 кг</td></tr></table>', title=title)
        self.assertNotIn('sheets', primary_requirement_observations(source, request()))
        source = document('<table><tr><td>Марка</td><td>B</td></tr></table>', title=title)
        self.assertEqual(primary_requirement_observations(source, request())['grade']['value'], 'B')

    def test_title_fallback_rejects_ambiguous_grade_count_and_nonpaper_liters(self):
        for title in ['Бумага А3, класс С или B, 490–510 л.',
                      'Бумага А3, класс С/B, 500 л. ± 2%',
                      'Жидкость А3, класс С, 500 л.',
                      'Бумага SC500л']:
            facts = primary_requirement_observations(document('', title=title), request())
            self.assertNotIn('grade', facts)
            self.assertNotIn('sheets', facts)

    def test_brand_and_manufacturer_remain_different_concepts(self):
        source = document('<div class="property"><span class="property-name">Бренд</span>'
                          '<span class="property-value">Fixture Brand</span></div>')
        facts = primary_requirement_observations(source, request())
        self.assertEqual(facts['brand']['value'], 'Fixture Brand')
        self.assertNotIn('maker', facts)

    def test_scalar_range_and_relative_tolerance_never_copy_requested_values(self):
        source = document('<table><tr><td>Белизна CIE (%)</td><td>140–150 ± 3</td></tr>'
                          '<tr><td>Толщина, мкм</td><td>104 +/- 2</td></tr>'
                          '<tr><td>Плотность (г/м2)</td><td>77–83</td></tr>'
                          '<tr><td>Количество листов в пачке</td><td>500 (±2%)</td></tr></table>')
        target = request(); facts = primary_requirement_observations(source, target)
        self.assertEqual(facts['cie']['value'], [137, 153])
        self.assertEqual(facts['thickness']['value'], [102, 106])
        self.assertEqual(facts['density']['value'], [77, 83])
        self.assertEqual(facts['sheets']['value'], [490, 510])
        for requirement in target['requirements']:
            if isinstance(requirement['value'], (int, float)):
                requirement['value'] = 999999
        self.assertEqual(facts, primary_requirement_observations(source, target))

    def test_label_units_convert_bounds_and_reject_explicit_wrong_units(self):
        source = document('<dl><dt>Толщина, мм</dt><dd>0.102–0.106</dd></dl>')
        self.assertEqual(primary_requirement_observations(source, request())['thickness']['value'], [102, 106])
        source = document('<dl><dt>Плотность</dt><dd>80 г</dd></dl>')
        self.assertNotIn('density', primary_requirement_observations(source, request()))

    def test_primary_generic_roots_keep_long_specs_but_reject_other_products(self):
        source = document('<p>' + 'Описание товара. ' * 200 + '</p>'
                          '<div class="price">123 ₽ за шт.</div>'
                          '<div class="related-products"><div class="price">12 ₽</div>'
                          '<div>Толщина: 900 мкм</div></div>'
                          '<div class="char-list"><li class="char-list__item">'
                          '<span class="char-list__prop">Толщина, мкм</span>'
                          '<span class="char-list__value">104 +/- 2</span></li></div>',
                          root='div class="product type-product"')
        self.assertEqual({block['amount'] for block in primary_price_contexts(source)}, {Decimal('123')})
        self.assertEqual(primary_requirement_observations(source, request())['thickness']['value'], [102, 106])

    def test_only_primary_declared_technical_tabs_can_supply_html_only_rows(self):
        row = '<table><tr><td>Толщина, мкм</td><td>104 +/- 2</td></tr></table>'
        for control, panel in [
            ('', '<div class="tab-pane" id="specs">' + row + '</div>'),
            ('<a href="#specs">Отзывы</a>', '<div class="tab-pane" id="specs">' + row + '</div>'),
            ('<div class="related"><a href="#specs">Характеристики</a></div>', '<div class="tab-pane" id="specs">' + row + '</div>'),
            ('<a href="#specs">Характеристики</a>', '<div class="tab-pane" id="other">' + row + '</div>'),
            ('<a href="#specs">Характеристики</a>', '<div class="tab-pane" id="specs"><div hidden>' + row + '</div></div>'),
            ('<a href="#specs">Характеристики</a>', '<div class="related"><div class="tab-pane" id="specs">' + row + '</div></div>'),
        ]:
            with self.subTest(control=control, panel=panel):
                source = document(control + panel, visible='Бумага Fixture A3')
                self.assertEqual(primary_attribute_rows(source), [])
        source = document('<button role="tab" aria-controls="specs">Характеристики</button>'
                          '<div role="tabpanel" id="specs">' + row + '</div>', visible='Бумага Fixture A3')
        fact = primary_requirement_observations(source, request())['thickness']
        self.assertEqual(fact['value'], [102, 106])
        self.assertEqual(fact['visibility'], 'reachable_tab')

    def test_tab_access_never_makes_hidden_or_unrendered_prices_visible(self):
        for price in ['<div class="price">999 ₽</div>', '<div hidden class="price">999 ₽</div>']:
            source = document('<a href="#specs">Характеристики</a>'
                              '<div class="tab-pane" id="specs">' + price + '</div>',
                              visible='Бумага Fixture A3')
            self.assertEqual(primary_price_contexts(source), [])

    def test_separate_product_cards_cannot_supply_a_price_even_with_same_title(self):
        for card in ['<li class="product type-product">', '<div class="product_block">']:
            closing = '</li>' if card.startswith('<li') else '</div>'
            source = document('<div class="price">123 ₽</div>' + card +
                              '<h2>Бумага Fixture A3</h2><div class="price">12 ₽</div>' + closing,
                              root='div class="product type-product"')
            self.assertEqual({block['amount'] for block in primary_price_contexts(source)}, {Decimal('123')})

    def test_distant_foreign_price_owner_cannot_be_dropped_with_a_long_parent(self):
        for owner in ['Цена для Other-200 ', 'Price for <strong>Other-200</strong> ']:
            source = document('<div>' + owner + 'х ' * 360 +
                              '<span class="price">1 200 ₽</span></div>', title='Бумага Scope-100')
            self.assertEqual(primary_price_contexts(source), [])
        primary = document('<div>Цена для Scope-100 ' + 'х ' * 360 +
                           '<span class="price">1 200 ₽</span></div>', title='Бумага Scope-100')
        self.assertEqual({block['amount'] for block in primary_price_contexts(primary)}, {Decimal('1200')})

    def test_separate_foreign_paragraph_or_menu_cannot_veto_bound_primary_price(self):
        for other in ['<p>Цена для Other-200 ' + 'х ' * 360 + '</p>',
                      '<header>Price for Other-200 ' + 'х ' * 360 + '</header>']:
            source = document(other + '<div class="product-sidebar"><span class="price">1 200 ₽</span></div>',
                              title='Бумага Scope-100')
            self.assertEqual({block['amount'] for block in primary_price_contexts(source)}, {Decimal('1200')})


if __name__ == '__main__':
    unittest.main()
