"""Security boundaries and explicitly synthetic QA fixtures for public acquisition."""
import hashlib
import io
import json
import os
import shutil
import socket
import threading
import time
import unittest
from unittest.mock import Mock, patch

from quotelens.acquisition import (Acquisition, AcquisitionError, MAX_BYTES,
                                   _PinnedHTTPS, _Response, _RobotRules,
                                   _normalize_url, _public_addresses)

PUBLIC = [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("93.184.216.34", 443))]


def response(url="https://supplier.example/product", body=b"<h1>QA fixture product</h1><p>Price 100 RUB</p>", status=200, mime="text/html; charset=utf-8"):
    return _Response(url, status, body, {"content-type": mime})


class PublicPolicyTests(unittest.TestCase):
    def test_disallows_protocols_credentials_ports_and_secret_urls(self):
        for url in ["http://example.com", "file:///etc/passwd", "https://user:secret@example.com", "https://example.com:444/", "https://localhost/", "https://x.internal/", "https://example.com/?api_key=private", "https://example.com/?access_token=private", "https://example.com/\r\nHeader:hi"]:
            with self.subTest(url=url), self.assertRaises(AcquisitionError):
                _normalize_url(url)

    def test_scope_includes_subdomain_but_not_suffix_lookalike(self):
        acq = Acquisition(["supplier.example"])
        self.assertEqual(acq._scoped("https://www.supplier.example/a#fragment"), "https://www.supplier.example/a")
        for target in ["https://supplier.example.evil.test/", "https://othersupplier.example/", "https://other.example/"]:
            with self.subTest(target=target), self.assertRaises(AcquisitionError):
                acq._scoped(target)

    def test_rejects_any_private_address_in_dns_results(self):
        for addr in ["127.0.0.1", "10.1.2.3", "169.254.169.254", "::1", "fc00::1", "::ffff:127.0.0.1", "224.0.0.1", "ff02::1"]:
            records = PUBLIC + [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (addr, 443))]
            with self.subTest(addr=addr), patch("quotelens.acquisition.socket.getaddrinfo", return_value=records), self.assertRaises(AcquisitionError):
                _public_addresses("supplier.example", time.monotonic() + 2)

    @patch("quotelens.acquisition.socket.getaddrinfo", return_value=PUBLIC)
    def test_public_dns_is_accepted(self, _):
        self.assertEqual(_public_addresses("supplier.example", time.monotonic() + 2), ["93.184.216.34"])

    def test_connection_uses_checked_ip_and_original_hostname_for_tls(self):
        conn = _PinnedHTTPS("supplier.example", "93.184.216.34", 2)
        sock = Mock()
        conn._context = Mock()
        with patch("quotelens.acquisition.socket.create_connection", return_value=sock) as connect:
            conn.connect()
        connect.assert_called_once_with(("93.184.216.34", 443), timeout=2)
        conn._context.wrap_socket.assert_called_once_with(sock, server_hostname="supplier.example")

    def test_robots_longest_wildcard_and_allow_tie(self):
        rules = _RobotRules("User-agent: *\nDisallow: /private/*\nAllow: /private/ok$\nDisallow: /*?token=\n")
        self.assertFalse(rules.allowed("https://supplier.example/private/a"))
        self.assertTrue(rules.allowed("https://supplier.example/private/ok"))
        self.assertFalse(rules.allowed("https://supplier.example/private/ok/child"))
        self.assertFalse(rules.allowed("https://supplier.example/public?token=x"))

    def test_robots_specific_user_agent_overrides_wildcard(self):
        rules = _RobotRules("User-agent: *\nDisallow: /\nUser-agent: QuoteLensResearch\nAllow: /\nDisallow: /blocked\n")
        self.assertTrue(rules.allowed("https://supplier.example/product"))
        self.assertFalse(rules.allowed("https://supplier.example/blocked"))

    def test_cannot_assume_robots_permission_after_server_failure(self):
        acq = Acquisition()
        with patch.object(acq, "_request", return_value=response(status=503)), self.assertRaises(AcquisitionError):
            acq._check_robots("https://supplier.example/product", time.monotonic() + 1)

    def test_html_captcha_at_robots_is_not_permission(self):
        acq = Acquisition()
        with patch.object(acq, "_request", return_value=response(body=b"<html>CAPTCHA</html>")), self.assertRaises(AcquisitionError):
            acq._check_robots("https://supplier.example/product", time.monotonic() + 1)


class DocumentAndSearchTests(unittest.TestCase):
    def test_catalog_annotations_prioritize_product_details_beyond_navigation_limit(self):
        # Fabricated links/price; the shape mirrors captured supplier catalogs.
        navigation = "".join('<a href="/category/' + str(i) + '/">Раздел ' + str(i) + '</a>' for i in range(100))
        raw = ('<header>' + navigation + '</header><h1>Осциллографы</h1><p>Сортировать по цене</p>'
               '<div class="product-item"><a href="/catalog/qa-4131/">QA-4131 — осциллограф</a>Цена QA 100 RUB</div>'
               '<div class="product-item"><a href="/catalog/qa-4132/">QA-4132 — осциллограф</a></div>'
               '<div class="product-item"><a href="https://outside.example/product/qa-4133/">QA-4133</a></div>').encode()
        acq = Acquisition(["supplier.example"])
        with patch.object(acq, "_request", return_value=response(body=raw)):
            document = acq.open("https://supplier.example/catalog/oscilloscopes/")
        self.assertEqual(document["page_kind"], "listing")
        self.assertTrue(document["page_kind_reason"])
        self.assertEqual([link["url"] for link in document["product_links"]], ["https://supplier.example/catalog/qa-4131/", "https://supplier.example/catalog/qa-4132/"])
        self.assertEqual(document["links"][:2], document["product_links"])
        self.assertEqual(len(document["links"]), 80)

    def test_catalog_path_specific_h1_remains_product_with_recommendations(self):
        raw = ('<title>Каталог осциллографов QA</title><h1>QA-4131 — цифровой осциллограф</h1>'
               '<p>Цена QA 100 RUB за шт.</p><h2>Похожие товары</h2>'
               '<div class="product-item"><a href="/catalog/qa-4132/">QA-4132</a></div>').encode()
        acq = Acquisition()
        with patch.object(acq, "_request", return_value=response(body=raw)):
            document = acq.open("https://supplier.example/catalog/qa-4131/")
        self.assertEqual(document["page_kind"], "product")
        self.assertEqual(document["primary_title"], "QA-4131 — цифровой осциллограф")

    def test_raw_html_hash_and_full_text_are_preserved(self):
        raw = '<html><title>QA page</title><body><h1>QA fixture product</h1><p>Цена 100 ₽</p><script>secret instructions</script><a href="/detail">Описание</a><a href="http://127.0.0.1/">local</a></body></html>'.encode()
        acq = Acquisition()
        with patch.object(acq, "_request", return_value=response(body=raw)):
            doc = acq.open("https://supplier.example/product")
        self.assertEqual(doc["sha256"], hashlib.sha256(raw).hexdigest())
        self.assertEqual(doc["html"], raw.decode())
        self.assertIn("Цена 100 ₽", doc["text"])
        self.assertNotIn("secret instructions", doc["text"])
        self.assertEqual(doc["links"], [{"title": "Описание", "url": "https://supplier.example/detail"}])
        self.assertEqual(doc["method"], "http")
        self.assertEqual(doc["title"], "QA fixture product")
        self.assertEqual(doc["primary_title"], "QA fixture product")
        self.assertEqual(doc["seo_title"], "QA page")
        self.assertTrue(doc["untrusted"])
        self.assertTrue(doc["retrieved_at"])

    def test_nontext_and_http_error_cannot_become_a_document(self):
        for item in [response(mime="image/png"), response(status=403)]:
            acq = Acquisition()
            with patch.object(acq, "_request", return_value=item), self.assertRaises(AcquisitionError):
                acq.open("https://supplier.example/product")

    def test_search_decodes_public_redirect_and_limits_hits(self):
        raw = b'<a class="result-link" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fsupplier.example%2Fproduct">QA fixture product</a><td class="result-snippet">Price information</td><a class="result-link" href="https://other.example/product">Other fixture</a>'
        acq = Acquisition()
        with patch.object(acq, "_request", return_value=response(url="https://lite.duckduckgo.com/lite/?q=fixture", body=raw)), patch("quotelens.acquisition._public_addresses", return_value=["93.184.216.34"]):
            result = acq.search("QA fixture", 1)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["hits"], [{"title": "QA fixture product", "url": "https://supplier.example/product", "snippet": "Price information"}])
        self.assertEqual(result["engine"], "duckduckgo-lite")
        self.assertIn("html", result["document"])

    def test_search_scopes_results_and_never_returns_local_candidate(self):
        raw = b'<a class="result-link" href="https://127.0.0.1/private">local fixture</a><a class="result-link" href="https://outside.example/product">outside fixture</a><a class="result-link" href="https://supplier.example/product">allowed fixture</a>'
        acq = Acquisition(["supplier.example"])
        with patch.object(acq, "_request", return_value=response(url="https://lite.duckduckgo.com/lite/", body=raw)) as fetch, patch("quotelens.acquisition._public_addresses", return_value=["93.184.216.34"]):
            result = acq.search("QA fixture")
        self.assertEqual([hit["url"] for hit in result["hits"]], ["https://supplier.example/product"])
        self.assertIn("site%3Asupplier.example", fetch.call_args.args[0])

    def test_captcha_is_explicit_unavailable_not_empty_or_synthetic(self):
        acq = Acquisition()
        with patch.object(acq, "_request", return_value=response(body=b'<form id="challenge-form">CAPTCHA</form>')):
            result = acq.search("QA fixture")
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["hits"], [])
        self.assertIn("CAPTCHA", result["reason"])


class _WireResponse:
    def __init__(self, status, body=b"", headers=()):
        self.status, self.body = status, io.BytesIO(body)
        self.headers = headers

    def getheaders(self):
        return self.headers

    def read1(self, limit):
        return self.body.read(limit)


class WireTransportTests(unittest.TestCase):
    def test_absolute_deadline_interrupts_slow_headers(self):
        expired = threading.Event()
        sock = Mock()
        sock.shutdown.side_effect = lambda _: expired.set()
        conn = Mock(sock=sock)

        def slow_headers():
            if expired.wait(2):
                raise OSError("QA fixture connection closed by deadline")
            raise AssertionError("deadline did not close the connection")

        conn.getresponse.side_effect = slow_headers
        acq = Acquisition()
        began = time.monotonic()
        with patch.object(acq, "_check_robots"), patch("quotelens.acquisition._public_addresses", return_value=["93.184.216.34"]), patch("quotelens.acquisition._PinnedHTTPS", return_value=conn), self.assertRaises(AcquisitionError):
            acq._request("https://supplier.example/product", began + .15)
        self.assertTrue(expired.is_set())
        self.assertLess(time.monotonic() - began, 1)

    def test_redirect_to_private_is_checked_before_second_connection(self):
        acq = Acquisition()
        conn = Mock()
        conn.getresponse.return_value = _WireResponse(302, headers=[("Location", "https://127.0.0.1/secret")])

        def addresses(host, _deadline):
            if host == "127.0.0.1":
                raise AcquisitionError("private destination")
            return ["93.184.216.34"]

        with patch.object(acq, "_check_robots"), patch("quotelens.acquisition._public_addresses", side_effect=addresses), patch("quotelens.acquisition._PinnedHTTPS", return_value=conn) as connection, self.assertRaises(AcquisitionError):
            acq._request("https://supplier.example/product", time.monotonic() + 3)
        self.assertEqual(connection.call_count, 1)

    def test_redirect_outside_scope_is_denied(self):
        acq = Acquisition(["supplier.example"])
        conn = Mock()
        conn.getresponse.return_value = _WireResponse(302, headers=[("Location", "https://outside.example/")])
        with patch.object(acq, "_check_robots"), patch("quotelens.acquisition._public_addresses", return_value=["93.184.216.34"]), patch("quotelens.acquisition._PinnedHTTPS", return_value=conn), self.assertRaises(AcquisitionError):
            acq._request("https://supplier.example/product", time.monotonic() + 3)

    def test_stream_body_cannot_exceed_byte_limit(self):
        acq = Acquisition()
        conn = Mock()
        conn.getresponse.return_value = _WireResponse(200, b"x" * 101)
        with patch.object(acq, "_check_robots"), patch("quotelens.acquisition._public_addresses", return_value=["93.184.216.34"]), patch("quotelens.acquisition._PinnedHTTPS", return_value=conn), self.assertRaises(AcquisitionError):
            acq._request("https://supplier.example/product", time.monotonic() + 3, max_bytes=100)


class RealBrowserFixtureTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("google-chrome") or shutil.which("chromium") or os.getenv("BROWSER_EXECUTABLE_PATH"), "Optional real Chromium QA needs a browser executable")
    def test_primary_h1_is_offer_title_and_seo_title_is_separate(self):
        # Explicitly synthetic QA page: SEO text is not quoted product evidence.
        raw = """<html><title>Купить осциллограф QA-4131 в QA магазине</title>
        <body><h1>  QA-4131/1В  - <span>Осциллограф цифровой</span> </h1>
        <p>Цена 100 RUB за шт</p><h1>Похожие товары QA</h1></body></html>""".encode()
        acq = Acquisition(timeout=12)
        with patch.object(acq, "_request", return_value=response(body=raw)), patch.object(acq, "_check_robots"), patch("quotelens.acquisition._public_addresses", return_value=["93.184.216.34"]):
            document = acq.open("https://supplier.example/product", browser=True)
        expected = "QA-4131/1В - Осциллограф цифровой"
        self.assertEqual(document["title"], expected)
        self.assertEqual(document["primary_title"], expected)
        self.assertEqual(document["seo_title"], "Купить осциллограф QA-4131 в QA магазине")
        self.assertIn(expected, " ".join(document["text"].split()))
        self.assertNotIn("Похожие товары", document["title"])

    @unittest.skipUnless(shutil.which("google-chrome") or shutil.which("chromium") or os.getenv("BROWSER_EXECUTABLE_PATH"), "Optional real Chromium QA needs a browser executable")
    def test_infinite_javascript_has_absolute_deadline(self):
        acq = Acquisition(timeout=3)
        fixture = response(body=b"<body><h1>QA fixture</h1><script>while(true){}</script></body>")
        began = time.monotonic()
        with patch.object(acq, "_request", return_value=fixture), patch.object(acq, "_check_robots"), patch("quotelens.acquisition._public_addresses", return_value=["93.184.216.34"]), self.assertRaises(AcquisitionError):
            acq.open("https://supplier.example/product", browser=True)
        self.assertLess(time.monotonic() - began, 5)

    @unittest.skipUnless(shutil.which("google-chrome") or shutil.which("chromium") or os.getenv("BROWSER_EXECUTABLE_PATH"), "Optional real Chromium QA needs a browser executable")
    def test_real_javascript_runs_and_network_stays_read_only_and_public(self):
        # Explicitly synthetic public-source QA response, rendered by real Chromium.
        raw = b'''<html><title>QA browser fixture</title><body><h1>QA fixture product</h1><div id="price">Loading</div><script>
          setTimeout(()=>{document.getElementById('price').textContent='Price 321 RUB'},30);
          fetch('https://127.0.0.1/private').catch(()=>{});
          fetch('/write',{method:'POST',body:'forbidden QA write'}).catch(()=>{});
        </script></body></html>'''
        acq = Acquisition(timeout=12)
        requested = []

        def source(url, _deadline, **kwargs):
            # Unlike the real transport, this fixture explicitly checks URL policy.
            if "127.0.0.1" in url:
                raise AcquisitionError("private QA target denied")
            requested.append(url)
            return response(url=url, body=raw)

        with patch.object(acq, "_request", side_effect=source), patch.object(acq, "_check_robots"), patch("quotelens.acquisition._public_addresses", return_value=["93.184.216.34"]), patch.dict(os.environ, {"MODEL_API_KEY": "qa-secret-never-passed-to-browser"}):
            doc = acq.open("https://supplier.example/product", browser=True)
        self.assertEqual(doc["method"], "browser")
        self.assertIn("Price 321 RUB", doc["text"])
        self.assertNotIn("Loading", doc["text"])
        self.assertEqual(requested, ["https://supplier.example/product"])
        self.assertGreaterEqual(doc["network"]["blocked"], 2)
        self.assertNotIn("qa-secret", json.dumps(doc))


if __name__ == "__main__":
    unittest.main()
