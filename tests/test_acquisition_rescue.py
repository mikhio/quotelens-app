"""Transport rescue checks; synthetic bytes only, no network or files."""
import gzip
import io
import json
import os
import socket
import ssl
import time
import unittest
from unittest.mock import Mock, patch
from quotelens.acquisition import Acquisition, AcquisitionError, _PinnedHTTPS, _public_addresses, _Response

class Wire:
    status=200
    def __init__(self,body,encoding):self.body=io.BytesIO(body);self.encoding=encoding
    def getheaders(self):return [('Content-Type','text/html; charset=utf-8'),('Content-Encoding',self.encoding)]
    def read1(self,n):return self.body.read(n)

class TransportRescueTests(unittest.TestCase):
    def read_wire(self,body,encoding,limit=1000):
        a=Acquisition();c=Mock();c.getresponse.return_value=Wire(body,encoding)
        with patch.object(a,'_check_robots'),patch('quotelens.acquisition._public_addresses',return_value=['93.184.216.34']),patch('quotelens.acquisition._PinnedHTTPS',return_value=c):
            return a._request('https://supplier.example/card',time.monotonic()+3,max_bytes=limit)
    def test_valid_gzip_is_decoded_without_losing_source_bytes(self):
        body='<h1>Учебная карточка</h1><p>100 рублей</p>'.encode()
        r=self.read_wire(gzip.compress(body),'gzip')
        self.assertEqual(r.body,body);self.assertNotIn('content-encoding',r.headers)
    def test_gzip_bomb_corruption_and_unknown_encoding_fail_closed(self):
        for body,encoding in [(gzip.compress(b'x'*10001),'gzip'),(b'invalid','gzip'),(b'bytes','br')]:
            with self.subTest(encoding=encoding),self.assertRaises(AcquisitionError):self.read_wire(body,encoding,limit=1000)
    def test_plain_bytes_still_obey_bound(self):
        with self.assertRaises(AcquisitionError):self.read_wire(b'x'*1001,'identity')
    def test_dns_timeout_is_specific_retryable_failure(self):
        with patch.dict(os.environ,{'APP_PUBLIC_DNS_RESOLVER':''}),patch('quotelens.acquisition.socket.getaddrinfo',side_effect=socket.gaierror('synthetic DNS failure')):
            with self.assertRaises(AcquisitionError) as e:_public_addresses('supplier.example',time.monotonic()+2)
        self.assertEqual(e.exception.code,'dns_resolution');self.assertTrue(e.exception.retryable)
    def test_fake_and_private_ips_remain_blocked(self):
        for ip in ['198.18.0.64','127.0.0.1','10.0.0.1']:
            record=[(socket.AF_INET,socket.SOCK_STREAM,6,'',(ip,443))]
            with patch('quotelens.acquisition.socket.getaddrinfo',return_value=record),self.assertRaises(AcquisitionError):
                _public_addresses('supplier.example',time.monotonic()+2)
    def test_http_source_failure_is_scoped_and_retryable_only_when_transient(self):
        a=Acquisition()
        for code,retry in [(403,False),(503,True)]:
            with patch.object(a,'_request',return_value=_Response('https://supplier.example/card',code,b'',{})),self.assertRaises(AcquisitionError) as e:a.open('https://supplier.example/card')
            self.assertEqual(e.exception.http_status,code);self.assertEqual(e.exception.retryable,retry)
    def test_tls_verification_and_hostname_binding_remain_enabled(self):
        connection=_PinnedHTTPS('supplier.example','93.184.216.34',1)
        self.assertEqual(connection._context.verify_mode,ssl.CERT_REQUIRED);self.assertTrue(connection._context.check_hostname)

if __name__=='__main__':unittest.main()
