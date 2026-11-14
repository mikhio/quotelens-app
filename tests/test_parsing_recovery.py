"""Actual public captures and controlled comparison/capture boundaries."""
from copy import deepcopy
import hashlib
import unittest
from tests.test_paper_attribute_recovery import capture,document
from quotelens.engine import _requirement_match,_attribute_match,parse_request,condition_specs
from quotelens.extraction import extract_product_offer
from quotelens.selection import finalize,validate_candidate
from quotelens.source_documents import classify_page
from quotelens.review import field_specs
from quotelens.agent import Research
from quotelens.storage import Store
import threading


def paper_request():
    return {'description':'QA paper A3','category':'paper','product':'Бумага A3','quantity':25,'unit':'пачка','missing_critical_fields':[],
            'requirements':[{'key':key,'label':label,'value':value,'op':op,'unit':unit} for key,label,value,op,unit in [
                ('manufacturer','Производитель','Synthetic maker','eq',None),('country','Страна происхождения','Россия','eq',None),
                ('format','Формат бумаги','А3','eq',None),('paper_grade','Марка бумаги','С','eq',None),
                ('sheets_per_pack','Количество листов в пачке',[490,510],'between','листов'),
                ('color','Цвет','белый','eq',None),('whiteness_cie','Белизна CIE',[143,149],'between','%'),
                ('density_gsm','Масса листа бумаги площадью 1 м2',[77,83],'between','г/м²'),('thickness_um','Толщина',[102,106],'between','мкм')]]}


class SourceRecoveryIntegration(unittest.TestCase):
    def test_six_primary_prices_and_actual_units_survive_full_validation(self):
        prices={'offmar':740,'ofisshop':791,'liner':868,'office':1061,'diapazon':808.78,'kancmag':895.32}
        for name,price in prices.items():
            with self.subTest(name=name):
                source=capture(name);offer=extract_product_offer(source,paper_request())
                self.assertIsNotNone(offer);self.assertEqual(offer['price'],price);self.assertEqual(offer['currency'],'RUB')
                self.assertEqual(offer['source_hash'],source['sha256'])
                self.assertEqual(offer['unit'],'шт' if name in {'liner','office','diapazon'}else None)
                self.assertEqual(offer['tax'],'unknown')
        stock=extract_product_offer(capture('liner'),paper_request())
        self.assertFalse(stock.get('pricing_conditions'))
        tier=extract_product_offer(capture('office'),paper_request())
        self.assertTrue(tier['pricing_conditions']);self.assertIsNone(tier['pricing_conditions'][0]['applies'])
        # An observed baseprice is retained even when its quantity unit is not
        # comparable to the request. It does not become a ready calculation.
        self.assertIsNone(finalize(paper_request(),[tier],{'min_suppliers':1})['calculation']['total'])
        forged=deepcopy(tier);forged['price']=80;forged['evidence']['price']['quote']='80₽'
        self.assertIsNone(validate_candidate(forged,capture('office'),paper_request())['price'])

    def test_ofisshop_observed_ranges_compare_without_target_copy(self):
        source=capture('ofisshop');request=paper_request();before=deepcopy(source)
        o=extract_product_offer(source,request)
        self.assertEqual(o['attributes']['density_gsm'],[77,83]);self.assertEqual(o['attributes']['thickness_um'],[102,106])
        self.assertEqual(o['attributes']['whiteness_cie'],[143,149]);self.assertEqual(o['attributes']['paper_grade'],'C')
        req=next(r for r in request['requirements']if r['key']=='density_gsm');self.assertTrue(_attribute_match(o,req))
        req['value']=[79,81];self.assertIsNone(_attribute_match(o,req))
        req['value']=[90,100];self.assertFalse(_attribute_match(o,req));self.assertEqual(source,before)
        self.assertEqual(extract_product_offer(source,request)['attributes']['density_gsm'],[77,83])

    def test_captured_inactive_tab_is_explicit_html_proof_not_changed_body_text(self):
        source=capture('kancmag');before=deepcopy(source);o=extract_product_offer(source,paper_request())
        proof=o['evidence']['attributes.thickness_um'];self.assertEqual(proof['visibility'],'reachable_tab')
        self.assertEqual(proof['basis'],'primary_product_dom_attribute');self.assertIn(proof['tab_id'],source['html'])
        self.assertEqual(o['attributes']['thickness_um'],[102,106]);self.assertEqual(source,before)
        forged=deepcopy(o);forged['attributes']['thickness_um']=[1,2]
        self.assertNotIn('thickness_um',validate_candidate(forged,source,paper_request())['attributes'])

    def test_unknown_unit_is_kept_visible_and_not_assumed_from_requested_unit(self):
        o=extract_product_offer(capture('diapazon'),paper_request());req=paper_request()['requirements'][-1]
        self.assertEqual(o['attributes']['thickness_um'],[102,106]);self.assertEqual(o['evidence']['attributes.thickness_um']['unit_reason'],'source_unit_not_stated')
        self.assertIsNone(_attribute_match(o,req))

    def test_alphabet_equivalence_is_narrow_and_partial_intervals_remain_uncertain(self):
        self.assertTrue(_requirement_match('C','С','eq','paper_grade'))
        self.assertFalse(_requirement_match('A','С','eq','paper_grade'))
        self.assertFalse(_requirement_match('C','С','eq','manufacturer'))
        self.assertTrue(_requirement_match([143,149],[143,149],'between','any'))
        self.assertIsNone(_requirement_match([137,153],[143,149],'between','any'))
        self.assertFalse(_requirement_match([160,170],[143,149],'between','any'))

    def test_duplicate_contract_keys_produce_separate_review_identities(self):
        r={'conditions':[{'key':'warranty','text':'Only new goods'},{'key':'warranty','text':'Warranty at least twelve months'}]}
        ids=[key for _,key,_ in condition_specs(r)];self.assertEqual(len(set(ids)),2)
        self.assertEqual(ids,[key for _,key,_ in condition_specs(deepcopy(r))])
        self.assertEqual(set(field_specs(r))&{'conditions.'+key for key in ids},{'conditions.'+key for key in ids})
        self.assertNotIn('conditions.warranty',field_specs(r))

    def test_category_first_tile_does_not_override_listing_controls(self):
        source=document('<div>Сортировать по цене</div><section itemscope itemtype="https://schema.org/Product"><meta itemprop="name" content="Бумага A3"><div class="price">100 руб.</div></section>',title='Бумага A3')
        self.assertEqual(classify_page(source)['kind'],'listing');self.assertIsNone(extract_product_offer(source,paper_request()))

    def test_price_on_request_has_a_distinct_source_state(self):
        source=document('<p>Цена по запросу</p>',title='Осциллограф FixtureD100')
        source['html']=source['html'].replace('<main>','<main class="product-detail">');source['sha256']=hashlib.sha256(source['html'].encode()).hexdigest()
        o=extract_product_offer(source,parse_request('Осциллограф 4 канала 100МГц'))
        self.assertIsNone(o['price']);self.assertEqual(o['validation']['price_state'],'on_request')
        self.assertEqual(o['evidence']['price_availability']['quote'],'Цена по запросу')



class SellerTermCollection(unittest.TestCase):
    def test_linked_same_vendor_terms_are_captured_and_tax_provenance_is_separate(self):
        primary=document('<p class="price">1000 руб. за штуку</p><p>Количество каналов: 4</p><p>Полоса пропускания: 100 МГц</p><a href="/payment">Оплата</a>',root='main class="product-detail"',title='Осциллограф FixtureD100')
        primary['url']='https://supplier.example/item'
        related={'url':'https://supplier.example/payment','title':'Оплата','primary_title':'Оплата',
                 'html':'<main><h1>Оплата</h1><p>Все цены в каталоге указаны с НДС.</p></main>',
                 'text':'Оплата Все цены в каталоге указаны с НДС.', 'retrieved_at':'2026-11-13T01:00:00Z'}
        related['sha256']=hashlib.sha256(related['html'].encode()).hexdigest()
        class Acquisition:
            timeout=18
            def open(self,url,browser=False): return deepcopy(primary if url==primary['url']else related)
        class Provider:
            configured=False
            model='no-provider-QA'
        store=Store(':memory:');jid='f'*24
        payload={'description':'Осциллограф 4 канала 100 МГц','quantity':1,'mode':'live','source_urls':[primary['url']],'limits':{'max_model_calls':0}}
        store.create_job({'id':jid,'status':'running'},payload)
        research=Research(store,jid,payload,threading.Event(),lambda *a:None,provider=Provider(),acquisition=Acquisition())
        research.execute_tool('open_page',{'url':primary['url']},allow_diversity_pause=False)
        self.assertEqual(research.offers[0]['tax'],'unknown')
        self.assertEqual(research.next_frontier()['url'],related['url'])
        research.execute_tool('open_page',{'url':related['url']})
        offer=research.offers[0];self.assertEqual(offer['tax'],'included');self.assertEqual(offer['evidence']['tax']['url'],related['url'])
        self.assertNotEqual(offer['evidence']['tax']['document_id'],offer['document_id'])
        self.assertEqual(store.quota_usage('model_calls','all'),0);self.assertEqual(len(research.offers),1)

class QualifiedHints(unittest.TestCase):
    def test_description_context_is_visible_without_becoming_confirmed_country(self):
        source=capture('ofisshop');offer=extract_product_offer(source,paper_request())
        self.assertNotIn('country',offer['attributes'])
        hint=offer['attribute_hints']['country'][0]
        self.assertEqual(hint['value'],'Россия');self.assertFalse(hint['confirmed_for_product'])
        self.assertIn(hint['quote'],' '.join(source['text'].split()))
        self.assertNotIn('attributes.country',offer['evidence'])

    def test_merchant_delivery_country_and_negated_manufacture_are_not_origin_hints(self):
        from quotelens.attribute_hints import contextual_attribute_hints
        for text in ('Доставка по России.', 'Бумага не производится в России.'):
            source=document('<p>'+text+'</p>',root='main class="product-detail"')
            self.assertNotIn('country',contextual_attribute_hints(source,paper_request(),{}))

if __name__=='__main__':unittest.main()
