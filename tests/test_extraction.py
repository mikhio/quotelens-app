"""Primary-page extraction guards; all inline page examples are fabricated QA."""
from copy import deepcopy
import hashlib
import unittest

from quotelens.acquisition import _PageParser, _space
from quotelens.engine import parse_request
from quotelens.extraction import extract_product_offer
from quotelens.selection import finalize, validate_candidate


def qa_document(body, title="QA осциллограф Scope-100"):
    html = '<main><h1>' + title + '</h1><div class="product-top">' + body + '</div></main>'
    parser = _PageParser()
    parser.feed(html)
    text = _space(" ".join(parser.parts))
    return {"id": "qa-primary", "url": "https://qa.example/product/scope-100/", "title": title,
            "primary_title": title, "html": html, "text": text,
            "sha256": hashlib.sha256(html.encode()).hexdigest(), "retrieved_at": "2026-01-01T00:00:00Z"}


class ProductExtractionTests(unittest.TestCase):
    def test_primary_price_survives_warranty_and_related_prices_without_inventing_units(self):
        doc = qa_document('<div class="price-block"><span class="price-current">12 300 ₽</span><span>с НДС 22%</span></div>'
                          '<div class="warranty">Доп. гарантия 1 год +1 800 ₽</div>В наличии 15 шт.'
                          '<h2>Похожие товары</h2><div class="product-item"><span class="price">800 ₽</span><a>Other-200</a></div>')
        before = deepcopy(doc)
        offer = extract_product_offer(doc, parse_request("осцилограф"))
        self.assertEqual(offer["price"], 12300)
        self.assertEqual(offer["currency"], "RUB")
        self.assertEqual(offer["tax"], "included")
        self.assertIsNone(offer["unit"])
        self.assertEqual(offer["supplier"], "qa.example")
        self.assertEqual(offer["supplier_basis"], "observed_hostname")
        self.assertEqual(offer["evidence"]["price"]["context"], "12 300 ₽ с НДС 22%")
        self.assertEqual(doc, before)
        self.assertEqual(finalize(parse_request("осцилограф"), [offer])["calculation"]["status"], "insufficient_data")

    def test_true_unknown_fnirsi_identity_and_visible_price_are_retained(self):
        doc = qa_document('<div class="price">9 100 ₽</div>', "Цифровой осциллограф FNIRSI 1014D")
        doc["page_kind"] = "unknown"
        offer = extract_product_offer(doc, parse_request("осцилограф"))
        self.assertEqual(offer["price"], 9100)
        self.assertEqual(offer["attributes"]["model"], "FNIRSI 1014D")
        self.assertEqual(offer["tax"], "unknown")

    def test_catalog_and_false_product_schema_never_become_offers(self):
        doc = qa_document('Сортировать по цене <div class="price">2 000 ₽</div>'
                          '<script type="application/ld+json">{"@type":"Product","name":"Осциллографы","offers":{"price":2000}}</script>', "Осциллографы")
        self.assertIsNone(extract_product_offer(doc, parse_request("осцилограф")))

    def test_invisible_or_wrong_primary_schema_price_cannot_supply_amount_or_currency(self):
        doc = qa_document('<script type="application/ld+json">{"@type":"Product","name":"Other-200","offers":{"price":2000,"priceCurrency":"RUB"}}</script>'
                          '<meta itemprop="price" content="2000"><div class="price">Цена по запросу</div>')
        offer = extract_product_offer(doc, parse_request("Осциллограф"))
        self.assertIsNone(offer["price"])
        self.assertIsNone(offer["currency"])

    def test_visible_amount_wins_over_disagreeing_hidden_schema(self):
        doc = qa_document('<script type="application/ld+json">{"@type":"Product","name":"QA осциллограф Scope-100","offers":{"price":2000}}</script><div class="price">3 000 ₽</div>')
        self.assertEqual(extract_product_offer(doc, parse_request("Осциллограф"))["price"], 3000)

    def test_old_and_supplementary_amounts_are_not_primary_prices(self):
        for markup in ['<s class="price">12 300 ₽</s>',
                       '<div>Доп. гарантия: <span class="price">12 300 ₽</span></div>',
                       '<div class="price-old">12 300 ₽</div>',
                       '<div class="price" style="display:none">12 300 ₽</div>',
                       '<div class="price">от 12 300 ₽</div>',
                       '<div class="price">1200 Вт</div>',
                       '<div class="price">1 200 ₽ в месяц</div>']:
            with self.subTest(markup=markup):
                offer = extract_product_offer(qa_document(markup), parse_request("Осциллограф"))
                self.assertIsNone(offer["price"])

    def test_current_price_can_be_separate_from_visible_struck_price(self):
        doc = qa_document('<div class="price-block"><del class="price-old">15 000 ₽</del><span class="price-current">12 300 ₽</span></div>')
        self.assertEqual(extract_product_offer(doc, parse_request("Осциллограф"))["price"], 12300)

    def test_unlabelled_different_prices_in_the_same_row_are_ambiguous(self):
        doc = qa_document('<div><span class="price">15 000 ₽</span><span class="price">12 300 ₽</span></div>')
        offer = extract_product_offer(doc, parse_request("Осциллограф"))
        self.assertIsNone(offer["price"])

    def test_dom_price_is_observed_but_order_quantity_condition_blocks_calculation(self):
        doc = qa_document('<div class="price-block">При заказе от 100 штук: <span class="price">1 200 ₽</span> за штуку с НДС</div>')
        offer = extract_product_offer(doc, parse_request("Осциллограф", 2))
        self.assertEqual(offer["price"], 1200)
        self.assertIs(offer["pricing_conditions"][0]["applies"], False)
        from quotelens.engine import _normalized
        self.assertIsNone(_normalized(offer, parse_request("Осциллограф", 2))[0])
        offer = extract_product_offer(doc, parse_request("Осциллограф", 100))
        self.assertEqual(offer["price"], 1200)
        self.assertEqual(offer["unit"], "шт")

    def test_other_model_price_in_local_dom_row_is_rejected_by_validator(self):
        doc = qa_document('<div class="price-block">Цена для Other-200: <span class="price">1 200 ₽</span></div>')
        candidate = {"title": doc["title"], "price": 1200, "evidence": {"price": {"quote": "1 200 ₽"}},
                     "primary_price_context": "1 200 ₽", "trusted_primary": True}
        self.assertIsNone(validate_candidate(candidate, doc, parse_request("Осциллограф"))["price"])

    def test_distant_tier_or_model_in_an_unsegmented_dom_row_is_not_hidden(self):
        for markup in [
            '<div>При заказе от 100 штук</div>' + 'Описание ' * 220 + '<div class="price-block"><span class="price">1 200 ₽</span></div>',
            '<div>Не менее 100 штук</div>' + 'Описание ' * 220 + '<div class="price-block"><span class="price">1 200 ₽</span></div>',
            '<div>Цена для Other-200 ' + 'х ' * 360 + '<span class="price">1 200 ₽</span></div>',
        ]:
            with self.subTest(markup=markup[:50]):
                doc = qa_document(markup)
                self.assertIsNone(extract_product_offer(doc, parse_request("Осциллограф", 2))["price"])

    def test_analogue_card_before_primary_price_cannot_supply_the_price(self):
        doc = qa_document('<div class="product-item-small"><a>Other-200</a><div class="price">900 ₽</div></div>'
                          '<div class="product-sidebar"><div class="price-block"><span class="price-current">1 200 ₽</span></div></div>')
        self.assertEqual(extract_product_offer(doc, parse_request("Осциллограф"))["price"], 1200)

    def test_unlabelled_frequency_and_ghz_are_not_guessed_as_mhz(self):
        for spec in ["Полоса пропускания 1 ГГц", "Полоса пропускания 200", "Частота дискретизации 100 МГц"]:
            doc = qa_document('<div class="price">1 200 ₽</div><div>' + spec + '</div>')
            self.assertNotIn("bandwidth_mhz", extract_product_offer(doc, parse_request("Осциллограф"))["attributes"])

    def test_alternative_specs_and_conflicting_currencies_remain_unknown(self):
        doc = qa_document('<div class="price">1 200 RUB</div><div class="price">1 200 USD</div>'
                          '<div>Полоса пропускания 100 МГц или 200 МГц, 2 или 4 канала</div>')
        offer = extract_product_offer(doc, parse_request("Осциллограф"))
        self.assertIsNone(offer["price"])
        self.assertNotIn("bandwidth_mhz", offer["attributes"])
        self.assertNotIn("channels", offer["attributes"])

    def test_primary_identity_mismatch_between_capture_and_html_has_no_dom_price_bypass(self):
        doc = qa_document('<div class="price">1 200 ₽</div>')
        doc["html"] = doc["html"].replace("Scope-100", "Other-200")
        # The flat quotation can still be evaluated by the original strict
        # validator; it may not receive a fabricated primary DOM association.
        doc["text"] += " Цена Other-200 900 ₽"
        self.assertIsNone(extract_product_offer(doc, parse_request("Осциллограф"))["price"])

    def test_large_navigation_outside_primary_purchase_scope_does_not_hide_price(self):
        doc = qa_document('<div class="price">Цена: 10 100 руб. за шт. с НДС</div>')
        doc["html"] = '<header>' + 'QA меню ' * 2000 + '</header>' + doc["html"]
        doc["text"] = 'QA меню ' * 2000 + doc["text"]
        offer = extract_product_offer(doc, parse_request("Осциллограф"))
        self.assertEqual(offer["price"], 10100)
        self.assertEqual(offer["tax"], "included")

    def test_named_primary_schema_table_excludes_other_product_channel_counts(self):
        # Simplified structure observed in the saved skomplekt DS1202Z-E page.
        doc = qa_document('<span itemscope itemtype="https://schema.org/Product">'
                          '<meta itemprop="name" content="QA осциллограф Scope-100">'
                          '<p>Товар не поставляется</p>'
                          '<table><tr><td>Аналоговая полоса пропускания</td><td>200 МГц</td></tr>'
                          '<tr><td>Аналоговые каналы</td><td>2</td></tr></table>'
                          '<div class="panel panel-releated"><div class="product__related">'
                          'Fluke Other-200, 4 канала</div></div></span>')
        offer = extract_product_offer(doc, parse_request("Осциллограф"))
        self.assertEqual(offer["attributes"]["channels"], 2)
        self.assertEqual(offer["attributes"]["bandwidth_mhz"], 200)
        self.assertIsNone(offer["price"])
        model_candidate = {"title": doc["title"], "attributes": {"channels": 4},
                           "evidence": {"attributes.channels": {"quote": "4 канала"}}}
        checked = validate_candidate(model_candidate, doc, parse_request("Осциллограф"))
        self.assertNotIn("channels", checked["attributes"])

    def test_primary_bandwidth_is_not_the_switchable_bandwidth_limiter(self):
        # Mixed Latin M + Cyrillic Гц is present verbatim in the actual table.
        doc = qa_document('<table><tr><th>Полоса пропускания</th><td>100 MГц</td></tr>'
                          '<tr><th>Количество каналов</th><td>4</td></tr>'
                          '<tr><th>Максимальное входное напряжение</th>'
                          '<td>Аналоговые каналы: 300 В</td></tr>'
                          '<tr><th>Ограничение полосы пропускания</th><td>20 МГц, полный диапазон</td></tr></table>')
        offer = extract_product_offer(doc, parse_request("Осциллограф"))
        self.assertEqual(offer["attributes"]["bandwidth_mhz"], 100)
        self.assertEqual(offer["attributes"]["channels"], 4)
        self.assertEqual(offer["evidence"]["attributes.bandwidth_mhz"]["quote"], "Полоса пропускания 100 MГц")
        for quote in ["Ограничение полосы пропускания 20 МГц", "полосы пропускания 20 МГц"]:
            candidate = {"title": doc["title"], "attributes": {"bandwidth_mhz": 20},
                         "evidence": {"attributes.bandwidth_mhz": {"quote": quote}}}
            self.assertNotIn("bandwidth_mhz", validate_candidate(candidate, doc, parse_request("Осциллограф"))["attributes"])

    def test_other_category_products_heading_is_an_attribute_boundary_without_html(self):
        doc = qa_document('Товар не поставляется Другие товары из категории Other-200, 4 канала')
        doc.pop("html")
        candidate = {"title": doc["title"], "attributes": {"channels": 4},
                     "evidence": {"attributes.channels": {"quote": "4 канала"}}}
        self.assertNotIn("channels", validate_candidate(candidate, doc, parse_request("Осциллограф"))["attributes"])

    def test_unmarked_nested_product_scope_cannot_supply_primary_attributes(self):
        doc = qa_document('<span itemscope itemtype="https://schema.org/Product">'
                          '<meta itemprop="name" content="QA осциллограф Scope-100">'
                          '<span itemscope itemtype="https://schema.org/Product">'
                          '<span itemprop="name">Other-200</span>Количество каналов 4</span></span>')
        candidate = {"title": doc["title"], "attributes": {"channels": 4},
                     "evidence": {"attributes.channels": {"quote": "Количество каналов 4"}}}
        self.assertNotIn("channels", validate_candidate(candidate, doc, parse_request("Осциллограф"))["attributes"])


if __name__ == "__main__":
    unittest.main()
