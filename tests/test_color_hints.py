"""Keep categorical color, measured CIE and the user's decision distinct."""
from copy import deepcopy
import json
import unittest

from quotelens.attribute_hints import contextual_attribute_hints
from quotelens.extraction import extract_product_offer
from quotelens.selection import validate_candidate
from quotelens.sharing import sanitize_snapshot, reader_html
from quotelens.reports import document_model
from tests.test_paper_attribute_recovery import capture, document
from tests.test_parsing_recovery import paper_request


class PaperColorTests(unittest.TestCase):
    def test_explicit_primary_title_color_is_revalidated_source_fact(self):
        source = document('<p class="price">100 руб.</p>', root='main class="product-detail"', title='Бумага Fixture A3 белая')
        before = deepcopy(source)
        offer = extract_product_offer(source, paper_request())
        self.assertEqual(offer['attributes']['color'], 'белый')
        proof = offer['evidence']['attributes.color']
        self.assertEqual(proof['quote'], source['primary_title'])
        self.assertEqual(proof['basis'], 'primary_product_title_attribute')
        forged = deepcopy(offer); forged['attributes']['color'] = 'красный'
        self.assertNotIn('color', validate_candidate(forged, source, paper_request())['attributes'])
        self.assertEqual(source, before)

    def test_cie_only_is_a_qualified_hint_and_does_not_confirm_color(self):
        source = capture('ofisshop'); before = deepcopy(source)
        offer = extract_product_offer(source, paper_request())
        self.assertNotIn('color', offer['attributes'])
        self.assertNotIn('attributes.color', offer['evidence'])
        hint = offer['attribute_hints']['color'][0]
        self.assertTrue(hint['inferred']); self.assertFalse(hint['confirmed_for_product'])
        self.assertEqual(hint['related_field'], 'attributes.whiteness_cie')
        self.assertEqual(hint['quote'], offer['evidence']['attributes.whiteness_cie']['quote'])
        self.assertEqual(offer['attributes']['whiteness_cie'], [143, 149])
        self.assertEqual(source, before)
        # The requested color is not used to invent the suggestion.
        changed = paper_request(); changed['requirements'][5]['value'] = 'красный'
        self.assertEqual(extract_product_offer(source, changed)['attribute_hints']['color'][0]['value'], 'белый')

    def test_colored_negated_or_unproven_cie_never_suggests_white(self):
        proof = {'quote':'Белизна CIE 146', 'url':'https://supplier.example/item', 'document_id':'synthetic-source', 'source_hash':'a'*64}
        for title in ('Красная бумага A3', 'Бумага A3 цветная', 'Бумага не белая', 'Red paper'):
            with self.subTest(title=title):
                source = document('<p>Белизна CIE 146</p>', root='main class="product-detail"', title=title)
                hints = contextual_attribute_hints(source, paper_request(), {'whiteness_cie':146}, {'attributes.whiteness_cie':proof})
                self.assertNotIn('color', hints)
                offer = extract_product_offer(source, paper_request())
                self.assertNotEqual((offer or {}).get('attributes', {}).get('color'), 'белый')
        source = capture('ofisshop')
        self.assertNotIn('color', contextual_attribute_hints(source, paper_request(), {'whiteness_cie':146}, {}))
        self.assertNotIn('color', contextual_attribute_hints(source, paper_request(), {'color':'синий','whiteness_cie':146}, {'attributes.whiteness_cie':proof}))

    def test_breadcrumb_has_priority_over_cie_inference(self):
        offer = extract_product_offer(capture('kancmag'), paper_request())
        hint = offer['attribute_hints']['color'][0]
        self.assertFalse(hint.get('inferred', False))
        self.assertIn('белая', hint['quote'].lower())

    def test_shared_snapshot_and_report_keep_acceptance_basis(self):
        offer = extract_product_offer(capture('ofisshop'), paper_request())
        hint = offer['attribute_hints']['color'][0]
        offer['attributes']['color'] = 'белый'
        offer['manual_confirmations'] = {'attributes.color': {'kind':'manual', 'origin':'accepted_suggestion', 'value':'белый', 'original_value':None,
            'reason':'Показанное значение подтверждено пользователем', 'revision':3, 'at':'2026-11-13T19:00:00Z', 'supporting_source':hint}}
        result = {'request':paper_request(), 'offers':[offer], 'calculation':{}, 'search':{}}
        snapshot, docs = sanitize_snapshot(result)
        self.assertTrue(snapshot['offers'][0]['attribute_hints']['color'][0]['inferred'])
        self.assertEqual(snapshot['offers'][0]['attribute_hints']['color'][0]['related_field'], 'attributes.whiteness_cie')
        page = reader_html(snapshot, docs)
        report = json.dumps(document_model(result), ensure_ascii=False)
        for text in (page, report):
            self.assertIn('предположение по белизне CIE'.lower(), text.lower())
            self.assertIn(hint['quote'], text)
            self.assertIn(hint['url'], text)
        self.assertNotIn('attributes.color', snapshot['offers'][0]['evidence'])

    def test_legacy_shared_manual_edit_keeps_original_value_and_reason(self):
        offer = extract_product_offer(capture('ofisshop'), paper_request())
        offer['price'] = 125
        offer['manual_confirmations'] = {'price': {'kind':'manual', 'original_value':100, 'value':125,
            'reason':'Уточнение по коммерческому предложению <QA>', 'revision':1, 'at':'2026-11-13T19:00:00Z'}}
        snapshot, docs = sanitize_snapshot({'request':paper_request(), 'offers':[offer]})
        before = deepcopy(snapshot)
        page = reader_html(snapshot, docs)
        self.assertIn('100 → 125', page)
        self.assertIn('Уточнение по коммерческому предложению &lt;QA&gt;', page)
        self.assertIn('Введено пользователем', page)
        self.assertEqual(snapshot, before)


if __name__ == '__main__': unittest.main()
