"""History supplies URLs only; all observations must be fetched again."""
import tempfile
import unittest
from pathlib import Path
from quotelens.storage import Store
from quotelens.engine import parse_request

class WarmDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.s=Store(Path(self.tmp.name)/'qa.sqlite')
    def add(self,identifier,domain='qa.invalid',description='осциллограф',mode='live',valid=True,excluded=False):
        job={'id':identifier,'status':'complete','events':[]};self.s.create_job(job,{'description':description,'quantity':1,'mode':mode})
        url='https://'+domain+'/'+identifier;docid=self.s.add_document(identifier,{'url':url,'title':'QA осциллограф','text':'QA цена 123 ₽','html':'<h1>QA осциллограф</h1>'});d=self.s.get_document(identifier,docid)
        proof={'quote':'123 ₽','url':url,'document_id':docid,'source_hash':d['sha256'] if valid else 'wrong-hash'}
        o={'id':identifier,'url':url,'title':'QA осциллограф','price':123,'status':'uncertain','provenance':'live','evidence':{'price':proof},'attributes':{'channels':99}}
        self.s.save_result(identifier,{'request':parse_request(description),'offers':[o],'review':{'excluded_ids':[identifier]if excluded else []}})
        return url
    def test_round_robin_hints_have_no_prior_prices_or_attributes(self):
        for domain in ['a.invalid','b.invalid','c.invalid']:
            for i in range(4):self.add(domain+str(i),domain=domain)
        hints=self.s.discovery_urls(parse_request('осцилограф'),limit=9)
        self.assertEqual(len(hints),9);self.assertEqual(len({h['url'].split('/')[2]for h in hints[:3]}),3)
        self.assertTrue(all(set(h)=={'url','title','source_job_id'}for h in hints))
        self.assertTrue(all(sum(h['url'].split('/')[2]==d for h in hints)==3 for d in ['a.invalid','b.invalid','c.invalid']))
    def test_snapshot_unbound_hash_and_user_exclusion_do_not_supply_hints(self):
        self.add('snapshot',mode='snapshot');self.add('unbound',valid=False);self.add('excluded',excluded=True);url=self.add('live')
        self.assertEqual([h['url']for h in self.s.discovery_urls(parse_request('осциллограф'))],[url])
    def test_explicit_requirements_must_match_and_quantity_is_not_copied(self):
        self.add('four',description='Осциллограф 4 канала 100 МГц');url=self.add('two',description='Осциллограф 2 канала 100 МГц')
        hints=self.s.discovery_urls(parse_request('Осциллограф 2 канала 100 МГц',quantity=10))
        self.assertEqual([h['url']for h in hints],[url]);self.assertNotIn('quantity',hints[0])
    def test_unknown_categories_require_same_description(self):
        self.add('kettle',description='Чайник QA');self.assertEqual(self.s.discovery_urls(parse_request('Стул QA')),[])
    def test_hints_survive_store_restart_without_changing_quota(self):
        url=self.add('live');self.assertEqual(Store(self.s.path).discovery_urls(parse_request('осцилограф'))[0]['url'],url)
        self.assertEqual(self.s.quota_usage('model_calls',period='all'),0)

if __name__=='__main__':unittest.main()
