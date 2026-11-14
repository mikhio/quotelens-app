/* Browser acceptance against explicitly synthetic, intercepted API snapshots.
 * Run: node --test tests/ui-confirm-values.spec.js — no research or model calls.
 */
const {test,before,after} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const http = require('node:http');
const {execFileSync} = require('node:child_process');
let playwright;
try { playwright = require('playwright'); }
catch { playwright = require(path.join(execFileSync('python3',['-c','import playwright;print(playwright.__path__[0])'],{encoding:'utf8'}).trim(),'driver/package')); }
const ROOT = path.resolve(__dirname,'..'), OUTPUT = process.env.QUOTELENS_CONFIRM_UI_OUTPUT;
const sourceURL = 'https://synthetic-supplier.invalid/paper';
const countryHint = {value:'Россия',quote:'Учебный бренд: бумага производится в России.',url:sourceURL,basis:'qualified_source_context',reason:'Из описания бренда; происхождение конкретной поставки требует подтверждения.',confirmed_for_product:false};
const colorHint = {value:'белый',quote:'Учебная бумага. Белизна CIE: 146%.',url:sourceURL,basis:'inferred_from_CIE_whiteness',related_field:'attributes.whiteness_cie',inferred:true,reason:'Вывод по показателю CIE; цвет отдельно не указан.',confirmed_for_product:false};
let browser,server,base;
before(async () => {
  server=http.createServer((req,res)=>{
    const pathname=new URL(req.url,'http://localhost').pathname, file=pathname==='/' ? 'static/index.html' : pathname.slice(1);
    if(!['static/index.html','static/app.js','static/style.css'].includes(file)){res.writeHead(404);res.end();return;}
    res.setHeader('Content-Type',file.endsWith('.js')?'text/javascript':file.endsWith('.css')?'text/css':'text/html');res.end(fs.readFileSync(path.join(ROOT,file)));
  });
  await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));base=`http://127.0.0.1:${server.address().port}`;
  browser=await playwright.chromium.launch({executablePath:'/usr/bin/google-chrome',headless:true,args:['--no-sandbox','--disable-dev-shm-usage']});
  if(OUTPUT) fs.mkdirSync(OUTPUT,{recursive:true});
});
after(async()=>{await browser?.close();await new Promise(resolve=>server?.close(resolve));});
const confirmation=(hint,revision)=>({kind:'manual',origin:'accepted_suggestion',value:hint.value,original_value:null,reason:'Показанное значение подтверждено пользователем',revision,supporting_source:hint});
function fixture(revision=2,countryAccepted=false,colorAccepted=false) {
  const requirements=[{key:'country',label:'Страна производства',op:'eq',value:'Россия'},{key:'color',label:'Цвет',op:'eq',value:'белый'},{key:'whiteness_cie',label:'Белизна CIE',op:'gte',value:146,unit:'%'}];
  const item={id:'paper',title:'Учебная бумага',quantity:1,unit:'пачка',requirements};
  const request={description:'Учебные данные · бумага',quantity:1,unit:'пачка',source_scope:{mode:'web',domains:[]},items:[item]};
  const offer={id:'synthetic-offer',provenance:'synthetic',item_id:'paper',title:'Учебная бумага',supplier:'Учебный поставщик',domain:'synthetic-supplier.invalid',url:sourceURL,price:300,normalized_price:300,currency:'RUB',unit:'пачка',tax:'included',status:'uncertain',retrieved_at:'2026-11-13T12:00:00Z',attributes:{country:countryAccepted?'Россия':null,color:colorAccepted?'белый':null,whiteness_cie:146},attribute_hints:{country:[countryHint],color:[colorHint]},evidence:{'attributes.whiteness_cie':{quote:colorHint.quote,url:sourceURL}},requirement_checks:[{key:'country',label:'Страна производства',expected:'Россия',actual:countryAccepted?'Россия':null,status:countryAccepted?'matches':'unconfirmed'},{key:'color',label:'Цвет',expected:'белый',actual:colorAccepted?'белый':null,status:colorAccepted?'matches':'unconfirmed'},{key:'whiteness_cie',label:'Белизна CIE',expected:146,actual:146,status:'matches'}],review_fields:[{key:'attributes.country',label:'Страна производства',type:'string',value:countryAccepted?'Россия':null,original_value:null,expected:'Россия',status:countryAccepted?'manual_confirmed':'unconfirmed',required:true,...(countryAccepted?{confirmation:confirmation(countryHint,revision)}:{confirmable_suggestion:countryHint})},{key:'attributes.color',label:'Цвет',type:'string',value:colorAccepted?'белый':null,original_value:null,expected:'белый',status:colorAccepted?'manual_confirmed':'unconfirmed',required:true,...(colorAccepted?{confirmation:confirmation(colorHint,revision)}:{confirmable_suggestion:colorHint})},{key:'attributes.whiteness_cie',label:'Белизна CIE',type:'number',unit:'%',value:146,original_value:146,expected:146,status:'matches',required:true}],reasons:['Учебные данные: другие предложения ещё не найдены.']};
  const result={request,items:[{...item,request:item,target_group:{required:3,count:1,ready:false,offer_ids:[offer.id],reason:'Учебные данные: недостаточно предложений.'},calculation:{status:'insufficient',n:1}}],offers:[offer],calculation:{status:'insufficient',n:1},search:{mode:'live_agent',scenario_kind:'synthetic',coverage:{target_offers:3,items_total:1,items_complete:0,target_matches:1}},review:{revision}};
  return structuredClone({id:'synthetic-confirm-job',status:'complete',revision,created_at:'2026-11-13T12:00:00Z',request,result});
}
const payload=(action,field,revision)=>({offer_id:'synthetic-offer',action,fields:{[field]:true},expected_revision:revision});
async function setup(initial,steps=[]) {
  const context=await browser.newContext({viewport:{width:1292,height:754}}),page=await context.newPage(),calls=[],errors=[],remaining=[...steps];let current=initial;
  page.setDefaultTimeout(6000);page.on('pageerror',error=>errors.push(String(error)));
  await page.route('**/api/**',async route=>{
    const req=route.request(),endpoint=new URL(req.url()).pathname,body=req.postDataJSON();calls.push({endpoint,method:req.method(),body});let data={},status=200;
    if(endpoint==='/api/config') data={defaults:{min_suppliers:3}};
    else if(endpoint==='/api/sources') data=[];
    else if(endpoint==='/api/jobs') data={items:[],total:0,next_cursor:null};
    else if(endpoint==='/api/jobs/synthetic-confirm-job') data=current;
    else if(endpoint.endsWith('/review')) {
      const step=remaining.shift();assert.ok(step,'Unexpected review write');assert.deepEqual(body,step.expected);
      if(step.current) current=step.current;
      data=step.response||current;status=step.status||200;
    } else throw new Error('Unexpected API endpoint '+endpoint);
    await route.fulfill({status,json:data});
  });
  const open=async()=>{await page.goto(`${base}/#job=synthetic-confirm-job`);await page.locator('[data-offer="synthetic-offer"]').waitFor();await page.locator('[data-evidence="synthetic-offer"]').last().click();await select('attributes.country');};
  const select=async field=>{const facts=page.locator('[data-evidence-pane="facts"]');if(await facts.isVisible()) await facts.click();await page.locator(`[data-proof-field="${field}"]`).click();};
  return {page,calls,open,select,get current(){return current},assertCompleted:()=>assert.equal(remaining.length,0,'All expected user actions must reach the API'),close:async()=>{await context.close();assert.deepEqual(errors,[]);}};
}
async function compactProof(page) {
  assert.equal(await page.locator('#field-edit').getAttribute('open'),null);
  assert.equal(await page.locator('#field-proof input:visible, #field-proof select:visible, #field-proof textarea:visible').count(),0);
  assert.equal(await page.locator('#field-proof .field-quote').isVisible(),true);
}
async function screenshot(page,name){if(OUTPUT)await page.screenshot({path:path.join(OUTPUT,name+'.png'),fullPage:true});}
test('shown country and inferred color accept in one click, survive reload, and undo only the selected field',async()=>{
  const t=await setup(fixture(),[
    {expected:payload('confirm_value','attributes.country',2),current:fixture(3,true)},
    {expected:payload('confirm_value','attributes.color',3),current:fixture(4,true,true)},
    {expected:payload('reset_field','attributes.country',4),current:fixture(5,false,true)}
  ]);
  try {
    await t.open();assert.equal(await t.page.locator('#evidence-body .synthetic-label').innerText(),'Учебные данные');await compactProof(t.page);
    assert.equal(await t.page.locator('#deviation-form').count(),0);assert.equal(await t.page.locator('.field-quote').innerText(),countryHint.quote);await screenshot(t.page,'shown-country-desktop');
    await t.page.getByRole('button',{name:'Принять',exact:true}).click();await t.page.locator('.shown-value-accepted').waitFor();await compactProof(t.page);
    assert.equal(await t.page.locator('.field-quote').innerText(),countryHint.quote);assert.match(await t.page.locator('.proof-origin').innerText(),/Из контекста источника.*подтверждено вами/);
    await t.select('attributes.color');await compactProof(t.page);assert.match(await t.page.locator('#field-proof').innerText(),/Вывод по CIE.*требует подтверждения/s);
    assert.match(await t.page.locator('#field-proof').innerText(),/Цвет — отдельная характеристика; CIE показывает степень белизны/);
    await t.page.getByRole('button',{name:'Принять',exact:true}).click();await t.page.locator('.shown-value-accepted').waitFor();
    await t.page.reload();await t.page.locator('[data-offer="synthetic-offer"]').waitFor();await t.page.locator('[data-evidence="synthetic-offer"]').last().click();await t.select('attributes.country');
    await t.page.getByRole('button',{name:'Отменить принятие',exact:true}).click();await t.page.getByRole('button',{name:'Принять',exact:true}).waitFor();
    assert.match(await t.page.locator('[data-fact-field="attributes.color"]').innerText(),/Принято вручную/);assert.equal(t.current.result.offers[0].attributes.color,'белый');
    await t.page.setViewportSize({width:390,height:844});await t.select('attributes.color');await compactProof(t.page);
    assert.match(await t.page.locator('#field-proof').innerText(),/Вывод по CIE.*подтверждено вами/s);assert.equal(await t.page.locator('.field-quote').innerText(),colorHint.quote);
    assert.equal(await t.page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);await screenshot(t.page,'accepted-color-mobile');
    assert.equal(t.calls.filter(call=>call.method==='POST').length,3);t.assertCompleted();
  }finally{await t.close();}
});
test('a revision conflict changes no local confirmation and reload uses the current revision',async()=>{
  const t=await setup(fixture(),[
    {expected:payload('confirm_value','attributes.country',2),status:409,current:fixture(7),response:{error:'revision_conflict'}},
    {expected:payload('confirm_value','attributes.country',7),current:fixture(8,true)}
  ]);
  try {
    await t.open();await t.page.getByRole('button',{name:'Принять',exact:true}).click();await t.page.locator('[data-reload-revision]').waitFor();
    assert.equal(await t.page.locator('.shown-value-accepted').count(),0);assert.equal(await t.page.getByRole('button',{name:'Принять',exact:true}).isEnabled(),true);
    assert.equal(await t.page.locator('#shown-value-feedback').getAttribute('role'),'alert');assert.equal(t.calls.filter(call=>call.method==='POST').length,1);
    await t.page.locator('[data-reload-revision]').click();await t.page.waitForFunction(()=>!document.querySelector('[data-reload-revision]'));
    await compactProof(t.page);await t.page.getByRole('button',{name:'Принять',exact:true}).click();await t.page.locator('.shown-value-accepted').waitFor();
    assert.equal(await t.page.locator('.field-quote').innerText(),countryHint.quote);t.assertCompleted();
  }finally{await t.close();}
});
test('an already sourced matching value needs no accept action and editing starts closed',async()=>{
  const job=fixture(),offer=job.result.offers[0];offer.attributes.country='Россия';delete offer.attribute_hints.country;
  Object.assign(offer.review_fields[0],{value:'Россия',original_value:'Россия',status:'matches'});delete offer.review_fields[0].confirmable_suggestion;
  Object.assign(offer.requirement_checks[0],{actual:'Россия',status:'matches'});offer.evidence['attributes.country']={quote:'Учебные данные. Страна производства: Россия.',url:sourceURL};
  const t=await setup(job);try {
    await t.open();await compactProof(t.page);assert.equal(await t.page.locator('#shown-value-form').count(),0);assert.match(await t.page.locator('[data-fact-field="attributes.country"]').innerText(),/Соответствует/);
    assert.equal(await t.page.locator('.field-quote').innerText(),offer.evidence['attributes.country'].quote);
    await t.page.locator('#field-edit > summary').click();assert.equal(await t.page.locator('#correction-value').isVisible(),true);assert.equal(await t.page.locator('#correction-value').inputValue(),'Россия');
    assert.equal(t.calls.some(call=>call.method==='POST'),false);
  }finally{await t.close();}
});
test('a source range, actual mismatch, and an unknown source unit keep the separate review path',async()=>{
  const job=fixture(),offer=job.result.offers[0],item=job.request.items[0];
  item.requirements.push({key:'density',label:'Плотность',op:'gte',value:80,unit:'г/м²'},{key:'thickness',label:'Толщина',op:'gte',value:100,unit:'мкм'});
  job.result.items[0].request=item;job.result.request=job.request;
  offer.attributes.whiteness_cie=[110,146];offer.attributes.density=60;offer.attributes.thickness=null;
  offer.evidence['attributes.whiteness_cie']={quote:'Учебные данные. Белизна CIE: 110–146%.',url:sourceURL,raw_value:'110–146',source_unit:'%'};
  offer.evidence['attributes.density']={quote:'Учебные данные. Плотность: 60 г/м².',url:sourceURL};
  offer.evidence['attributes.thickness']={quote:'Учебные данные. Толщина: 100.',url:sourceURL,unit_reason:'source_unit_not_stated'};
  offer.attribute_hints.whiteness_cie=[{value:[110,146],quote:offer.evidence['attributes.whiteness_cie'].quote,url:sourceURL}];
  offer.attribute_hints.thickness=[{value:100,quote:offer.evidence['attributes.thickness'].quote,url:sourceURL,unit_reason:'source_unit_not_stated'}];
  Object.assign(offer.review_fields[2],{value:[110,146],original_value:[110,146],status:'unconfirmed',reason:'Диапазон источника частично пересекается с ТЗ; нужно подтверждение.'});
  Object.assign(offer.requirement_checks[2],{actual:[110,146],status:'unconfirmed'});
  offer.review_fields.push({key:'attributes.density',label:'Плотность',type:'number',unit:'г/м²',value:60,original_value:60,expected:80,status:'mismatch',required:true},{key:'attributes.thickness',label:'Толщина',type:'number',unit:'мкм',value:null,original_value:null,expected:100,status:'unconfirmed',required:true});
  offer.requirement_checks.push({key:'density',label:'Плотность',expected:80,actual:60,status:'mismatch'},{key:'thickness',label:'Толщина',expected:100,actual:null,status:'unconfirmed'});
  const t=await setup(job);try {
    await t.open();for(const field of ['attributes.whiteness_cie','attributes.density','attributes.thickness']) {
      await t.select(field);assert.equal(await t.page.locator('#shown-value-form').count(),0);assert.equal(await t.page.getByRole('button',{name:'Принять отклонение для расчёта',exact:true}).isVisible(),true);
      assert.equal(await t.page.locator('.field-quote').isVisible(),true);assert.equal(await t.page.locator('#field-edit').getAttribute('open'),null);
    }
    assert.match(await t.page.locator('#field-proof').innerText(),/единица не указана/);
    await t.select('attributes.whiteness_cie');assert.match(await t.page.locator('#field-proof').innerText(),/110–146/);assert.match(await t.page.locator('#field-proof').innerText(),/Диапазон источника частично пересекается/);
    assert.equal(t.calls.some(call=>call.method==='POST'),false);
  }finally{await t.close();}
});
