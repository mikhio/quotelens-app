"""Regression checks for primary current and previous price extraction.

Inline QA markup reproduces the observed classes and relevant primary text.
The actual capture was separately replayed from the immutable production
snapshot: job0b11a4b323953395cecfa2dc, documentd75d703d8076d9f1e3d542af.
"""
import unittest
from quotelens.acquisition import _PageParser, _space
from quotelens.extraction import extract_product_offer
from quotelens.engine import parse_request, analyze_offers


def paper_document(*, current="507", old="655", extra=""):
    title = "Бумага писчая А4, 65 г/м2, 500 л., Россия, STAFF, 114215"
    markup = ('<main><div class="card" itemscope itemtype="http://schema.org/Product">'
              '<div class="card__top"><h1 itemprop="name">' + title + '</h1></div>'
              '<div class="card__body"><div class="card__actions"><div class="card__box js-count-price-wrapper">'
              '<div class="card__price"><div class="price price_main card__price-item">' + current + ' ₽</div>'
              '<div class="price price_old card__price-item">' + old + ' ₽</div></div></div></div></div>'
              '<p>Формат а4. Плотность 65 г/м2. Количество листов в пачке 500.</p></div>' + extra + '</main>')
    parser = _PageParser();parser.feed(markup)
    return {"id": "qa-paper", "url": "https://qa.example/paper/", "primary_title": title, "title": title,
            "html": markup, "text": _space(" ".join(parser.parts)), "retrieved_at": "2026-11-12T20:00:00Z"}


class CurrentPaperPriceTests(unittest.TestCase):
    def test_explicit_main_price_is_not_lost_beside_old_price(self):
        offer = extract_product_offer(paper_document(), parse_request("Бумага А4 65 г/м2 500 листов в пачке", 50))
        self.assertEqual(offer["price"], 507)
        self.assertEqual(offer["evidence"]["price"]["quote"], "507 ₽")
        self.assertEqual(offer["attributes"]["density_gsm"], 65)
        self.assertEqual(offer["attributes"]["sheets_per_pack"], 500)
        self.assertNotIn("model", offer["attributes"])
        self.assertIsNone(offer["unit"])

    def test_recommended_product_price_does_not_replace_primary(self):
        extra = '<div class="catalog-item"><h2>Бумага другая</h2><span class="price price_main" itemprop="price">12 ₽</span></div>'
        offer = extract_product_offer(paper_document(extra=extra), parse_request("Бумага А4 65 г/м2 500 листов в пачке"))
        self.assertEqual(offer["price"], 507)

    def test_observed_pack_allows_only_explicit_conditional_unit_assumption(self):
        request = parse_request("Бумага А4 65 г/м2 500 листов в пачке", 50)
        offer = extract_product_offer(paper_document(), request)
        self.assertIsNone(offer["unit"])
        self.assertEqual(offer["provisional_unit"]["unit"], "пачка")
        offer["provenance"] = "synthetic"
        result = analyze_offers(request, [offer])
        self.assertIsNone(result["calculation"]["total"])
        estimate = result["calculation"]["provisional"]
        self.assertEqual(estimate["total"], 25350)
        self.assertEqual(estimate["normalized_prices"][offer["id"]], 507)
        self.assertEqual(estimate["unit_assumptions"][0]["offer_id"], offer["id"])
        self.assertTrue(any("Предполагается" in text for text in estimate["warnings"]))
        self.assertIsNone(result["offers"][0]["unit"])

    def test_inventory_and_bare_sheet_count_do_not_establish_pack_assumption(self):
        doc = paper_document()
        doc["html"] = doc["html"].replace("Количество листов в пачке 500", "Количество листов 500. В наличии 15 шт")
        parser = _PageParser();parser.feed(doc["html"])
        doc["text"] = _space(" ".join(parser.parts))
        offer = extract_product_offer(doc, parse_request("Бумага А4 65 г/м2 500 листов в пачке"))
        self.assertNotIn("provisional_unit", offer)

    def test_from_and_installment_labels_remain_rejected(self):
        for value in ["от 507", "507 ₽ в месяц"]:
            with self.subTest(value=value):
                offer = extract_product_offer(paper_document(current=value), parse_request("Бумага А4 65 г/м2 500 листов в пачке"))
                self.assertIsNone(offer["price"])


if __name__ == "__main__":
    unittest.main()
