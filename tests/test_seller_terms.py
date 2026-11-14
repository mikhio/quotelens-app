import copy
import hashlib
from html.parser import HTMLParser
import unittest

from quotelens.seller_terms import enrich_offer, seller_terms, seller_terms_links


class _Text(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []

    def handle_data(self, data):
        self.parts.append(data)


def document(markup, url="https://seller.example/product/paper", identifier="primary"):
    parser = _Text()
    parser.feed(markup)
    return {"html": markup, "text": " ".join(" ".join(parser.parts).split()), "title": "Бумага А3", "primary_title": "Бумага А3",
            "url": url, "document_id": identifier,
            "sha256": hashlib.sha256(markup.encode()).hexdigest(),
            "retrieved_at": "2026-11-13T12:00:00Z"}


class SellerTermsTests(unittest.TestCase):
    def test_only_visible_public_same_seller_navigation_is_discovered(self):
        doc = document('''<nav>
          <a href="/delivery/">Доставка</a>
          <a href="https://www.seller.example/payment/">Условия оплаты</a>
          <a href="/warranty/">Гарантия</a>
          <a href="/terms/">Условия продажи</a>
          <a href="https://other.example/delivery/">Доставка</a>
          <a href="https://seller.example.evil.example/payment/">Оплата</a>
          <a href="https://billing.seller.example/payment/">Оплата</a>
          <a href="http://seller.example/payment/">Оплата</a>
          <a href="https://u:password@seller.example/payment/">Оплата</a>
          <a href="/payment/?access_token=secret">Оплата</a>
          <a hidden href="/hidden/">Доставка</a>
          <div style="display: none"><a href="/hidden2/">Оплата</a></div>
          <a href="/personal/payment/">Оплата</a>
          <a href="#delivery">Доставка</a>
          <a href="https://127.0.0.1/delivery/">Доставка</a>
          <a href="/paper2/">Бумага офисная с доставкой по России</a>
        </nav>''')
        self.assertEqual(seller_terms_links(doc), [
            {"url": "https://seller.example/delivery/", "kind": "delivery"},
            {"url": "https://www.seller.example/payment/", "kind": "payment"},
            {"url": "https://seller.example/warranty/", "kind": "warranty"},
            {"url": "https://seller.example/terms/", "kind": "terms"}])

    def test_plain_links_require_their_visible_captured_label(self):
        doc = {"url": "https://seller.example/product/paper", "text": "Доставка", "links": [
            {"title": "Доставка", "url": "/delivery/"},
            {"title": "Оплата", "url": "/payment/"}]}
        self.assertEqual(seller_terms_links(doc), [{"url": "https://seller.example/delivery/", "kind": "delivery"}])

    def test_navigation_keywords_and_product_guarantee_are_not_contract_claims(self):
        doc = document('''<nav><a href="/warranty/">Гарантия на товар</a><a href="/delivery/">Доставка</a></nav>
          <p>Соответствие ГОСТ гарантирует многолетнее хранение ваших документов.</p>
          <p>Политика персональных данных применяется при оплате банковской картой.</p>
          <div>Телефон +7 123 456-78-90 <a href="/warranty/">Гарантия на товар</a></div>
          <footer><div><ul><li>Телефон +7 123 456-78-90</li><li><a href="/payment/">Оплата</a></li><li><a href="/delivery/">Доставка</a></li></ul></div></footer>
          <p>Вместе с заказом будут предоставлены документы для бухгалтерии.</p>''')
        self.assertEqual(seller_terms(doc), {})

    def test_inline_rendering_differences_quote_captured_text_exactly(self):
        doc = document('<p>Доставка внутри МКАД от <b>360 руб.</b>, при заказе до 15:00 в течение 2-5 дней</p>')
        doc['text'] = 'Доставка внутри МКАД от 360 Руб., при заказе до 15:00 в течение 2-5 дней'
        proof = seller_terms(doc)['delivery'][0]
        self.assertEqual(proof['quote'], doc['text'])
        self.assertIn('360 Руб.,', proof['quote'])

    def test_delivery_table_binds_label_and_cost_in_the_same_row(self):
        doc = document('<div class="shipping_info"><table><tr><td>Доставка по Екатеринбургу</td><td>500₽</td></tr><tr><td>Доставка в Берёзовский</td><td>700₽</td></tr></table></div>')
        rows = seller_terms(doc)['delivery']
        self.assertEqual([row['quote'] for row in rows], ['Доставка по Екатеринбургу 500₽', 'Доставка в Берёзовский 700₽'])

    def test_primary_tabs_retain_observed_geographic_and_payment_clauses(self):
        doc = document('''<main><h1>Бумага А3</h1>
          <div role="tabpanel" style="display:none">
            <p>Для юридических лиц - безналичный расчет</p>
            <p>Оплата осуществляется на основании счета.</p>
            <p>Бесплатная доставка по Калужской, Московской и Тульской областям</p>
            <p>Бесплатно доставим заказ нашим транспортом в будние дни с 8:00 до 17:00. Дату и время доставки согласует менеджер.</p>
          </div></main>''')
        terms = seller_terms(doc)
        self.assertIn("Московской", terms["delivery"][-1]["quote"])
        self.assertIn("согласует менеджер", terms["delivery"][-1]["value"])
        self.assertEqual(terms["payment"][0]["status"], "unconfirmed")
        for observations in terms.values():
            for proof in observations:
                self.assertIn(proof["quote"], doc["text"])
                for key in ("document_id", "retrieved_at"):
                    self.assertEqual(proof[key], doc[key])
                self.assertEqual(proof["source_hash"], doc["sha256"])
                self.assertEqual(proof["url"], doc["url"])

    def test_warranty_duration_and_new_goods_remain_separate_observations(self):
        doc = document('<p>Гарантийный срок составляет 12 месяцев.</p><p>Все товары новые и не бывшие в употреблении.</p>')
        terms = seller_terms(doc)
        self.assertEqual(terms["warranty"][0]["value"], "Гарантийный срок составляет 12 месяцев.")
        self.assertIn("Все товары новые", terms["terms"][0]["quote"])
        self.assertNotIn("delivery", terms)

    def test_hidden_and_related_cards_cannot_supply_terms(self):
        doc = document('''<main><p hidden>Гарантия предоставляется на 36 месяцев.</p>
          <section class="related"><p>Доставка всех товаров бесплатно за 1 день.</p></section>
          <div class="product-card"><p>Гарантия составляет 24 месяца.</p></div>
          <p>Доставка по России осуществляется по тарифам транспортной компании.</p></main>''')
        terms = seller_terms(doc)
        self.assertNotIn("warranty", terms)
        self.assertEqual(len(terms["delivery"]), 1)
        self.assertIn("тарифам", terms["delivery"][0]["quote"])

    def test_random_footer_tax_does_not_apply_to_product_price(self):
        doc = document('<main><h1>Бумага А3</h1><span class="price">100 ₽</span></main><footer><p>Оператор ООО Тест работает без НДС.</p></footer>')
        self.assertNotIn("tax", seller_terms(doc))
        enriched = enrich_offer({"tax": "unknown", "conditions": {}}, {}, doc)
        self.assertEqual(enriched["tax"], "unknown")

    def test_explicit_all_prices_vat_applies_even_in_footer(self):
        doc = document('<footer><p>Все цены указаны с НДС. Информация не является публичной офертой.</p></footer>')
        tax = seller_terms(doc)["tax"]
        self.assertEqual((tax["value"], tax["scope"]), ("included", "all_prices"))
        enriched = enrich_offer({"tax": "unknown"}, {}, doc)
        self.assertEqual(enriched["tax"], "included")
        self.assertEqual(enriched["evidence"]["tax"]["source_hash"], doc["sha256"])

    def test_service_prices_vat_cannot_supply_product_vat(self):
        doc = document('<p>Все цены на доставку указаны с НДС.</p>')
        self.assertNotIn("tax", seller_terms(doc))

    def test_qualified_or_negated_global_tax_is_not_an_unconditional_price_fact(self):
        for clause in ('Не все цены включают НДС.', 'Все цены могут быть указаны без НДС.',
                       'Все цены не указаны с НДС.', 'Все цены указаны с НДС, кроме товаров под заказ.'):
            with self.subTest(clause=clause):
                self.assertNotIn('tax', seller_terms(document('<p>' + clause + '</p>')))

    def test_primary_price_tax_needs_a_bounded_purchase_block(self):
        doc = document('''<main><h1>Бумага А3</h1><div class="product-detail">
          <div class="price"><span itemprop="price">100 ₽</span> с НДС</div>
        </div></main><footer>НДС не включён в стоимость дополнительных услуг.</footer>''')
        self.assertEqual(seller_terms(doc)["tax"]["value"], "included")
        self.assertEqual(seller_terms(doc)["tax"]["scope"], "primary_price")

    def test_explicit_exclusion_and_conflicting_tax_never_become_inclusion(self):
        doc = document('<p>Все цены не включают НДС.</p>')
        self.assertEqual(seller_terms(doc)["tax"]["value"], "excluded")
        conflict = document('<p>Все цены указаны с НДС.</p><p>Все цены указаны без НДС.</p>')
        self.assertNotIn("tax", seller_terms(conflict))
        result = enrich_offer({"tax": "included", "evidence": {"tax": {"quote": "старый факт"}}}, {}, conflict)
        self.assertEqual(result["tax"], "unknown")
        self.assertNotIn("tax", result["evidence"])
        self.assertEqual(len(result["observed_terms"]["tax_conflicts"]), 2)

    def test_only_linked_same_seller_immutable_capture_can_enrich_offer(self):
        primary = document('<nav><a href="/terms/">Условия продажи</a></nav>')
        linked = document('<p>Все цены указаны с НДС.</p><p>Гарантия составляет 12 месяцев.</p>', 'https://seller.example/terms/', 'terms')
        unlinked = document('<p>Доставка всех заказов бесплатна.</p>', 'https://seller.example/secret-terms/', 'unlinked')
        foreign = document('<p>Все цены указаны без НДС.</p>', 'https://another.example/terms/', 'foreign')
        incomplete = copy.deepcopy(linked)
        incomplete.pop('retrieved_at')
        offer = {"tax": "unknown", "conditions": {"warranty": None}, "unit": None}
        before = copy.deepcopy((offer, primary, linked))
        result = enrich_offer(offer, {"conditions": [{"key": "warranty", "value": "36 месяцев"}]}, primary, [linked, unlinked, foreign, incomplete])
        self.assertEqual(result['tax'], 'included')
        self.assertEqual(result['observed_terms']['warranty'][0]['document_id'], 'terms')
        self.assertNotIn('delivery', result['observed_terms'])
        self.assertEqual(result['conditions'], {"warranty": None})
        self.assertIsNone(result['unit'])
        self.assertEqual((offer, primary, linked), before)

    def test_related_product_price_tax_is_not_global_policy(self):
        primary = document('<nav><a href="/payment/">Оплата</a></nav>')
        related = document('<main><h1>Другая бумага</h1><div class="product-detail"><div class="price">500 ₽ с НДС</div></div></main>', 'https://seller.example/payment/', 'payment')
        self.assertEqual(enrich_offer({"tax": "unknown"}, {}, primary, [related])['tax'], 'unknown')

    def test_general_terms_never_accept_customer_specific_conditions(self):
        doc = document('<p>Доставка по России от 2 до 5 дней. Стоимость зависит от региона.</p><p>Гарантия составляет 12 месяцев.</p>')
        request = {"conditions": [{"key": "delivery", "value": "Долгопрудный, бесплатно с разгрузкой, 5 дней с подписания"}, {"key": "warranty", "value": "товар новый"}]}
        result = enrich_offer({"tax": "unknown", "delivery": None, "conditions": {}}, request, doc)
        self.assertEqual(result['conditions'], {})
        self.assertIsNone(result['delivery'])
        self.assertNotIn('Долгопрудный', str(result))
        self.assertNotIn('terms', result['observed_terms'])
        self.assertTrue(all(p['status'] == 'unconfirmed' for rows in result['observed_terms'].values() for p in rows))

    def test_search_snippets_and_marketplace_operator_terms_are_not_seller_terms(self):
        doc = document('<p>Все цены указаны с НДС.</p><p>Доставка осуществляется за 1 день.</p>')
        doc['method'] = 'search_api'
        self.assertEqual(seller_terms(doc), {})
        doc.pop('method')
        doc['text'] += ' Маркетплейс'
        self.assertEqual(seller_terms(doc), {})


if __name__ == '__main__':
    unittest.main()
