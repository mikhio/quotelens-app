"""Fabricated server adapters exercise capture policy without private services."""
import hashlib
import json
import unittest
from unittest.mock import patch

from quotelens.acquisition import Acquisition, AcquisitionError
from quotelens.engine import parse_request
from quotelens.extraction import extract_product_offer
from quotelens.selection import finalize
from quotelens.source_adapters import build_acquisition, register_source_adapter, unregister_source_adapter


URL = "https://catalog.example/products/scope-100"
HTML = ('<main><h1>QA осциллограф Scope-100</h1><div class="product-top">'
        '<div class="price-block"><span class="price">100 ₽</span> за шт. с НДС</div>'
        '<p>Количество каналов 4. Полоса пропускания 100 МГц.</p></div></main>'
        '<footer>ООО «QA Каталог». ИНН 1234567890.</footer>')


class FixtureAdapter:
    def __init__(self):
        self.calls = []

    def discover(self, query, *, limit, timeout):
        self.calls.append(("discover", query, limit, timeout))
        return [{"url": URL, "title": "QA Scope-100", "snippet": "QA fixture only",
                 "headers": {"Authorization": "QA_PRIVATE_SECRET"}, "api_key": "QA_PRIVATE_SECRET"}]

    def capture(self, url, *, timeout):
        self.calls.append(("capture", url, timeout))
        return {"url": url, "html": HTML, "retrieved_at": "2026-01-01T00:00:00Z",
                "sha256": "untrusted-hash", "headers": {"Authorization": "QA_PRIVATE_SECRET"},
                "credentials": "QA_PRIVATE_SECRET", "supplier_details": {"inn": "9999999999"}}


class SourceAdapterTests(unittest.TestCase):
    def tearDown(self):
        for source_id in ("qa", "other"):
            unregister_source_adapter(source_id)

    def register(self, adapter=None):
        adapter = adapter or FixtureAdapter()
        register_source_adapter("qa", ["catalog.example"], lambda: adapter)
        return adapter

    def test_no_registration_returns_the_original_acquisition(self):
        acquisition = build_acquisition(["supplier.example"], timeout=7)
        self.assertIs(type(acquisition), Acquisition)
        self.assertEqual(acquisition.timeout, 7)

    def test_discovery_capture_validation_and_calculation_share_one_pipeline(self):
        adapter = self.register()
        acquisition = build_acquisition(["catalog.example"], timeout=7)
        with patch.object(Acquisition, "search", side_effect=AssertionError("private-only scope reached public search")):
            found = acquisition.search("QA осциллограф", limit=3)
        self.assertEqual(found["hits"][0]["url"], URL)
        acquisition.timeout = 2
        document = acquisition.open(found["hits"][0]["url"], browser=True)
        self.assertEqual(document["method"], "source_adapter")
        self.assertEqual(document["sha256"], hashlib.sha256(HTML.encode()).hexdigest())
        self.assertEqual(document["retrieved_at"], "2026-01-01T00:00:00Z")
        document["document_id"] = "qa-adapter-document"
        request = parse_request("Осциллограф 4 канала, полоса пропускания 100 МГц", 2)
        offer = extract_product_offer(document, request)
        self.assertEqual(offer["price"], 100)
        self.assertEqual(offer["supplier_details"]["inn"], "1234567890")
        self.assertEqual(offer["evidence"]["price"]["source_hash"], document["sha256"])
        self.assertEqual(finalize(request, [offer], {"min_suppliers": 1})["calculation"]["total"], 200)
        self.assertEqual(adapter.calls[-1], ("capture", URL, 2))
        serialized = json.dumps({"search": found, "document": document, "offer": offer})
        for secret in ("QA_PRIVATE_SECRET", "Authorization", "credentials", "9999999999", "untrusted-hash"):
            self.assertNotIn(secret, serialized)

    def test_registration_is_code_only_and_rejects_overlap_or_client_urls(self):
        self.register()
        for domains in (["https://api.example/private?key=QA_PRIVATE_SECRET"], ["catalog.example"]):
            with self.assertRaises(ValueError):
                register_source_adapter("other", domains, FixtureAdapter)
        with self.assertRaises(ValueError):
            register_source_adapter("qa", ["other.example"], FixtureAdapter)

    def test_scope_does_not_instantiate_or_route_unselected_adapter(self):
        factory = unittest.mock.Mock(side_effect=AssertionError("unselected source instantiated"))
        register_source_adapter("qa", ["catalog.example"], factory)
        acquisition = build_acquisition(["other.example"])
        self.assertIs(type(acquisition), Acquisition)
        factory.assert_not_called()

    def test_public_urls_keep_safe_original_transport(self):
        self.register()
        acquisition = build_acquisition()
        with patch.object(Acquisition, "open", return_value={"qa": True}) as original:
            self.assertEqual(acquisition.open("https://supplier.example/product"), {"qa": True})
            original.assert_called_once_with("https://supplier.example/product", browser=False)
        for unsafe in ("https://127.0.0.1/", "https://catalog.example/?access_token=QA", "https://user:QA@catalog.example/"):
            with self.subTest(url=unsafe), self.assertRaises(AcquisitionError):
                acquisition.open(unsafe)

    def test_out_of_scope_or_secret_discovery_links_are_dropped(self):
        adapter = self.register()
        adapter.discover = lambda *args, **kwargs: [{"url": url} for url in (
            URL, "https://other.example/product", "https://127.0.0.1/", "https://catalog.example/?api_key=QA_PRIVATE_SECRET")]
        found = build_acquisition(["catalog.example"]).search("QA")
        self.assertEqual([hit["url"] for hit in found["hits"]], [URL])

    def test_bad_capture_or_private_exception_never_leaks_metadata(self):
        adapter = self.register()
        acquisition = build_acquisition(["catalog.example"])
        for result in ({"url": "https://other.example/", "html": HTML},
                       {"url": URL, "text": "x", "retrieved_at": "yesterday"},
                       {"url": URL, "text": "x", "title": {"api_key": "QA_PRIVATE_SECRET"}}):
            adapter.capture = lambda *args, **kwargs: result
            with self.assertRaises(AcquisitionError) as raised:
                acquisition.open(URL)
            self.assertNotIn("QA_PRIVATE_SECRET", str(raised.exception))
        def fail(*args, **kwargs):
            raise RuntimeError("Authorization: QA_PRIVATE_SECRET")
        adapter.capture = fail
        with self.assertRaises(AcquisitionError) as raised:
            acquisition.open(URL)
        self.assertNotIn("QA_PRIVATE_SECRET", str(raised.exception))
