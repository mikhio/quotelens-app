/* Public view over the dated real catalog only. No invented offers or network search. */
(() => {
  'use strict';
  const saved = window.QUOTE_LENS_CATALOG;
  const clone = x => JSON.parse(JSON.stringify(x));
  const norm = x => String(x ?? '').toLowerCase().replace(/ё/g, 'е').replace(/акип/g, 'akip').replace(/в/g, 'v').replace(/[^\p{L}\p{N}]/gu, '');
  const modelNorm = x => String(x ?? '').toLowerCase().replace(/акип/g,'akip').replace(/актаком/g,'aktakom').replace(/в/g,'v').replace(/а/g,'a').replace(/с/g,'c').replace(/[–—]/g,'-').replace(/[^a-z0-9/]/g,'');
  const esc = x => String(x ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const amount = x => x == null ? 'Не рассчитано' : new Intl.NumberFormat('ru-RU', {maximumFractionDigits:2}).format(x) + ' ₽';
  const jobs = new Map();
  let sequence = 0;

  function bandwidthMatch(text) {
    const frequency=String.raw`(не\s+менее\s+|не\s+более\s+|от\s+|до\s+|[≥≤]\s*)?(\d+(?:[.,]\d+)?)\s*(мгц|mhz|ггц|ghz)(?![\p{L}\p{N}_])`;
    const label=String.raw`(?<![\p{L}\p{N}_])(?:полос[аыу](?:\s+пропускания)?|bandwidth)\s*(?:[:=–—-]\s*)?`;
    const all=[...text.matchAll(new RegExp(frequency,'giu'))], labelled=[...text.matchAll(new RegExp(label+frequency,'giu'))];
    const other=/частот[аыу]|дискретизац|sampling|sample\s+rate|тактов|генератор|процессор/i;
    if(labelled.length===1){
      const chosen=labelled[0];
      for(let i=0;i<all.length;i++){
        const candidate=all[i];
        if(chosen.index<=candidate.index&&candidate.index+candidate[0].length<=chosen.index+chosen[0].length)continue;
        const previous=i?all[i-1].index+all[i-1][0].length:0;
        const clause=text.slice(previous,candidate.index).split(/[,;\n]/).pop();
        if(!other.test(clause))return null;
      }
      return chosen;
    }
    return !labelled.length&&all.length===1&&!other.test(text)?all[0]:null;
  }

  function requestOf(text, quantity) {
    const description = text.trim();
    const known = /осциллограф|акип|akip|ads[-\s]*\d/i.test(description);
    const request = {description, product:description, category:known?'oscilloscope':'other', quantity, unit:'шт', requirements:[]};
    const add = (key,label,value,op='eq',unit='') => request.requirements.push({key,label,value,op,unit});
    const op = s => /не менее|минимум|≥|>=|от\s*$/i.test(s||'') ? 'gte' : /не более|максимум|≤|<=|до\s*$/i.test(s||'') ? 'lte' : 'eq';
    const model = description.match(/(?:АКИП|AKIP)[\s-]*\d{4}(?:\/\d+[А-ЯA-Z]?)?|\bADS[\s-]*\d{4}\b/i);
    if (model) add('model','Модель',model[0].replace(/\s+/g,''));
    const channels = description.match(/(не менее\s+|не более\s+|от\s+|до\s+|[≥≤]\s*)?(\d+)\s*[- ]?канал/i) || description.match(/(?:число|количество)\s+каналов\s*[:=]?\s*(не менее\s+|не более\s+|от\s+|до\s+|[≥≤]\s*)?(\d+)/i);
    if (channels) add('channels','Количество каналов',Number(channels[2]),op(channels[1]||''),'канала');
    const band = bandwidthMatch(description);
    if (band) add('bandwidth_mhz','Полоса пропускания',Number(band[2].replace(',','.'))*(/ггц|ghz/i.test(band[3])?1000:1),op(band[1]||''),'МГц');
    request.missing_critical_fields = known ? ['channels','bandwidth_mhz'].filter(k=>!request.requirements.some(r=>r.key===k)) : ['категория'];
    return request;
  }

  function decimal(value) {
    let s=String(value), exponent=0;
    if (/e/i.test(s)) {const pair=s.split(/e/i);s=pair[0];exponent=Number(pair[1]);}
    const parts=s.split('.');let denominator=10n**BigInt((parts[1]||'').length);
    let numerator=BigInt(parts.join(''));
    if(exponent>0) numerator*=10n**BigInt(exponent); else if(exponent<0) denominator*=10n**BigInt(-exponent);
    return [numerator,denominator];
  }
  const rounded = (numerator,denominator) => Number((2n*numerator+denominator)/(2n*denominator))/100;
  function calculation(offers,quantity) {
    const accepted=offers.filter(o=>o.status==='included');
    const suppliers=new Set(accepted.map(o=>o.attributes?.supplier_inn||norm(o.supplier))).size;
    const n=accepted.length, ready=n>=3&&suppliers>=3;
    const sum=accepted.reduce((s,o)=>s+BigInt(Math.round(o.normalized_price*100)),0n);
    const mean=n?Number(sum)/100/n:null;
    const stddev=n>1?Math.sqrt(accepted.reduce((s,o)=>s+(o.normalized_price-mean)**2,0)/(n-1)):null;
    const [q,den]=decimal(quantity);
    return {status:ready?'ready':'insufficient_data',n,independent_suppliers:suppliers,mean,stddev,
      cv:stddev==null||!mean?null:stddev/mean*100,
      unit_price:ready?rounded(sum,BigInt(n)):null,total:ready?rounded(sum*q,BigInt(n)*den):null,
      rule:'Среднее сопоставимых цен с НДС.',reason:ready?'Расчёт по сопоставимым предложениям.':'Недостаточно сопоставимых предложений для расчёта.'};
  }

  function analyze(payload) {
    const quantity=Number(payload.quantity??1);
    if(!Number.isFinite(quantity)||quantity<=0||quantity>1000000)throw Error('Укажите положительное количество.');
    const request=requestOf(String(payload.description||''),quantity);
    if(request.description.length<3)throw Error('Опишите закупку.');
    const sourceIds=payload.source_ids||saved.sources.map(s=>s.id);
    const chosenDomains=payload.source_scope?.mode==='domains'?payload.source_scope.domains:null;
    const allowed=new Set(chosenDomains||saved.sources.filter(s=>!payload.source_scope&&sourceIds.includes(s.id)||payload.source_scope?.mode==='web').map(s=>s.domain));
    const offers=request.category==='oscilloscope'?clone(saved.offers).filter(o=>allowed.has(o.domain)):[];
    for(const offer of offers) {
      const rejected=[],unknown=[];
      for(const r of request.requirements) {
        const v=offer.attributes?.[r.key];
        if(v==null){unknown.push('Не подтверждено: '+r.label.toLowerCase()+'.');continue;}
        const pass=r.op==='gte'?Number(v)>=Number(r.value):r.op==='lte'?Number(v)<=Number(r.value):typeof r.value==='number'?Number(v)===r.value:r.key==='model'?modelNorm(v)===modelNorm(r.value):norm(v)===norm(r.value);
        if(!pass) rejected.push(r.label+': '+v+(r.unit?' '+r.unit:'')+'; требуется '+({'gte':'не менее','lte':'не более','eq':''}[r.op])+' '+r.value+(r.unit?' '+r.unit:'')+'.');
      }
      if(request.missing_critical_fields.length)unknown.push('Уточните количество каналов и полосу пропускания.');
      offer.status=rejected.length?'excluded':unknown.length?'uncertain':'included';
      offer.reasons=rejected.length?rejected:unknown.length?unknown:['Соответствует требованиям.'];
      offer.normalized_price=offer.price;
    }
    const calc=calculation(offers,quantity);
    const sensitivity=offers.filter(o=>o.status==='included').map(o=>({offer_id:o.id,supplier:o.supplier,...calculation(offers.filter(i=>i.id!==o.id),quantity)}));
    const statuses=saved.sources.filter(s=>allowed.has(s.domain)).map(s=>({name:s.name,domain:s.domain,status:'available',message:'Данные на '+saved.date_label}));
    return {request,offers,calculation:calc,sensitivity,warnings:[],search:{mode:'snapshot',scenario_kind:'real_snapshot',captured_at:saved.captured_at,
      started_at:new Date().toISOString(),finished_at:new Date().toISOString(),
      source_statuses:statuses,stop:{code:'catalog_complete',label:'Предложения обработаны',detail:''},
      budget:{pages_used:0,max_pages:20,queries_used:0,max_queries:6,model_calls:0,max_model_calls:8,elapsed_seconds:0,max_seconds:90},completeness:'bounded_not_exhaustive'}};
  }

  function report(result) {
    const r=result.request,c=result.calculation;
    const titles={included:'Принято',excluded:'Исключено',uncertain:'Нужно уточнить'};
    const rows=result.offers.map(o=>'<tr><td>'+esc(o.title)+'<br>'+esc(o.supplier)+'</td><td>'+amount(o.price)+' / '+esc(o.unit)+'</td><td>'+esc(titles[o.status])+'</td><td>'+esc(o.reasons.join(' '))+'</td><td><a href="'+esc(o.url)+'">Источник</a><br>'+esc(o.retrieved_at)+'</td></tr>').join('');
    const quotes=result.offers.map(o=>Object.entries(o.evidence||{}).map(([k,v])=>'<tr><td>'+esc(o.supplier)+'</td><td>'+esc(k)+'</td><td>'+esc(v.quote)+'</td></tr>').join('')).join('');
    const material=result.offers.some(o=>o.status==='included')?'<p>Расчёт предварительный: единица цены определена по карточкам товаров и требует подтверждения; доставка не включена.</p>':'';
    return '<!doctype html><html lang="ru"><meta charset="utf-8"><title>Расчёт закупки</title><style>body{font:15px/1.6 system-ui;color:#202b33;padding:32px;max-width:1100px;margin:auto}table{border-collapse:collapse;width:100%;font-size:13px}th,td{text-align:left;vertical-align:top;padding:10px;border-bottom:1px solid #ddd}h1{font-size:24px}a{color:#275f51}</style><h1>'+esc(r.description)+'</h1><p>Цены на '+esc(saved.date_label)+'. Количество: '+r.quantity+' '+esc(r.unit)+'.</p><h2>'+amount(c.total)+'</h2><p>Средняя цена: '+amount(c.mean)+'. Принятых предложений: '+c.n+'.</p>'+material+'<table><tr><th>Предложение</th><th>Цена</th><th>Решение</th><th>Основание</th><th>Источник</th></tr>'+rows+'</table><h2>Расчёт</h2><p>Среднее принятых цен × количество: '+amount(c.unit_price)+' × '+r.quantity+' = '+amount(c.total)+'.</p><h2>Подтверждения</h2><table>'+quotes+'</table></html>';
  }

  window.QUOTE_LENS_BOOT={description:saved.description,quantity:saved.quantity};
  window.QuoteLens=Object.assign(window.QuoteLens||{}, {
    savedCatalog:true,
    request:async(path,options={})=>{
      if(path==='/api/config')return clone(saved.config);
      if(path==='/api/sources')return clone(saved.sources);
      if(path==='/api/scenarios')return [];
      if(path==='/api/search') {
        const result=analyze(JSON.parse(options.body||'{}'));
        const id='catalog-'+(++sequence);jobs.set(id,{id,status:'complete',events:[],result});
        return {id,status:'running'};
      }
      if(path.endsWith('/cancel')){const id=path.split('/')[3];const job=jobs.get(id);if(job)job.status='cancelled';return {id,status:'cancelled'};}
      if(path.startsWith('/api/jobs/')){const job=jobs.get(path.split('/')[3]);if(job)return clone(job);}
      throw Error('Запрос не удалось выполнить.');
    },
    reportURL:(id,format)=>{const job=jobs.get(id);return URL.createObjectURL(new Blob([format==='html'?report(job.result):JSON.stringify(job.result,null,2)],{type:format==='html'?'text/html':'application/json'}));}
  });
})();
