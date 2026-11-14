"""Browser contract for file -> reviewed lines -> research -> recipient report.

The local HTTP fixture is explicitly synthetic. It uses the real file parser
and report renderers but never performs paid research or changes owner data.
"""
from __future__ import annotations

import argparse
import copy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import mimetypes
import os
from pathlib import Path
import re
import sys
import threading
import unittest
from urllib.parse import unquote, urlsplit
from xml.etree import ElementTree as ET
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from quotelens.spec_ingest import prepare_specifications
from quotelens.server import prepare_input
from quotelens.reports import report_html, report_docx, report_pdf

PREFIX = "/procurement-qa/"


class Fixture:
    def __init__(self):
        self.jobs = {}
        self.calls = []
        self.payloads = []
        self.preparations = []
        self.fail_pdf = False

    def new_job(self, payload):
        self.payloads.append(copy.deepcopy(payload))
        identifier = "qa-procurement-" + str(len(self.jobs) + 1)
        request = copy.deepcopy(payload)
        request.pop("mode", None)
        items = copy.deepcopy(payload.get("items") or [{"id":"line-1", "title":payload["description"],
                    "description":payload["description"], "quantity":payload["quantity"], "unit":"шт", "requirements":[]}])
        results, offers = [], []
        for index, item in enumerate(items):
            price = 100 + index * 20
            attrs = {}
            evidence = {"price":{"quote":f"QA: цена {price} рублей за 1 {item.get('unit') or 'шт'}", "url":"https://synthetic.example.invalid/product"},
                        "unit":{"quote":f"QA: за 1 {item.get('unit') or 'шт'}", "url":"https://synthetic.example.invalid/product"}}
            for req in item.get("requirements", []):
                value = req["value"]
                attrs[req["key"]] = sum(value) / 2 if req.get("op") == "between" else value
                evidence["attributes."+req["key"]] = {"quote":f"QA: {req['key']}={attrs[req['key']]}","url":"https://synthetic.example.invalid/product"}
            offer = {"id":item["id"]+":qa-offer", "original_offer_id":"qa-offer", "title":"QA предложение " + item["title"],
                "item_id":item["id"], "item_title":item["title"], "supplier":f"QA поставщик {index+1}","domain":f"qa-{index+1}.example.invalid",
                "url":"https://synthetic.example.invalid/product", "retrieved_at":"2026-11-12T12:00:00Z", "document_id":"qa-document",
                "price":price, "currency":"RUB", "unit":item.get("unit") or "шт", "tax":"unknown", "normalized_price":None,
                "status":"uncertain", "functional_status":"matches", "attributes":attrs, "evidence":evidence,
                "reasons":["QA: НДС не подтверждён."], "provenance":"synthetic"}
            calculation = {"status":"insufficient_data", "unit_price":None, "total":None, "n":0, "independent_suppliers":0,
                "reason":"QA: условия цены требуют проверки.", "rule":"QA: цена × количество", "provisional":{
                "unit_price":price,"total":price*item["quantity"],"n":1,"offer_ids":[offer["id"]],
                "warnings":["QA: НДС не подтверждён."],"reason":"QA: условная арифметика."}}
            line = {**item,"request":copy.deepcopy(item),"offers":[copy.deepcopy(offer)],"calculation":calculation}
            line["offers"][0]["id"] = "qa-offer"
            results.append(line); offers.append(offer)
        result = {"request":request,"items":results if payload.get("items") else [],"offers":offers,"calculation":{},
            "input_documents":copy.deepcopy(payload.get("input_documents",[])),"review":{"excluded_ids":[]},
            "search":{"mode":"live_agent","scenario_kind":"synthetic","search_policy":copy.deepcopy(payload["search_policy"]),
            "finished_at":"2026-11-12T12:01:00Z","stop":{"code":"source_exhausted","label":"Доступные найденные ссылки обработаны", "detail":"QA: синтетическая выборка, сеть не использовалась."},
            "source_statuses":[],"budget":{"pages_used":len(offers),"actual_usage":{"cost":0}}}}
        job = {"id":identifier,"status":"complete","created_at":"2026-11-12T12:00:00Z","revision":0,"request":request,"result":result}
        self.jobs[identifier] = job
        self.recalculate(job)
        return job

    def recalculate(self, job):
        result = job["result"]
        excluded = set(result["review"]["excluded_ids"])
        total = 0; missing = []; considered = []
        for line in result["items"]:
            offer = next(o for o in result["offers"] if o["item_id"] == line["id"])
            removed = offer["id"] in excluded
            offer.update(status="excluded" if removed else "uncertain",manual_excluded=removed)
            line["offers"][0].update(status=offer["status"],manual_excluded=removed)
            if removed:
                missing.append(line["id"])
                line["calculation"]["provisional"]["total"] = None
            else:
                value = offer["price"] * line["quantity"]
                line["calculation"]["provisional"]["total"] = value
                total += value; considered.append(offer["id"])
        if not result["items"]:
            total = result["offers"][0]["price"] * result["request"]["quantity"]
            considered = [result["offers"][0]["id"]]
        result["calculation"] = {"status":"insufficient_data", "unit_price":None, "total":None, "n":0,"independent_suppliers":0,
            "reason":"QA: НДС и состав закупки требуют проверки.","provisional":{"unit_price":None,"total":None if missing else total,
            "known_subtotal":total if considered else None,"missing_item_ids":missing,"n":len(considered),"offer_ids":considered,
            "warnings":["QA: НДС не подтверждён."],"reason":"QA: предварительная стоимость."}}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args): pass

    def respond(self, value, mime="application/json", status=200, filename=None):
        body = json.dumps(value,ensure_ascii=False).encode() if isinstance(value,(dict,list)) else value
        if isinstance(body,str):body=body.encode()
        self.send_response(status); self.send_header("Content-Type",mime); self.send_header("Content-Length",str(len(body)))
        if filename:self.send_header("Content-Disposition",f'attachment; filename="{filename}"')
        self.end_headers(); self.wfile.write(body)

    def dispatch(self):
        fixture=self.server.fixture; path=unquote(urlsplit(self.path).path)
        fixture.calls.append((self.command,path))
        if not path.startswith(PREFIX):return self.respond({"error":"QA: вышли из UID"},status=404)
        path="/"+path[len(PREFIX):]
        payload=json.loads(self.rfile.read(int(self.headers.get("Content-Length","0")))) if self.command=="POST" else None
        if path=="/api/config":return self.respond({"model":{"configured":True},"defaults":{"min_suppliers":3}})
        if path=="/api/sources":return self.respond([{"id":"web","kind":"web","name":"Интернет","connected":True}])
        if path=="/api/jobs":return self.respond({"items":[{"id":j["id"],"status":j["status"],"request":j["request"],"description":j["request"]["description"],"created_at":j["created_at"],"offers_count":len(j["result"]["offers"])} for j in fixture.jobs.values()],"next_cursor":None,"total":len(fixture.jobs)})
        if path=="/api/prepare":
            fixture.preparations.append(copy.deepcopy(payload))
            try:return self.respond(prepare_input(payload.get("description",""),payload.get("files",[])))
            except ValueError as e:return self.respond({"error":str(e)},status=400)
        if path=="/api/search":
            job=fixture.new_job(payload); return self.respond({k:v for k,v in job.items() if k!="result"},status=202)
        match=re.fullmatch(r"/api/jobs/([^/]+)(?:/(review|report\.(pdf|docx|html|json)|documents/([^/]+)))?",path)
        if match and match[1] in fixture.jobs:
            job=fixture.jobs[match[1]]
            if match[2]=="review":
                excluded=set(job["result"]["review"]["excluded_ids"])
                if payload["action"]=="exclude":excluded.add(payload["offer_id"])
                else:excluded.discard(payload["offer_id"])
                job["result"]["review"]["excluded_ids"]=sorted(excluded); job["revision"]+=1; fixture.recalculate(job)
            elif match[4]:return self.respond({"text":"QA: <script>window.qaInjection=true</script> цена100рублей"})
            elif match[3]:
                fmt=match[3]
                if fmt=="pdf" and fixture.fail_pdf:return self.respond({"error":"QA: PDF временно недоступен."},status=503)
                if fmt=="pdf":return self.respond(report_pdf(job["result"]),"application/pdf",filename="qa.pdf")
                if fmt=="docx":return self.respond(report_docx(job["result"]),"application/vnd.openxmlformats-officedocument.wordprocessingml.document",filename="qa.docx")
                if fmt=="html":return self.respond(report_html(job["result"]),"text/html",filename="qa.html")
                return self.respond(job["result"],filename="qa.json")
            return self.respond(job)
        if path.startswith("/api/"):return self.respond({"error":"QA: не найдено"},status=404)
        name="index.html" if path=="/" else path.removeprefix("/static/")
        if name not in {"index.html","app.js","style.css"}:return self.respond(b"not found","text/plain",404)
        return self.respond((ROOT/"static"/name).read_bytes(),mimetypes.guess_type(name)[0] or "text/plain")

    do_GET=dispatch
    do_POST=dispatch


class ProcurementUi(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright
        cls.output=Path(os.environ["QUOTELENS_PROCUREMENT_UI_OUTPUT"]);cls.output.mkdir(parents=True,exist_ok=True)
        cls.server=ThreadingHTTPServer(("127.0.0.1",0),Handler)
        cls.thread=threading.Thread(target=cls.server.serve_forever,daemon=True);cls.thread.start()
        cls.base=f"http://127.0.0.1:{cls.server.server_port}{PREFIX}"
        cls.pw=sync_playwright().start();cls.browser=cls.pw.chromium.launch(executable_path="/usr/bin/google-chrome",headless=True,args=["--no-sandbox","--disable-dev-shm-usage"])

    @classmethod
    def tearDownClass(cls):
        cls.browser.close();cls.pw.stop();cls.server.shutdown();cls.server.server_close();cls.thread.join(timeout=2)

    def setUp(self):
        self.fixture=self.server.fixture=Fixture();self.context=self.browser.new_context(viewport={"width":1292,"height":900},accept_downloads=True)
        self.page=self.context.new_page();self.page.set_default_timeout(12000);self.errors=[]
        self.page.on("pageerror",lambda e:self.errors.append(str(e)))
        self.page.goto(self.base,wait_until="networkidle")

    def tearDown(self):
        self.page.screenshot(path=str(self.output/(self._testMethodName+".png")),full_page=True)
        self.context.close();self.assertEqual(self.errors,[])

    def attach(self,*names):
        self.page.locator("#spec-files").set_input_files([str(ROOT/"tests/fixtures/specifications"/name) for name in names])
        self.page.get_by_text("Проверьте позиции, количества и требования. Затем начните поиск.",exact=True).wait_for()

    def start(self):
        self.page.locator("#search-submit").click();self.page.locator("#result-screen").wait_for(state="visible")

    def no_overflow(self):
        dims=self.page.evaluate('({viewport:innerWidth,width:document.documentElement.scrollWidth})')
        self.assertLessEqual(dims["width"],dims["viewport"]+1,dims)

    def test_01_multiple_files_edit_preview_and_submit_exact_quantities(self):
        self.attach("paper.docx","printer.docx")
        self.assertEqual(self.page.locator(".spec-item").count(),4)
        self.assertEqual(self.page.locator('[data-item-field="quantity"]').evaluate_all('(els)=>els.map(el=>Number(el.value))'),[2500,25,50,1])
        self.page.locator('[data-item-field="quantity"]').first.fill("2600")
        first=self.page.locator('.spec-item').first;first.locator('.spec-item-details > summary').click()
        first.locator('[data-requirement-field="value"]').first.fill("QA исправление")
        self.assertEqual(self.page.locator('#stop-mode').input_value(),'target')
        self.assertEqual(self.page.locator('#search-minutes').input_value(),'10')
        self.page.locator('.search-options > summary').click()
        self.page.locator('#search-minutes').fill("3")
        self.start()
        payload=self.fixture.payloads[0]
        self.assertEqual(payload['search_policy'],{'mode':'target','max_seconds':180})
        self.assertEqual([i['quantity'] for i in payload['items']],[2600,25,50,1])
        self.assertEqual(len(payload['input_documents']),2)
        self.assertTrue(payload['items'][0]['requirements'][0]['manual_override'])
        self.assertEqual(payload['items'][0]['requirements'][0]['value'],'QA исправление')
        self.assertIn('Предварительная стоимость',self.page.locator('#calculation-summary').inner_text())
        self.assertEqual(self.page.locator('.item-calculation').count(),4)
        self.no_overflow()

    def test_02_description_edit_requires_reparse_and_keeps_manual_quantity(self):
        self.attach('printer.docx')
        item=self.page.locator('.spec-item');item.locator('.spec-item-details > summary').click()
        description=item.locator('[data-item-field="description"]')
        description.fill('3D принтер CREATBOT D600 PRO; 2 экструдера')
        self.page.locator('#search-submit').click()
        self.assertEqual(len(self.fixture.payloads),0)
        self.assertIn('Обновите требования',self.page.locator('#global-error').inner_text())
        item.locator('[data-reparse-item]').click()
        self.page.get_by_text('Требования обновлены. Проверьте их перед поиском.',exact=True).wait_for()
        self.assertEqual(self.page.locator('[data-item-field="quantity"]').input_value(),'1')
        self.start();self.assertEqual(len(self.fixture.payloads),1)
        self.assertEqual(self.fixture.payloads[0]['items'][0]['description'],'3D принтер CREATBOT D600 PRO; 2 экструдера')

    def test_03_filters_item_text_evidence_and_review_change_partial_total(self):
        self.attach('paper.docx');self.start()
        self.page.locator('#result-filters summary').click()
        second=self.fixture.payloads[0]['items'][1]['id']
        self.page.locator('[data-select-item="'+second+'"]').first.click()
        self.assertEqual(self.page.locator('[data-offer]').count(),1)
        self.page.locator('#result-filters summary').click()
        self.page.locator('#offer-search').fill('НЕТ СОВПАДЕНИЙ')
        self.assertEqual(self.page.locator('[data-offer]').count(),0)
        self.page.locator('#offer-search').fill('')
        self.page.locator('[data-evidence]').last.click()
        self.assertIn('A3',self.page.locator('.evidence-table').inner_text())
        self.page.locator('#review-submit').click()
        self.page.get_by_text('Решение сохранено. Расчёт и отчёт обновлены.',exact=True).wait_for()
        self.page.locator('#close-evidence').click()
        self.assertIn('Часть закупки · 2 из 3 позиций',self.page.locator('#calculation-summary').inner_text())
        self.assertIn('Полная стоимость пока неизвестна',self.page.locator('#calculation-summary').inner_text())

    def test_04_pdf_docx_downloads_and_report_error_keeps_results(self):
        self.attach('printer.docx');self.start()
        self.fixture.fail_pdf=True
        self.page.locator('#report-pdf').click()
        self.page.get_by_text('QA: PDF временно недоступен.',exact=True).wait_for()
        self.assertTrue(self.page.locator('#result-screen').is_visible())
        self.fixture.fail_pdf=False
        for fmt in ('pdf','docx'):
            with self.page.expect_download(timeout=30000) as event:self.page.locator('#report-'+fmt).click()
            path=self.output/('actual-recipient.'+fmt);event.value.save_as(path)
            if fmt=='pdf':self.assertTrue(path.read_bytes().startswith(b'%PDF-'))
            else:
                with ZipFile(path) as z:
                    self.assertNotIn('word/vbaProject.bin',z.namelist())
                    content=''.join(ET.fromstring(z.read('word/document.xml')).itertext())
                    self.assertIn('CREATBOT',content)
                    self.assertNotIn('Прикрепить ТЗ',content)

    def test_05_simple_text_no_prepare_time_limit_and_reopen_history(self):
        self.page.locator('#description').fill('Осциллограф цифровой, 4 канала, 100 МГц')
        self.page.locator('#quantity').fill('3')
        self.page.locator('.search-options > summary').click()
        self.page.locator('#stop-mode').select_option('time');self.page.locator('#search-minutes').fill('1')
        self.start()
        self.assertEqual(self.fixture.preparations,[])
        self.assertEqual(self.fixture.payloads[0]['search_policy']['max_seconds'],60)
        self.assertEqual(self.fixture.payloads[0]['search_policy']['mode'],'time')
        self.page.reload(wait_until='networkidle')
        self.page.locator('#result-screen').wait_for(state='visible')
        self.assertEqual(len(self.fixture.payloads),1)

    def test_06_mobile_files_warnings_and_no_horizontal_overflow(self):
        self.page.set_viewport_size({'width':390,'height':844})
        self.attach('cytokines.docx')
        self.assertEqual(self.page.locator('.spec-item').count(),5)
        self.assertIn('другой товар',self.page.locator('#spec-warnings').inner_text())
        self.no_overflow()
        self.page.locator('.spec-item').last.locator('.spec-item-details > summary').click();self.no_overflow()
        self.start();self.no_overflow()
        self.page.locator('#result-filters summary').click();self.no_overflow()
        self.page.locator('#result-filters summary').click()
        self.page.locator('[data-evidence]').first.click();self.no_overflow()

    def test_07_corrupt_file_error_remove_and_retry_valid_file(self):
        self.page.locator('#spec-files').set_input_files({'name':'broken.docx','mimeType':'application/vnd.openxmlformats-officedocument.wordprocessingml.document','buffer':b'broken'})
        self.page.locator('#spec-preparation.error').wait_for()
        self.assertEqual(len(self.fixture.payloads),0)
        self.page.locator('[data-remove-file]').click()
        self.assertEqual(self.page.locator('#spec-file-list li').count(),0)
        self.attach('paper.docx');self.assertEqual(self.page.locator('.spec-item').count(),3)


    def test_08_unknown_provider_cost_does_not_display_zero(self):
        self.attach('printer.docx');self.start()
        job=next(iter(self.fixture.jobs.values()))
        job['result']['search']['budget']['actual_cost_known']=False
        self.page.reload(wait_until='networkidle')
        self.page.locator('#result-screen').wait_for(state='visible')
        self.page.locator('.research-details').first.locator(':scope > summary').click()
        text=self.page.locator('.research-details').first.inner_text()
        self.assertIn('провайдер не передал полные данные',text)
        self.assertNotIn('$0',text)

    def test_09_long_pasted_word_specification_is_not_truncated(self):
        with ZipFile(ROOT/'tests/fixtures/specifications/paper.docx') as z:
            root=ET.fromstring(z.read('word/document.xml'))
            ns={'w':'http://schemas.openxmlformats.org/wordprocessingml/2006/main'}
            text='\n'.join(''.join(t.text or '' for t in p.findall('.//w:t',ns)) for p in root.findall('.//w:p',ns))
        self.assertGreater(len(text),4000)
        self.page.locator('#description').fill(text)
        self.assertEqual(self.page.locator('#description').input_value(),text)
        self.page.locator('#search-submit').click()
        self.page.get_by_text('Проверьте позиции, количества и требования. Затем начните поиск.',exact=True).wait_for()
        self.assertEqual(len(self.fixture.payloads),0)
        self.assertEqual(self.page.locator('.spec-item').count(),3)
        self.assertEqual(self.page.locator('[data-item-field="quantity"]').evaluate_all('(els)=>els.map(el=>Number(el.value))'),[2500,25,50])
        self.start()
        self.assertEqual(self.fixture.payloads[0]['description'],text.strip())


    def test_10_add_file_to_saved_request_keeps_old_items_until_source_removed(self):
        self.attach('paper.docx');self.start()
        self.page.reload(wait_until='networkidle')
        self.page.locator('#result-screen').wait_for(state='visible')
        self.page.locator('#edit-query').click()
        self.attach('printer.docx')
        self.assertEqual(self.page.locator('.spec-item').count(),4)
        self.assertEqual(self.page.locator('[data-item-field="quantity"]').evaluate_all('(els)=>els.map(el=>Number(el.value))'),[2500,25,50,1])
        self.page.get_by_role('button',name='Убрать файл paper.docx',exact=True).click()
        self.assertEqual(self.page.locator('.spec-item').count(),1)
        self.assertIn('CREATBOT',self.page.locator('[data-item-field="title"]').input_value())
        self.start()
        self.assertEqual(len(self.fixture.payloads[-1]['items']),1)
        self.assertEqual([d['name'] for d in self.fixture.payloads[-1]['input_documents']],['printer.docx'])


    def test_11_reparse_keeps_edited_text_source_and_report_provenance(self):
        import hashlib
        self.attach('paper.docx')
        item=self.page.locator('.spec-item').first;item.locator('.spec-item-details > summary').click()
        edited='Бумага офисная A4, плотность 90 г/м², 500 листов в пачке'
        item.locator('[data-item-field="description"]').fill(edited)
        item.locator('[data-reparse-item]').click()
        self.page.get_by_text('Требования обновлены. Проверьте их перед поиском.',exact=True).wait_for()
        self.start()
        payload=self.fixture.payloads[0];line=payload['items'][0]
        original=next(d for d in payload['input_documents'] if d['name']=='paper.docx')
        changed=next(d for d in payload['input_documents'] if d['id']==line['source']['document_id'])
        self.assertTrue(changed['id'].startswith('edit-'))
        self.assertEqual(changed['text'],edited)
        self.assertEqual(changed['sha256'],hashlib.sha256(edited.encode()).hexdigest())
        self.assertEqual(line['source']['original']['document_id'],original['id'])
        self.assertTrue(line['requirements'])
        for requirement in line['requirements']:
            self.assertTrue(requirement['manual_override'])
            self.assertEqual(requirement['source']['document_id'],changed['id'])
            self.assertIn(requirement['quote'],changed['text'])
        with self.page.expect_download() as event:self.page.locator('#report-docx').click()
        saved=self.output/'edited-provenance.docx';event.value.save_as(saved)
        with ZipFile(saved) as z:content=''.join(ET.fromstring(z.read('word/document.xml')).itertext())
        self.assertIn('изменено пользователем',content)
        self.assertIn('paper.docx',content)
        self.assertIn(changed['name'],content)
        self.assertIn(changed['sha256'],content)
        self.assertIn('90',content)

    def test_12_open_data_notice_visible_before_upload_desktop_and_mobile(self):
        for width in (1292,390):
            self.page.set_viewport_size({'width':width,'height':900 if width==1292 else 844})
            notice=self.page.locator('#data-boundary')
            self.assertTrue(notice.is_visible())
            self.assertEqual(notice.inner_text(),'Только открытые данные. Текст запроса и ТЗ обрабатывается внешней моделью.')
            box=notice.bounding_box()
            self.assertGreater(box['height'],0)
            self.assertGreaterEqual(box['y'],0)
            self.assertLess(box['y']+box['height'],844)
            self.assertEqual(self.page.locator('dialog[open]').count(),0)
            self.no_overflow()
            self.page.screenshot(path=str(self.output/f'open-data-notice-{width}.png'))


    def test_13_fractional_unit_price_and_mean_remain_nonzero(self):
        self.page.locator('#description').fill('Бумага офисная A4 80 г/м² 500 листов в пачке')
        self.page.locator('#quantity').fill('1000');self.start()
        job=next(iter(self.fixture.jobs.values()));result=job['result']
        result['request'].update(unit='лист',quantity=1000)
        offer=result['offers'][0];offer.update(price=1.23,unit='пачка',normalized_price=.00246)
        result['calculation']['provisional'].update(unit_price=.00246,total=2.46)
        self.page.reload(wait_until='networkidle');self.page.locator('#result-screen').wait_for(state='visible')
        self.assertIn('0,00246',self.page.locator('.offer-price').inner_text())
        self.page.locator('.calculation-details > summary').click()
        self.assertIn('0,00246',self.page.locator('#calculation-summary').inner_text())
        self.assertEqual(self.page.locator('.total-price').inner_text(),'2,46 ₽')
        self.page.locator('[data-evidence]').last.click()
        self.assertIn('За 1 лист: 0,00246 ₽',self.page.locator('.evidence-price').inner_text())
        self.page.locator('#close-evidence').click()
        offer['normalized_price']=.00000012
        result['calculation']['provisional']['unit_price']=.00000012
        self.page.reload(wait_until='networkidle');self.page.locator('#result-screen').wait_for(state='visible')
        self.assertIn('0,00000012',self.page.locator('.offer-price').inner_text())
        self.page.locator('.calculation-details > summary').click()
        self.assertIn('0,00000012',self.page.locator('#calculation-summary').inner_text())


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--output-dir',required=True);args,remaining=parser.parse_known_args()
    os.environ['QUOTELENS_PROCUREMENT_UI_OUTPUT']=args.output_dir
    unittest.main(argv=[sys.argv[0],*remaining],verbosity=2)
