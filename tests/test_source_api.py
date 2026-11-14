"""Source scope and catalog-tool regressions, no model or public network calls."""
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from quotelens.agent import Research
from quotelens.acquisition import AcquisitionError
from quotelens.storage import Store
from tests.test_history_api import HistoryServerFixture

class SourceApiTests(unittest.TestCase):
    def test_snapshot_scope_filters_actual_backend_candidates_and_persists_metadata(self):
        fixture=HistoryServerFixture();self.addCleanup(fixture.close)
        payload={'description':'Осциллограф 4 канала полоса 100 МГц','quantity':3,'mode':'snapshot','scenario_id':'real-oscilloscopes','source_scope':{'mode':'domains','domains':['souz-pribor.ru']}}
        status,started,_=fixture.request('/api/search',payload);self.assertEqual(status,202)
        fixture.wait(lambda: fixture.db.get_job(started['id'])['status']!='running')
        job=fixture.db.get_job(started['id']);self.assertEqual(job['status'],'complete')
        self.assertTrue(job['result']['offers']);self.assertTrue(all(o['domain']=='souz-pribor.ru'for o in job['result']['offers']))
        self.assertEqual(job['request']['source_scope'],payload['source_scope'])
        self.assertEqual(fixture.db.quota_usage('search_runs'),0)

    def test_domains_scope_is_enforced_by_network_tool_not_just_sent_to_model(self):
        with tempfile.TemporaryDirectory()as d:
            store=Store(Path(d)/'qa.sqlite');store.create_job({'id':'qa','status':'running','events':[]},{'mode':'live'})
            research=Research(store,'qa',{'description':'QA осциллограф','quantity':1,'source_scope':{'mode':'domains','domains':['radioelementy.ru']}},threading.Event(),lambda*a:None)
            self.assertEqual(research.acquisition.allowed_domains,{'radioelementy.ru'})
            with patch.object(research.acquisition,'_request')as network:
                with self.assertRaises(AcquisitionError):research.acquisition.open('https://outside.example/product/QA-100/')
                network.assert_not_called()
            self.assertEqual(store.quota_usage('model_calls',period='all'),0)

    def test_catalog_feedback_provides_real_detail_links_without_recording_price(self):
        with tempfile.TemporaryDirectory()as d:
            store=Store(Path(d)/'qa.sqlite');store.create_job({'id':'qa','status':'running','events':[]},{'mode':'live'})
            research=Research(store,'qa',{'description':'QA осциллограф','quantity':1},threading.Event(),lambda*a:None)
            document={'url':'https://merchant.example/catalog/oscilloscopes/','title':'Осциллографы','text':'Осциллографы Фильтр Сортировать Найдено: 30 товаров QA-100 1000 ₽ QA-200 2000 ₽','html':'<h1>Осциллографы</h1><div>Фильтр Сортировать Найдено30 товаров</div><a href="/product/QA-100/">QA-100</a><a href="/product/QA-200/">QA-200</a>','links':[]}
            docid=research.save_document(document)
            response=research.execute_tool('record_offer',{'document_id':docid,'offer':{'price':1000,'title':'Осциллографы'}})
            self.assertEqual(response['page_kind'],'listing');self.assertEqual(research.offers,[])
            self.assertTrue(response['product_links']);self.assertTrue(all(link['url'] in research.discovered for link in response['product_links']))
            self.assertEqual(len(store.list_documents('qa')),1);self.assertEqual(store.quota_usage('model_calls',period='all'),0)

if __name__=='__main__':unittest.main()
