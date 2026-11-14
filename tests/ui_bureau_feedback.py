"""Regression checks for the six second-round Bureau remarks.

Uses the actual Handler, SQLite and browser polling. Research/DNS are isolated
synthetic fixtures from ui_bureau_product; no owner data, provider, external
browser profile or feedback submission is used.
"""
from copy import deepcopy
import json
import os
from pathlib import Path
import re
import socket
import sys
import threading
import unittest
from unittest.mock import patch
from urllib.parse import unquote, urlsplit
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tests'))
import ui_bureau_product as bureau


class BureauFeedbackTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright
        cls.out = Path(os.environ.get('BUREAU_FEEDBACK_OUTPUT', '/evidence/bureau-feedback'))
        cls.out.mkdir(parents=True, exist_ok=True)
        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch(
            executable_path=os.environ.get('BROWSER_EXECUTABLE_PATH', '/usr/bin/google-chrome'),
            headless=True, args=['--no-sandbox', '--disable-dev-shm-usage'])

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()

    def setUp(self):
        bureau.BureauBrowserTests.setUp(self)
        with urlopen(self.base + 'api/sources', timeout=5) as response:
            self.sources = json.load(response)
        self.custom_domain = 'independent-supplier.example.org'
        domains = {row['domain'] for row in self.sources if row.get('domain')}
        domains.add(self.custom_domain)
        previous_resolve = socket.getaddrinfo

        def resolve(host, port, *args, **kwargs):
            if host in domains:
                return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, '', ('93.184.216.34', port))]
            return previous_resolve(host, port, *args, **kwargs)

        dns = patch('socket.getaddrinfo', side_effect=resolve)
        dns.start()
        self.patches.append(dns)

    def tearDown(self):
        try:
            self.assertEqual(self.db.quota_usage('model_calls', 'all'), 0)
        finally:
            bureau.BureauBrowserTests.tearDown(self)

    def no_overflow(self):
        bureau.BureauBrowserTests.no_overflow(self)

    def prepare_text(self):
        bureau.BureauBrowserTests.prepare_text(self)

    def seed_result(self, count=3):
        return bureau.BureauBrowserTests.seed_result(self, count)

    def research_settings(self):
        self.prepare_text()
        self.page.locator('#review-next').click()
        self.assertTrue(self.page.locator('#research-settings').is_visible())

    def choose_result_status(self, value):
        filters = self.page.locator('#result-filters')
        if not filters.evaluate('node => node.open'):
            filters.locator('summary').click()
        self.page.locator('#status-filter').select_option(value)

    def assert_source_controls_are_square(self):
        boxes = self.page.locator('#source-options .source-preset input').evaluate_all(
            "nodes => nodes.filter(n => n.getClientRects().length).map(n => { const r=n.getBoundingClientRect(); return {w:r.width,h:r.height}; })")
        self.assertTrue(boxes)
        for box in boxes:
            self.assertGreaterEqual(box['w'], 16)
            self.assertLessEqual(box['w'], 30)
            self.assertAlmostEqual(box['w'], box['h'], delta=1)

    def assert_tooltip_readable(self):
        tooltip = self.page.locator('#cost-term')
        self.assertTrue(tooltip.is_visible())
        self.assertIn('цена', tooltip.inner_text().lower())
        geometry = tooltip.evaluate("""node => {
            const r=node.getBoundingClientRect(), pad=7;
            const points=[[r.left+pad,r.top+pad],[r.right-pad,r.top+pad],
                [r.left+pad,r.bottom-pad],[r.right-pad,r.bottom-pad],
                [(r.left+r.right)/2,(r.top+r.bottom)/2]];
            return {x:r.left,y:r.top,right:r.right,bottom:r.bottom,w:r.width,h:r.height,
                width:innerWidth,height:innerHeight,
                hit:points.map(([x,y]) => {const top=document.elementFromPoint(x,y);return !!top&&(top===node||node.contains(top));})};
        }""")
        self.assertGreater(geometry['w'], 150)
        self.assertGreater(geometry['h'], 20)
        self.assertGreaterEqual(geometry['x'], 0)
        self.assertGreaterEqual(geometry['y'], 0)
        self.assertLessEqual(geometry['right'], geometry['width'])
        self.assertLessEqual(geometry['bottom'], geometry['height'])
        self.assertTrue(all(geometry['hit']), geometry)

    def seed_running_result(self):
        self.page.set_viewport_size({'width':1440, 'height':760})
        result = self.seed_result(3)
        offer = result['offers'][0]
        offer['evidence']['tax']['quote'] = 'НДС включён. Учебное подтверждение.\n' + '\n'.join(
            f'Учебная строка {n}: условия цены и их подтверждение сохраняются в источнике.' for n in range(35))
        offer['observed_terms'] = {'warranty': [
            {'quote': f'Учебное условие гарантии {n}: сохранённый текст для проверки без обращения к продавцу.', 'url':offer['url']}
            for n in range(18)]}
        job = self.db.get_job(bureau.JOB)
        job.update(status='running', result=result)
        self.db.save_job(job)
        self.page.reload()
        self.page.locator('#progress-screen:not([hidden])').wait_for()
        self.page.locator('#offer-list [data-offer]').first.wait_for()
        self.assertTrue(self.page.locator('#synthetic-label').is_visible())
        return result

    def polling_update(self, label, mutate=None):
        job = self.db.get_job(bureau.JOB)
        result = deepcopy(job['result'])
        if mutate:
            mutate(result)
        job.update(status='running', result=result)
        job.setdefault('events', []).append({'kind':'qa', 'message':label})
        self.db.save_job(job)
        with self.page.expect_response(lambda response: response.url == self.base + 'api/jobs/' + bureau.JOB and response.status == 200) as reply:
            self.page.evaluate("window.dispatchEvent(new Event('focus'))")
        received = reply.value.json()
        self.assertEqual(received['status'], 'running')
        self.page.wait_for_function("label => document.querySelector('#progress-description').textContent.includes(label)", arg=label)
        # Let the same response finish its scheduled paint before checking DOM
        # state; no synthetic renderResult calls bypass the real polling path.
        self.page.evaluate('() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))')
        return received

    def scroll_evidence(self):
        return self.page.evaluate("""() => {
            const selectors=['#evidence-dialog','#evidence-body','.evidence-workspace','.evidence-facts','.evidence-source'];
            return selectors.map(selector => {
                const node=document.querySelector(selector), max=node.scrollHeight-node.clientHeight;
                if(max>60) node.scrollTop=Math.min(120,max-20);
                return {selector,top:node.scrollTop,max};
            });
        }""")

    def assert_evidence_state(self, scrolls, selected, pane):
        self.assertTrue(self.page.locator('#evidence-dialog').evaluate('node => node.open'))
        self.assertEqual(self.page.locator('[data-proof-field][aria-pressed=true]').get_attribute('data-proof-field'), selected)
        self.assertEqual(self.page.locator('#evidence-body').get_attribute('data-pane'), pane)
        for before in scrolls:
            after = self.page.locator(before['selector']).evaluate('node => node.scrollTop')
            self.assertAlmostEqual(after, before['top'], delta=1, msg=before['selector'])

    def test_01_back_arrow_and_all_mode_cards_post_actual_policy(self):
        for mode, minutes, count in [('target','2',None), ('offers','3','17'), ('time','4',None)]:
            with self.subTest(mode=mode):
                self.page.locator('.brand').click()
                self.research_settings()
                back = self.page.locator('#research-back')
                self.assertEqual(back.locator('svg use').get_attribute('href'), '#i-arrow-left')
                back.click()
                self.assertTrue(self.page.locator('#spec-preview').is_visible())
                self.assertEqual(self.page.locator('#procurement-stages [aria-current=step]').get_attribute('data-workflow-stage'), 'review')
                self.page.locator('#review-next').click()
                radios = self.page.locator('#stop-mode-cards input[name=stop_mode_radio]')
                self.assertEqual(radios.count(), 3)
                for value in ('target','offers','time'):
                    card = self.page.locator(f'#stop-mode-cards label.stop-mode-card:has(input[value="{value}"])')
                    self.assertTrue(card.is_visible())
                    self.assertGreater(card.bounding_box()['height'], 40)
                    radio_box = card.locator('input').bounding_box()
                    self.assertGreaterEqual(radio_box['width'], 16)
                    self.assertLessEqual(radio_box['width'], 30)
                    self.assertAlmostEqual(radio_box['width'], radio_box['height'], delta=1)
                self.page.locator(f'input[name=stop_mode_radio][value="{mode}"]').check()
                self.assertEqual(self.page.locator('#stop-mode').input_value(), mode)
                self.page.locator('#search-minutes').fill(minutes)
                if count:
                    self.page.locator('#max-offers').fill(count)
                if mode == 'target':
                    self.page.screenshot(path=str(self.out / 'research-mode-cards-back-arrow.png'), full_page=True)
                before = len(self.searches)
                self.page.locator('#search-submit').click()
                self.page.locator('#result-screen:not([hidden])').wait_for()
                self.assertEqual(len(self.searches), before + 1)
                policy = self.searches[-1]['search_policy']
                self.assertEqual(policy['mode'], mode)
                self.assertEqual(policy['max_seconds'], int(minutes) * 60)
                if count:
                    self.assertEqual(policy['max_offers'], int(count))
                identifier = self.page.url.split('job=')[1].split('&')[0]
                saved = self.db.get_job(identifier)
                self.assertEqual(saved['request']['search_policy']['mode'], mode)
                self.assertEqual(saved['request']['search_policy']['max_seconds'], int(minutes) * 60)
                self.no_overflow()

    def test_02_icon_bulk_sources_preserve_custom_domain_in_real_post(self):
        self.research_settings()
        self.page.locator('#source-picker-summary').click()
        self.page.locator('[name=source_scope_mode][value=domains]').check()
        case_domains = {row['domain'] for row in self.sources if row.get('case_source') and row.get('selectable') and row.get('domain')}
        self.assertEqual(len(case_domains), 18)
        available = {row['domain'] for row in self.sources if row.get('domain') and row.get('kind') != 'web' and (row.get('selectable') or row.get('connected'))}
        self.assertTrue(case_domains <= available)
        custom = self.page.locator('.source-custom')
        if not custom.evaluate('node => node.open'):
            custom.locator('summary').click()
        self.page.locator('#source-domains').fill(self.custom_domain)
        for selector in ('[data-source-select-all]', '[data-source-clear-all]'):
            button = self.page.locator(selector)
            self.assertEqual(button.inner_text().strip(), '')
            self.assertTrue(button.get_attribute('aria-label'))
            self.assertTrue(button.get_attribute('title'))
            self.assertEqual(button.locator('svg').count(), 1)
        self.page.locator('[data-source-select-all]').click()
        chosen = set(self.page.locator('#source-options [data-source-domain]:checked').evaluate_all('nodes => nodes.map(node => node.dataset.sourceDomain)'))
        self.assertEqual(chosen, available)
        self.assertFalse(self.page.locator('[data-source-domain=""]').is_checked())
        self.assertTrue(self.page.locator('[data-source-domain=""]').is_disabled())
        self.assertEqual(self.page.locator('#source-domains').input_value(), self.custom_domain)
        self.assert_source_controls_are_square()
        self.page.locator('[data-source-clear-all]').click()
        self.assertEqual(self.page.locator('#source-options [data-source-domain]:checked').count(), 0)
        self.assertEqual(self.page.locator('#source-domains').input_value(), self.custom_domain)
        self.page.locator('[data-source-select-all]').click()
        self.page.locator('#search-submit').click()
        self.page.locator('#result-screen:not([hidden])').wait_for()
        expected = available | {self.custom_domain}
        self.assertEqual(set(self.searches[-1]['source_scope']['domains']), expected)
        identifier = self.page.url.split('job=')[1].split('&')[0]
        self.assertEqual(set(self.db.get_job(identifier)['request']['source_scope']['domains']), expected)

    def test_03_price_explanation_is_unclipped_hover_focus_and_mobile(self):
        self.seed_result(3)
        button = self.page.locator('.term-help-button')
        button.hover()
        self.assert_tooltip_readable()
        self.page.screenshot(path=str(self.out / 'tooltip-hover.png'), full_page=True)
        self.page.mouse.move(2, 2)
        button.focus()
        self.assert_tooltip_readable()
        self.page.screenshot(path=str(self.out / 'tooltip-focus.png'), full_page=True)
        self.page.set_viewport_size({'width':375, 'height':844})
        self.page.reload()
        self.page.locator('#offer-list [data-offer]').first.wait_for()
        self.page.locator('.term-help-button').click()
        self.assert_tooltip_readable()
        self.page.screenshot(path=str(self.out / 'tooltip-mobile.png'), full_page=True)
        self.no_overflow()

    def test_04_running_polling_preserves_analysis_scroll_proof_and_feedback_focus(self):
        self.seed_running_result()
        action = self.page.locator('#offer-list [data-evidence]').first
        self.assertEqual(action.inner_text().strip(), 'Разбор')
        self.assertEqual(self.page.get_by_role('button', name='Доказательства', exact=True).count(), 0)
        action.click()
        self.page.locator('[data-proof-field=tax]').click()
        self.page.locator('.observed-seller-terms details > summary').first.click()
        scrolls = self.scroll_evidence()
        self.assertTrue(any(item['top'] > 0 for item in scrolls), scrolls)
        selected = self.page.locator('[data-proof-field][aria-pressed=true]').get_attribute('data-proof-field')
        pane = self.page.locator('#evidence-body').get_attribute('data-pane')
        self.page.evaluate("""() => {
            const host=document.createElement('public-feedback-widget');
            host.setAttribute('aria-label','Учебная форма замечания без отправки');
            host.style.cssText='position:fixed;right:20px;bottom:20px;z-index:2147483647';
            host.attachShadow({mode:'open'}).innerHTML='<style>textarea{box-sizing:border-box;width:260px;height:80px;padding:12px;background:white;color:black;border:2px solid #72253e}</style><label>Учебное замечание<textarea aria-label="Учебное замечание"></textarea></label>';
            document.body.append(host);
        }""")
        feedback = self.page.locator('public-feedback-widget textarea')
        feedback.fill('Учебный черновик: фокус и текст сохраняются во время поиска.')
        feedback.focus()
        updates = [
            ('QA: результат без изменений', None),
            ('QA: изменено другое предложение', lambda result: result['offers'][-1].update(title='QA: обновлён другой товар')),
            ('QA: изменено открытое предложение', lambda result: result['offers'][0].update(title='QA: обновлён открытый товар')),
        ]
        for label, mutate in updates:
            self.polling_update(label, mutate)
            self.assert_evidence_state(scrolls, selected, pane)
            self.assertTrue(feedback.evaluate('node => node.getRootNode().activeElement === node'))
            self.assertTrue(self.page.evaluate("document.activeElement.localName === 'public-feedback-widget'"))
            self.assertIn('фокус и текст сохраняются', feedback.input_value())
        self.page.screenshot(path=str(self.out / 'analysis-polling-feedback-focus.png'), full_page=True)

    def test_05_calculation_expansion_survives_changed_and_unchanged_running_polls(self):
        self.seed_running_result()
        self.page.locator('.calculation-details > summary').click()
        self.assertTrue(self.page.locator('.calculation-details').evaluate('node => node.open'))
        for label, mutate in [
            ('QA: расчёт без изменений', None),
            ('QA: первое обновление результатов', lambda result: result['offers'][-1].update(title='QA: новая информация 1')),
            ('QA: второе обновление результатов', lambda result: result['offers'][0].update(title='QA: новая информация 2')),
        ]:
            self.polling_update(label, mutate)
            self.assertTrue(self.page.locator('.calculation-details').evaluate('node => node.open'))
            self.assertIn('В расчёте', self.page.locator('.calculation-details').inner_text())
        self.page.screenshot(path=str(self.out / 'calculation-polling-open.png'), full_page=True)

    def test_06_wrong_product_is_rejected_and_possible_printer_stays_active(self):
        from quotelens.procurement import aggregate_results, request_for_item
        from quotelens.selection import finalize
        item = {
            'id':'qa-printer', 'title':'3D-принтер CreatBot D600 PRO',
            'description':'3D-принтер CreatBot D600 PRO', 'quantity':1, 'unit':'шт',
            'requirements':[{'key':'build_width_mm', 'label':'Ширина печати', 'op':'gte', 'value':600, 'unit':'мм',
                             'quote':'Учебное ТЗ: ширина печати не менее 600 мм.'}],
            'conditions':[],
        }
        request = request_for_item(item)
        offers = []
        titles = [('engraver','Станок гравировальный DIAOTU T1 Pro'),
                  ('pen','3D-ручка для пластика PLA'),
                  ('printer','3D-принтер CreatBot D600 PRO')]
        for n, (identifier, title) in enumerate(titles):
            url = f'https://qa-{identifier}.example.invalid/product'
            offers.append({
                'id':identifier, 'title':title, 'supplier':'Учебный поставщик ' + identifier,
                'domain':f'qa-{identifier}.example.invalid', 'url':url,
                'price':1000 + n * 100, 'currency':'RUB', 'unit':'шт', 'tax':'included',
                'attributes':{}, 'provenance':'synthetic', 'retrieved_at':'2026-01-01T00:00:00Z',
                'evidence':{
                    'title':{'quote':title, 'url':url},
                    'price':{'quote':f'{1000+n*100} ₽ за шт.', 'url':url},
                    'currency':{'quote':'Цена в рублях RUB', 'url':url},
                    'unit':{'quote':'Цена за шт.', 'url':url},
                    'tax':{'quote':'НДС включён', 'url':url},
                },
            })
        line = finalize(request, offers)
        line['search'] = {'mode':'live_agent', 'scenario_kind':'synthetic', 'budget':{'model_calls':0}, 'source_statuses':[]}
        decisions = {offer['id']:offer for offer in line['offers']}
        for identifier in ('engraver','pen'):
            self.assertEqual(decisions[identifier]['status'], 'excluded', decisions[identifier].get('reasons'))
            self.assertEqual(decisions[identifier]['product_identity']['status'], 'mismatch')
        self.assertEqual(decisions['printer']['status'], 'uncertain')
        self.assertEqual(decisions['printer']['product_identity']['status'], 'matches')
        payload = {'mode':'live', 'quantity':1, 'description':'Учебная закупка 3D-принтера', 'items':[item],
                   'search_policy':{'mode':'target', 'max_seconds':600}}
        result = aggregate_results(payload, [item], {item['id']:line}, stop='source_exhausted')
        result['search'].update(mode='live_agent', scenario_kind='synthetic')
        self.db.create_job({'id':bureau.JOB, 'status':'complete', 'result':result}, payload)
        self.page.goto(self.base + '#job=' + bureau.JOB)
        self.page.locator('#offer-list [data-offer]').first.wait_for()
        self.assertEqual(self.page.locator('#status-filter').input_value(), 'active')
        active_titles = self.page.locator('#offer-list .offer-title').all_text_contents()
        self.assertEqual(active_titles, ['3D-принтер CreatBot D600 PRO'])
        self.assertTrue(self.page.locator('#synthetic-label').is_visible())
        self.page.screenshot(path=str(self.out / 'product-identity-active.png'), full_page=True)
        self.assertTrue(self.page.locator('[data-show-rejected]').is_visible())
        self.page.locator('[data-show-rejected]').click()
        self.assertEqual(self.page.locator('#status-filter').input_value(), 'excluded')
        rejected_titles = self.page.locator('#offer-list .offer-title').all_text_contents()
        self.assertEqual(set(rejected_titles), {title for identifier,title in titles if identifier != 'printer'})
        self.page.screenshot(path=str(self.out / 'product-identity-rejected.png'), full_page=True)

    def test_07_manual_deviation_has_honest_soft_green_badge_after_real_review(self):
        import test_review_acceptances as acceptance
        result, payload, identifier = acceptance.fixture()
        result.setdefault('search', {}).update(mode='live_agent', scenario_kind='synthetic')
        before = next(offer for offer in result['offers'] if offer['id'] == identifier)
        original_evidence = deepcopy(before.get('evidence'))
        self.assertEqual(before['status'], 'excluded')
        self.db.create_job({'id':bureau.JOB, 'status':'complete', 'result':result}, payload)
        self.page.goto(self.base + '#job=' + bureau.JOB)
        self.page.locator('#result-screen:not([hidden])').wait_for()
        self.choose_result_status('excluded')
        self.page.locator('#offer-list [data-offer]').filter(has=self.page.locator(f'[data-evidence="{identifier}"]')).get_by_role('button', name='Разбор', exact=True).click()
        self.page.locator('[data-proof-field="attributes.channels"]').click()
        self.page.locator('#deviation-reason').fill('Учебное решение: два канала допустимы для этой закупки.')
        with self.page.expect_response(lambda response: response.url.endswith('/review') and response.request.method == 'POST') as review:
            self.page.locator('#deviation-form button[type=submit]').click()
        self.assertEqual(review.value.status, 200)
        self.page.locator('#deviation-form[data-accepted=true]').wait_for()
        accepted_row = self.page.locator('[data-fact-field="attributes.channels"]')
        self.assertIn('fact-confirmed', accepted_row.get_attribute('class'))
        self.assertIn('Отклонение принято вручную', accepted_row.inner_text())
        saved = self.db.get_job(bureau.JOB)
        accepted = next(offer for offer in saved['result']['offers'] if offer['id'] == identifier)
        self.assertEqual(accepted['status'], 'included')
        self.assertEqual(accepted['attributes']['channels'], 2)
        self.assertEqual(accepted['requirement_checks'][0]['status'], 'mismatch')
        self.assertTrue(accepted['requirement_checks'][0]['acceptance'])
        self.assertEqual(accepted.get('evidence'), original_evidence)
        self.page.locator('#close-evidence').click()
        self.choose_result_status('active')
        card = self.page.locator('#offer-list [data-offer]').filter(has=self.page.locator(f'[data-evidence="{identifier}"]'))
        badge = card.get_by_text('Принято с отклонением вручную', exact=True)
        self.assertTrue(badge.is_visible())
        color = badge.evaluate('node => getComputedStyle(node).backgroundColor')
        rgb = [float(value) for value in re.findall(r'[\d.]+', color)[:3]]
        self.assertEqual(len(rgb), 3, color)
        self.assertGreater(rgb[1], rgb[0], color)
        self.assertGreater(rgb[1], rgb[2], color)
        self.assertGreater(min(rgb), 150, color)
        self.page.screenshot(path=str(self.out / 'manual-deviation-soft-green.png'), full_page=True)

    def test_08_saved_history_loading_and_failed_get_never_start_new_search(self):
        self.seed_result(3)
        self.page.locator('.brand').click()
        history_link = self.page.locator(f'#history-list a[href="#job={bureau.JOB}"]')
        history_link.wait_for()
        job_path = '/api/jobs/' + bureau.JOB
        entered, release = threading.Event(), threading.Event()
        state = {'fail':False}
        original_get = bureau.server.Handler.do_GET

        def controlled_read(handler):
            if urlsplit(handler.path).path == job_path:
                entered.set()
                if not release.wait(timeout=8):
                    return handler.respond(503, {'error':'Учебная задержка чтения'})
                if state['fail']:
                    return handler.respond(503, {'error':'Учебный сбой чтения сохранённого поиска'})
            return original_get(handler)

        delayed = patch.object(bureau.server.Handler, 'do_GET', controlled_read)
        delayed.start()
        self.patches.append(delayed)
        posts = []
        self.page.on('request', lambda request: posts.append(urlsplit(request.url).path) if request.method == 'POST' else None)
        try:
            with self.page.expect_request(lambda request: urlsplit(request.url).path == job_path):
                history_link.click()
            self.assertTrue(entered.wait(timeout=3))
            self.assertEqual(self.page.locator('#progress-heading').inner_text(), 'Открываем сохранённый поиск')
            for selector in ('#elapsed-time','#cancel-search','#search-progress-sources'):
                self.assertFalse(self.page.locator(selector).is_visible(), selector)
            self.assertEqual(posts, [])
            self.page.screenshot(path=str(self.out / 'history-saved-loading.png'), full_page=True)
            release.set()
            self.page.locator('#offer-list [data-offer]').first.wait_for()
            self.assertEqual(self.db.get_job(bureau.JOB)['status'], 'complete')
            self.assertEqual(posts, [])

            self.page.locator('.brand').click()
            history_link.wait_for()
            state['fail'] = True
            history_link.click()
            self.page.locator('#global-error:not([hidden])').wait_for()
            self.assertNotIn('Ищем предложения', self.page.locator('#progress-heading').inner_text())
            for selector in ('#elapsed-time','#cancel-search','#search-progress-sources'):
                self.assertFalse(self.page.locator(selector).is_visible(), selector)
            self.assertEqual(posts, [])
            self.page.screenshot(path=str(self.out / 'history-saved-read-error.png'), full_page=True)
            state['fail'] = False
            with self.page.expect_response(lambda response: urlsplit(response.url).path == job_path and response.status == 200):
                self.page.locator('#global-error').get_by_role('button', name='Повторить', exact=True).click()
            self.page.locator('#offer-list [data-offer]').first.wait_for()
            self.assertEqual(self.db.get_job(bureau.JOB)['status'], 'complete')
            self.assertEqual(posts, [])
            self.assertEqual(self.searches, [])
        finally:
            release.set()

    def test_09_lazy_supplier_read_keeps_analysis_interactive_and_draft_focused(self):
        import test_supplier as supplier_fixture
        result = self.seed_running_result()
        offer = result['offers'][0]
        capture = supplier_fixture.captured(supplier_fixture.CONTACTS, url=offer['url'])
        offer['document_id'] = self.db.add_document(bureau.JOB, capture)
        offer['source_hash'] = capture['sha256']
        offer.pop('supplier_details', None)
        job = self.db.get_job(bureau.JOB)
        job.update(status='running', result=result)
        self.db.save_job(job)
        self.page.reload()
        self.page.locator('#offer-list [data-offer]').first.wait_for()
        supplier_path = '/api/jobs/' + bureau.JOB + '/suppliers/' + offer['id']
        entered, release = threading.Event(), threading.Event()
        original_get = bureau.server.Handler.do_GET

        def delayed_supplier(handler):
            if unquote(urlsplit(handler.path).path) == supplier_path:
                entered.set()
                if not release.wait(timeout=8):
                    return handler.respond(503, {'error':'Учебная задержка реквизитов'})
            return original_get(handler)

        delayed = patch.object(bureau.server.Handler, 'do_GET', delayed_supplier)
        delayed.start()
        self.patches.append(delayed)
        posts = []
        self.page.on('request', lambda request: posts.append(urlsplit(request.url).path) if request.method == 'POST' else None)
        try:
            with self.page.expect_request(lambda request: unquote(urlsplit(request.url).path) == supplier_path):
                self.page.locator('#offer-list [data-evidence]').first.click()
            self.assertTrue(entered.wait(timeout=3))
            slot = self.page.locator('#supplier-detail-slot')
            self.assertTrue(slot.is_visible())
            self.assertTrue(slot.inner_text().strip())
            self.page.locator('[data-proof-field=tax]').click()
            self.page.locator('.observed-seller-terms details > summary').first.click()
            scrolls = self.scroll_evidence()
            self.assertTrue(any(item['top'] > 0 for item in scrolls), scrolls)
            selected = self.page.locator('[data-proof-field][aria-pressed=true]').get_attribute('data-proof-field')
            pane = self.page.locator('#evidence-body').get_attribute('data-pane')
            self.page.evaluate("""() => {
                const host=document.createElement('public-feedback-widget');
                host.style.cssText='position:fixed;right:20px;bottom:20px;z-index:2147483647';
                host.attachShadow({mode:'open'}).innerHTML='<label>Учебное замечание<textarea aria-label="Учебное замечание" style="width:260px;height:80px;background:white;color:black"></textarea></label>';
                document.body.append(host);
            }""")
            feedback = self.page.locator('public-feedback-widget textarea')
            feedback.fill('Учебный черновик во время загрузки реквизитов.')
            feedback.focus()
            self.assertEqual(posts, [])
            with self.page.expect_response(lambda response: unquote(urlsplit(response.url).path) == supplier_path and response.status == 200) as response:
                release.set()
            supplier = response.value.json()
            self.assertEqual(supplier['offer_id'], offer['id'])
            self.assertIn('QA Поставщик', supplier['supplier_details']['legal_name'])
            self.page.wait_for_function("() => document.querySelector('#supplier-detail-slot').textContent.includes('QA Поставщик')")
            self.assert_evidence_state(scrolls, selected, pane)
            self.assertTrue(feedback.evaluate('node => node.getRootNode().activeElement === node'))
            self.assertTrue(self.page.evaluate("document.activeElement.localName === 'public-feedback-widget'"))
            self.assertIn('во время загрузки реквизитов', feedback.input_value())
            self.assertEqual(posts, [])
            self.page.screenshot(path=str(self.out / 'lazy-supplier-analysis-focus.png'), full_page=True)
        finally:
            release.set()


if __name__ == '__main__':
    unittest.main(verbosity=2)
