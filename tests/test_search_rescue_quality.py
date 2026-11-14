"""Synthetic eligibility and atomic fact-join regressions; no network or files."""
from copy import deepcopy
import hashlib
import json
import unittest

from quotelens.acquisition import _PageParser, _space
from quotelens.engine import _attribute_match
from quotelens.extraction import extract_product_offer
from quotelens.selection import validate_candidate, quality_requirement_observations, _value_backed, primary_cms_financial_facts
from quotelens.source_documents import classify_page


TITLE = "3D принтер MakerQA D600 Pro"


def capture(title=TITLE, body="", *, url="https://seller.example/product/qa/", schema=None):
    raw = '<main class="main-product"><h1>' + title + '</h1>' + body + '</main>'
    if schema:
        raw += '<script type="application/ld+json">' + json.dumps(schema) + '</script>'
    parser = _PageParser()
    parser.feed(raw)
    return {"title": title, "primary_title": title, "url": url, "html": raw,
            "text": _space(" ".join(parser.parts)), "sha256": hashlib.sha256(raw.encode()).hexdigest(),
            "document_id": hashlib.sha256((url + raw).encode()).hexdigest()[:24],
            "retrieved_at": "2026-01-01T00:00:00Z"}


def request(requirements=None):
    return {"description": TITLE, "product": TITLE, "category": "other", "quantity": 1,
            "unit": "шт", "requirements": requirements or [], "conditions": []}


BUY = '<div class="product-top"><span class="price">100 000 ₽ за 1 шт. с НДС</span></div>'
REQUIREMENTS = [{"key": "manufacturer", "label": "Производитель", "op": "eq", "value": "MakerQA"},
                {"key": "power", "label": "Мощность", "op": "gte", "value": 1000, "unit": "Вт"}]


class ArticleEligibilityTests(unittest.TestCase):
    def test_editorial_model_token_does_not_create_an_unpriced_offer(self):
        doc = capture("Знакомимся с фотополимером QA Standard Resin 14k",
                      '<article><p>Обзор материала для печати.</p><a href="/product/d600-pro/">MakerQA D600 Pro</a></article>',
                      url="https://seller.example/blog/reviews/resin-14k/")
        before = deepcopy(doc)
        shape = classify_page(doc)
        self.assertEqual(shape["kind"], "article")
        self.assertTrue(shape["product_links"])
        self.assertIsNone(extract_product_offer(doc, request()))
        self.assertIsNone(validate_candidate({"title": doc["title"], "price": None}, doc, request()))
        self.assertEqual(doc, before)

    def test_article_schema_without_editorial_words_remains_discovery(self):
        doc = capture(TITLE, '<p>Описание испытания.</p>', schema={"@type": "BlogPosting", "headline": TITLE})
        self.assertEqual(classify_page(doc)["kind"], "article")

    def test_real_purchase_block_is_not_rejected_for_an_incidental_review_word(self):
        doc = capture("3D принтер MakerQA Review D600 Pro", BUY)
        self.assertEqual(classify_page(doc)["kind"], "product")
        self.assertEqual(extract_product_offer(doc, request())["price"], 100000)


class PrimaryFinancialMetadataTests(unittest.TestCase):
    def document(self, **changes):
        obj = {'PRODUCT_ID':'7','NAME':TITLE,'DETAIL_PAGE_URL':'/product/qa/','CATALOG_MEASURE_NAME':'шт',
               'MIN_PRICE':{'VALUE':'100000','VALUE_VAT':'100000','VALUE_NOVAT':'83333','CURRENCY':'RUB','CAN_BUY':'Y'}}
        obj.update(changes)
        return capture(body='<div class="product-top"><span class="price">100 000 ₽</span></div><script>setViewedProduct(7, '+repr(obj)+');</script>')

    def test_literal_unit_and_vat_have_explicit_metadata_provenance(self):
        doc = self.document();offer = extract_product_offer(doc, request())
        self.assertEqual((offer['price'],offer['unit'],offer['tax']), (100000,'шт','included'))
        for field in ('unit','tax'):
            proof = offer['evidence'][field]
            self.assertEqual(proof['visibility'],'embedded_product_data')
            self.assertEqual(proof['source_hash'],doc['sha256'])
            self.assertIn(proof['quote'],doc['html'])

    def test_missing_or_foreign_primary_url_and_name_are_rejected(self):
        for changes in ({'DETAIL_PAGE_URL':''},{'DETAIL_PAGE_URL':'/other/'},{'NAME':'OtherBrand D600'}):
            self.assertEqual(primary_cms_financial_facts(self.document(**changes)),{})

    def test_conflicting_visible_price_does_not_borrow_metadata_tax_or_unit(self):
        doc = self.document(MIN_PRICE={'VALUE':'200000','VALUE_VAT':'200000','VALUE_NOVAT':'160000','CURRENCY':'RUB'})
        offer = extract_product_offer(doc, request())
        self.assertEqual(offer['price'],100000)
        self.assertIsNone(offer['unit']);self.assertEqual(offer['tax'],'unknown')

    def test_arbitrary_javascript_is_not_evaluated(self):
        doc = self.document();doc['html']=doc['html'].replace("'VALUE': '100000'", "'VALUE': arbitrary_function()")
        self.assertEqual(primary_cms_financial_facts(doc),{})

    def test_same_amount_different_visible_currency_refuses_all_cms_supplements(self):
        doc = self.document();doc['html'] = doc['html'].replace('100 000 ₽</span>', '100 000 USD</span>')
        parser = _PageParser();parser.feed(doc['html']);doc['text'] = _space(' '.join(parser.parts))
        offer = extract_product_offer(doc, request())
        self.assertEqual((offer['price'],offer['currency']), (100000,'USD'))
        self.assertIsNone(offer['unit']);self.assertEqual(offer['tax'],'unknown')
        self.assertTrue(offer.get('source_issues'))

    def test_duplicate_primary_literals_with_unit_or_tax_disagreement_are_unknown(self):
        for changes in ({'CATALOG_MEASURE_NAME':'пачка'}, {'MIN_PRICE':{'VALUE':'100000','VALUE_VAT':'120000','VALUE_NOVAT':'100000','CURRENCY':'RUB'}}):
            doc = self.document();other = self.document(**changes)
            doc['html'] += other['html'][other['html'].index('<script>'):]
            self.assertEqual(primary_cms_financial_facts(doc), {})
            offer = extract_product_offer(doc, request())
            self.assertEqual(offer['price'],100000)
            self.assertIsNone(offer['unit']);self.assertEqual(offer['tax'],'unknown')


class AtomicFactJoinTests(unittest.TestCase):
    def test_material_order_or_partial_capability_list_is_not_a_hard_source_conflict(self):
        req = [{"key": "materials", "label": "Поддерживаемые пластики", "op": "eq", "value": "ABS, PLA"}]
        primary = capture(body=BUY + '<table><tr><td>Поддерживаемые пластики</td><td>ABS, PLA</td></tr></table>')
        for value in ('PLA, ABS', 'ABS, PLA, PETG', 'ABS'):
            fact = capture(body='<table><tr><td>Поддерживаемые пластики</td><td>' + value + '</td></tr></table>',
                           url='https://manufacturer.example/d600-pro/spec/')
            offer = extract_product_offer(primary, request(req), fact_documents=[fact])
            self.assertFalse(offer.get('source_issues'))
            self.assertEqual(offer['attributes']['materials'], 'ABS, PLA')

    def test_shared_professional_epithet_does_not_join_different_brands(self):
        primary = capture('3D принтер профессиональный MakerQA D600 Pro', BUY)
        fact = capture('3D принтер профессиональный OtherBrand D600 Pro',
                       '<table><tr><td>Мощность</td><td>1200 Вт</td></tr></table>',
                       url='https://manufacturer.example/other/')
        offer = extract_product_offer(primary, request(REQUIREMENTS), fact_documents=[fact])
        self.assertNotIn('power', offer['attributes'])
        primary = capture('3D принтер MakerQA профессиональный D600 Pro', BUY)
        fact = capture('3D принтер OtherBrand профессиональный D600 Pro',
                       '<table><tr><td>Мощность</td><td>1200 Вт</td></tr></table>',
                       url='https://manufacturer.example/other-ambiguous/')
        offer = extract_product_offer(primary, request(REQUIREMENTS), fact_documents=[fact])
        self.assertNotIn('power', offer['attributes'])

    def test_explicit_negative_secondary_claim_remains_a_source_conflict(self):
        req = [{"key": "materials", "label": "Поддерживаемые пластики", "op": "eq", "value": "ABS"}]
        primary = capture(body=BUY + '<table><tr><td>Поддерживаемые пластики</td><td>ABS</td></tr></table>')
        fact = capture(body='<table><tr><td>Поддерживаемые пластики</td><td>ABS не поддерживается</td></tr></table>',
                       url='https://manufacturer.example/d600-pro/spec/')
        offer = extract_product_offer(primary, request(req), fact_documents=[fact])
        self.assertTrue(offer['source_issues'])

    def test_exact_model_technical_facts_keep_independent_document_proof(self):
        primary = capture(body=BUY)
        facts = capture(body='<table><tr><td>Производитель</td><td>MakerQA</td></tr>'
                            '<tr><td>Мощность</td><td>1200 Вт</td></tr></table>',
                        url="https://manufacturer.example/products/d600-pro/")
        before = deepcopy((primary, facts))
        offer = extract_product_offer(primary, request(REQUIREMENTS), fact_documents=[facts])
        self.assertEqual(offer["price"], 100000)
        self.assertEqual(offer["evidence"]["price"]["document_id"], primary["document_id"])
        self.assertEqual(offer["attributes"]["manufacturer"], "MakerQA")
        self.assertEqual(offer["attributes"]["power"], 1200)
        for key in ("manufacturer", "power"):
            proof = offer["evidence"]["attributes." + key]
            self.assertEqual(proof["document_id"], facts["document_id"])
            self.assertEqual(proof["source_hash"], facts["sha256"])
            self.assertEqual(proof["url"], facts["url"])
            self.assertIn(proof["quote"], facts["text"])
            self.assertEqual(proof["binding"]["primary_document_id"], primary["document_id"])
        self.assertEqual((primary, facts), before)

    def test_different_variant_or_editorial_document_cannot_fill_the_fact(self):
        primary = capture(body=BUY)
        for title in ("3D принтер MakerQA D600", "3D принтер MakerQA D600 Pro 2", "Обзор MakerQA D600 Pro", "3D принтер OtherBrand D600 Pro"):
            with self.subTest(title=title):
                fact = capture(title, '<table><tr><td>Производитель</td><td>MakerQA</td></tr></table>',
                               url="https://manufacturer.example/docs/variant/")
                offer = extract_product_offer(primary, request(REQUIREMENTS), fact_documents=[fact])
                self.assertNotIn("manufacturer", offer["attributes"])

    def test_secondary_document_price_tax_and_sale_unit_are_not_borrowed(self):
        primary = capture(body=BUY)
        fact = capture(body='<div class="product-top"><span class="price">200 000 USD за упаковку без НДС</span></div>'
                            '<table><tr><td>Мощность</td><td>1200 Вт</td></tr></table>',
                       url="https://manufacturer.example/products/d600-pro/")
        offer = extract_product_offer(primary, request(REQUIREMENTS), fact_documents=[fact])
        self.assertEqual(offer["price"], 100000)
        self.assertEqual(offer["currency"], "RUB")
        self.assertEqual(offer["tax"], "included")
        self.assertEqual(offer["unit"], "шт")
        for field in ("price", "currency", "tax", "unit"):
            self.assertEqual(offer["evidence"][field]["document_id"], primary["document_id"])

    def test_conflicting_seller_fact_is_not_overwritten_to_satisfy_request(self):
        primary = capture(body=BUY + '<table><tr><td>Мощность</td><td>500 Вт</td></tr></table>')
        fact = capture(body='<table><tr><td>Мощность</td><td>1200 Вт</td></tr></table>',
                       url="https://manufacturer.example/products/d600-pro/")
        offer = extract_product_offer(primary, request(REQUIREMENTS), fact_documents=[fact])
        self.assertEqual(offer["attributes"]["power"], 500)
        self.assertEqual(offer["evidence"]["attributes.power"]["document_id"], primary["document_id"])
        self.assertTrue(offer["validation"]["fact_conflicts"])
        self.assertTrue(offer["source_issues"])

    def test_conflicting_additional_documents_do_not_supply_a_convenient_value(self):
        primary = capture(body=BUY)
        facts = [capture(body='<table><tr><td>Мощность</td><td>' + value + ' Вт</td></tr></table>',
                         url='https://manufacturer.example/products/version-' + str(index) + '/')
                 for index, value in enumerate(('500', '1200'))]
        offer = extract_product_offer(primary, request(REQUIREMENTS), fact_documents=facts)
        self.assertNotIn('power', offer['attributes'])
        self.assertTrue(offer['validation']['fact_conflicts'])


class NumericAndAnonymousRowTests(unittest.TestCase):
    def test_anonymous_two_child_property_row_is_recovered(self):
        doc = capture(body=BUY + '<div><div>Мощность</div><div>1200 Вт</div></div>')
        facts = quality_requirement_observations(doc, request(REQUIREMENTS))
        self.assertEqual(facts["power"]["value"], 1200)
        self.assertIn(facts["power"]["quote"], doc["text"])

    def test_millilitres_are_normalized_and_never_compared_as_litres(self):
        requirement = {"key": "volume", "label": "Объём", "value": 2, "op": "gte", "unit": "л"}
        doc = capture(body=BUY + '<div><div>Объём</div><div>500 мл</div></div>')
        facts = quality_requirement_observations(doc, request([requirement]))
        self.assertEqual(facts["volume"]["value"], .5)
        legacy = {"attributes": {"volume": 500}, "evidence": {"attributes.volume": {"quote": "Объём 500 мл"}}}
        self.assertIsNot(_attribute_match(legacy, requirement), True)

    def test_power_suffix_and_collapsed_interval_cannot_confirm_a_scalar(self):
        self.assertFalse(_value_backed(200, "Мощность 1200 Вт"))
        requirement = {"key": "power", "label": "Мощность", "value": 200, "op": "eq", "unit": "Вт"}
        doc = capture(body=BUY + '<table><tr><td>Мощность</td><td>1200 Вт</td></tr></table>')
        candidate = {"title": TITLE, "attributes": {"power": 200}, "evidence": {"attributes.power": {"quote": "Мощность 1200 Вт"}}}
        validated = validate_candidate(candidate, doc, request([requirement]))
        self.assertNotIn('power', validated['attributes'])
        speed = {"key": "speed", "label": "Скорость", "value": 150, "op": "gte", "unit": "мм/с"}
        offer = {"attributes": {"speed": 200}, "evidence": {"attributes.speed": {"quote": "Скорость 1–200 мм/с"}}}
        self.assertIsNone(_attribute_match(offer, speed))

    def test_observed_interval_keeps_both_bounds(self):
        req = {"key": "layer", "label": "Толщина слоя", "value": [.05, .4], "op": "between", "unit": "мм"}
        doc = capture(body=BUY + '<table><tr><td>Толщина слоя</td><td>0.05–0.3 мм</td></tr></table>')
        facts = quality_requirement_observations(doc, request([req]))
        self.assertEqual(facts["layer"]["value"], [.05, .3])

    def test_anonymous_foreign_product_card_does_not_supply_a_primary_fact(self):
        doc = capture(body=BUY + '<section><div>3D принтер MakerQA F430</div><div>'
                      '<div><div>Мощность</div><div>9000 Вт</div></div></div></section>')
        self.assertNotIn('power', quality_requirement_observations(doc, request(REQUIREMENTS)))

    def test_spelled_speed_unit_keeps_original_quote_and_normalized_value(self):
        req = {"key": "speed", "label": "Максимальная скорость печати", "value": 120, "op": "eq", "unit": "мм/с"}
        doc = capture(body=BUY + '<table><tr><td>Максимальная скорость печати</td><td>120 мм/сек</td></tr></table>')
        fact = quality_requirement_observations(doc, request([req]))['speed']
        self.assertEqual(fact['value'], 120)
        self.assertEqual(fact['normalized_unit'], 'мм/с')
        self.assertIn('120 мм/сек', fact['quote'])

    def test_file_format_label_alias_has_primary_document_evidence(self):
        req = {"key": "formats", "label": "Поддержка форматов", "value": "AMF, OBJ, STL", "op": "eq"}
        doc = capture(body=BUY + '<table><tr><td>Формат файлов</td><td>STL, OBJ, AMF</td></tr></table>')
        offer = extract_product_offer(doc, request([req]))
        self.assertEqual(offer['attributes']['formats'], 'STL, OBJ, AMF')
        self.assertEqual(offer['evidence']['attributes.formats']['document_id'], doc['document_id'])


class SemanticCapabilityTests(unittest.TestCase):
    @staticmethod
    def match(label, actual, expected, unit=None):
        req = {"key": "property", "label": label, "value": expected, "op": "eq", "unit": unit}
        return _attribute_match({"attributes": {"property": actual}, "evidence": {}}, req)

    def test_explicit_nozzle_options_cover_required_subset(self):
        self.assertIs(self.match("Диаметр сопел", "0.4 мм (0.3, 0.5, 0.6, 0.8, 1.0 мм)", "0.3/0.4/0.6/0.8/1.0мм", "мм"), True)
        self.assertIsNone(self.match("Диаметр сопел", "0.4 мм", "0.3/0.4мм", "мм"))

    def test_input_voltage_and_frequency_ranges_cover_required_points(self):
        self.assertIs(self.match("Электропитание", "200В — 240В / 50-60Гц", "230 В, 50 Гц"), True)
        self.assertIs(self.match("Электропитание", "110В / 60Гц", "230 В, 50 Гц"), False)

    def test_explicit_glass_alternative_is_not_a_string_mismatch(self):
        self.assertIs(self.match("Материал рабочей платформы", "Боросиликатное стекло", "Алюминиевая платформа с Buildtak или стеклом"), True)

    def test_partial_material_list_is_unknown_not_false(self):
        self.assertIsNone(self.match("Поддерживаемые пластики", "PLA, ABS, PETG", "PLA, ABS, PETG, TPU и другие"))
        self.assertIs(self.match("Поддерживаемые пластики", "PLA, ABS, PETG, TPU, PEEK", "PLA, ABS, PETG, TPU и другие"), True)

    def test_file_format_order_and_extra_capabilities_do_not_change_coverage(self):
        self.assertIs(self.match("Поддержка форматов", "STL, GCODE, AMF, OBJ", "AMF, OBJ, STL"), True)
        self.assertIsNone(self.match("Поддержка форматов", "STL, OBJ", "AMF, OBJ, STL"))

    def test_negated_or_exclusive_clauses_do_not_use_positive_subset_shortcuts(self):
        self.assertIsNot(self.match("Поддерживаемые пластики", "ABS не поддерживается", "ABS"), True)
        self.assertIsNot(self.match("Поддержка форматов", "STL, OBJ", "только STL"), True)

    def test_polymer_compounds_do_not_prove_support_for_the_plain_polymer(self):
        for compound in ('PC-ABS', 'ABS-CF', 'ABS+PC', 'ABS/PC'):
            self.assertIsNot(self.match('Поддерживаемые пластики', compound, 'ABS'), True)


if __name__ == "__main__":
    unittest.main()
