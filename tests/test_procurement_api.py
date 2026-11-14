"""Real HTTP upload, background persistence, review and exports on isolated QA data."""
from __future__ import annotations
import base64
import copy
from http.server import ThreadingHTTPServer
from io import BytesIO
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request,urlopen
from zipfile import ZipFile

from quotelens import server
from quotelens.engine import parse_request
from quotelens.procurement import aggregate_results,request_for_item
from quotelens.scenarios import load_scenario
from quotelens.selection import finalize
from quotelens.storage import Store

FIXTURES=Path(__file__).parent/'fixtures/specifications'

class SyntheticProcurement:
    def __init__(self,store,job_id,payload,cancel,event):
        self.store,self.id,self.payload=store,job_id,payload
    def run(self):
        results={}
        for item in self.payload['items']:
            offers=copy.deepcopy(load_scenario('synthetic-comparable')['offers'])
            results[item['id']]=finalize(request_for_item(item),offers,{'min_suppliers':2})
            results[item['id']]['search']={'budget':{'model_calls':0,'actual_usage':{'cost':0}},'source_statuses':[]}
        result=aggregate_results(self.payload,self.payload['items'],results,stop='source_exhausted')
        result['search']['scenario_kind']='synthetic'
        return result

class ProcurementApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.store=Store(Path(self.tmp.name)/'quotelens.sqlite3')
        self.patches=[patch.object(server,'DATA_DIR',Path(self.tmp.name)),patch.object(server,'_STORE',self.store),
                      patch.object(server,'JOBS',{}),patch.object(server,'ACTIVE',threading.BoundedSemaphore(2)),
                      patch('quotelens.procurement.ProcurementResearch',SyntheticProcurement)]
        for p in self.patches:p.start();self.addCleanup(p.stop)
        self.http=ThreadingHTTPServer(('127.0.0.1',0),server.Handler)
        self.thread=threading.Thread(target=self.http.serve_forever,daemon=True);self.thread.start()
        self.base='http://127.0.0.1:'+str(self.http.server_address[1])
        self.addCleanup(self.http.server_close);self.addCleanup(self.http.shutdown)
    def request(self,path,payload=None):
        data=json.dumps(payload,ensure_ascii=False).encode() if payload is not None else None
        request=Request(self.base+path,data=data,headers={'Content-Type':'application/json'} if data else {})
        try:r=urlopen(request,timeout=25)
        except HTTPError as e:r=e
        with r:
            raw=r.read();mime=r.headers.get('Content-Type','')
            return r.status,json.loads(raw) if 'application/json' in mime else raw,dict(r.headers)
    def wait_job(self,id):
        end=time.monotonic()+10
        while time.monotonic()<end:
            status,job,_=self.request('/api/jobs/'+id)
            if job['status']!='running':return job
            time.sleep(.01)
        self.fail('QA background job did not finish')
    @staticmethod
    def files(names):
        return [{'name':n,'data':base64.b64encode((FIXTURES/n).read_bytes()).decode()} for n in names]
    def test_multiple_real_docx_uploads_prepare_nine_exact_items_without_paid_calls(self):
        status,v,_=self.request('/api/prepare',{'description':'','files':self.files(['printer.docx','paper.docx','cytokines.docx'])})
        self.assertEqual(status,200,v)
        self.assertEqual(len(v['items']),9)
        self.assertEqual([i['quantity'] for i in v['items']],[1,2500,25,50,2,1,1,2,1])
        self.assertEqual(len(v['documents']),3)
        self.assertTrue(all(d['sha256'] and d['text'] for d in v['documents']))
        self.assertTrue(any('IL-2' in w for w in v['warnings']))
        self.assertEqual(self.store.quota_usage('model_calls',period='all'),0)
        self.assertEqual(self.store.quota_usage('search_runs'),0)
    def test_edited_item_quantities_and_sources_survive_history_restore(self):
        status,v,_=self.request('/api/prepare',{'description':'','files':self.files(['paper.docx'])})
        self.assertEqual(status,200)
        v['items'][0]['quantity']=123
        status,start,_=self.request('/api/search',{'description':v['description'],'quantity':1,'items':v['items'],
                         'input_documents':v['documents'],'search_policy':{'mode':'offers','max_offers':100,'max_seconds':60}})
        self.assertEqual(status,202,start)
        job=self.wait_job(start['id'])
        self.assertEqual(job['status'],'complete',job)
        self.assertEqual(job['request']['items'][0]['quantity'],123)
        self.assertEqual(len(job['result']['items']),3)
        self.assertEqual(job['request']['input_documents'][0]['name'],'paper.docx')
        self.assertNotIn('text',job['request']['input_documents'][0])
        self.assertEqual(self.store.quota_usage('search_runs'),1)
        # Restarting the Store or restoring by ID does not create paid work.
        reopened=Store(self.store.path).get_job(start['id'])
        self.assertEqual(reopened['request']['items'][0]['quantity'],123)
        self.assertEqual(reopened['request']['search_policy']['max_offers'],100)
    def test_real_http_current_exports_and_review_recalculate_same_saved_result(self):
        item={'id':'osc','title':'QA осциллограф','description':'Осциллограф 4 канала полоса пропускания 100 МГц','quantity':3,'unit':'шт'}
        status,start,_=self.request('/api/search',{'description':'Явно синтетический QA','items':[item],'quantity':1,
                    'search_policy':{'mode':'offers','max_offers':100,'max_seconds':60}})
        self.assertEqual(status,202,start)
        job=self.wait_job(start['id']);self.assertEqual(job['status'],'complete',job)
        prefix='/api/jobs/'+start['id']
        status,docx,headers=self.request(prefix+'/report.docx')
        self.assertEqual(status,200)
        with ZipFile(BytesIO(docx)) as z:xml=z.read('word/document.xml').decode()
        self.assertIn('осциллограф',xml.lower());self.assertNotIn('review-widget',xml)
        self.assertIn('attachment',headers.get('Content-Disposition',''))
        status,pdf,_=self.request(prefix+'/report.pdf')
        self.assertEqual(status,200);self.assertTrue(pdf.startswith(b'%PDF-'))
        status,html,_=self.request(prefix+'/report.html')
        self.assertEqual(status,200);self.assertNotIn(b'public-feedback-widget',html)
        offer=next(o for o in job['result']['offers'] if o.get('status')=='included')
        status,changed,_=self.request(prefix+'/review',{'offer_id':offer['id'],'action':'exclude','reason':'Синтетическая QA проверка'})
        self.assertEqual(status,200,changed)
        self.assertEqual(changed['revision'],1)
        found=next(o for o in changed['result']['offers'] if o['id']==offer['id'])
        self.assertTrue(found['manual_excluded'])
        status,reset,_=self.request(prefix+'/review',{'offer_id':offer['id'],'action':'reset'})
        self.assertEqual(status,200);self.assertEqual(reset['revision'],2)
        self.assertFalse(next(o for o in reset['result']['offers'] if o['id']==offer['id'])['manual_excluded'])
    def test_invalid_upload_and_unknown_quantity_are_clear_errors(self):
        status,v,_=self.request('/api/prepare',{'files':[{'name':'../bad.docx','data':'YWJj'}]})
        self.assertEqual(status,400);self.assertIn('error',v)
        status,v,_=self.request('/api/search',{'description':'QA товар','items':[{'id':'x','title':'QA товар','description':'QA товар','quantity':None}],
                    'search_policy':{'mode':'offers','max_offers':100,'max_seconds':60}})
        self.assertEqual(status,400);self.assertIn('количеств',v['error'].lower())

if __name__=='__main__':unittest.main()
