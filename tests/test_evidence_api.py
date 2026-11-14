"""Explicit user requirements and commercial facts stay separate."""
import tempfile
import unittest
from pathlib import Path
from quotelens.server import annotate_decisions
from quotelens.storage import Store

class EvidenceDecisionTests(unittest.TestCase):
    def test_accessory_title_does_not_confirm_oscilloscope_type(self):
        for title in ['Щуп осциллограф P6100', 'Кабель осциллограф BNC-100']:
            result = {'request': {'description': 'осцилограф', 'quantity': 1}, 'offers': [{'title': title}]}
            annotate_decisions(result)
            self.assertNotEqual(result['offers'][0]['functional_status'], 'matches')
            self.assertEqual(result['offers'][0]['requirement_checks'], [])

    def test_broad_typo_query_does_not_invent_functional_constraints(self):
        result={'request':{'description':'осцилограф','quantity':1,'requirements':[]},'offers':[{'title':'Цифровой осциллограф Hantek DSO2C10','attributes':{'channels':2,'bandwidth_mhz':100},'price':21520,'unit':'шт','tax':'unknown','evidence':{'title':{'quote':'Цифровой осциллограф Hantek DSO2C10','url':'https://qa.invalid/product'}}}]}
        annotate_decisions(result);offer=result['offers'][0]
        self.assertEqual(offer['functional_status'],'matches');self.assertEqual(offer['functional_reasons'],[])
        self.assertEqual([c['key']for c in offer['requirement_checks']],['category'])
        self.assertEqual(offer['commercial_reasons'],['НДС не указан.'])
        self.assertEqual(offer['price'],21520)

    def test_only_confirmed_explicit_mismatch_is_functional_warning(self):
        req={'description':'Осциллограф 4 канала','quantity':1,'requirements':[{'key':'channels','label':'Число каналов','op':'gte','value':4,'unit':'канала'}]}
        offer={'title':'Осциллограф QA-2','attributes':{'channels':2},'price':100,'unit':'шт','tax':'unknown','evidence':{'attributes.channels':{'quote':'2 канала','url':'https://qa.invalid/product'}}}
        result={'request':req,'offers':[offer]};annotate_decisions(result)
        self.assertEqual(offer['functional_status'],'mismatch');self.assertEqual(len(offer['functional_reasons']),1)
        self.assertNotIn('НДС',offer['functional_reasons'][0]);self.assertEqual(offer['commercial_reasons'],['НДС не указан.'])
        offer['evidence'].clear();annotate_decisions(result);self.assertEqual(offer['functional_status'],'unconfirmed');self.assertEqual(offer['functional_reasons'],[])

    def test_live_progress_survives_final_job_save_without_changing_job_state(self):
        with tempfile.TemporaryDirectory()as d:
            s=Store(Path(d)/'qa.sqlite');job={'id':'qa','status':'running','events':[]};s.create_job(job,{'description':'QA','quantity':1,'mode':'live'})
            progress={'phase':'reading','sources':[{'domain':'qa.invalid','url':'https://qa.invalid/product','status':'visited','message':'QA controlled'}],'offers_count':1}
            s.set_search_progress('qa',progress);self.assertEqual(s.get_job('qa')['search_progress'],progress)
            s.save_job({**job,'status':'complete'});restored=Store(s.path).get_job('qa')
            self.assertEqual(restored['status'],'complete');self.assertEqual(restored['search_progress'],progress)
            self.assertEqual(s.quota_usage('model_calls',period='all'),0)

if __name__=='__main__':unittest.main()
