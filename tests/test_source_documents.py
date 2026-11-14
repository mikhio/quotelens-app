"""Page-shape regressions. QA prices/models are fabricated unless named capture shape."""
from copy import deepcopy
import json
import re
import unittest

from quotelens.source_documents import classify_page


def page(heading, body="", *, url="https://qa.example/catalog/oscilloscopes/", schemas=None, prefix=""):
    schemas = schemas or []
    html = prefix + "<h1>" + heading + "</h1>" + body
    html += "".join('<script type="application/ld+json">' + json.dumps(schema) + "</script>" for schema in schemas)
    return {"url": url, "title": heading, "primary_title": heading, "html": html, "text": heading + " " + re.sub(r"<[^>]*>", " ", body)}


class SourceDocumentTests(unittest.TestCase):
    def test_souz_shape_generic_heading_and_itemlist_are_discovery_only(self):
        # Minimal saved-page shape; this is the previously misattributed tile.
        document = page("Осциллографы", '<div class="product-item"><a href="/product/akip-4115-2a/">АКИП-4115/2А - цифровой осциллограф</a>34 865 ₽</div>',
                        schemas=[{"@type": "CollectionPage", "mainEntity": {"@type": "ItemList"}}],
                        prefix='<nav><a href="/catalog/filter/qa-100/">QA-100</a></nav>')
        result = classify_page(document)
        self.assertEqual(result["kind"], "listing")
        self.assertEqual(result["product_links"], [{"title": "АКИП-4115/2А - цифровой осциллограф", "url": "https://qa.example/product/akip-4115-2a/"}])

    def test_radioelement_shape_mislabels_category_as_schema_product(self):
        document = page("Осциллографы", '1-2 из 2 товара Сортировка: <div class="catalog-list-item__name"><a href="/catalog/oscilloscopes/dso-6102be/">USB-осциллограф DSO-6102BE</a></div><div class="catalog-list-item__name"><a href="/catalog/oscilloscopes/wfs210/">WFS210, осциллограф цифровой</a></div>',
                        schemas=[{"@type": "Product", "name": "Осциллографы", "offers": {"@type": "AggregateOffer", "lowPrice": 24400}}])
        result = classify_page(document)
        self.assertEqual(result["kind"], "listing")
        self.assertEqual([link["title"] for link in result["product_links"]], ["USB-осциллограф DSO-6102BE", "WFS210, осциллограф цифровой"])

    def test_novapribor_shape_nested_products_do_not_override_category_h1(self):
        names = ["VERDO SH1401 Осциллограф-мультиметр", "RIGOL DS1054Z Осциллограф цифровой"]
        document = page("Осциллографы", 'Сортировать: ' + "".join('<a href="/catalog/detail-' + str(i) + '/">' + name + '</a>' for i, name in enumerate(names)),
                        schemas=[{"@type": "ItemList", "itemListElement": [{"@type": "Product", "name": name} for name in names]}])
        self.assertEqual(classify_page(document)["kind"], "listing")

    def test_primary_specific_product_wins_over_recommendations_and_cart_count(self):
        document = page("RIGOL DS1054Z — цифровой осциллограф", "Цена QA 69 300 ₽ за 1 шт. Похожие товары RIGOL DS1104Z",
                        schemas=[{"@type": "ItemList", "itemListElement": [{"@type": "Product", "name": "RIGOL DS1104Z"}]}], prefix="<header>Корзина 0 ТОВАРОВ</header>")
        self.assertEqual(classify_page(document)["kind"], "product")

    def test_catalog_path_does_not_reject_actual_detail(self):
        document = page("АКИП-4115/2А — цифровой осциллограф", "Цена QA 100 RUB", url="https://qa.example/catalog/oscilloscopes/akip-4115-2a/")
        self.assertEqual(classify_page(document)["kind"], "product")

    def test_primary_h1_takes_precedence_over_seo_catalog_title(self):
        document = page("QA-4131 — осциллограф", "Цена QA 100 RUB")
        document["title"] = "Каталог осциллографов QA"
        self.assertEqual(classify_page(document)["kind"], "product")

    def test_incomplete_product_without_listing_structure_remains_unknown(self):
        for heading in ["Чайник", "Осциллограф 100 МГц"]:
            with self.subTest(heading=heading):
                self.assertEqual(classify_page(page(heading, "Цена по запросу. В комплекте 2 товара."))["kind"], "unknown")

    def test_specific_product_schema_without_model_can_identify_the_main_product(self):
        document = page("Чайник стеклянный", "Цена QA 100 RUB", schemas=[{"@type": "Product", "name": "Чайник стеклянный", "sku": "qa-1"}])
        self.assertEqual(classify_page(document)["kind"], "product")

    def test_filters_and_multiple_product_cards_identify_listing_without_model_codes(self):
        document = page("Чайники", 'Фильтр <div class="product-item__name"><a href="/red/">Красный чайник</a></div><div class="product-item__name"><a href="/blue/">Синий чайник</a></div>')
        self.assertEqual(classify_page(document)["kind"], "listing")

    def test_related_models_do_not_turn_an_incomplete_main_product_into_listing(self):
        document = page("Чайник", 'Цена по запросу. <h2>Похожие товары</h2><div class="product-item"><a href="/qa-101/">QA-101</a><a href="/qa-202/">QA-202</a></div>', schemas=[{"@type": "ItemList", "itemListElement": [{"@type": "Product", "name": "QA-101"}, {"@type": "Product", "name": "QA-202"}]}])
        self.assertEqual(classify_page(document)["kind"], "unknown")

    def test_multiple_identity_links_identify_listing_without_schema(self):
        document = page("Осциллографы", '<a href="/qa-101/">QA-101</a><a href="/qa-202/">QA-202</a>')
        self.assertEqual(classify_page(document)["kind"], "listing")

    def test_does_not_fabricate_schema_url_or_mutate_document(self):
        document = page("Осциллографы", '<a href="http://qa.example/qa-101/">QA-101</a><a href="https://user:secret@qa.example/qa-202/">QA-202</a>',
                        schemas=[{"@type": "ItemList", "itemListElement": [{"@type": "Product", "name": "QA-303", "url": "https://qa.example/invented"}]}])
        before = deepcopy(document)
        result = classify_page(document)
        self.assertEqual(result["product_links"], [])
        self.assertEqual(document, before)


if __name__ == "__main__":
    unittest.main()
