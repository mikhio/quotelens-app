/* Deterministic browser acceptance: synthetic fixtures, intercepted API, no model calls.
 * Run: node --test tests/ui-expert.spec.js
 */
const {test, before, after} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const http = require('node:http');
const {execFileSync} = require('node:child_process');
let playwright;
try { playwright = require('playwright'); }
catch { playwright = require(path.join(execFileSync('python3',['-c','import playwright;print(playwright.__path__[0])'],{encoding:'utf8'}).trim(),'driver/package')); }
const ROOT = path.resolve(__dirname,'..');
const OUTPUT = process.env.QUOTELENS_EXPERT_UI_OUTPUT;
let browser, server, base;
before(async () => {
  server = http.createServer((req,res) => {
    const pathname = new URL(req.url,'http://localhost').pathname;
    const filename = pathname === '/' ? 'static/index.html' : pathname.slice(1);
    if (!['static/index.html','static/app.js','static/style.css'].includes(filename)) { res.writeHead(404); res.end(); return; }
    res.setHeader('Content-Type',filename.endsWith('.js') ? 'text/javascript' : filename.endsWith('.css') ? 'text/css' : 'text/html');
    res.end(fs.readFileSync(path.join(ROOT,filename)));
  });
  await new Promise(resolve => server.listen(0,'127.0.0.1',resolve));
  base = `http://127.0.0.1:${server.address().port}`;
  browser = await playwright.chromium.launch({executablePath:'/usr/bin/google-chrome',headless:true,args:['--no-sandbox','--disable-dev-shm-usage']});
  if (OUTPUT) fs.mkdirSync(OUTPUT,{recursive:true});
});
after(async () => { await browser?.close(); await new Promise(resolve => server?.close(resolve)); });
const originalCondition = 'Поставка оборудования осуществляется силами и за счёт поставщика в течение 30 календарных дней. ' + 'Оборудование должно быть доставлено с комплектом документов и упаковкой, обеспечивающей сохранность. '.repeat(12);
function fixture(status='complete') {
  const request = {description:'QA Осциллограф',quantity:1,unit:'шт.',source_scope:{mode:'web',domains:[]},search_policy:{mode:'target',max_offers:3,max_seconds:600},items:[{id:'line-a',title:'QA Осциллограф',quantity:1,unit:'шт.',requirements:[{key:'channels',label:'Каналов',op:'gte',value:4}],conditions:[{key:'delivery',label:'Условия закупки',text:originalCondition,quote:originalCondition}]}]};
  const offer = {id:'offer-a',provenance:'synthetic',item_id:'line-a',title:'QA Scope 2',supplier:'QA Поставщик',domain:'supplier.invalid',url:'https://supplier.invalid/product',price:1000,normalized_price:1000,currency:'RUB',unit:'шт.',tax:'included',status:'excluded',functional_status:'mismatch',retrieved_at:'2026-11-13T12:00:00Z',attributes:{channels:2},evidence:{'attributes.channels':{quote:'QA Scope: 2 канала',url:'https://supplier.invalid/product'}},requirement_checks:[{key:'channels',label:'Каналов',expected:4,actual:2,status:'mismatch'}],condition_checks:[{key:'delivery',label:'Доставка',expected:originalCondition,actual:null,status:'unconfirmed'}],review_fields:[{key:'attributes.channels',label:'Каналов',type:'number',value:2,original_value:2,expected:4,status:'mismatch',required:true},{key:'conditions.delivery',label:'Доставка',type:'string',value:null,original_value:null,expected:originalCondition,status:'unconfirmed',required:true}],supplier_details:{legal_name:'ООО «QA Поставщик»',inn:'7701234567',email:'qa@supplier.invalid',phone:'+7 000 123-45-67',website:'https://supplier.invalid',verification:{status:'format_valid',registry_url:'https://egrul.nalog.ru/'},evidence:{inn:{quote:'ИНН 7701234567',url:'https://supplier.invalid/contacts',retrieved_at:'2026-11-13T12:00:00Z'}}}};
  const group = {required:3,count:2,ready:false,offer_ids:['offer-a'],cv:12.5,candidate_cv:41,cv_limit:33,reason:'Нужно ещё одно сопоставимое предложение.',manual_count:0};
  const result = {request,items:[{...request.items[0],request:request.items[0],target_group:group,calculation:{status:'insufficient',n:2}}],offers:[offer],calculation:{status:'insufficient',n:2},search:{mode:'live_agent',scenario_kind:'synthetic',audit:{queries:['QA осциллограф 4 канала цена'],checked_pages:[{url:'https://supplier.invalid/product',title:'QA Scope 2',sha256:'a'.repeat(64),retrieved_at:'2026-11-13T12:00:00Z',method:'http',page_kind:'product'}],conclusion:'QA: проверена одна страница; цены недостаточно.'},source_statuses:[{domain:'blocked.invalid',status:'robots_disallowed',message:'Получение запрещено правилами сайта.'}],coverage:{target_offers:3,items_total:1,items_complete:0,target_matches:2},budget:{model_calls:2,actual_usage:{prompt_tokens:100,completion_tokens:20,total_tokens:120,cost:0},actual_cost_known:false}},review:{revision:4}};
  return {id:'expert-job',status,revision:4,created_at:'2026-11-13T12:00:00Z',updated_at:'2026-11-13T12:00:00Z',request,result,search_progress:{coverage:result.search.coverage}};
}
async function setup(job=fixture(),costData=null) {
  const context = await browser.newContext({viewport:{width:1292,height:754}});
  const page = await context.newPage(); page.setDefaultTimeout(6000);
  const errors=[], calls=[]; page.on('pageerror',e=>errors.push(String(e)));
  let current=job;
  await page.route('**/api/**',async route => {
    const req=route.request(), url=new URL(req.url()), endpoint=url.pathname, body=req.postDataJSON(); calls.push({endpoint,method:req.method(),body});
    let data={}, status=200;
    if (endpoint==='/api/config') data={defaults:{min_suppliers:3}};
    else if(endpoint==='/api/sources') data=[];
    else if(endpoint==='/api/jobs') data={items:[],total:0,next_cursor:null};
    else if(endpoint==='/api/costs') data=costData || {jobs:[{id:'expert-job',title:'QA Осциллограф',model_calls:2,prompt_tokens:100,completion_tokens:20,total_tokens:120,cost_usd:0,cost_known:false,passes:[{number:1,budget:{model_calls:1,actual_usage:{total_tokens:60,cost:0},actual_cost_known:false}},{number:2,budget:{model_calls:1,actual_usage:{total_tokens:60,cost:0},actual_cost_known:true}}]}],totals:{model_calls:2,total_tokens:120,cost_usd:0,cost_known:false}};
    else if(endpoint.endsWith('/review')) {
      assert.equal(current.status,'complete'); assert.equal(body.expected_revision,current.revision);
      if(body.action==='accept_deviation') {
        const field=Object.keys(body.fields)[0], descriptor=current.result.offers[0].review_fields.find(row=>row.key===field);
        assert.equal(typeof body.fields[field],'boolean');
        descriptor.acceptance=body.fields[field] ? {reason:body.reason,revision:current.revision+1} : null;
        current.result.items[0].target_group.manual_count=body.fields[field] ? 1 : 0;
      }
      current.revision++;current.result.review.revision=current.revision;data=current;
    }
    else if(endpoint.endsWith('/continue')) { assert.equal(body.expected_revision,current.revision);current.status='running';data=current; }
    else if(endpoint.endsWith('/share')) { assert.equal(body.expected_revision,current.revision);data={url:`${base}/quotelens-share/synthetic-token/`,revision:current.revision,created_at:current.created_at}; }
    else if(endpoint==='/api/search') { status=400;data={error:'QA intentional stop; no research'}; }
    else if(endpoint==='/api/jobs/expert-job') data=current;
    else throw new Error('Unexpected endpoint '+endpoint);
    await route.fulfill({status,json:data});
  });
  const open=async () => { await page.goto(`${base}/#job=expert-job`);await page.locator('[data-offer="offer-a"]').waitFor(); };
  return {context,page,errors,calls,open,get job(){return current},close:async()=>{await context.close();assert.deepEqual(errors,[]);}};
}
async function screenshot(page,name) { if(OUTPUT) await page.screenshot({path:path.join(OUTPUT,name+'.png'),fullPage:true}); }
async function noOverflow(page) { assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false); }

test('default targets per position and ten-minute advanced setting; legacy time policy retained',async () => {
  const t=await setup();try {
    await t.page.goto(base);assert.equal(await t.page.locator('#stop-mode').inputValue(),'target');assert.equal(await t.page.locator('#search-minutes').inputValue(),'10');
    assert.equal(await t.page.locator('.search-options').getAttribute('open'),null);
    await t.page.locator('#description').fill('QA Осциллограф');await t.page.locator('#search-submit').click();await t.page.getByText('QA intentional stop; no research',{exact:true}).waitFor();
    assert.deepEqual(t.calls.find(c=>c.endpoint==='/api/search').body.search_policy,{mode:'target',max_seconds:600});
    await t.open();await t.page.locator('#edit-query').click();await t.page.locator('.search-options>summary').click();await t.page.locator('#stop-mode').selectOption('time');await t.page.locator('#search-minutes').fill('6');await t.page.locator('#search-submit').click();
    await t.page.getByText('QA intentional stop; no research',{exact:true}).waitFor();
    assert.equal(t.calls.filter(c=>c.endpoint==='/api/search').at(-1).body.search_policy.mode,'time');
  } finally {await t.close();}
});

test('running partial results allow evidence but no correction, exclusion, acceptance or share writes',async () => {
  const t=await setup(fixture('running'));try {
    await t.open();assert.equal(await t.page.locator('#progress-screen').isVisible(),true);assert.equal(await t.page.locator('#partial-result-notice').isVisible(),true);
    assert.match(await t.page.locator('#progress-offers').innerText(),/Готово позиций: 0 из 1/);
    await t.page.locator('[data-evidence="offer-a"]').last().click();
    assert.equal(await t.page.locator('#field-correction-form, #offer-review-form, #deviation-form, #share-result, #continue-search').count(),0);
    assert.match(await t.page.locator('#field-proof').innerText(),/2/);assert.equal(t.calls.some(c=>c.method==='POST'),false);
    await screenshot(t.page,'partial-readonly-desktop');await noOverflow(t.page);
    t.job.status='complete';t.job.updated_at='2026-11-13T12:01:00Z';await t.page.locator('#field-edit').waitFor();
  }finally{await t.close();}
});

test('CV and raw values remain visible while accepting and withdrawing one deviation',async () => {
  const t=await setup();try {
    await t.open();assert.match(await t.page.locator('.target-group').first().innerText(),/CV всех кандидатов: 41%/);
    await t.page.locator('[data-evidence="offer-a"]').last().click();await t.page.locator('[data-proof-field="attributes.channels"]').click();
    await t.page.locator('#deviation-reason').fill('QA разрешение заказчика');await t.page.locator('#deviation-form button[type=submit]').click();await t.page.getByRole('button',{name:'Отменить принятие отклонения',exact:true}).waitFor();
    assert.match(await t.page.locator('[data-fact-field="attributes.channels"]').innerText(),/Отклонение принято вручную/);assert.equal(t.job.result.offers[0].attributes.channels,2);assert.equal(t.job.result.offers[0].requirement_checks[0].status,'mismatch');
    assert.deepEqual(t.calls.filter(c=>c.endpoint.endsWith('/review')).at(-1).body.fields,{'attributes.channels':true});
    await screenshot(t.page,'manual-deviation-desktop');await t.page.getByRole('button',{name:'Отменить принятие отклонения',exact:true}).click();await t.page.getByRole('button',{name:'Принять отклонение для расчёта',exact:true}).waitFor();
    assert.deepEqual(t.calls.filter(c=>c.endpoint.endsWith('/review')).at(-1).body.fields,{'attributes.channels':false});
    await t.page.locator('#contract-conditions > summary').click();
    await t.page.locator('[data-proof-field="conditions.delivery"]').click();await t.page.locator('#deviation-reason').fill('QA условие принято');await t.page.locator('#deviation-form button[type=submit]').click();await t.page.getByRole('button',{name:'Отменить принятие отклонения',exact:true}).waitFor();
    assert.deepEqual(t.calls.filter(c=>c.endpoint.endsWith('/review')).at(-1).body.fields,{'conditions.delivery':true});
  }finally{await t.close();}
});

test('supplier evidence, compact original conditions and mobile layout preserve complete request',async () => {
  const t=await setup();try {
    await t.open();await t.page.locator('#edit-query').click();await t.page.locator('.spec-item-details>summary').click();
    assert.equal(await t.page.locator('.condition-label').innerText(),'Доставка');assert.equal(await t.page.locator('.condition-original').getAttribute('open'),null);await screenshot(t.page,'condition-preview-desktop');await t.page.locator('.condition-original>summary').click();assert.equal(await t.page.locator('.condition-original p').textContent(),originalCondition);
    await t.page.locator('#search-submit').click();await t.page.getByText('QA intentional stop; no research',{exact:true}).waitFor();assert.equal(t.calls.filter(c=>c.endpoint==='/api/search').at(-1).body.items[0].conditions[0].text,originalCondition);
    await t.open();await t.page.locator('[data-evidence="offer-a"]').last().click();assert.match(await t.page.locator('.supplier-details').innerText(),/7701234567/);assert.match(await t.page.locator('.supplier-verification').innerText(),/реестр не проверен/);
    await t.page.locator('.supplier-details details>summary').click();assert.equal(await t.page.locator('.supplier-details blockquote').innerText(),'ИНН 7701234567');await t.page.setViewportSize({width:390,height:844});await t.page.locator('.supplier-details').scrollIntoViewIfNeeded();await screenshot(t.page,'supplier-evidence-mobile');await noOverflow(t.page);
    await t.page.locator('#close-evidence').click();await screenshot(t.page,'target-mobile');await noOverflow(t.page);
  }finally{await t.close();}
});

test('unknown actual cost stays unknown, owner details and immutable share link use revision',async () => {
  const t=await setup();try {
    await t.open();await t.page.locator('.research-details').first().locator(':scope > summary').click();assert.match(await t.page.locator('.actual-usage').innerText(),/Стоимость\s+Неизвестна/);assert.equal(await t.page.locator('.audit-queries').getAttribute('open'),null);await t.page.locator('.audit-queries>summary').click();assert.match(await t.page.locator('.audit-queries').innerText(),/QA осциллограф 4 канала цена/);await t.page.locator('.audit-unavailable>summary').click();assert.match(await t.page.locator('.audit-unavailable').innerText(),/Доступ закрыт сайтом/);
    await t.page.locator('#open-costs').click();await t.page.locator('.costs-table').waitFor();assert.match(await t.page.locator('#costs-body').innerText(),/100 \/ 20/);assert.match(await t.page.locator('.costs-total').innerText(),/Неизвестна/);await t.page.locator('.cost-passes>summary').click();assert.match(await t.page.locator('.cost-passes').innerText(),/Проход 2/);assert.match(await t.page.locator('.cost-passes').innerText(),/Стоимость: \$0/);await t.page.locator('#close-costs').click();
    t.job.result.search.budget.actual_cost_known=true;await t.page.reload();await t.page.locator('[data-offer="offer-a"]').waitFor();await t.page.locator('.research-details').first().locator(':scope > summary').click();assert.match(await t.page.locator('.actual-usage').innerText(),/Стоимость\s+\$0/);
    await t.page.locator('#share-result').click();await t.page.locator('#share-url').waitFor();assert.match(await t.page.locator('#share-url').inputValue(),/\/quotelens-share\/synthetic-token\/$/);assert.equal(t.calls.find(c=>c.endpoint.endsWith('/share')).body.expected_revision,4);await screenshot(t.page,'share-costs-desktop');
    await t.page.locator('#continue-search').click();await t.page.locator('#partial-result-notice').waitFor();assert.deepEqual(t.calls.find(c=>c.endpoint.endsWith('/continue')).body,{max_seconds:600,expected_revision:4});assert.equal(await t.page.locator('#continue-search').count(),0);
  }finally{await t.close();}
});


test('partial receipts retain confirmed costs and tokens while full totals stay unknown',async () => {
  const job=fixture(), budget={model_calls:11,metered_calls:10,actual_cost_known:false,actual_usage_known:false,actual_usage:{prompt_tokens:60289,completion_tokens:4098,total_tokens:64387,cost:.0467011}};
  job.result.search.budget=budget;
  const costs={jobs:[{id:'expert-job',title:'Учебные данные · частичные ответы',model_calls:11,metered_calls:10,prompt_tokens:60289,completion_tokens:4098,total_tokens:64387,tokens_known:false,cost_known:false,cost_usd:null,known_cost_usd:.0467011,passes:[{number:1,budget}]},{id:'previous-job',title:'Учебные данные · завершённый поиск',model_calls:218,metered_calls:218,prompt_tokens:1000000,completion_tokens:10000,total_tokens:1010000,tokens_known:true,cost_known:true,cost_usd:.7674788}],totals:{model_calls:229,metered_calls:228,total_tokens:1074387,cost_known:false,cost_usd:.8141799}};
  const t=await setup(job,costs);try {
    await t.open();await t.page.locator('.research-details').first().locator(':scope > summary').click();
    const usage=t.page.locator('.actual-usage');assert.match(await usage.innerText(),/Подтверждено \$0,0467011/);assert.match(await usage.innerText(),/Полная стоимость неизвестна/);assert.match(await usage.innerText(),/Токены по ответам\s+64.?387/);assert.match(await usage.innerText(),/Без полного ответа: 1/);
    await t.page.locator('#open-costs').click();await t.page.locator('.costs-table').waitFor();
    assert.match(await t.page.locator('.costs-total').innerText(),/Подтверждено \$0,8141799/);assert.match(await t.page.locator('.costs-total').innerText(),/Полная стоимость неизвестна/);
    const partial=t.page.locator('.costs-table tbody tr').first(), previous=t.page.locator('.costs-table tbody tr').nth(1);
    assert.match(await partial.innerText(),/Подтверждено \$0,0467011/);assert.match(await partial.innerText(),/Без полного ответа: 1/);assert.match(await partial.innerText(),/По ответам провайдера/);assert.match(await previous.innerText(),/\$0,7674788/);assert.doesNotMatch(await previous.innerText(),/неизвестна|Без полного ответа|По ответам провайдера/i);
    await partial.locator('.cost-passes > summary').click();assert.match(await partial.locator('.cost-passes').innerText(),/токены по ответам: 64.?387/);assert.match(await partial.locator('.cost-passes').innerText(),/Подтверждено \$0,0467011/);
    await screenshot(t.page,'partial-receipts-costs-desktop');await noOverflow(t.page);
    await t.page.setViewportSize({width:390,height:844});await screenshot(t.page,'partial-receipts-costs-mobile');await noOverflow(t.page);
  }finally{await t.close();}
});
