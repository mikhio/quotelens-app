"""User stopping policy is exercised on synthetic local sources, never the market."""
from pathlib import Path
import copy
import tempfile
import threading
import unittest
from unittest.mock import patch

from quotelens.agent import BudgetStop
from quotelens.procurement import JobBudget, ProcurementResearch, normalize_search_policy
from quotelens.storage import Store


class FinishPlanner:
    configured, base, model = True, "https://qa.invalid", "synthetic-test-planner"
    def __init__(self): self.calls = 0
    def complete(self, messages, tools, **kwargs):
        self.calls += 1
        return {"message": {"role": "assistant", "content": None, "tool_calls": [{"id": "finish-"+str(self.calls), "type":"function",
            "function":{"name":"finish","arguments":'{"reason":"synthetic early finish"}'}}]},
            "usage":{"prompt_tokens":100,"completion_tokens":20,"total_tokens":120,"cost":.0001}, "finish_reason":"tool_calls"}


class SyntheticSources:
    def __init__(self, prefix="qa", count=105, on_open=None):
        self.prefix,self.count,self.on_open=prefix,count,on_open;self.opened=[];self.queries=[]
        self.hits=[{"url":f"https://shop-{i%5}.test/{prefix}-{1000+i}","title":f"Осциллограф QA-{prefix}-{1000+i}"} for i in range(count)]
    def search(self, query, limit=6):
        self.queries.append(query)
        return {"hits":self.hits,"status":"ok","query":query}
    def open(self,url,browser=False):
        if self.on_open:self.on_open(url)
        self.opened.append(url);name=url.rsplit('/',1)[1]
        title="Осциллограф QA-"+name
        text=f"{title} Цена: 10000 руб. за шт. с НДС Число каналов: 4 Полоса пропускания 100 МГц В корзину"
        html=f"<main class='product-detail'><h1>{title}</h1><div class='price'>10000 руб. за шт. с НДС</div><p>Число каналов: 4 Полоса пропускания 100 МГц</p><button>В корзину</button></main>"
        return {"url":url,"title":title,"primary_title":title,"text":text,"html":html,"links":[],"retrieved_at":"2026-11-12T00:00:00Z","method":"explicit-synthetic-QA"}


class SearchPolicyTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.store=Store(Path(self.tmp.name)/'qa.sqlite3');self.cancel=threading.Event()
        self.store.create_job({"id":"qa","status":"running","events":[]},{"mode":"live"})
    def payload(self, count=100, mode="offers", items=1):
        return {"description":"Синтетическая QA закупка","quantity":1,"mode":"live","limits":{"min_suppliers":3},
            "search_policy":{"mode":mode,"max_offers":count,"max_seconds":120},
            "items":[{"id":f"item-{i}","title":f"Осциллограф QA-{i}","description":"Осциллограф 4 канала полоса пропускания 100 МГц", "quantity":2,"unit":"шт"} for i in range(items)]}
    def test_one_hundred_unique_priced_offers_stop_without_legacy_six_offer_finish(self):
        sources=SyntheticSources();planner=FinishPlanner()
        research=ProcurementResearch(self.store,"qa",self.payload(),self.cancel,lambda *args:None,provider=planner,acquisition_factory=lambda _:sources)
        result=research.run()
        self.assertEqual(result['search']['stop']['code'],'offer_target')
        self.assertEqual(result['search']['coverage']['priced_offers'],100)
        self.assertEqual(len(result['offers']),100)
        self.assertEqual(len(sources.opened),100)
        self.assertEqual(planner.calls,1)
        self.assertTrue(result['search']['budget']['actual_cost_known'])
        self.assertEqual(result['search']['budget']['actual_usage']['cost'],.0001)
    def test_exhausted_sources_return_every_saved_offer_and_explicit_reason(self):
        sources=SyntheticSources(count=7)
        result=ProcurementResearch(self.store,'qa',self.payload(),self.cancel,lambda *args:None,
                 provider=FinishPlanner(),acquisition_factory=lambda _:sources).run()
        self.assertEqual(result['search']['stop']['code'],'source_exhausted')
        self.assertEqual(len(result['offers']),7)
        self.assertLess(len(sources.queries),30)
    def test_global_target_is_shared_and_lines_are_advanced_in_turn(self):
        sources={}
        def factory(item):
            source=SyntheticSources(prefix=item['id']);sources[item['id']]=source;return source
        result=ProcurementResearch(self.store,'qa',self.payload(items=2),self.cancel,lambda *args:None,
                 provider=FinishPlanner(),acquisition_factory=factory).run()
        self.assertEqual(result['search']['coverage']['priced_offers'],100)
        counts=[len(s.opened) for s in sources.values()]
        self.assertEqual(sum(counts),100)
        self.assertLessEqual(abs(counts[0]-counts[1]),1)
        self.assertEqual(len(result['items']),2)
        self.assertEqual(len({o['id'] for o in result['offers']}),100)
    def test_cancel_keeps_the_inflight_page_but_does_not_read_another(self):
        sources=SyntheticSources(on_open=lambda _:self.cancel.set())
        result=ProcurementResearch(self.store,'qa',self.payload(),self.cancel,lambda *args:None,
                 provider=FinishPlanner(),acquisition_factory=lambda _:sources).run()
        self.assertEqual(result['search']['stop']['code'],'cancelled')
        self.assertEqual(len(sources.opened),1)
        self.assertEqual(len(result['offers']),1)
    def test_time_mode_does_not_finish_at_the_count_target(self):
        sources=SyntheticSources(count=7)
        result=ProcurementResearch(self.store,'qa',self.payload(count=1,mode='time'),self.cancel,lambda *args:None,
                 provider=FinishPlanner(),acquisition_factory=lambda _:sources).run()
        self.assertEqual(result['search']['stop']['code'],'source_exhausted')
        self.assertEqual(len(result['offers']),7)
    def test_shared_time_deadline_preserves_partial_items_without_starting_next_line(self):
        holder = {}
        def expire(_):
            holder['research'].budget.started -= 121
        sources=SyntheticSources(on_open=expire)
        research=ProcurementResearch(self.store,'qa',self.payload(mode='time',items=2),self.cancel,lambda *args:None,
                 provider=FinishPlanner(),acquisition_factory=lambda _:sources)
        holder['research']=research
        result=research.run()
        self.assertEqual(result['search']['stop']['code'],'time_budget')
        self.assertEqual(len(sources.opened),1)
        self.assertEqual(len(result['offers']),1)
        self.assertIsNone(result['calculation']['total'])
        self.assertTrue(result['calculation']['missing_item_ids'])

    def test_budget_counts_canonical_card_once_across_lines(self):
        b=JobBudget({'mode':'offers','max_offers':2,'max_seconds':120},self.cancel)
        a={'title':'Осциллограф QA','supplier':'QA Seller','price':100,'url':'https://qa.test/product?utm_source=x','evidence':{'price':{'quote':'100 руб.'}}}
        b.remember('one',[a],lambda _:True)
        b.remember('two',[{**a,'url':'https://qa.test/product?utm_source=y#price'}],lambda _:True)
        self.assertEqual(b.count,1)
        a['attributes']={'model':'QA123'}
        b.remember('one',[a],lambda _:True)
        b.remember('two',[{**a,'url':'https://qa.test/alias'}],lambda _:True)
        self.assertEqual(b.count,1)
    def test_paid_call_quota_reservation_is_all_or_nothing(self):
        self.assertFalse(self.store.quota_reserve_many([('calls',1,10),('tokens',100,10)]))
        self.assertEqual(self.store.quota_usage('calls',period='all'),0)
        self.assertEqual(self.store.quota_usage('tokens',period='all'),0)
    def test_policy_rejects_unbounded_or_invalid_controls(self):
        for value in [{'mode':'forever'},{'max_seconds':0},{'max_offers':101},{'max_cost_usd':float('nan')},{'max_tokens':True}]:
            with self.assertRaises(ValueError):normalize_search_policy(value)

if __name__=='__main__':unittest.main()
