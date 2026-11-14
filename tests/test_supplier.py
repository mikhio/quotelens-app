"""Supplier facts from fabricated QA captures, never actual organisations."""
from copy import deepcopy
import hashlib
import unittest

from quotelens.acquisition import _PageParser, _space
from quotelens.engine import parse_request
from quotelens.extraction import extract_product_offer
from quotelens.selection import finalize, validate_candidate
from quotelens.supplier import supplier_details


def captured(contacts="", url="https://supplier.example/product/scope-100/"):
    html = ('<main><h1>QA осциллограф Scope-100</h1><div class="product-top">'
            '<div class="price-block"><span class="price">100 ₽</span> за шт. с НДС</div>'
            '<p>Количество каналов 4. Полоса пропускания 100 МГц.</p></div></main>' + contacts)
    parser = _PageParser()
    parser.feed(html)
    return {"id": "qa-supplier-capture", "url": url, "title": "QA осциллограф Scope-100",
            "html": html, "text": _space(" ".join(parser.parts)),
            "sha256": hashlib.sha256(html.encode()).hexdigest(), "retrieved_at": "2026-01-01T00:00:00Z"}


CONTACTS = ('<footer><p>Реквизиты: ООО «QA Поставщик». ИНН 1234567890.</p>'
            '<p>Телефон: +7 (495) 123-45-67. Email: sales@supplier.example.</p>'
            '<p>Сайт: https://supplier.example/</p></footer>')


class SupplierDetailsTests(unittest.TestCase):
    def test_footer_facts_have_same_capture_hash_date_and_exact_quotes(self):
        doc = captured(CONTACTS)
        before = deepcopy(doc)
        details = supplier_details(doc)
        self.assertEqual(details["legal_name"], "ООО «QA Поставщик»")
        self.assertEqual(details["inn"], "1234567890")
        self.assertEqual(details["email"], "sales@supplier.example")
        self.assertEqual(details["phone"], "+7 (495) 123-45-67")
        self.assertEqual(details["website"], "https://supplier.example/")
        self.assertEqual(details["verification"]["status"], "format_valid")
        self.assertIn("только формат", details["verification"]["reason"])
        for field, fact in details["evidence"].items():
            with self.subTest(field=field):
                self.assertIn(fact["quote"], doc["text"])
                self.assertEqual(fact["source_hash"], doc["sha256"])
                self.assertEqual(fact["document_id"], doc["id"])
                self.assertEqual(fact["retrieved_at"], doc["retrieved_at"])
                self.assertEqual(fact["url"], doc["url"])
        self.assertEqual(doc, before)

    def test_selection_recomputes_details_instead_of_trusting_model(self):
        doc = captured(CONTACTS)
        request = parse_request("Осциллограф 4 канала, полоса пропускания 100 МГц")
        offer = extract_product_offer(doc, request)
        self.assertEqual(offer["supplier_details"]["inn"], "1234567890")
        self.assertEqual(offer["attributes"]["supplier_inn"], "1234567890")
        offer["supplier_details"] = {"inn": "9999999999", "email": "invented@qa.example", "verification": {"status": "registry_verified"}}
        replayed = validate_candidate(offer, doc, request)
        self.assertEqual(replayed["supplier_details"], supplier_details(doc))
        self.assertEqual(replayed["supplier"], "ООО «QA Поставщик»")

    def test_missing_details_remain_missing(self):
        details = supplier_details(captured())
        self.assertEqual(details["verification"]["status"], "not_checked")
        self.assertTrue(all(details[key] is None for key in ("legal_name", "inn", "email", "phone", "website")))
        self.assertEqual(details["evidence"], {})

    def test_multiple_inns_cannot_be_chosen_by_model_or_footer(self):
        doc = captured(CONTACTS + '<aside>Другое юридическое лицо. ИНН 9876543210.</aside>')
        details = supplier_details(doc)
        self.assertIsNone(details["inn"])
        self.assertEqual(details["verification"]["status"], "conflict")
        proposal = {"title": doc["title"], "attributes": {"supplier_inn": "1234567890"},
                    "evidence": {"attributes.supplier_inn": {"quote": "ИНН 1234567890"}}}
        offer = validate_candidate(proposal, doc, parse_request("Осциллограф"))
        self.assertNotIn("supplier_inn", offer["attributes"])

    def test_marketplace_footer_is_not_a_seller_identity(self):
        doc = captured(CONTACTS, "https://www.ozon.ru/product/qa-scope-100/")
        details = supplier_details(doc)
        self.assertIsNone(details["inn"])
        self.assertIsNone(details["email"])
        self.assertEqual(details["verification"]["status"], "conflict")
        proposal = {"title": doc["title"], "supplier": "ООО «QA Поставщик»", "attributes": {"supplier_inn": "1234567890"},
                    "evidence": {"supplier": {"quote": "ООО «QA Поставщик»"}, "attributes.supplier_inn": {"quote": "ИНН 1234567890"}}}
        offer = validate_candidate(proposal, doc, parse_request("Осциллограф"))
        self.assertEqual(offer["supplier"], "ozon.ru")
        self.assertNotIn("supplier_inn", offer["attributes"])

    def test_explicit_marketplace_seller_block_excludes_platform_contacts(self):
        seller = '<section class="seller-info">Продавец ООО «QA Продавец». ИНН 9876543210. Телефон: +7 (495) 111-22-33. seller@qa.example</section>'
        doc = captured(seller + CONTACTS, "https://market.yandex.ru/product/qa-scope-100/")
        details = supplier_details(doc)
        self.assertEqual(details["legal_name"], "ООО «QA Продавец»")
        self.assertEqual(details["inn"], "9876543210")
        self.assertEqual(details["email"], "seller@qa.example")

    def test_invisible_contacts_and_schema_do_not_create_identity(self):
        doc = captured('<footer hidden>' + CONTACTS + '</footer><script type="application/ld+json">{"name":"ООО QA","taxID":"1234567890"}</script>')
        details = supplier_details(doc)
        self.assertIsNone(details["inn"])
        self.assertIsNone(details["legal_name"])

    def test_wrong_format_is_reported_without_claiming_registry_check(self):
        doc = captured(CONTACTS.replace("1234567890", "12345"))
        details = supplier_details(doc)
        self.assertEqual(details["inn"], "12345")
        self.assertEqual(details["verification"]["status"], "invalid")
        self.assertIsNone(details["verification"]["registry_url"])

    def test_literal_copyright_footer_stops_at_the_quoted_legal_name(self):
        # The literal wording reproduces the captured footer shape reported by
        # visual QA; this isolated document remains a fabricated QA capture.
        footer = ('ООО "Экотек" 2026 © Внимание ! Все указанные сведения приведены '
                  'как справочная информация и не являются публичной офертой, '
                  'определяемой положениями статьи 437 ГК РФ')
        doc = captured('<footer>' + footer + '</footer>')
        before = deepcopy(doc)
        details = supplier_details(doc)
        self.assertEqual(details["legal_name"], 'ООО "Экотек"')
        proof = details["evidence"]["legal_name"]
        self.assertEqual(proof["quote"], 'ООО "Экотек"')
        self.assertIn(proof["quote"], doc["text"])
        self.assertEqual(proof["source_hash"], hashlib.sha256(doc["html"].encode()).hexdigest())
        self.assertEqual(proof["retrieved_at"], doc["retrieved_at"])
        self.assertEqual(proof["document_id"], doc["id"])
        self.assertEqual(doc, before)

    def test_typographic_closing_quotes_are_name_boundaries_before_prose(self):
        for name in ('ООО «Экотек»', 'ООО „Экотек“', 'ООО “Экотек”'):
            with self.subTest(name=name):
                doc = captured('<footer>' + name + ' 2026 © Все права защищены</footer>')
                details = supplier_details(doc)
                self.assertEqual(details["legal_name"], name)
                self.assertEqual(details["evidence"]["legal_name"]["quote"], name)

    def test_unquoted_name_stops_before_parenthesized_requisites(self):
        name = 'ООО Торговый дом Канцелярские товары'
        doc = captured('<footer>' + name + ' (ИНН 1234567890, КПП 123456789).</footer>')
        details = supplier_details(doc)
        self.assertEqual(details["legal_name"], name)
        self.assertEqual(details["inn"], "1234567890")
        self.assertEqual(details["evidence"]["legal_name"]["quote"], name)
        self.assertEqual(details["evidence"]["legal_name"]["source_hash"], doc["sha256"])

    def test_unquoted_copyright_boundary_does_not_include_year(self):
        for suffix in (' 2026 © Все права защищены', ' 2020–2026 © Все права защищены', ' © Все права защищены'):
            with self.subTest(suffix=suffix):
                details = supplier_details(captured('<footer>ООО Экотек' + suffix + '</footer>'))
                self.assertEqual(details["legal_name"], 'ООО Экотек')

    def test_ambiguous_unquoted_prose_and_unclosed_quotes_remain_unknown(self):
        for text in ('ООО Экотек предлагает лучшие товары и оказывает услуги.',
                     'ООО Экотек Все указанные сведения приведены как справочная информация.',
                     'ООО «Экотек 2026 © Внимание Все права защищены.',
                     'ООО "Экотек 2026 © Внимание Все права защищены.'):
            with self.subTest(text=text):
                details = supplier_details(captured('<footer>' + text + '</footer>'))
                self.assertIsNone(details["legal_name"])
                self.assertNotIn("legal_name", details["evidence"])


class SelectionAcceptanceTests(unittest.TestCase):
    def fixture(self):
        request = parse_request("Осциллограф 8 каналов, полоса пропускания 100 МГц", 2)
        offer = extract_product_offer(captured(CONTACTS), request)
        offer["relevance"] = {"verdict": "mismatch", "basis": "model_assessment", "reason": "Только 4 канала вместо 8."}
        offer["manual_acceptances"] = {"attributes.channels": {"reason": "Для нашей задачи допустимо 4 канала.", "revision": 2, "at": "2026-01-02T00:00:00Z"}}
        return request, offer

    def test_exact_accepted_mismatch_preserves_model_assessment_and_source_values(self):
        request, offer = self.fixture()
        before = deepcopy(offer)
        result = finalize(request, [offer], {"min_suppliers": 1})
        self.assertEqual(result["calculation"]["total"], 200)
        reviewed = result["offers"][0]
        self.assertEqual(reviewed["attributes"]["channels"], 4)
        self.assertEqual(reviewed["relevance"], offer["relevance"])
        self.assertEqual(reviewed["relevance_acceptance"]["fields"], ["attributes.channels"])
        self.assertEqual(result["base_result"]["offers"][0]["relevance"], offer["relevance"])
        withdrawn = deepcopy(result["base_result"]["offers"])
        withdrawn[0].pop("manual_acceptances")
        self.assertIsNone(finalize(request, withdrawn, {"min_suppliers": 1})["calculation"]["total"])
        self.assertEqual(offer, before)

    def test_acceptance_does_not_remove_unknown_fit_source_or_unaccepted_mismatch(self):
        for mode in ("uncertain", "source_variant", "source_conflict", "wrong_field", "no_mismatch"):
            with self.subTest(mode=mode):
                request, offer = self.fixture()
                if mode == "uncertain":
                    offer["relevance"]["verdict"] = "uncertain"
                elif mode == "source_variant":
                    offer["relevance"]["basis"] = "source_variant_comparison"
                elif mode == "source_conflict":
                    offer["source_issues"] = ["Несколько фасовок источника."]
                elif mode == "wrong_field":
                    offer["manual_acceptances"] = {"attributes.bandwidth_mhz": next(iter(offer["manual_acceptances"].values()))}
                else:
                    request = parse_request("Осциллограф 4 канала, полоса пропускания 100 МГц", 2)
                self.assertIsNone(finalize(request, [offer], {"min_suppliers": 1})["calculation"]["total"])
