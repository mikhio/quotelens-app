"""Concrete integration regressions from independent release review (local QA)."""
from __future__ import annotations
import copy
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from quotelens.agent import PolicyResearch
from quotelens.engine import parse_request
from quotelens.procurement import JobBudget,aggregate_results,request_for_item
from quotelens.reports import report_html
from quotelens.scenarios import load_scenario
from quotelens.selection import finalize
from quotelens.server import prepare_input,validate_payload
from quotelens.storage import Store
import test_procurement_api as apiqa


class IntegrationRegressionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.s=Store(Path(self.temp.name)/'qa.sqlite3')
    def test_flattened_ids_leave_native_item_evidence_namespace_intact(self):
        items=[{'id':'first','title':'QA осциллограф','description':'Осциллограф 4 канала 100 МГц','quantity':2,'unit':'шт'},
               {'id':'second','title':'QA осциллограф2','description':'Осциллограф 4 канала 100 МГц','quantity':3,'unit':'шт'}]
        originals={}
        for item in items:
            offers=copy.deepcopy(load_scenario('synthetic-comparable')['offers'])
            for offer in offers:
                offer['tax']='unknown'
                offer['title']='Осциллограф '+offer['title']
                offer['evidence']['currency']=dict(offer['evidence']['price'])
                offer['evidence']['unit']=dict(offer['evidence']['price'])
                offer['evidence'].pop('tax',None)
            originals[item['id']]=finalize(request_for_item(item),offers,{'min_suppliers':3})
        payload={'description':'QA явная синтетика','quantity':1,'items':items,'search_policy':{'mode':'offers','max_offers':100,'max_seconds':60}}
        result=aggregate_results(payload,items,originals,stop='source_exhausted')
        for item in result['items']:
            ids={o['id'] for o in item['offers']}
            provisional=item['calculation']['provisional']
            self.assertTrue(set(provisional['offer_ids'])<=ids)
            self.assertTrue(set(provisional['normalized_prices'])<=ids)
            self.assertFalse(any(identifier.startswith(item['id']+':') for identifier in ids))
        self.assertTrue(all(':' in o['id'] for o in result['offers']))
        self.assertEqual(originals['first']['offers'][0]['id'],result['items'][0]['offers'][0]['id'])
        html=report_html(result)
        self.assertIn('Расчёт:',html)
        self.assertIn('69 000',html.replace('\xa0',' '))
    def test_explicit_empty_requirements_are_preserved_through_model_configuration(self):
        item={'id':'paper','title':'QA бумага','description':'Бумага A4 80 г/м² 500 листов в пачке','quantity':50,'unit':'пачка','requirements':[]}
        request=request_for_item(item)
        self.assertEqual(request['requirements'],[])
        self.s.create_job({'id':'qa','status':'running','events':[]},{})
        budget=JobBudget({'mode':'offers','max_offers':100,'max_seconds':60},threading.Event())
        research=PolicyResearch(self.s,'qa',{'description':item['description'],'quantity':50,'item_id':'paper','structured_request':request},
                                budget.cancel,lambda *args:None,budget=budget)
        output=research.execute_tool('configure_request',{'proposal':{'requirements':[{'key':'format','value':'A4'}]}})
        self.assertEqual(output['request']['requirements'],[])
        self.assertIn('requirements_notice',output['request'])
    def test_short_text_preparation_exposes_known_requirements_before_user_edits(self):
        prepared=prepare_input('Бумага A4 80 ± 3 г/м² 500 листов в пачке')
        fields={r['key']:r for r in prepared['items'][0]['requirements']}
        self.assertEqual(fields['density_gsm']['value'],[77,83])
        self.assertEqual(fields['sheets_per_pack']['value'],500)
    def test_nested_history_hints_survive_restart_and_respect_prefixed_manual_exclusion(self):
        request=parse_request('Бумага A4 80 г/м² 500 листов в пачке')
        self.s.create_job({'id':'past','status':'complete','events':[]},{'mode':'live'})
        url='https://qa.invalid/paper';did=self.s.add_document('past',{'url':url,'title':'QA paper','text':'Цена 123 ₽','html':'<h1>QA paper</h1>'})
        doc=self.s.get_document('past',did)
        proof={'quote':'123 ₽','url':url,'document_id':did,'source_hash':doc['sha256']}
        offer={'id':'native','title':'QA бумага','url':url,'price':123,'status':'uncertain','provenance':'live','evidence':{'price':proof}}
        result={'request':{'category':'procurement'},'items':[{'id':'paper','request':request,'offers':[offer]}],
                'offers':[{**offer,'id':'paper:native'}],'review':{'excluded_ids':[]}}
        self.s.save_result('past',result)
        reopened=Store(self.s.path)
        hints=reopened.discovery_urls(request)
        self.assertEqual([h['url'] for h in hints],[url])
        self.assertEqual(set(hints[0]),{'url','title','source_job_id'})
        result['review']['excluded_ids']=['paper:native'];reopened.save_result('past',result)
        self.assertEqual(reopened.discovery_urls(request),[])
        self.assertEqual(reopened.quota_usage('model_calls',period='all'),0)
    def test_document_cache_is_shared_bounded_and_saved_old_text_can_be_read(self):
        self.s.create_job({'id':'qa','status':'running','events':[]},{})
        event=threading.Event();budget=JobBudget({'mode':'offers','max_offers':100,'max_seconds':60},event)
        item={'id':'one','title':'QA осциллограф','description':'Осциллограф','quantity':1,'unit':'шт'}
        r=PolicyResearch(self.s,'qa',{'description':'Осциллограф','quantity':1,'item_id':'one','structured_request':request_for_item(item)},
                         event,lambda *args:None,budget=budget)
        ids=[]
        for i in range(20):ids.append(r.save_document({'url':f'https://qa.invalid/{i}','title':'QA page','text':f'QA saved text {i}','html':f'<h1>QA page{i}</h1>'}))
        self.assertLessEqual(len(r.documents),8)
        self.assertEqual(r.result()['search']['documents_count'],20)
        output=r.execute_tool('read_document',{'document_id':ids[0]})
        self.assertIn('QA saved text 0',output['text'])
        self.assertLessEqual(len(r.documents),8)
    def test_generated_edit_sources_fit_with_original_documents_but_keep_total_bound(self):
        value={'description':'QA закупка','items':[{'id':'x','title':'QA товар','description':'QA товар','quantity':1,'requirements':[]}],
               'input_documents':[{'id':str(i),'name':f'Правка{i}.txt','text':'QA source'} for i in range(12)]}
        parsed=validate_payload(value)
        self.assertEqual(len(parsed['input_documents']),12)
        value['input_documents']=[{'id':str(i),'name':f'Правка{i}.txt','text':'a'*200000} for i in range(6)]
        with self.assertRaises(ValueError):validate_payload(value)


class ParseAdmissionTests(unittest.TestCase):
    setUp = apiqa.ProcurementApiTests.setUp
    request = apiqa.ProcurementApiTests.request
    wait_job = apiqa.ProcurementApiTests.wait_job

    def test_prepare_and_direct_search_share_the_same_parser_gate(self):
        from quotelens import server
        entered=0;maximum=0;lock=threading.Lock();two_entered=threading.Event();release=threading.Event();responses=[]
        def parser(description='',files=None):
            nonlocal entered,maximum
            with lock:
                entered+=1;maximum=max(maximum,entered)
                if entered==2:two_entered.set()
            if not release.wait(5):raise RuntimeError('QA parser release missing')
            with lock:entered-=1
            return {'description':'QA parse','items':[{'id':'x','title':'QA товар','description':'QA товар','quantity':1,'requirements':[]}],
                    'documents':[{'id':'d','name':'qa.txt','text':'QA','sha256':'a'*64}],'warnings':[]}
        upload={'description':'','files':[{'name':'qa.txt','data':'UUE='}]}
        def send(path):responses.append(self.request(path,upload)[0])
        with patch.object(server,'PARSE_ACTIVE',threading.BoundedSemaphore(2)),patch('quotelens.spec_ingest.prepare_specifications',parser):
            threads=[threading.Thread(target=send,args=(path,)) for path in ['/api/prepare','/api/search']]
            for thread in threads:thread.start()
            self.assertTrue(two_entered.wait(3))
            status,body,_=self.request('/api/search',upload)
            self.assertEqual(status,429,body);self.assertEqual(maximum,2)
            release.set()
            for thread in threads:thread.join(timeout=6)
            self.assertEqual(sorted(responses),[200,202])
    def test_disabled_daily_limit_keeps_audit_counter_without_blocking(self):
        self.store.quota_reserve('search_runs',50,100)
        with patch.dict('os.environ',{'APP_SEARCH_RUNS_DAY':'0'}):
            status,body,_=self.request('/api/search',{'description':'QA синтетическая закупка','items':[{'id':'x','title':'QA осциллограф','description':'Осциллограф 4 канала 100 МГц','quantity':1}],
                       'search_policy':{'mode':'offers','max_offers':100,'max_seconds':60}})
            self.assertEqual(status,202,body);self.wait_job(body['id'])
        self.assertEqual(self.store.quota_usage('search_runs'),51)



class MultiCapturedRepairTests(unittest.TestCase):
    def test_repair_uses_line_bound_documents_preserves_sources_and_paid_usage(self):
        from quotelens.procurement import revalidate_captured_job
        from quotelens.extraction import extract_product_offer
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        store=Store(Path(temp.name)/'qa.sqlite3')
        items=[{'id':'one','title':'QA осциллограф A','description':'Осциллограф 4 канала 100 МГц','quantity':2,'unit':'шт'},
               {'id':'two','title':'QA осциллограф B','description':'Осциллограф 4 канала 100 МГц','quantity':3,'unit':'шт'}]
        payload={'description':'Явно синтетический QA replay','quantity':1,'items':items,'search_policy':{'mode':'offers','max_offers':100,'max_seconds':60}}
        store.create_job({'id':'qa','status':'complete','events':[]},{**payload,'mode':'live'})
        empty={i['id']:finalize(request_for_item(i),[],{'min_suppliers':3}) for i in items}
        original=aggregate_results(payload,items,empty,stop='time_budget')
        original['search']['budget']['actual_usage']={'prompt_tokens':100,'completion_tokens':20,'total_tokens':120,'cost':.01}
        original['search']['scenario_kind']='synthetic'
        store.save_result('qa',original)
        doc_ids=[]
        for item,price in zip(items,[100,200]):
            title='Осциллограф QA-'+str(1000+items.index(item))
            text=f'{title} Цена {price} руб. за шт. с НДС Число каналов: 4 Полоса пропускания 100 МГц В корзину'
            document={'url':'https://qa.invalid/'+item['id'],'title':title,'primary_title':title,'text':text,
                'html':f'<main class="product-detail"><h1>{title}</h1><div class="price">{price} руб. за шт. с НДС</div><p>Число каналов: 4 Полоса пропускания 100 МГц</p><button>В корзину</button></main>',
                'retrieved_at':'2026-11-12T00:00:00Z','method':'explicit-synthetic-QA','page_kind':'product'}
            did=store.add_document('qa',document);doc_ids.append(did)
            store.add_step('qa',{'kind':'automatic','name':'extract_product_offer','item_id':item['id'],'document_id':did})
        store.quota_reserve('model_calls',2,100,period='all')
        old_documents=store.list_documents('qa')
        repaired=revalidate_captured_job(store,store.get_job('qa'))
        self.assertEqual([len(i['offers']) for i in repaired['items']],[1,1])
        self.assertEqual([i['offers'][0]['price'] for i in repaired['items']],[100,200])
        self.assertEqual([i['offers'][0]['document_id'] for i in repaired['items']],doc_ids)
        self.assertEqual(repaired['search']['budget']['actual_usage']['cost'],.01)
        self.assertEqual(repaired['search']['stop']['code'],'time_budget')
        self.assertEqual(repaired['search']['source_revalidation']['network_calls'],0)
        self.assertEqual(repaired['search']['source_revalidation']['model_calls'],0)
        self.assertEqual(store.list_documents('qa'),old_documents)
        self.assertEqual(store.quota_usage('model_calls',period='all'),2)
        self.assertIsNone(repaired['calculation']['total'])
        self.assertEqual(repaired['calculation']['provisional']['total'],800)
        store.revalidate_result('qa',repaired,expected_revision=0,note='QA same captured inputs only')
        self.assertEqual(store.get_job('qa')['revision'],1)
        self.assertEqual(store.quota_usage('model_calls',period='all'),2)


if __name__=='__main__':unittest.main()
