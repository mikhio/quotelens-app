"""Scope and source role checks; all offers/documents are explicit QA data."""
import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from quotelens.source_policy import normalize_scope, effective_scope, source_metadata, annotate_result, rebuild_saved_sources
from quotelens.server import validate_payload
from quotelens.storage import Store
from quotelens.selection import finalize
from quotelens.engine import parse_request

class SourcePolicyTests(unittest.TestCase):
    def test_scope_accepts_all_named_sources_and_remains_bounded(self):
        from quotelens.source_presets import CASE_SOURCES
        # All available case sources plus a manually entered site must fit.
        domains = [source[2] for source in CASE_SOURCES if source[2]]
        domains.append('custom-shop.ru')
        with patch('quotelens.server.validate_public_url', side_effect=lambda url, **kw: url):
            result = validate_payload({'description':'QA все источники', 'source_scope':{'mode':'domains', 'domains':domains}})
        self.assertEqual(result['source_scope']['domains'], list(dict.fromkeys(domains)))
        boundary = [f'site{i}.ru' for i in range(32)]
        self.assertEqual(normalize_scope({'mode':'domains','domains':boundary})['domains'], boundary)
        with self.assertRaises(ValueError):
            normalize_scope({'mode':'domains','domains':boundary + ['overflow.ru']})

    def test_scope_is_exclusive_canonical_and_legacy_compatible(self):
        self.assertEqual(normalize_scope({'mode':'domains','domains':['WWW.PRIST.RU','prist.ru','radioelementy.ru']}),{'mode':'domains','domains':['prist.ru','radioelementy.ru']})
        self.assertEqual(effective_scope({'source_ids':['web','prist']})['mode'],'web')
        self.assertEqual(effective_scope({'source_ids':['prist']})['domains'],['prist.ru'])
        self.assertEqual(effective_scope({'source_scope':{'mode':'domains','domains':['radioelementy.ru']}})['domains'],['radioelementy.ru'])

    def test_scope_rejects_credentials_private_literals_and_competing_inputs(self):
        for domain in ['https://prist.ru/path','user:pass@prist.ru','127.0.0.1','10.0.0.1','localhost','x.ru?key=secret']:
            with self.subTest(domain=domain),self.assertRaises(ValueError):normalize_scope({'mode':'domains','domains':[domain]})
        with self.assertRaises(ValueError):normalize_scope({'mode':'web','domains':['prist.ru']})
        with self.assertRaises(ValueError):validate_payload({'description':'QA осциллограф','source_ids':['web'],'source_scope':{'mode':'web'}})
        with patch('quotelens.server.validate_public_url',side_effect=lambda u,**kw:u)as verify:
            result=validate_payload({'description':'QA осциллограф','source_scope':{'mode':'domains','domains':['radioelementy.ru']}})
            self.assertEqual(result['source_scope']['domains'],['radioelementy.ru']);verify.assert_called_once_with('https://radioelementy.ru/',require_allowed=False)

    def test_grouping_never_promotes_a_shop_to_manufacturer_or_changes_money(self):
        self.assertEqual(source_metadata('rigol-shop.ru')['source_group'],'retail')
        self.assertEqual(source_metadata('rigol.com')['source_group'],'manufacturers')
        self.assertEqual(source_metadata('radioelementy.ru')['source_group'],source_metadata('prist.ru')['source_group'])
        self.assertEqual(source_metadata('unclassified.example')['source_group'],'other')
        self.assertEqual(source_metadata('zakupki.gov.ru')['source_group'],'public_records')
        data={'offers':[{'id':'qa','domain':'radioelementy.ru','price':100,'status':'uncertain'}],'calculation':{'total':None},'search':{'source_statuses':[{'domain':'radioelementy.ru'}]}}
        original=copy.deepcopy(data);annotate_result(data)
        self.assertEqual(data['calculation'],original['calculation']);self.assertEqual(data['offers'][0]['price'],100);self.assertEqual(data['offers'][0]['status'],'uncertain')
        self.assertEqual(data['search']['source_statuses'][0]['source_group'],'retail')

    def test_new_scope_survives_store_history_and_reopening(self):
        with tempfile.TemporaryDirectory()as d:
            path=Path(d)/'qa.sqlite';store=Store(path);payload={'mode':'live','description':'QA исходный','quantity':2,'source_scope':{'mode':'domains','domains':['radioelementy.ru']},'source_ids':None}
            store.create_job({'id':'qa','status':'running','events':[]},payload)
            reopened=Store(path)
            self.assertEqual(reopened.get_job('qa')['request']['source_scope'],payload['source_scope'])
            self.assertEqual(reopened.job_history()['items'][0]['request']['source_scope'],payload['source_scope'])

    def test_saved_catalog_is_removed_without_deleting_capture_or_history(self):
        with tempfile.TemporaryDirectory()as d:
            s=Store(Path(d)/'qa.sqlite');request=parse_request('Осциллограф 4 канала полоса 100 МГц',2)
            s.create_job({'id':'qa','status':'complete','events':[]},{'mode':'live','description':request['description'],'quantity':2})
            doc={'url':'https://qa.example/catalog/oscilloscopes/','title':'Осциллографы','primary_title':'Осциллографы','text':'Осциллографы Фильтр Сортировать Найдено: 25 товаров. QA-100 1000 ₽ QA-200 2000 ₽','html':'<h1>Осциллографы</h1><div>Сортировать Найдено 25 товаров</div><a href="/product/qa-100/">QA-100</a><a href="/product/qa-200/">QA-200</a>','links':[]}
            did=s.add_document('qa',doc)
            old=finalize(request,[{'id':'bad','title':'Осциллографы','domain':'qa.example','url':doc['url'],'price':1000,'document_id':did,'attributes':{},'evidence':{},'provenance':'synthetic'}])
            old['search']={'mode':'live_agent','source_statuses':[{'domain':'qa.example'}]};s.save_result('qa',old)
            job=s.get_job('qa');repair=rebuild_saved_sources(s,job);self.assertIsNotNone(repair);self.assertEqual(repair['result']['offers'],[])
            self.assertEqual(repair['removed_offer_ids'],['bad']);self.assertEqual(len(repair['result']['discovery_pages']),1)
            revision=s.revalidate_result('qa',repair['result'],expected_revision=0,note='QA rule correction')
            self.assertEqual(revision,1);self.assertEqual(s.get_job('qa')['result']['offers'],[])
            self.assertEqual(s.get_document('qa',did)['text'],doc['text']);self.assertEqual(s.job_history()['total'],1)
            with s._connection()as db:
                audit=db.execute("select step_json from steps where kind='source_revalidation'").fetchone()[0]
                self.assertIn('before_result',audit);self.assertIn('1000',audit)
            with self.assertRaises(ValueError):s.revalidate_result('qa',repair['result'],expected_revision=0,note='stale revision')

if __name__=='__main__':unittest.main()
