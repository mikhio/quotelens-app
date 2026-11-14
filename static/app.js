/* Buildless QuoteLens client. Offer data and calculations come from the API. */
(() => {
  'use strict';
  const $ = (selector, scope = document) => scope.querySelector(selector);
  const $$ = (selector, scope = document) => [...scope.querySelectorAll(selector)];
  // Preserve local reading/editing state when a saved result changes in the
  // background. Focus outside this root (including a shadow widget) is untouched.
  function viewSnapshot(root) {
    const key = node => {
      if (node === root) return '';
      if (node.id) return `#${CSS.escape(node.id)}`;
      if (node.hasAttribute('data-calculation-item')) return `[data-calculation-item="${CSS.escape(node.dataset.calculationItem)}"]`;
      const parts = [];
      while (node && node !== root) {
        const siblings = [...node.parentElement.children].filter(other => other.localName === node.localName);
        parts.unshift(`${node.localName}:nth-of-type(${siblings.indexOf(node) + 1})`);
        node = node.parentElement;
      }
      return parts.join(' > ');
    };
    const active = document.activeElement;
    return {
      details: $$('details',root).map(node => [key(node),node.open]),
      scroll: [root,...$$('*',root)].filter(node => node.scrollTop || node.scrollLeft).map(node => [key(node),node.scrollTop,node.scrollLeft]),
      edits: $$('input,textarea,select',root).filter(node => node.tagName === 'SELECT' ? [...node.options].some(option => option.selected !== option.defaultSelected) : node.value !== node.defaultValue || node.checked !== node.defaultChecked).map(node => [key(node),node.value,node.checked]),
      focus: root.contains(active) ? {key:key(active),start:active.selectionStart,end:active.selectionEnd} : null,
      window:[window.scrollX,window.scrollY]
    };
  }
  function restoreView(root, saved) {
    if (!saved) return;
    const find = key => key ? $(key,root) : root;
    for (const [key,open] of saved.details) { const node = find(key); if (node?.tagName === 'DETAILS') node.open = open; }
    for (const [key,value,checked] of saved.edits) { const node = find(key); if (node?.matches('input,textarea,select')) { node.value = value; if (node.matches('input[type=checkbox],input[type=radio]')) node.checked = checked; } }
    if (saved.focus) {
      const node = find(saved.focus.key);
      if (node && !node.disabled) {
        node.focus({preventScroll:true});
        if (typeof saved.focus.start === 'number' && typeof node.setSelectionRange === 'function') {
          try { node.setSelectionRange(saved.focus.start,saved.focus.end); } catch {}
        }
      }
    }
    for (const [key,top,left] of saved.scroll) { const node = find(key); if (node) { node.scrollTop = top; node.scrollLeft = left; } }
    window.scrollTo(...saved.window);
  }
  const escape = value => String(value ?? '').replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
  const icon = name => `<svg aria-hidden="true"><use href="#i-${name}"/></svg>`;
  const numeric = value => typeof value !== 'boolean' && value !== null && value !== undefined && value !== '' && Number.isFinite(Number(value));
  const number = (value, max = 2) => numeric(value) ? new Intl.NumberFormat('ru-RU', {maximumFractionDigits:max}).format(Number(value)) : 'Не указано';
  const money = value => numeric(value) ? `${number(value)} ₽` : 'Нет цены';
  const unitNumber = value => {
    if (!numeric(value)) return 'Не указано';
    const amount = Math.abs(Number(value));
    return amount > 0 && amount < 1e-8 ? Number(value).toExponential(4).replace('.',',') : number(value,amount > 0 && amount < 1e-6 ? 8 : 6);
  };
  const unitMoney = value => numeric(value) ? `${unitNumber(value)} ₽` : 'Нет цены';
  const rubles = offer => ['rub','руб','руб.','₽'].includes(String(offer.currency || '').toLowerCase());
  const plain = value => value === null || value === undefined || value === '' ? 'Не указано' : typeof value === 'object' ? JSON.stringify(value) : String(value);
  const date = value => {
    if (!value) return null;
    const parsed = new Date(value);
    return Number.isNaN(parsed.getTime()) ? null : new Intl.DateTimeFormat('ru-RU', {day:'2-digit',month:'2-digit',year:'numeric'}).format(parsed);
  };
  const safeURL = value => {
    if (typeof value !== 'string' || !value.trim()) return null;
    try { const url = new URL(value, location.href); return ['https:','http:'].includes(url.protocol) ? url.href : null; } catch { return null; }
  };
  const safeReportURL = value => {
    try { const url = new URL(value, location.href); return url.protocol === 'blob:' && url.origin === location.origin ? url.href : safeURL(value); } catch { return null; }
  };
  const safeImage = value => {
    if (typeof value !== 'string') return null;
    if (/^data:image\/(?:png|jpeg|webp|gif|avif);base64,[a-z0-9+/=\s]+$/i.test(value)) return value;
    try { const url = new URL(value, location.href); return ['https:','http:'].includes(url.protocol) && url.origin === location.origin ? url.href : null; } catch { return null; }
  };
  const sourceLink = (url, label, classes = '') => {
    const safe = /^https?:\/\//i.test(String(url || '')) ? safeURL(url) : null;
    return safe ? `<a class="${escape(classes)}" href="${escape(safe)}" target="_blank" rel="noopener noreferrer">${escape(label)}${icon('link')}</a>` : '';
  };
  const appScript = document.currentScript?.src;
  const inferredBase = (() => {
    try {
      const path = appScript ? new URL(appScript, location.href).pathname.replace(/\/static\/app\.js$/, '') : new URL('.', location.href).pathname.replace(/\/$/, '');
      return path === '/' ? '' : path;
    } catch { return ''; }
  })();
  const apiBase = String(window.QUOTE_LENS_API_BASE ?? window.QUOTE_LENS_BOOT?.api_base ?? inferredBase).replace(/\/$/, '');
  const apiURL = path => `${apiBase}${path}`;
  const defaultRequest = async (path, options = {}) => {
    const response = await fetch(apiURL(path), {cache:'no-store',...options,headers:{'Content-Type':'application/json',...(options.headers || {})}});
    let payload;
    try { payload = await response.json(); } catch { throw new Error('Не удалось прочитать ответ сервера. Повторите попытку.'); }
    if (!response.ok) { const error = new Error(payload.error || 'Не удалось выполнить запрос. Повторите попытку.'); error.status = response.status; error.payload = payload; throw error; }
    return payload;
  };
  window.QuoteLens = Object.assign({request:defaultRequest,reportURL:(id, format) => apiURL(`/api/jobs/${encodeURIComponent(id)}/report.${format}`)}, window.QuoteLens || {});
  const api = (path, options) => window.QuoteLens.request(path, options);
  const state = {sources:[],sourceScope:{mode:'web',domains:[]},domainDraft:'',requestSourceIDs:null,sourcesLoaded:false,formBusy:false,config:null,job:null,result:null,view:'home',status:'active',source:'all',item:'all',text:'',layout:'list',page:1,pageSize:20,timer:null,clock:null,started:0,pollFailures:0,polling:null,lastFocused:null,evidenceId:null,evidenceField:'price',evidenceOverflow:null,reviewBusy:false,continueBusy:false,shareBusy:false,costsSequence:0,resumeSequence:0,initialized:false,recoveryTimer:null};
  const supplierCache = new Map();
  const draft = {files:[],items:[],documents:[],savedItems:[],savedDocuments:[],warnings:[],description:'',preparing:false,generation:0,restoredJobID:null,stage:'input',activeItem:0,removedItemIDs:[]};
  const searches = {items:[],cursor:null,total:null,loaded:false,loading:null,timer:null,error:null,generation:0,refreshQueued:false,target:20};
  const serverHistoryEnabled = () => window.QuoteLens.savedCatalog !== true;
  const validJobID = value => typeof value === 'string' && /^[a-zA-Z0-9_-]{1,100}$/.test(value);
  const jobLabels = {running:'Выполняется',complete:'Завершён',cancelled:'Остановлен',failed:'Ошибка',interrupted:'Прерван'};
  function renderWorkflow() {
    const editing = state.view === 'home' || !$('#search-form').hidden;
    const stage = draft.stage;
    if ($('#request-input')) $('#request-input').hidden = stage !== 'input';
    $('#spec-preview').hidden = stage !== 'review' || !draft.items.length;
    if ($('#research-settings')) $('#research-settings').hidden = stage !== 'research';
    if ($('#draft-stage-actions')) {
      $('#draft-stage-actions').hidden = stage === 'input';
      $('#review-back').hidden = stage !== 'review';
      $('#review-next').hidden = stage !== 'review';
      $('#research-back').hidden = stage !== 'research';
    }
    if (state.view === 'home') {
      $('#page-title').textContent = {input:'Новая закупка',review:'Проверка позиций',research:'Параметры поиска'}[stage];
      $('#search-history').hidden = stage !== 'input' || !serverHistoryEnabled();
    }
    const current = !editing || state.view === 'submitting' || state.job?.status === 'running' ? 'research' : stage;
    const order = ['input','review','research'];
    $$('#procurement-stages [data-workflow-stage]').forEach(item => {
      const active = item.dataset.workflowStage === current;
      item.classList.toggle('workflow-stage-current',active);
      item.classList.toggle('workflow-stage-complete',order.indexOf(item.dataset.workflowStage) < order.indexOf(current));
      if (active) item.setAttribute('aria-current','step'); else item.removeAttribute('aria-current');
    });
    document.body.dataset.workflowStage = current;
    $$('#request-input input, #request-input textarea, #request-input button').forEach(input => { input.disabled = state.formBusy || stage !== 'input'; });
    $$('#spec-items input, #spec-items textarea, #spec-items select, #spec-items button').forEach(input => {
      input.disabled = state.formBusy || draft.preparing || stage !== 'review' || Number(input.closest('[data-item-index]')?.dataset.itemIndex) !== draft.activeItem;
    });
    $$('#research-settings input, #research-settings select, #research-settings textarea, #research-settings button').forEach(input => {
      input.disabled = state.formBusy || stage !== 'research' || input.dataset.sourceUnavailable === 'true' || input.id === 'quantity' && !!draft.items.length || input.id === 'max-offers' && $('#stop-mode').value !== 'offers';
    });
    $$('#draft-stage-actions button, #draft-item-navigation button').forEach(button => { button.disabled = state.formBusy || draft.preparing; });
  }
  function setDraftStage(stage, focus = false) {
    draft.stage = stage !== 'input' && !draft.items.length ? 'input' : stage;
    renderWorkflow();
    if (draft.stage === 'research') $('.search-options').open = true;
    if (focus) {
      if (draft.stage === 'input') $('#description').focus();
      else if (draft.stage === 'review') $('#spec-items [data-item-index]:not([hidden]) input')?.focus();
      else ($('#stop-mode-cards input:checked') || $('#stop-mode')).focus();
    }
  }
  function selectDraftItem(index) {
    if (state.formBusy || draft.preparing || !draft.items[index]) return;
    draft.activeItem = index;
    renderSpecPreview();
  }
  function advanceDraft() {
    hideError();
    const invalid = $$('#spec-items [data-item-index]:not([hidden]) input, #spec-items [data-item-index]:not([hidden]) select, #spec-items [data-item-index]:not([hidden]) textarea').find(input => !input.checkValidity());
    if (invalid) { invalid.reportValidity(); return; }
    try {
      if (!readItems()) throw new Error('Сначала разберите текст или файлы ТЗ.');
      setDraftStage('research',true);
      specStatus('');
    } catch(error) {
      const position = error.message.match(/позиции (\d+)/);
      if (position) draft.activeItem = Number(position[1]) - 1;
      setDraftStage('review'); renderSpecPreview(); showError(error.message);
      $('#spec-items [data-item-index]:not([hidden]) input:invalid')?.reportValidity();
    }
  }
  function historyTime(value) {
    const parsed = new Date(value);
    return value && !Number.isNaN(parsed.getTime()) ? new Intl.DateTimeFormat('ru-RU', {day:'2-digit',month:'2-digit',year:'numeric',hour:'2-digit',minute:'2-digit'}).format(parsed) : null;
  }
  function renderHistory() {
    $('#search-history').hidden = state.view !== 'home' || draft.stage !== 'input' || !serverHistoryEnabled();
    $('#history-total').textContent = numeric(searches.total) ? number(searches.total,0) : '';
    $('#history-loading').hidden = !searches.loading;
    $('#history-refresh').disabled = !!searches.loading;
    $('#history-empty').hidden = !searches.loaded || !!searches.items.length || !!searches.error;
    $('#history-more').hidden = !searches.cursor;
    $('#history-more').disabled = !!searches.loading;
    $('#history-list').innerHTML = searches.items.map(item => {
      const request = item.request || {}, status = Object.hasOwn(jobLabels,item.status) ? item.status : 'unknown';
      const time = historyTime(item.created_at), quantity = item.quantity ?? request.quantity;
      const count = numeric(item.offers_count) && (item.status === 'complete' || Number(item.offers_count) > 0) ? `<span>Предложений: ${number(item.offers_count,0)}</span>` : '';
      const event = item.status === 'running' ? item.latest_event?.message : ['failed','interrupted'].includes(item.status) ? item.error : null;
      return `<li data-job-id="${escape(item.id)}"><a class="history-link" href="#job=${encodeURIComponent(item.id)}"><span class="history-main"><span class="history-description">${escape(item.description || request.description || 'Анализ закупки')}</span><span class="history-meta">${time ? `<time datetime="${escape(item.created_at)}">${escape(time)}</time>` : ''}${numeric(quantity) ? `<span>Количество: ${number(quantity)}</span>` : ''}</span>${event ? `<span class="history-event">${escape(String(event).slice(0,240))}</span>` : ''}</span><span class="history-outcome"><span class="history-status ${status}">${escape(jobLabels[item.status] || 'Статус уточняется')}</span>${count}</span></a></li>`;
    }).join('');
    const error = $('#history-error'); error.hidden = !searches.error;
    error.replaceChildren();
    if (searches.error) {
      error.append(document.createTextNode(searches.error));
      const retry = document.createElement('button'); retry.type = 'button'; retry.className = 'text-button'; retry.textContent = 'Повторить';
      retry.addEventListener('click',() => loadHistory()); error.append(retry);
    }
  }
  async function loadHistory(append = false) {
    if (!serverHistoryEnabled()) { renderHistory(); return; }
    if (searches.loading) {
      // Home and lifecycle notifications need a read begun after the event.
      // Joining the older snapshot alone can miss a newly persisted search.
      if (!append) searches.refreshQueued = true;
      return searches.loading;
    }
    clearTimeout(searches.timer); searches.timer = null; searches.error = null;
    const generation = searches.generation;
    searches.target = Math.max(20,searches.target,searches.items.length + (append ? 20 : 0));
    const operation = Promise.resolve().then(async () => {
      try {
        const target = searches.target;
        let cursor = append ? searches.cursor : null, items = append ? [...searches.items] : [], total = searches.total;
        do {
          const page = await api(`/api/jobs?${new URLSearchParams({limit:'20',...(cursor ? {cursor} : {})})}`);
          // A 202 accepted after this read started is already visible locally.
          // Keep that row and the existing pages until a current read finishes.
          if (generation !== searches.generation) return;
          if (!Array.isArray(page.items)) throw new Error('Не удалось прочитать список поисков.');
          const known = new Set(items.map(item => item.id));
          items.push(...page.items.filter(item => validJobID(item?.id) && !known.has(item.id)));
          const next = typeof page.next_cursor === 'string' && page.next_cursor ? page.next_cursor : null;
          if (next && next === cursor) throw new Error('Не удалось загрузить следующие поиски. Повторите попытку.');
          cursor = next; total = page.total;
        } while (!append && cursor && items.length < target);
        searches.items = items; searches.cursor = cursor; searches.total = total; searches.loaded = true;
      } catch(error) {
        if (generation === searches.generation) searches.error = error.message || 'Не удалось загрузить поиски. Повторите попытку.';
      } finally {
        searches.loading = null;
        if (searches.refreshQueued) {
          searches.refreshQueued = false;
          return loadHistory();
        }
        renderHistory();
        if (state.view === 'home' && !document.hidden && searches.items.some(item => item.status === 'running')) searches.timer = setTimeout(() => loadHistory(),4000);
      }
    });
    searches.loading = operation; renderHistory();
    return operation;
  }
  function acceptedSearch(job, request) {
    if (!serverHistoryEnabled()) return;
    const existing = searches.items.find(item => item.id === job.id);
    if (!existing && (searches.target > 20 || searches.items.length >= 20)) searches.target += 1;
    ++searches.generation;
    if (searches.loading) searches.refreshQueued = true;
    searches.items = [existing || {...job,request:job.request || request,description:(job.request || request).description,quantity:(job.request || request).quantity,offers_count:0},...searches.items.filter(item => item.id !== job.id)];
    if (!existing && numeric(searches.total)) searches.total += 1;
    renderHistory();
  }
  function rememberHash(id) {
    if (!validJobID(id) || !serverHistoryEnabled()) return;
    const url = new URL(location.href); url.hash = new URLSearchParams({job:id}).toString();
    try { history.replaceState(null,'',url); } catch {}
  }
  const statusLabels = {included:'В расчёте',excluded:'Вне расчёта',uncertain:'Нужны уточнения'};
  const matchesStatus = (offer, status = state.status) => status === 'all' || (status === 'active' ? offer.status !== 'excluded' : offer.status === status);
  const statusPill = value => `<span class="status-pill ${['included','excluded','uncertain'].includes(value) ? value : 'uncertain'}">${escape(statusLabels[value] || statusLabels.uncertain)}</span>`;
  function functionalPill(offer) {
    const status = offer.functional_status;
    const accepted = Object.keys(offer.manual_acceptances || {}).length || [...(offer.review_fields || []),...(offer.requirement_checks || []),...(offer.condition_checks || [])].some(field => field.acceptance);
    const mismatches = [
      ...(offer.requirement_checks || []).map(check => [check,checkField(check)]),
      ...(offer.condition_checks || []).map(check => [check,checkField(check,'conditions')])
    ].filter(([check]) => check.status === 'mismatch');
    const unresolved = mismatches.some(([check,field]) => !check.acceptance && !reviewField(offer,field)?.acceptance && !offer.manual_acceptances?.[field]);
    // Raw mismatches remain in the audit after an explicit field acceptance.
    // A different product or any remaining unaccepted mismatch stays rejected.
    if (accepted && offer.status !== 'excluded' && offer.product_identity?.status !== 'mismatch' && !unresolved && (status !== 'mismatch' || mismatches.length)) return '<span class="status-pill included">Принято с отклонением вручную</span>';
    const label = {matches:'Соответствует запросу',mismatch:'Не соответствует',unconfirmed:'Проверить характеристики',not_specified:'Требования не заданы'}[status];
    const field = firstReviewField(offer);
    if (field && (offer.status === 'uncertain' || ['unconfirmed','mismatch'].includes(status))) return `<button type="button" class="status-pill uncertain verify-action" data-evidence="${escape(offer.id)}" data-evidence-field="${escape(field)}">${status === 'unconfirmed' || status === 'mismatch' ? 'Проверить характеристики' : 'Уточнить условия'} →</button>`;
    if (status === 'unconfirmed') return `<button type="button" class="status-pill uncertain verify-action" data-evidence="${escape(offer.id)}">Проверить предложение →</button>`;
    return label ? `<span class="status-pill ${status === 'matches' ? 'included' : status === 'mismatch' ? 'excluded' : 'uncertain'}">${label}</span>` : '';
  }
  const attributeLabels = {brand:'Бренд',channels:'Каналов',bandwidth_mhz:'Полоса, МГц',format:'Формат',density_gsm:'Плотность, г/м²',sheets_per_pack:'Листов в пачке',model:'Модель',product:'Товар',service:'Услуга',supplier_inn:'ИНН поставщика',supplier_group:'Группа поставщика'};
  const evidenceLabels = {price:'Цена',title:'Наименование',supplier:'Поставщик',unit:'Единица цены',pack_size:'Упаковка',currency:'Валюта',tax:'НДС',delivery:'Доставка',...Object.fromEntries(Object.entries(attributeLabels).map(([key,value]) => [`attributes.${key}`,value]))};
  function showError(message, retry = null) {
    const node = $('#global-error'); node.replaceChildren(document.createTextNode(message));
    if (retry) { const button = document.createElement('button'); button.type = 'button'; button.className = 'text-button'; button.textContent = 'Повторить'; button.addEventListener('click', () => { node.hidden = true; state.pollFailures = 0; retry(); }); node.append(button); }
    node.hidden = false;
  }
  const hideError = () => { $('#global-error').hidden = true; };
  function clearTimers() { clearTimeout(state.timer); clearInterval(state.clock); clearTimeout(searches.timer); state.timer = null; state.clock = null; searches.timer = null; }
  function showSelected() {
    $('#search-history').hidden = true; $('#all-searches').hidden = false;
  }
  function restoreRequest(request) {
    if (!request) return;
    if (request.description || request.product) $('#description').value = request.description || request.product;
    if (numeric(request.quantity)) $('#quantity').value = request.quantity;
    if (request.source_scope && ['web','domains'].includes(request.source_scope.mode)) {
      state.requestSourceIDs = null;
      const domains = request.source_scope.mode === 'domains' && Array.isArray(request.source_scope.domains) ? request.source_scope.domains.map(String) : [];
      state.sourceScope = {mode:request.source_scope.mode,domains};
      state.domainDraft = domains.join(', ');
    } else {
      state.requestSourceIDs = Array.isArray(request.source_ids) ? request.source_ids.map(String) : null;
      restoreLegacyScope();
    }
    if (state.job?.id && draft.restoredJobID !== state.job.id) {
      ++draft.generation; draft.files = []; draft.items = clone(request.items || []); draft.removedItemIDs = [];
      draft.documents = clone(state.result?.input_documents || request.input_documents || []);
      draft.savedItems = clone(draft.items); draft.savedDocuments = clone(draft.documents);
      draft.warnings = []; draft.description = $('#description').value.trim(); draft.restoredJobID = state.job.id;
      draft.activeItem = 0; draft.stage = 'research';
      if (request.search_policy) { $('#stop-mode').value = request.search_policy.mode; $('#max-offers').value = request.search_policy.max_offers || 100; $('#search-minutes').value = (request.search_policy.max_seconds || 600) / 60; renderSearchPolicy(); }
      renderSpecPreview();
    }
    renderSourcePicker();
  }
  function showHome(updateURL = true) {
    ++state.resumeSequence; clearTimers(); hideError();
    if ($('#evidence-dialog').open) closeEvidence();
    state.view = 'home'; state.job = null; state.result = null; state.pollFailures = 0;
    $('#query-summary').hidden = true; $('#search-form').hidden = false; $('#page-title').hidden = false;
    $('#edit-query').setAttribute('aria-expanded','false');
    document.body.classList.remove('has-results'); $('#page-title').classList.remove('sr-only'); $('#page-title').textContent = 'Новая закупка';
    $('#result-screen').hidden = true; $('#progress-screen').hidden = true; $('#all-searches').hidden = true;
    setFormBusy(false);
    setDraftStage('input');
    if (updateURL) { const url = new URL(location.href); url.hash = ''; try { history.replaceState(null,'',url); } catch {} }
    renderHistory(); loadHistory();
  }
  function restoreLegacyScope() {
    const ids = state.requestSourceIDs;
    const selected = state.sources.filter(source => ids?.includes(String(source.id)));
    const broad = !ids?.length || ids.includes('web') || selected.some(source => source.kind === 'web');
    const domains = broad ? [] : [...new Set(selected.map(source => canonicalDomain(source.domain)).filter(Boolean))];
    state.sourceScope = {mode:broad ? 'web' : 'domains',domains};
    state.domainDraft = domains.join(', ');
  }
  function updateSourceSummary() {
    const count = state.domainDraft.split(/[\s,]+/).filter(Boolean).length;
    $('#source-picker-summary').innerHTML = `Поиск: ${state.sourceScope.mode === 'web' ? 'весь интернет' : `сайты${count ? ` (${count})` : ''}`}${icon('chevron')}`;
  }
  function renderSourcePicker() {
    updateSourceSummary();
    const presets = state.sources.filter(source => source.kind !== 'web' && source.id !== 'web');
    const selected = new Set(state.domainDraft.split(/[\s,]+/).filter(Boolean).map(canonicalDomain));
    const presetDomains = new Set(presets.map(source => canonicalDomain(source.domain)).filter(Boolean));
    const custom = state.domainDraft.split(/[\s,]+/).filter(domain => domain && !presetDomains.has(canonicalDomain(domain))).join(', ');
    $('#source-options').innerHTML = `<legend class="sr-only">Область поиска</legend><label class="source-option"><input type="radio" name="source_scope_mode" value="web"${state.sourceScope.mode === 'web' ? ' checked' : ''}><span>Весь открытый интернет</span></label><label class="source-option"><input type="radio" name="source_scope_mode" value="domains"${state.sourceScope.mode === 'domains' ? ' checked' : ''}><span>Выбранные источники</span></label><div id="source-domain-field" class="source-domain-field"${state.sourceScope.mode === 'web' ? ' hidden' : ''}><div class="source-presets">${presets.map(source => {
      const domain = canonicalDomain(source.domain), selectable = !!domain && (source.selectable === true || source.connected === true);
      return `<label class="source-preset"><input type="checkbox" data-source-domain="${escape(domain)}" data-source-unavailable="${!selectable}"${selected.has(domain) ? ' checked' : ''}${selectable ? '' : ' disabled'}><span><strong>${escape(source.name || domain)}</strong><small>${escape(domain || source.description || 'Адрес не указан')}${domain && !selectable ? ' · пока недоступен' : domain && source.connected !== true ? ' · доступ проверится при поиске' : ''}</small></span></label>`;
    }).join('')}</div>${!presets.length ? `<p class="muted">${state.sourcesLoaded ? 'Список источников пуст. Можно добавить сайт ниже.' : 'Список известных источников загружается. Можно добавить сайт ниже.'}</p>` : ''}<details class="source-custom"${custom ? ' open' : ''}><summary>Добавить другие сайты</summary><label for="source-domains">Домены сайтов</label><textarea id="source-domains" name="source_domains" rows="2" maxlength="2600" placeholder="example.ru" aria-describedby="source-domain-hint" spellcheck="false" autocapitalize="none">${escape(custom)}</textarea><p id="source-domain-hint">До 32 выбранных сайтов вместе с добавленными. Домены без https:// и пути.</p></details></div>`;
    renderWorkflow();
  }
  function collectSourceDomains() {
    const chosen = $$('#source-options [data-source-domain]:checked').map(input => input.dataset.sourceDomain);
    const custom = $('#source-domains')?.value.split(/[\s,]+/).filter(Boolean) || [];
    state.domainDraft = [...new Set([...chosen,...custom])].join(', ');
    state.sourceScope.domains = state.domainDraft.split(/[\s,]+/).filter(Boolean);
    updateSourceSummary(); $('#source-error').hidden = true;
  }
  function readSourceScope() {
    if (state.sourceScope.mode === 'web') return {mode:'web',domains:[]};
    const tokens = state.domainDraft.split(/[\s,]+/).filter(Boolean), domains = [];
    if (!tokens.length) throw new Error('Укажите хотя бы один сайт для поиска.');
    for (const token of tokens) {
      if (/[\/:@?#\\]/.test(token)) throw new Error('Укажите домены без https://, пути и параметров.');
      let hostname;
      try { hostname = new URL(`https://${token}`).hostname.toLowerCase().replace(/^www\./,''); } catch { throw new Error('Проверьте домены сайтов. Например: radioelementy.ru.'); }
      if (hostname.length > 253 || !hostname.includes('.') || !hostname.split('.').every(label => /^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$/.test(label)) || /^[\d.]+$/.test(hostname) || /(?:^|\.)(?:localhost|local|internal|lan|home|invalid|test|example|onion)$/.test(hostname)) throw new Error('Укажите общедоступные домены сайтов. Например: radioelementy.ru.');
      if (!domains.includes(hostname)) domains.push(hostname);
    }
    if (domains.length > 32) throw new Error('Для одного поиска можно указать до 32 сайтов.');
    return {mode:'domains',domains};
  }
  const clone = value => JSON.parse(JSON.stringify(value));
  const operators = {eq:'=',gte:'≥',lte:'≤',gt:'>',lt:'<',between:'Диапазон',contains:'Содержит',in:'Один из'};
  function requirementText(value) { return Array.isArray(value) ? value.map(plain).join(' … ') : plain(value) === 'Не указано' ? '' : plain(value); }
  function displayRequirement(value, unit) {
    const text = requirementText(value).trim(), suffix = String(unit || '').trim();
    return suffix && !text.toLocaleLowerCase('ru').endsWith(suffix.toLocaleLowerCase('ru')) ? `${text} ${suffix}` : text;
  }
  function requirementInputValue(requirement) {
    const unit = String(requirement.unit || '').trim();
    const strip = value => {
      if (typeof value !== 'string' || !unit) return value;
      const text = value.trim();
      if (!text.toLocaleLowerCase('ru').endsWith(unit.toLocaleLowerCase('ru'))) return value;
      const numberPart = text.slice(0,-unit.length).trim();
      // Only a numeric value followed by exactly the stated unit is safe to split.
      return /^[+\-\d.,xх×/\s…]+$/.test(numberPart) && /\d/.test(numberPart) ? numberPart : value;
    };
    return requirementText(Array.isArray(requirement.value) ? requirement.value.map(strip) : strip(requirement.value));
  }
  function parseRequirementValue(raw, requirement) {
    const value = raw.trim(), asNumber = text => { const n = text.replace(/\s+/g,'').replace(',','.'); return /^[-+]?\d+(?:\.\d+)?$/.test(n) ? Number(n) : text.trim(); };
    if (requirement.op === 'between') {
      const parts = value.split(/\s*(?:…|\.\.|;)\s*/).filter(Boolean).map(asNumber);
      if (parts.length !== 2 || !parts.every(numeric) || Number(parts[0]) > Number(parts[1])) throw new Error('Для диапазона укажите две границы: например, 77 … 83.');
      return parts.map(Number);
    }
    if (requirement.op === 'in') return value.split(/\s*;\s*/).filter(Boolean).map(asNumber);
    return asNumber(value);
  }
  function specStatus(message, error = false) {
    const node = $('#spec-preparation'); node.textContent = message; node.hidden = !message;
    node.classList.toggle('error',error); node.setAttribute('role',error ? 'alert' : 'status');
  }
  function clearPrepared() {
    draft.items = []; draft.documents = []; draft.warnings = []; draft.description = '';
    renderSpecPreview();
  }
  function renderFiles() {
    const localNames = new Set(draft.files.map(file => file.name));
    const stored = draft.documents.filter(doc => !localNames.has(doc.name) && doc.name !== 'Текст запроса' && doc.origin !== 'user_edit');
    $('#spec-file-list').innerHTML = draft.files.map((file,index) => `<li><span>${icon('doc')}${escape(file.name)} <small>${number(file.size / 1024,0)} КБ</small></span><button type="button" class="text-button" data-remove-file="${index}" aria-label="Удалить файл ${escape(file.name)}">Убрать</button></li>`).join('') + stored.map(doc => `<li><span>${icon('doc')}${escape(doc.name)}</span><button type="button" class="text-button" data-remove-document="${escape(doc.id || doc.sha256 || doc.name)}" aria-label="Убрать файл ${escape(doc.name)}">Убрать</button></li>`).join('');
    $$('#spec-file-list button').forEach(button => { button.disabled = state.formBusy || draft.preparing; });
  }
  const conditionText = condition => typeof condition === 'string' ? condition : condition.text || condition.quote || requirementText(condition.value);
  function conditionLabel(condition) {
    const text = conditionText(condition).replace(/\s+/g,' ').trim(), label = typeof condition === 'object' ? condition.label : '';
    const keyLabels = {delivery:'Доставка',delivery_address:'Место поставки',delivery_time:'Срок поставки',warranty:'Гарантия',payment:'Оплата',documentation:'Документация',storage:'Хранение',quality:'Качество товара',packaging:'Упаковка'};
    const key = typeof condition === 'object' ? condition.key || condition.type : '';
    if (label && label !== key && label.length <= 70 && !/^(Условия (закупки|позиции|поставки)|Условие ТЗ|Дополнение к ТЗ)$/i.test(label)) return label;
    const primary = text.split(/[.!?](?:\s|$)/)[0].slice(0,220);
    if (/товар.{0,50}новым|не бывш.{0,20}употреб/i.test(primary)) return 'Состояние товара';
    if (/свободн.{0,20}прав.{0,20}треть/i.test(primary)) return 'Права на товар';
    if (/документац|сертифик|паспорт/i.test(primary)) return 'Документация';
    if (/устранени.{0,35}неисправ|ремонт/i.test(primary)) return 'Устранение неисправностей';
    if (/качество.{0,25}материал|качеств.{0,20}товар/i.test(primary)) return 'Качество товара';
    if (keyLabels[key]) return keyLabels[key];
    if (/гарант/i.test(text)) return 'Гарантия';
    if (/оплат|расч[её]т.{0,10}поставщик/i.test(text)) return 'Оплата';
    if (/срок.{0,25}постав|постав.{0,25}срок|в течение.{0,15}дн/i.test(text)) return 'Срок поставки';
    if (/достав/i.test(text)) return 'Доставка';
    if (/сертифик|документац|паспорт/i.test(text)) return 'Документация';
    if (/упаков/i.test(text)) return 'Упаковка';
    if (/хранени/i.test(text)) return 'Хранение';
    return text.length <= 70 ? text : 'Дополнительное условие поставки';
  }
  function conditionPreview(condition) {
    const text = conditionText(condition), label = conditionLabel(condition);
    return `<li><span class="condition-label">${escape(label)}</span><details class="condition-original"><summary>Точное условие из ТЗ</summary><p>${escape(text)}</p></details></li>`;
  }
  function requirementProof(requirement) {
    const quote = requirement.original_quote || requirement.quote || requirement.source?.original?.quote || requirement.source?.quote;
    const name = requirement.source?.original?.document_name || requirement.source?.document_name;
    return `<div class="requirement-proof">${requirement.manual_override ? '<span class="manual-confirmation">Изменено вручную</span>' : ''}${quote ? `<details><summary>Исходное требование${name ? ` · ${escape(name)}` : ''}</summary><blockquote>${escape(quote)}</blockquote></details>` : name ? `<small>${escape(name)}</small>` : ''}</div>`;
  }
  function renderSpecPreview() {
    draft.activeItem = Math.max(0,Math.min(draft.activeItem,draft.items.length - 1));
    $('#spec-preview').hidden = !draft.items.length || draft.stage !== 'review';
    $('#single-quantity-field').hidden = !!draft.items.length;
    $('#quantity').required = !draft.items.length; $('#quantity').disabled = state.formBusy || !!draft.items.length;
    $('#spec-item-count').textContent = draft.items.length ? `Позиций: ${draft.items.length}` : '';
    $('#spec-warnings').hidden = !draft.warnings.length;
    $('#spec-warnings').innerHTML = draft.warnings.length ? `<ul>${draft.warnings.map(w => `<li>${escape(typeof w === 'string' ? w : w.message || plain(w))}</li>`).join('')}</ul>` : '';
    if ($('#draft-item-navigation')) $('#draft-item-navigation').innerHTML = draft.items.map((item,index) => `<button type="button" class="draft-item-nav-button" data-draft-item="${index}" aria-current="${index === draft.activeItem ? 'true' : 'false'}"><span>${index + 1}</span><span>${escape(item.title || 'Без названия')}<small>${numeric(item.quantity) ? number(item.quantity,6) : 'Количество не указано'} ${escape(item.unit || '')}</small></span></button>`).join('');
    $('#spec-items').innerHTML = draft.items.map((item,index) => `<article class="spec-item" data-item-index="${index}"><div class="spec-item-heading"><span class="spec-position">${index + 1}</span><label><span>Наименование позиции</span><input type="text" data-item-field="title" value="${escape(item.title || '')}" maxlength="500" required></label><button type="button" class="text-button" data-remove-item="${index}">Убрать позицию</button></div><div class="spec-item-quantity"><label>Количество<input type="number" data-item-field="quantity" value="${numeric(item.quantity) ? escape(item.quantity) : ''}" min="0.000001" max="1000000" step="any" placeholder="Укажите" required></label><label>Единица<input type="text" data-item-field="unit" value="${escape(item.unit || '')}" maxlength="40" placeholder="шт., пачка…"></label></div>${(item.warnings || []).length ? `<ul class="spec-item-warnings">${item.warnings.map(w => `<li>${escape(typeof w === 'string' ? w : w.message || plain(w))}</li>`).join('')}</ul>` : ''}<details class="spec-item-details" open><summary>Требования и условия <span>${item.requirements?.length || 0}</span></summary><div class="spec-requirements">${(item.requirements || []).map((req,ri) => `<div class="spec-requirement" data-requirement-index="${ri}"><label><span>Характеристика</span><input data-requirement-field="label" value="${escape(req.label || req.key)}" maxlength="200" required></label><label><span>Условие</span><select data-requirement-field="op">${Object.entries(operators).map(([key,label]) => `<option value="${key}"${({'>=':'gte','<=':'lte','=':'eq'}[req.op] || req.op) === key || (!req.op && key === 'eq') ? ' selected' : ''}>${label}</option>`).join('')}</select></label><label><span>Значение</span><input data-requirement-field="value" value="${escape(req._inputValue ?? requirementInputValue(req))}" required></label><label><span>Единица</span><input data-requirement-field="unit" value="${escape(req.unit || '')}" maxlength="40"></label><button type="button" class="text-button" data-remove-requirement="${ri}" aria-label="Убрать требование ${escape(req.label || req.key)}">Убрать</button>${requirementProof(req)}</div>`).join('')}</div><button type="button" class="text-button spec-add-requirement" data-add-requirement="${index}">Добавить требование</button>${(item.conditions || []).length ? `<div class="spec-conditions"><h3>Условия поставки</h3><ul>${item.conditions.map(conditionPreview).join('')}</ul></div>` : ''}<label class="spec-description-label">Описание из ТЗ<textarea data-item-field="description" rows="3" maxlength="25000">${escape(item.description || '')}</textarea></label><div class="spec-reparse"><button type="button" class="text-button" data-reparse-item="${index}">Обновить требования из описания</button><span data-item-dirty${item._descriptionDirty ? '' : ' hidden'}>Описание изменено — обновите требования перед поиском.</span></div></details></article>`).join('');
    $$('#spec-items input, #spec-items textarea, #spec-items select, #spec-items button').forEach(input => { input.disabled = state.formBusy || draft.preparing; });
    $$('#spec-items [data-item-index]').forEach(article => {
      article.hidden = Number(article.dataset.itemIndex) !== draft.activeItem;
      $$('[data-requirement-index]',article).forEach(row => {
        const req = draft.items[Number(article.dataset.itemIndex)].requirements?.[Number(row.dataset.requirementIndex)];
        $('[data-requirement-field="value"]',row)?.setCustomValidity(req?._error || '');
      });
    });
    renderFiles();
    renderWorkflow();
  }
  async function fileData(file) {
    return new Promise((resolve,reject) => { const reader = new FileReader(); reader.onerror = () => reject(new Error(`Не удалось прочитать файл «${file.name}».`)); reader.onload = () => resolve({name:file.name,data:String(reader.result).split(',')[1] || ''}); reader.readAsDataURL(file); });
  }
  const isTextInputDocument = document => document.name === 'Текст запроса' && (!document.format || document.format === 'text');
  const isFileInputDocument = document => document.origin !== 'user_edit' && !isTextInputDocument(document) && (!!document.format && document.format !== 'text' || /\.(docx|pdf|txt|md)$/i.test(document.name || ''));
  function sourceReferencesDocument(source, document) {
    if (!source || !document) return false;
    const original = source.original && sourceReferencesDocument(source.original,document);
    if (source.document_id && document.id) return source.document_id === document.id || !!original;
    if (source.sha256 && document.sha256) return source.sha256 === document.sha256 || !!original;
    return !!original || !!document.name && (source.document_name === document.name || source.name === document.name);
  }
  function itemReferencesDocument(item, document) {
    return sourceReferencesDocument(item.source,document) || (item.requirements || []).some(requirement => sourceReferencesDocument(requirement.source,document)) || (item.conditions || []).some(condition => sourceReferencesDocument(condition.source,document));
  }
  function itemComesFromFile(item, document) {
    return sourceReferencesDocument(item.source,document) || !!document.id && String(item.id || '').startsWith(document.id + '-');
  }
  function isSharedNote(condition, documents) {
    if (!condition || typeof condition !== 'object') return false;
    if (condition.source?.document_name === 'Текст запроса') return true;
    // A matching label never turns a condition from the file itself into the
    // separate note entered alongside that file.
    if (documents.some(document => isFileInputDocument(document) && sourceReferencesDocument(condition.source,document))) return false;
    return condition.label === 'Дополнение к ТЗ' || documents.some(document => isTextInputDocument(document) && sourceReferencesDocument(condition.source,document));
  }
  function pruneDraftDocuments() {
    draft.documents = draft.documents.filter(document => (!isFileInputDocument(document) && !isTextInputDocument(document) && document.origin !== 'user_edit') || draft.items.some(item => itemReferencesDocument(item,document)));
    draft.savedDocuments = clone(draft.documents);
  }
  async function prepareDraft() {
    if (draft.preparing || state.formBusy || state.job?.status === 'running') return false;
    const description = $('#description').value.trim();
    const previousItems = clone(draft.items), previousDocuments = clone(draft.documents);
    const previousFiles = previousDocuments.filter(isFileInputDocument);
    const activeFiles = previousFiles.filter(document => previousItems.some(item => itemComesFromFile(item,document)));
    const hasFileContext = activeFiles.length > 0;
    if (previousItems.length && !draft.files.length && description === draft.description && !hasFileContext) {
      setDraftStage('review',true); specStatus(''); return true;
    }
    if (!description && !draft.files.length && !hasFileContext) { specStatus('Введите текст ТЗ или прикрепите файл.',true); return false; }
    const generation = ++draft.generation;
    draft.preparing = true; setFormBusy(true); specStatus('Разбираем позиции и требования…');
    try {
      const files = await Promise.all(draft.files.map(fileData));
      const contextOnly = hasFileContext && !files.length;
      // With saved file positions the free text is shared context, even when
      // the original file is no longer attached as raw bytes in this browser.
      const prepared = await api('/api/prepare',{method:'POST',body:JSON.stringify({description,files,...(contextOnly ? {context_only:true} : {})})});
      if (generation !== draft.generation) return false;
      if (!Array.isArray(prepared.items) || !Array.isArray(prepared.documents) || contextOnly && !Array.isArray(prepared.conditions)) throw new Error('Не удалось прочитать результат разбора ТЗ.');
      const freshItems = clone(prepared.items), freshDocuments = clone(prepared.documents);
      const freshFiles = freshDocuments.filter(isFileInputDocument);
      const replacedFiles = previousFiles.filter(old => freshFiles.some(fresh => fresh.name === old.name && fresh.id !== old.id));
      const removed = new Set(draft.removedItemIDs);
      const oldByID = new Map(previousItems.map(item => [item.id,item]));
      let items;
      if (contextOnly) {
        items = previousItems;
      } else if (files.length) {
        const freshByID = new Map(freshItems.map(item => [item.id,item]));
        items = previousItems.filter(item => activeFiles.some(document => itemComesFromFile(item,document)) && !replacedFiles.some(document => itemComesFromFile(item,document))).map(item => {
          const fresh = freshByID.get(item.id);
          // A stable file hash means the edited item is authoritative. Keep
          // requirements (including removals), dirty descriptions and sources;
          // only the newly parsed shared/file conditions replace old ones.
          return fresh ? {...item,conditions:clone(fresh.conditions || [])} : item;
        });
        const keptIDs = new Set(items.map(item => item.id));
        for (const fresh of freshItems) {
          const reattachedKnownFile = previousFiles.some(document => itemComesFromFile(fresh,document));
          if (keptIDs.has(fresh.id) || removed.has(fresh.id) || reattachedKnownFile && !oldByID.has(fresh.id)) continue;
          items.push(fresh); keptIDs.add(fresh.id);
        }
      } else {
        // Changed text-only requests are parsed as a new set of positions;
        // they must not inherit the previous text's items or constraints.
        items = freshItems.filter(item => !removed.has(item.id));
      }
      if (!items.length) throw new Error('Позиций для поиска нет. Добавьте ТЗ или проверьте удалённые позиции.');
      const noteDocuments = [...previousDocuments,...freshDocuments];
      const notes = contextOnly ? clone(prepared.conditions) : freshItems.flatMap(item => (item.conditions || []).filter(condition => isSharedNote(condition,freshDocuments)));
      const seenNotes = new Set();
      const sharedNotes = notes.filter(condition => {
        const key = JSON.stringify([condition.source?.document_id,condition.key,condition.text]);
        if (seenNotes.has(key)) return false; seenNotes.add(key); return true;
      });
      const allFiles = [...activeFiles.filter(document => !replacedFiles.includes(document)),...freshFiles];
      for (const item of items) if (allFiles.some(document => itemComesFromFile(item,document))) {
        item.conditions = [...(item.conditions || []).filter(condition => !isSharedNote(condition,noteDocuments)),...clone(sharedNotes)];
      }
      const documents = [...previousDocuments.filter(document => !isTextInputDocument(document) && !replacedFiles.includes(document)),...freshDocuments], seenDocuments = new Set();
      draft.items = items;
      draft.documents = documents.filter(document => { const key = document.sha256 || document.id || document.name; if (seenDocuments.has(key)) return false; seenDocuments.add(key); return true; });
      pruneDraftDocuments();
      draft.savedItems = clone(draft.items); draft.files = [];
      draft.warnings = [...clone(prepared.warnings || []),...replacedFiles.map(document => `Файл «${document.name}» изменён. Позиции разобраны заново; проверьте значения.`)];
      draft.description = description; draft.restoredJobID = null; draft.stage = 'review';
      specStatus(''); renderSpecPreview(); return true;
    } catch(error) { if (generation === draft.generation) { setDraftStage('input'); specStatus(error.message || 'Не удалось разобрать ТЗ.',true); } return false; }
    finally { if (generation === draft.generation) { draft.preparing = false; setFormBusy(false); renderFiles(); if (draft.stage === 'review') $('#spec-items [data-item-index]:not([hidden]) input')?.focus(); } }
  }
  async function addFiles(files) {
    if (state.formBusy || draft.preparing) return;
    const combined = [...draft.files];
    try {
      for (const file of files) {
        if (!/\.(docx|txt|md|pdf)$/i.test(file.name)) throw new Error(`Формат «${file.name}» не поддерживается. Выберите DOCX, TXT, MD или PDF с текстом.`);
        if (file.size > 5 * 1024 * 1024) throw new Error(`Файл «${file.name}» больше 5 МБ.`);
        if (!combined.some(old => old.name === file.name && old.size === file.size && old.lastModified === file.lastModified)) combined.push(file);
      }
      if (combined.length > 8 || combined.reduce((total,file) => total + file.size,0) > 12 * 1024 * 1024) throw new Error('Можно прикрепить до 8 файлов общим размером не больше 12 МБ.');
      draft.files = combined; renderFiles();
      setDraftStage('input'); specStatus('Файл добавлен. Нажмите «Проверить позиции», когда ТЗ готово.');
    } catch(error) { specStatus(error.message,true); }
    $('#spec-files').value = '';
  }
  async function reparseItem(index) {
    const item = draft.items[index]; if (!item || draft.preparing || state.formBusy) return;
    const generation = ++draft.generation, description = item.description;
    draft.preparing = true; setFormBusy(true); specStatus('Обновляем требования позиции…');
    try {
      const parsed = await api('/api/prepare',{method:'POST',body:JSON.stringify({description})});
      if (generation !== draft.generation || draft.items[index] !== item || item.description !== description) return;
      if (!Array.isArray(parsed.items) || parsed.items.length !== 1) throw new Error('Описание этой позиции должно содержать один товар. Для нескольких позиций используйте общий текст ТЗ.');
      if (!Array.isArray(parsed.documents) || !parsed.documents.length) throw new Error('Не удалось сохранить текст правки. Повторите разбор позиции.');
      const fresh = parsed.items[0];
      const originalSource = clone(item.source?.original || item.source || {});
      const previousRequirements = clone(item.requirements || []);
      const changedName = `Правка позиции ${index + 1} — ${String(item.title || 'ТЗ').replace(/[\\/\x00-\x1f]/g,' ').slice(0,160)}.txt`;
      const changedDocuments = clone(parsed.documents || []).map(document => ({...document,id:'edit-' + document.id,name:changedName,origin:'user_edit'}));
      const documentMap = new Map((parsed.documents || []).map((document,di) => [document.id,changedDocuments[di]]));
      const editedSource = source => {
        const copy = clone(source || {}), document = documentMap.get(copy.document_id) || changedDocuments[0];
        if (document) { copy.document_id = document.id; copy.document_name = document.name; }
        copy.kind = 'user_edit';
        return copy;
      };
      item.requirements = clone(fresh.requirements || []).map(requirement => {
        const previous = previousRequirements.find(old => old.key === requirement.key);
        requirement.manual_override = true;
        requirement.source = {...editedSource(requirement.source),original:clone(previous?.source?.original || previous?.source || originalSource)};
        if (previous?.quote) requirement.original_quote = previous.original_quote || previous.quote;
        return requirement;
      });
      if (!item.requirements.length && previousRequirements.length) {
        item.warnings = [...new Set([...(item.warnings || []),'В изменённом описании отдельные требования не выделены. Проверьте описание и добавьте необходимые требования вручную.'])];
      }
      item.source = {...editedSource(fresh.source),original:originalSource};
      const seenDocuments = new Set(draft.documents.map(document => document.id || document.sha256));
      draft.documents.push(...changedDocuments.filter(document => !seenDocuments.has(document.id || document.sha256)));
      if (draft.savedItems.some(saved => saved.id === item.id)) {
        const savedIDs = new Set(draft.savedDocuments.map(document => document.id || document.sha256));
        draft.savedDocuments.push(...changedDocuments.filter(document => !savedIDs.has(document.id || document.sha256)));
      }
      item._descriptionDirty = false;
      if (!item.unit && fresh.unit) item.unit = fresh.unit;
      specStatus('Требования обновлены. Проверьте их перед поиском.'); renderSpecPreview();
    } catch(error) { specStatus(error.message,true); }
    finally { if (generation === draft.generation) { draft.preparing = false; setFormBusy(false); } }
  }
  function readItems() {
    if (!draft.items.length) return null;
    if (draft.description !== $('#description').value.trim() || draft.files.length) throw new Error('Текст или файлы ТЗ изменены. Нажмите «Проверить позиции», чтобы обновить разбор.');
    return draft.items.map((item,index) => {
      if (item._descriptionDirty) throw new Error(`Обновите требования позиции ${index + 1} после изменения описания.`);
      if (!item.title?.trim() || !numeric(item.quantity) || Number(item.quantity) <= 0) throw new Error(`Проверьте наименование и количество позиции ${index + 1}.`);
      const clean = clone(item); delete clean._descriptionDirty;
      clean.quantity = Number(item.quantity);
      clean.requirements = (item.requirements || []).map(req => {
        if (req._error) throw new Error(`Проверьте значение требования в позиции ${index + 1}: ${req._error}`);
        if (!req.label?.trim() || req.value === '' || req.value === null || req.value === undefined) throw new Error(`Заполните требования позиции ${index + 1}.`);
        const copy = clone(req); delete copy._inputValue; delete copy._error; return copy;
      });
      return clean;
    });
  }
  function renderSearchPolicy() {
    $$('#stop-mode-cards input').forEach(input => { input.checked = input.value === $('#stop-mode').value; });
    const target = $('#stop-mode').value === 'target';
    const count = $('#stop-mode').value === 'offers';
    if ($('.search-target')) $('.search-target').hidden = !target;
    $('#offer-limit-field').hidden = !count; $('#max-offers').required = count; $('#max-offers').disabled = !count || state.formBusy;
    $('#search-minutes-label').textContent = count || target ? 'Не дольше, минут' : 'Искать, минут';
    const duration = $('.search-options > summary span');
    if (duration) duration.textContent = `${number($('#search-minutes').value)} мин.`;
    $('#search-policy-hint').textContent = target ? 'Найти по 3 предложения, подходящих для расчёта каждой позиции.' : count ? 'Найти выбранное число предложений с ценой. Часть условий может требовать проверки.' : 'Искать до указанного времени и сохранить найденное.';
    const help = $('#stop-mode-help');
    if (help) help.innerHTML = `<summary>Что считается предложением?</summary><p>${target ? 'Для расчёта нужны подтверждённые характеристики, единица цены и условия. Профиль примера: 3 сопоставимые цены на позицию и разброс не выше 33%. Это выбранные правила расчёта.' : count ? 'В счётчик входят отдельные товарные предложения с подтверждённой ценой, даже если ещё нужно уточнить условия. Дубли и известные несоответствия не считаются. Для расчёта подходит только проверенная сопоставимая выборка.' : 'Поиск может закончиться раньше, если доступные ссылки исчерпаны. Частичный результат и причина завершения сохранятся.'}</p>`;
    renderWorkflow();
  }
  function readSearchPolicy() {
    const seconds = Math.round(Number($('#search-minutes').value) * 60), count = $('#stop-mode').value === 'time' ? 100 : Number($('#max-offers').value);
    if (!Number.isInteger(seconds) || seconds < 1 || seconds > 3600) throw new Error('Выберите время от 1 секунды до 60 минут.');
    if (!Number.isInteger(count) || count < 1 || count > 100) throw new Error('Выберите от 1 до 100 предложений на закупку.');
    return {mode:$('#stop-mode').value,...($('#stop-mode').value === 'target' ? {} : {max_offers:count}),max_seconds:seconds};
  }
  function requestForOffer(offer) {
    return (state.result?.items || []).find(item => String(item.id) === String(offer.item_id))?.request || state.result?.request || {};
  }
  function setFormBusy(busy) {
    state.formBusy = busy;
    $('#search-submit').disabled = busy; $('#description').disabled = busy; $('#quantity').disabled = busy || !!draft.items.length;
    $$('#source-options input, #source-options textarea').forEach(input => { input.disabled = busy; });
    $('#edit-query').disabled = busy;
    $$('.search-policy input, .search-policy select, #spec-items input, #spec-items select, #spec-items textarea, #spec-items button, #spec-file-list button, #attach-files, #prepare-spec').forEach(input => { input.disabled = busy || input.id === 'max-offers' && $('#stop-mode').value !== 'offers'; });
    renderWorkflow();
  }
  function renderQuerySummary(collapse = false) {
    const request = state.job?.request || state.result?.request || {description:$('#description').value,quantity:$('#quantity').value,source_scope:state.sourceScope};
    const items = request.items || state.result?.items || [];
    const short = value => String(value || '').replace(/\s+/g,' ').trim();
    const categories = [...new Set(items.map(item => /принтер/i.test(item.title) ? 'принтер' : /бумаг/i.test(item.title) ? 'бумага' : /интерлейкин|белок|rhIL|rhTGF/i.test(item.title) ? 'реагенты' : short(item.title).split(' ').slice(0,3).join(' ')))];
    const title = items.length > 1 ? categories.slice(0,2).join(', ') + (categories.length > 2 ? ' и прочее' : '') : items[0]?.title || request.product || request.description || 'Поиск предложений';
    $('#query-heading').textContent = short(title).slice(0,1).toLocaleUpperCase('ru') + short(title).slice(1,90) + (short(title).length > 90 ? '…' : '');
    const scope = request.source_scope || state.sourceScope;
    const domains = Array.isArray(scope.domains) ? scope.domains : [];
    const scopeLabel = scope.mode === 'domains' ? `${domains.slice(0,2).join(', ')}${domains.length > 2 ? ` и ещё ${domains.length - 2}` : ''}` : 'Весь интернет';
    const policy = request.search_policy || state.result?.search?.search_policy;
    const stopLabel = policy ? policy.mode === 'target' ? `по 3 предложения на позицию · до ${number(policy.max_seconds / 60)} мин` : policy.mode === 'time' ? `до ${number(policy.max_seconds / 60)} мин` : `до ${number(policy.max_offers,0)} предложений · не дольше ${number(policy.max_seconds / 60)} мин` : '';
    $('#query-meta').innerHTML = `<span>${items.length ? `${items.length} позиций` : `${number(request.quantity)} ${escape(request.unit || state.result?.request?.unit || 'ед.')}`}</span><span>${escape(scopeLabel)}</span>`;
    $('#query-meta').title = stopLabel;
    $('#query-summary').hidden = false; $('#page-title').hidden = true;
    if (collapse) { $('#search-form').hidden = true; $('#edit-query').innerHTML = `Запрос и ТЗ${icon('chevron')}`; $('#edit-query').setAttribute('aria-expanded','false'); $('#query-heading').setAttribute('aria-expanded','false'); }
  }
  function renderSearchProgress(job) {
    const reading = viewSnapshot($('#search-progress-sources'));
    const progress = job?.search_progress, items = Array.isArray(progress) ? progress : progress?.sources || progress?.items || [];
    const priced = progress?.priced_offers ?? progress?.coverage?.priced_offers;
    const coverage = progress?.coverage || job?.result?.search?.coverage || {};
    const target = coverage.target_offers ?? job?.request?.search_policy?.max_offers;
    $('#search-progress-count').textContent = items.length ? `· ${items.length}` : '';
    $('#progress-offers').textContent = numeric(coverage.items_total) ? `Готово позиций: ${number(coverage.items_complete,0)} из ${number(coverage.items_total,0)} · цель: по ${number(coverage.target_offers ?? 3,0)} предложения` : numeric(priced) ? `Уникальных предложений с ценой: ${number(priced,0)}${job?.request?.search_policy?.mode === 'offers' && numeric(target) ? ` из ${number(target,0)}` : ''}` : '';
    const labels = {visited:'Просмотрен',ok:'Просмотрен',read:'Просмотрен',fetching:'Проверяем',running:'Проверяем',found:'Есть предложения',offers:'Есть предложения',no_offers:'Нет предложений',empty:'Нет предложений',discovery:'Каталог, поиск товаров',catalog:'Каталог, поиск товаров',product:'Страница товара',blocked:'Доступ ограничен',failed:'Не удалось открыть',error:'Не удалось открыть',robots_disallowed:'Доступ закрыт сайтом'};
    $('#search-progress-list').innerHTML = items.length ? items.map(item => {
      const label = item.domain || item.title || item.name || item.url || 'Источник';
      const verdict = item.message || item.reason || labels[item.verdict] || labels[item.status] || 'Просмотрен';
      return `<li><span>${sourceLink(item.url,label) || escape(label)}</span><span>${escape(verdict)}</span></li>`;
    }).join('') : '<li class="progress-sources-empty">Источники появятся по мере поиска.</li>';
    restoreView($('#search-progress-sources'),reading);
  }
  function showProgress(message = 'Проверяем выбранные источники.') {
    draft.stage = 'research';
    showSelected(); clearInterval(state.clock);
    $('#result-screen').hidden = true; $('#progress-screen').hidden = false; $('#page-title').textContent = 'Анализ закупки';
    $('#progress-screen .spinner').hidden = false;
    const loading = state.job?.status === 'loading';
    $('#progress-heading').textContent = loading ? 'Открываем сохранённый поиск' : 'Ищем предложения'; $('#progress-description').textContent = message;
    $('#cancel-search').disabled = loading; $('#cancel-search').hidden = loading; $('#cancel-search').textContent = 'Остановить';
    $('#elapsed-time').hidden = loading;
    $('#search-progress-sources').hidden = loading;
    renderQuerySummary(true); renderSearchProgress(state.job);
    renderWorkflow();
    if (!loading) startClock();
  }
  function startClock() {
    clearInterval(state.clock);
    const tick = () => { const seconds = Math.max(0,Math.floor((Date.now() - state.started) / 1000)); $('#elapsed-time').textContent = `${String(Math.floor(seconds / 60)).padStart(2,'0')}:${String(seconds % 60).padStart(2,'0')}`; };
    tick(); state.clock = setInterval(tick,1000);
  }
  function jobFailure(job) {
    const labels = {cancelled:'Поиск остановлен',interrupted:'Поиск прерван после перезапуска',failed:'Не удалось завершить поиск'};
    const fallback = job.status === 'interrupted' ? 'Сохранённые данные доступны. Для нового поиска отправьте запрос ещё раз.' : job.status === 'cancelled' ? 'Можно изменить запрос и начать новый поиск.' : state.config?.model?.configured === false ? 'Модель не подключена. Поиск станет доступен после подключения модели.' : 'Предложения не получены. Измените запрос и повторите поиск.';
    return {heading:labels[job.status] || 'Не удалось завершить поиск',detail:job.error || fallback};
  }
  async function resumeJob(id) {
    if (!validJobID(id) || !serverHistoryEnabled()) return;
    ++state.resumeSequence;
    clearTimers(); hideError();
    if ($('#evidence-dialog').open) closeEvidence();
    state.view = 'job'; state.job = {id,status:'loading'}; state.result = null; state.started = Date.now(); state.pollFailures = 0;
    setFormBusy(true); showProgress('Открываем сохранённый поиск.');
    await pollJob();
  }
  async function startSearch(event) {
    event.preventDefault(); hideError(); $('#source-error').hidden = true;
    if (state.formBusy || state.job?.status === 'running' || state.view === 'submitting') return;
    if (draft.stage === 'input' || !draft.items.length || draft.files.length || draft.description !== $('#description').value.trim()) { await prepareDraft(); return; }
    if (draft.stage === 'review') { advanceDraft(); return; }
    if (!$('#search-form').reportValidity()) return;
    let sourceScope;
    try { sourceScope = readSourceScope(); }
    catch(error) { $('#source-error').textContent = error.message; $('#source-error').hidden = false; $('#source-picker').open = true; const custom = $('.source-custom'); if (custom) custom.open = true; $('#source-domains').focus(); return; }
    state.sourceScope = sourceScope;
    let items, searchPolicy;
    try { items = readItems(); searchPolicy = readSearchPolicy(); }
    catch(error) { showError(error.message); return; }
    const description = $('#description').value.trim() || (items || []).map(item => item.title).join('; ');
    const payload = {description,quantity:items ? 1 : Number($('#quantity').value),mode:'live',source_scope:sourceScope,search_policy:searchPolicy};
    if (items) { payload.items = items; payload.input_documents = clone(draft.documents); }
    if (payload.description.length < 3) { showError('Опишите закупку или прикрепите ТЗ.'); $('#description').focus(); return; }
    const sequence = ++state.resumeSequence;
    clearTimers(); state.view = 'submitting'; state.job = null; state.result = null; state.started = Date.now(); state.pollFailures = 0;
    if (serverHistoryEnabled()) { const url = new URL(location.href); url.hash = ''; try { history.replaceState(null,'',url); } catch {} }
    setFormBusy(true); $('#source-picker').open = false; showProgress('Запускаем поиск.');
    try {
      const job = await api('/api/search',{method:'POST',body:JSON.stringify(payload)});
      if (!validJobID(job.id)) throw new Error('Не удалось начать поиск. Повторите попытку.');
      acceptedSearch(job,payload);
      if (sequence !== state.resumeSequence || state.view !== 'submitting') { if (state.view === 'home') loadHistory(); return; }
      state.view = 'job'; state.job = {...job,request:job.request || payload}; state.started = Date.parse(job.pass_context?.started_at || job.created_at) || Date.now();
      rememberHash(job.id); showProgress();
      await pollJob();
    } catch(error) {
      if (sequence !== state.resumeSequence || state.view !== 'submitting') { if (state.view === 'home') loadHistory(); return; }
      clearTimers(); state.view = 'home'; setFormBusy(false); $('#progress-screen').hidden = true; $('#all-searches').hidden = true;
      document.body.classList.remove('has-results'); $('#page-title').classList.remove('sr-only'); $('#page-title').textContent = 'Новая закупка';
      $('#query-summary').hidden = true; $('#search-form').hidden = false; $('#page-title').hidden = false;
      setDraftStage('research');
      showError(error.message || 'Не удалось получить подтверждение поиска. Проверьте список поисков.'); renderHistory(); loadHistory();
    }
  }
  async function pollJob() {
    clearTimeout(state.timer);
    if (state.view !== 'job' || !state.job?.id) return;
    const id = state.job.id, sequence = state.resumeSequence, token = `${sequence}:${id}`;
    if (state.polling?.token === token) return state.polling.promise;
    const current = () => sequence === state.resumeSequence && state.view === 'job' && state.job?.id === id;
    const promise = Promise.resolve().then(async () => {
      try {
        const job = await api(`/api/jobs/${encodeURIComponent(id)}`);
        if (!current()) return;
        const previous = state.job;
        state.job = job; state.pollFailures = 0;
        $('#cancel-search').hidden = false; $('#elapsed-time').hidden = false; $('#search-progress-sources').hidden = false;
        if (job.status === 'running') {
          const evidenceOpen = $('#evidence-dialog').open, evidenceID = state.evidenceId, evidenceField = state.evidenceField;
          if (job.result && (!state.result || previous.status !== job.status || previous.revision !== job.revision || JSON.stringify(state.result) !== JSON.stringify(job.result))) {
            renderResult(job.result,job,!!state.result);
            if (evidenceOpen) openEvidence(evidenceID,state.lastFocused,false,evidenceField,true);
          }
          if (!job.result) restoreRequest(job.request); setFormBusy(true); hideError();
          $('#progress-screen').hidden = false; $('#partial-result-notice').hidden = !job.result;
          renderQuerySummary(!state.result); renderSearchProgress(job);
          state.started = Date.parse(job.pass_context?.started_at || job.created_at) || state.started;
          if (!state.clock) startClock();
          $('#progress-heading').textContent = 'Ищем предложения'; $('#cancel-search').disabled = false; $('#cancel-search').textContent = 'Остановить';
          const latest = (job.events || []).filter(event => event.message).at(-1);
          if (latest) $('#progress-description').textContent = String(latest.message).slice(0,240);
          if (!document.hidden) state.timer = setTimeout(pollJob,1100);
          return;
        }
        clearTimers(); setFormBusy(false);
        if (job.result) {
          if (!state.result || previous.status !== job.status || previous.revision !== job.revision || previous.updated_at !== job.updated_at) {
            const evidenceOpen = $('#evidence-dialog').open, evidenceID = state.evidenceId, evidenceField = state.evidenceField;
            renderResult(job.result,job,!!state.result);
            if (evidenceOpen) openEvidence(evidenceID,state.lastFocused,false,evidenceField,true);
          }
          else hideError();
          if (['failed','cancelled','interrupted'].includes(job.status)) { const status = jobFailure(job); showError(`${status.heading}. ${status.detail}`); }
          return;
        }
        restoreRequest(job.request); hideError(); $('#progress-screen .spinner').hidden = true;
        $('#search-form').hidden = false; $('#edit-query').setAttribute('aria-expanded','true'); $('#edit-query').textContent = 'Свернуть';
        const status = jobFailure(job); $('#progress-heading').textContent = status.heading;
        $('#progress-description').textContent = status.detail;
        $('#cancel-search').textContent = 'Закрыть'; $('#cancel-search').disabled = false;
      } catch(error) {
        if (!current()) return;
        state.pollFailures += 1;
        if (state.job.status === 'loading') setFormBusy(false);
        if (state.job.status === 'loading') $('#search-form').hidden = false;
        showError(error.message || 'Не удалось обновить поиск. Повторите проверку.',pollJob);
        if (state.pollFailures <= 3 && !document.hidden) { state.timer = setTimeout(pollJob,2000 * state.pollFailures); return; }
        clearInterval(state.clock); state.clock = null;
        $('#progress-heading').textContent = 'Не удалось обновить поиск';
        $('#progress-description').textContent = 'Повторите проверку или вернитесь ко всем поискам.';
      } finally {
        if (state.polling?.token === token) state.polling = null;
      }
    });
    state.polling = {token,promise};
    return promise;
  }
  async function cancelSearch() {
    if (!state.job?.id || state.job.status !== 'running') { showHome(); return; }
    const id = state.job.id, sequence = state.resumeSequence;
    $('#cancel-search').disabled = true; $('#cancel-search').textContent = 'Останавливаем…';
    try { await api(`/api/jobs/${encodeURIComponent(id)}/cancel`,{method:'POST',body:'{}'}); if (sequence === state.resumeSequence && state.job?.id === id) await pollJob(); }
    catch(error) { if (sequence !== state.resumeSequence || state.job?.id !== id) return; $('#cancel-search').disabled = false; $('#cancel-search').textContent = 'Остановить'; showError('Не удалось остановить поиск. Повторите попытку.'); }
  }
  const canonicalDomain = value => String(value || '').trim().toLowerCase().replace(/^www\./,'');
  const sourceGroupLabels = {public_records:'Реестры договоров',industry:'Промышленные каталоги',manufacturers:'Сайты брендов и производителей',retail:'Магазины и поставщики',platforms:'Маркетплейсы и ЭТП',other:'Другие открытые сайты'};
  const sourceGroup = item => Object.hasOwn(sourceGroupLabels,item.source_group) ? item.source_group : 'other';
  const sourceGroupLabel = item => Object.hasOwn(sourceGroupLabels,item.source_group) && typeof item.source_group_label === 'string' && item.source_group_label.trim() ? item.source_group_label : sourceGroupLabels[sourceGroup(item)];
  function sourceKey(item) {
    if (item.domain) return canonicalDomain(item.domain);
    if (item.url) { try { return canonicalDomain(new URL(item.url).hostname); } catch {} }
    const connected = state.sources.find(source => item.source_id && String(source.id) === String(item.source_id) || (item.name || item.supplier) && String(source.name).toLocaleLowerCase('ru') === String(item.name || item.supplier).toLocaleLowerCase('ru'));
    return connected ? canonicalDomain(connected.domain) || String(connected.id) : String(item.source_id || item.name || item.supplier || 'unknown');
  }
  function sourceOptions() {
    const offers = state.result?.offers || [], sources = new Map();
    const add = item => {
      const key = sourceKey(item), connected = state.sources.find(source => sourceKey(source) === key);
      const label = item.name || connected?.name || item.supplier || item.domain || 'Источник не указан';
      const name = key.includes('.') && canonicalDomain(label) !== key ? `${label} · ${key}` : label;
      if (!sources.has(key)) sources.set(key,{key,name,status:item.status,group:sourceGroup(item),groupLabel:sourceGroupLabel(item),offers:offers.filter(offer => sourceKey(offer) === key).length});
      else if (Object.hasOwn(sourceGroupLabels,item.source_group)) Object.assign(sources.get(key),{group:sourceGroup(item),groupLabel:sourceGroupLabel(item)});
    };
    if (state.status === 'all' || state.status === 'active') (state.result?.search?.source_statuses || []).forEach(add);
    offers.filter(offer => matchesStatus(offer)).forEach(add);
    return [...sources.values()].sort((a,b) => a.name.localeCompare(b.name,'ru'));
  }
  function itemOffers() {
    return (state.result?.offers || []).filter(offer => state.item === 'all' || String(offer.item_id) === state.item);
  }
  function itemDisplayTitle(item) {
    const title = String(item.title || '').replace(/\s+/g,' ').trim();
    const reagent = title.match(/^rh[A-Za-z0-9β-]+/i);
    if (reagent) return reagent[0];
    if (/бумаг/i.test(title)) {
      const format = title.match(/[АA][34]/i)?.[0].replace('А','A').toUpperCase();
      const brand = title.match(/svetocopy/i)?.[0];
      return `Бумага${/писч|офсет/i.test(title) ? ' писчая' : ''}${format ? ' ' + format : ''}${brand ? ' SvetoCopy' : ''}`;
    }
    if (/принтер/i.test(title)) return title.replace(/^(?:3D\s*)?принтер\s*(?:3D\s*)?/i,'').replace(/creatbot/ig,'CreatBot') || title;
    return title;
  }
  function itemEstimate(item) {
    const calculation = item.calculation || {}, estimate = calculation.provisional || {};
    return calculation.status === 'ready' && numeric(calculation.total) ? {value:calculation.total,ready:true} : {value:estimate.total,ready:false};
  }
  function renderItemNavigation() {
    const items = state.result?.items || [], all = state.result?.offers || [];
    $('#item-navigation').innerHTML = `<div class="item-nav-heading"><h2>Позиции</h2><span>${items.length || 1}</span></div><div class="item-nav-list"><button type="button" class="item-nav-button" data-select-item="all" aria-current="${state.item === 'all' ? 'true' : 'false'}"><span class="item-nav-name">Все позиции</span><span class="item-nav-count">${all.length}</span></button>${items.map((item,index) => {
      const offers = all.filter(offer => String(offer.item_id) === String(item.id)), estimate = itemEstimate(item);
      return `<button type="button" class="item-nav-button" title="${escape(item.title)}" data-select-item="${escape(item.id)}" aria-current="${state.item === String(item.id) ? 'true' : 'false'}"><span class="item-nav-number">${index + 1}</span><span class="item-nav-copy"><span class="item-nav-name">${escape(itemDisplayTitle(item))}</span><span class="item-nav-meta">${item.target_group ? `Сопоставимых: ${number(item.target_group.count,0)} / ${number(item.target_group.required ?? 3,0)} · ` : ''}${numeric(estimate.value) ? `${estimate.ready ? '' : '≈ '}${money(estimate.value)}` : 'Стоимость не определена'}</span></span><span class="item-nav-count">${offers.length}</span></button>`;
    }).join('')}</div>`;
    const selected = items.find(item => String(item.id) === state.item) || (items.length === 1 ? items[0] : null);
    $('#selected-item-heading').innerHTML = `<h2${selected ? ` title="${escape(selected.title)}"` : ''}>${selected ? escape(itemDisplayTitle(selected)) : 'Все предложения'}</h2>${selected ? `<p>${number(selected.quantity,6)} ${escape(selected.unit || selected.request?.unit || 'ед.')}</p>${targetMarkup(selected)}` : ''}`;
  }
  function selectItem(id) {
    state.item = id; state.page = 1;
    renderFacets(); renderOffers();
  }
  function renderFacets() {
    const items = state.result?.items || [];
    if (!items.some(item => String(item.id) === state.item)) state.item = 'all';
    const offers = itemOffers();
    const labels = {active:'Подходящие и для проверки',all:'Все рассмотренные',included:'В расчёте',excluded:'Отклонённые',uncertain:'Нужна проверка'};
    $('#status-filter').innerHTML = Object.entries(labels).map(([key,label]) => `<option value="${key}">${label} · ${offers.filter(offer => matchesStatus(offer,key)).length}</option>`).join('');
    $('#status-filter').value = state.status;
    const rejected = $('[data-show-rejected]');
    if (rejected) { rejected.textContent = state.status === 'excluded' ? 'К предложениям' : `Отклонённые · ${offers.filter(offer => offer.status === 'excluded').length}`; rejected.setAttribute('aria-pressed',String(state.status === 'excluded')); }
    const options = sourceOptions();
    if (!options.some(source => source.key === state.source || `group:${source.group}` === state.source)) state.source = 'all';
    const failed = ['blocked','failed','unavailable','error','not_configured','robots_disallowed'];
    const groups = [...new Map(options.map(source => [source.group,source.groupLabel])).entries()].sort((a,b) => a[1].localeCompare(b[1],'ru'));
    $('#source-filter').innerHTML = '<option value="all">Все источники</option>' + groups.map(([group,label]) => `<optgroup label="${escape(label)}"><option value="group:${escape(group)}">Все: ${escape(label.toLocaleLowerCase('ru'))}</option>${options.filter(source => source.group === group).map(source => `<option value="${escape(source.key)}">${escape(source.name)}${state.status === 'all' && !source.offers ? failed.includes(source.status) ? ' — недоступен' : ' — нет предложений' : ''}</option>`).join('')}</optgroup>`).join('');
    $('#source-filter').value = state.source;
    $('#item-filter-field').hidden = true;
    $('#item-filter').innerHTML = '<option value="all">Все позиции</option>' + items.map(item => `<option value="${escape(item.id)}">${escape(item.title)}</option>`).join('');
    $('#item-filter').value = state.item;
    const count = Number(state.status !== 'all') + Number(state.source !== 'all');
    $('#active-filter-count').textContent = count; $('#active-filter-count').hidden = !count;
    renderItemNavigation();
  }
  function resultDate(result) {
    const dates = [...new Set((result.offers || []).map(offer => date(offer.retrieved_at)).filter(Boolean))];
    if (dates.length === 1) return `Цены на ${dates[0]}`;
    if (dates.length > 1) return 'Даты цен указаны в предложениях';
    const searched = date(result.search?.finished_at || result.search?.started_at);
    return searched ? `Поиск от ${searched}` : 'Дата цен не указана';
  }
  function renderResult(result, job = state.job, preserveFilters = false) {
    const reading = preserveFilters ? viewSnapshot($('#result-screen')) : null;
    if (job?.status !== 'running') clearTimers();
    hideError(); state.view = 'job'; state.result = result; state.job = job || null; draft.stage = 'research'; showSelected();
    if (!preserveFilters) { state.status = 'active'; state.source = 'all'; state.item = result.items?.length > 1 ? String(result.items[0].id) : 'all'; state.text = ''; state.page = 1; $('#offer-search').value = ''; }
    setFormBusy(job?.status === 'running'); $('#page-title').textContent = 'Анализ закупки';
    document.body.classList.add('has-results'); $('#page-title').classList.add('sr-only');
    $('#progress-screen').hidden = job?.status !== 'running'; $('#result-screen').hidden = false;
    $('#partial-result-notice').hidden = job?.status !== 'running';
    const request = result.request || {}, offers = result.offers || [];
    restoreRequest(job?.request || request);
    if ((result.input_documents || []).length && draft.restoredJobID === state.job?.id) {
      draft.documents = clone(result.input_documents); draft.savedDocuments = clone(result.input_documents); renderFiles();
    }
    renderQuerySummary(!preserveFilters);
    if (!preserveFilters) $('#result-filters').open = false;
    $('#result-description').textContent = request.description || request.product || 'Результаты поиска предложений';
    $('#offers-count').textContent = offers.length; $('#result-date').textContent = resultDate(result);
    $('#synthetic-label').hidden = !(result.search?.scenario_kind === 'synthetic' || offers.some(offer => offer.provenance === 'synthetic'));
    $('#validation-notice').textContent = typeof result.validation_notice === 'string' ? result.validation_notice : result.validation_notice?.message || '';
    $('#validation-notice').hidden = true;
    renderFacets(); renderOffers(); renderCalculation(); rememberHash(state.job?.id);
    renderWorkflow();
    restoreView($('#result-screen'),reading);
  }
  function attributesSummary(offer) {
    const attrs = offer.attributes || {}, parts = [];
    if (numeric(attrs.channels)) parts.push(`${number(attrs.channels,0)} кан.`);
    if (numeric(attrs.bandwidth_mhz)) parts.push(`${number(attrs.bandwidth_mhz)} МГц`);
    if (attrs.format) parts.push(String(attrs.format));
    if (numeric(attrs.density_gsm)) parts.push(`${number(attrs.density_gsm)} г/м²`);
    if (numeric(attrs.sheets_per_pack)) parts.push(`${number(attrs.sheets_per_pack,0)} листов в пачке`);
    if (numeric(offer.pack_size) && Number(offer.pack_size) > 1) parts.push(`Упаковка: ${number(offer.pack_size)} ${requestForOffer(offer).unit || 'ед.'}`);
    return parts.join(' · ');
  }
  const imagePlaceholder = () => `<span class="image-placeholder">${icon('photo')}<span>Нет фото</span></span>`;
  const priceUnconfirmed = offer => Array.isArray(offer.validation?.rejected_fields) && offer.validation.rejected_fields.includes('price');
  function priceMarkup(offer) {
    const normalized = numeric(offer.normalized_price) && rubles(offer), currency = rubles(offer) ? '₽' : offer.currency || 'валюта не указана';
    const price = normalized ? unitMoney(offer.normalized_price) : numeric(offer.price) ? `${unitNumber(offer.price)} ${escape(currency)}` : priceUnconfirmed(offer) ? 'Цена не подтверждена' : offer.validation?.price_state === 'on_request' ? 'Цена по запросу' : 'Цена не извлечена';
    const unit = normalized ? requestForOffer(offer).unit : offer.unit;
    const provenUnit = unit && !offer.unit_inference && !!offer.evidence?.unit?.quote;
    const tax = {included:'НДС включён',excluded:'Без НДС',unknown:'НДС не указан'}[offer.tax] || 'НДС не указан';
    const observed = sourceFact(offer,'price');
    const original = normalized && numeric(observed) && Number(observed) !== Number(offer.normalized_price) ? `<div class="price-details">В источнике: ${unitNumber(observed)} ${escape(sourceFact(offer,'currency') || 'валюта не указана')} / ${escape(sourceFact(offer,'unit') || 'единица не указана')}</div>` : '';
    return `<div class="offer-price">${price}${provenUnit && numeric(offer.price) ? `<span class="price-unit">/${escape(unit)}</span>` : ''}</div><div class="price-badges"><span>${tax}</span></div>${original}`;
  }
  function renderOffers() {
    const all = state.result?.offers || [], sources = sourceOptions();
    const query = state.text.toLocaleLowerCase('ru');
    const offers = all.filter(offer => matchesStatus(offer) && (state.source === 'all' || sourceKey(offer) === state.source || state.source === `group:${sources.find(source => source.key === sourceKey(offer))?.group}`) && (state.item === 'all' || String(offer.item_id) === state.item) && (!query || [offer.title,offer.supplier,offer.domain,offer.item_title,...Object.values(offer.attributes || {}),...(offer.reasons || [])].map(plain).join(' ').toLocaleLowerCase('ru').includes(query)));
    const pages = Math.max(1,Math.ceil(offers.length / state.pageSize)); state.page = Math.min(state.page,pages);
    const start = (state.page - 1) * state.pageSize, visible = offers.slice(start,start + state.pageSize);
    $('#visible-count').textContent = offers.length ? `Показано ${offers.length <= state.pageSize ? visible.length : `${start + 1}–${start + visible.length}`} из ${offers.length}` : 'Предложений нет';
    const pagination = (top = false) => `<label>На странице <select id="${top ? 'page-size-top' : 'page-size'}" data-page-size aria-label="${top ? 'Предложений на странице сверху' : 'Предложений на странице'}">${[20,50,100].map(size => `<option value="${size}"${size === state.pageSize ? ' selected' : ''}>${size}</option>`).join('')}</select></label><div class="page-controls"><button type="button" class="button button-secondary" data-page="${state.page - 1}"${state.page <= 1 ? ' disabled' : ''} aria-label="${top ? 'Предыдущая страница' : 'Назад по страницам'}">←</button><span>Страница ${state.page} из ${pages}</span><button type="button" class="button button-secondary" data-page="${state.page + 1}"${state.page >= pages ? ' disabled' : ''} aria-label="${top ? 'Следующая страница' : 'Далее по страницам'}">→</button></div>`;
    $('#offer-pagination').innerHTML = pagination();
    if ($('#offer-pagination-top')) $('#offer-pagination-top').innerHTML = pagination(true);
    const list = $('#offer-list'); list.classList.toggle('grid',state.layout === 'grid');
    if (!offers.length) {
      const source = sources.find(item => item.key === state.source);
      const group = sources.find(item => `group:${item.group}` === state.source);
      const text = state.status === 'active' && all.length && all.every(offer => offer.status === 'excluded') ? 'Рассмотренные варианты отклонены. Причины доступны в разделе «Отклонённые».' : source ? `В источнике «${source.name}» нет предложений с выбранным статусом.` : group ? `В группе «${group.groupLabel}» нет предложений с выбранным статусом.` : all.length ? 'Измените статус или источник, чтобы увидеть другие предложения.' : 'Измените запрос или выберите другую область поиска.';
      list.innerHTML = `<div class="empty-state"><h3>${source?.status && ['blocked','failed','unavailable','error','robots_disallowed'].includes(source.status) ? 'Источник недоступен' : state.status === 'active' ? 'Подходящих предложений не найдено' : 'Предложений не найдено'}</h3><p>${escape(text)}</p></div>`;
      return;
    }
    let lastItem = null;
    list.innerHTML = visible.map(offer => {
      const image = offer.image_url && state.job?.id ? apiURL(`/api/jobs/${encodeURIComponent(state.job.id)}/images/${encodeURIComponent(offer.id)}`) : safeImage(offer.image), attributes = attributesSummary(offer), captured = date(offer.retrieved_at), synthetic = offer.provenance === 'synthetic';
      const rawMismatches = offer.functional_status === 'mismatch' && Array.isArray(offer.functional_reasons) ? offer.functional_reasons : [];
      const checks = (offer.requirement_checks || []).filter(item => item.status === 'mismatch');
      const mismatches = rawMismatches.map((reason,index) => { const check = checks.length === rawMismatches.length ? checks[index] : null; if (!check) return reason.length > 120 ? reason.slice(0,117) + '…' : reason; const actual = displayRequirement(check.actual,check.unit), expected = displayRequirement(check.expected,check.unit); return actual.length + expected.length < 65 ? `${check.label}: ${actual} вместо ${expected}` : `${check.label}: требует проверки`; });
      const reason = mismatches.length ? `<ul class="offer-reason">${mismatches.slice(0,2).map(item => `<li>${escape(item)}</li>`).join('')}</ul>${mismatches.length > 2 ? `<details class="additional-mismatches"><summary>Ещё ${mismatches.length - 2} несоответствий</summary><ul>${mismatches.slice(2).map(item => `<li>${escape(item)}</li>`).join('')}</ul></details>` : ''}` : '';
      const group = (state.result?.items || []).length > 1 && state.item === 'all' && lastItem !== offer.item_id ? `<h3 class="offer-group-heading">${escape(offer.item_title || 'Позиция закупки')}</h3>` : '';
      lastItem = offer.item_id;
      return `${group}<article class="offer-card ${['included','excluded','uncertain'].includes(offer.status) ? offer.status : 'uncertain'}" data-offer="${escape(offer.id)}"><div class="offer-photo">${image ? `<img class="offer-image" src="${escape(image)}" alt="${escape(offer.title || 'Фото товара')}" loading="lazy">` : imagePlaceholder()}</div><div class="offer-content"><div class="offer-supplier"><span>${escape(offer.supplier || offer.domain || 'Поставщик не указан')}</span>${functionalPill(offer)}${offer.duplicate_of ? '<span class="manual-exclusion-label">Дубль</span>' : ''}${manuallyExcluded(offer) ? '<span class="manual-exclusion-label">Исключено вручную</span>' : ''}${offer.manual_review ? '<span class="manual-confirmation">Уточнено вручную</span>' : ''}${synthetic ? '<span class="synthetic-label">Учебные данные</span>' : ''}</div><h3 class="offer-title">${escape(offer.title || 'Название не указано')}</h3>${attributes ? `<p class="offer-attributes">${escape(attributes)}</p>` : ''}<div class="offer-actions">${sourceLink(offer.url,'Предложение')}<button type="button" class="text-button" data-evidence="${escape(offer.id)}">Разбор</button></div>${reason}</div><div class="offer-price-block">${priceMarkup(offer)}<div class="offer-date">${captured ? `Цена на ${escape(captured)}` : 'Дата цены не указана'}</div></div></article>`;
    }).join('');
    $$('.offer-image',list).forEach(image => image.addEventListener('error',() => { image.parentElement.innerHTML = imagePlaceholder(); },{once:true}));
  }
  const cvHelp = () => '<details class="inline-help cv-help"><summary aria-label="Что означает разброс цен">?</summary><p>Показывает, насколько цены отличаются от средней: чем меньше процент, тем ближе цены друг к другу. Коэффициент вариации (CV) = выборочное стандартное отклонение / средняя цена × 100%. Допустимый предел задаёт профиль расчёта.</p></details>';
  function targetMarkup(item) {
    const group = item.target_group;
    if (!group) return '';
    const selected = (group.offer_ids || []).map(id => (state.result?.offers || []).find(offer => offer.id === id)).filter(Boolean);
    const values = Array.isArray(group.selected_values) ? group.selected_values.map(value => typeof value === 'object' ? value.price : value).filter(numeric) : selected.map(offer => offer.normalized_price).filter(numeric);
    const fullCV = group.candidate_cv ?? group.full_cv ?? group.all_candidates_cv ?? group.cv;
    const profile = group.profile ? String(group.profile).replace(/\bCV\b/g,'разброс цен') : `3 сопоставимые цены · разброс до ${number(group.cv_limit ?? 33)}%`;
    return `<section class="target-group ${group.ready ? 'target-ready' : ''}" aria-label="Сопоставимость предложений"><strong>${group.ready ? 'Позиция готова' : 'Не хватает сопоставимых предложений'} · ${number(group.count,0)} / ${number(group.required ?? 3,0)}</strong><p>Профиль: ${escape(profile)}</p>${numeric(fullCV) ? `<p>Разброс всех цен: ${number(fullCV)}%${numeric(group.selected_cv) ? ` · выбранных: ${number(group.selected_cv)}%` : ''}</p>${cvHelp()}` : ''}${values.length ? `<p>Цены группы: ${values.map(unitMoney).join(' · ')}</p>` : ''}${numeric(group.manual_count) && group.manual_count > 0 ? `<p class="manual-confirmation">С ручным принятием отклонений: ${number(group.manual_count,0)}</p>` : ''}${group.reason ? `<p>${escape(group.reason)}</p>` : ''}</section>`;
  }
  function searchAuditMarkup(search) {
    const audit = search.audit, sources = search.source_statuses || [];
    const unavailable = sources.filter(source => ['unavailable','blocked','failed','error','robots_disallowed','timeout','rate_limited'].includes(source.status || source.verdict));
    const labels = {unavailable:'Источник недоступен',blocked:'Доступ ограничен',failed:'Не удалось открыть',error:'Ошибка получения',robots_disallowed:'Доступ закрыт сайтом',timeout:'Истекло время ожидания',rate_limited:'Ограничение запросов'};
    const queries = Array.isArray(audit?.queries) ? audit.queries : [], pages = Array.isArray(audit?.checked_pages) ? audit.checked_pages : [];
    return `<section class="search-audit" aria-label="История поиска">${audit?.conclusion ? `<p>${escape(audit.conclusion)}</p>` : ''}${audit ? `<details class="audit-queries"><summary>Поисковые запросы · ${queries.length}</summary>${queries.length ? `<ol>${queries.map(query => `<li>${escape(query)}</li>`).join('')}</ol>` : '<p>Поисковые запросы не сохранены.</p>'}</details><details class="audit-pages"><summary>Проверенные страницы · ${pages.length}</summary>${pages.length ? `<ol>${pages.map(page => `<li>${sourceLink(page.url,page.title || page.url) || escape(page.title || 'Адрес не сохранён')}<small>${escape([historyTime(page.retrieved_at),page.method,page.page_kind].filter(Boolean).join(' · '))}</small>${page.sha256 ? `<code>SHA-256: ${escape(page.sha256)}</code>` : ''}</li>`).join('')}</ol>` : '<p>Проверенные страницы не сохранены.</p>'}</details>` : '<p class="audit-missing">История запросов и проверенных страниц не сохранена.</p>'}${unavailable.length ? `<details class="audit-unavailable"><summary>Недоступные источники · ${unavailable.length}</summary><ul>${unavailable.map(source => `<li><strong>${escape(source.domain || source.name || source.url || 'Источник')}</strong><span>${escape(labels[source.status || source.verdict] || source.status || source.verdict)}</span>${source.message || source.reason ? `<p>${escape(source.message || source.reason)}</p>` : ''}</li>`).join('')}</ul></details>` : ''}</section>`;
  }
  const dollars = value => `$${number(value,8)}`;
  function confirmedCostMarkup(cost, complete) {
    if (complete === true && numeric(cost)) return dollars(cost);
    return numeric(cost) && Number(cost) > 0 ? `<span>Подтверждено ${dollars(cost)}</span><small class="cost-incomplete">Полная стоимость неизвестна</small>` : 'Неизвестна';
  }
  const tokenFact = (value, complete) => numeric(value) && (complete || Number(value) > 0) ? number(value,0) : 'Неизвестно';
  const missingResponses = budget => numeric(budget.model_calls) && numeric(budget.metered_calls) && Number(budget.model_calls) >= Number(budget.metered_calls) ? Number(budget.model_calls) - Number(budget.metered_calls) : null;
  const missingResponsesMarkup = budget => missingResponses(budget) > 0 ? `<small class="missing-responses">Без полного ответа: ${number(missingResponses(budget),0)}</small>` : '';
  const completeBudgetUsage = budget => budget.actual_usage_known === true || budget.actual_usage_known !== false && budget.actual_cost_known === true;
  function costPassesMarkup(passes) {
    if (!Array.isArray(passes) || !passes.length) return '';
    return `<details class="cost-passes"><summary>Проходы · ${passes.length}</summary><ol>${passes.map((pass,index) => {
      const budget = pass.budget || {}, usage = budget.actual_usage || {}, cost = usage.cost_usd ?? usage.cost, complete = completeBudgetUsage(budget);
      return `<li><strong>Проход ${escape(pass.number ?? index + 1)}</strong>${pass.started_at ? `<small>${escape(historyTime(pass.started_at) || '')}</small>` : ''}<span>Вызовы: ${number(budget.model_calls,0)} · ${complete ? 'токены' : 'токены по ответам'}: ${tokenFact(usage.total_tokens,complete)}</span>${missingResponsesMarkup(budget)}<span>Стоимость: ${confirmedCostMarkup(cost,budget.actual_cost_known)}</span></li>`;
    }).join('')}</ol></details>`;
  }
  function actualUsageMarkup(budget) {
    const usage = budget.actual_usage || {}, complete = completeBudgetUsage(budget);
    const cost = usage.cost_usd ?? usage.cost;
    return `<section class="actual-usage" aria-label="Фактические расходы"><h3>Расходы этого поиска</h3><dl><div><dt>Вызовы модели</dt><dd>${number(budget.model_calls,0)}${missingResponsesMarkup(budget)}</dd></div><div><dt>${complete ? 'Токены' : 'Токены по ответам'}</dt><dd>${tokenFact(usage.total_tokens,complete)}</dd></div><div><dt>Стоимость</dt><dd>${confirmedCostMarkup(cost,budget.actual_cost_known)}</dd></div></dl>${budget.actual_cost_known === false ? '<p class="muted">Полная стоимость неизвестна: провайдер не передал полные данные.</p>' : ''}<button type="button" class="text-button" data-open-costs>Все поиски и расходы →</button></section>`;
  }
  function renderCalculation() {
    const result = state.result, calc = result.calculation || {}, request = result.request || {}, items = result.items || [];
    const included = (result.offers || []).filter(offer => offer.status === 'included');
    const ready = calc.status === 'ready' && numeric(calc.total);
    const provisional = calc.provisional || {}, conditional = !ready && numeric(provisional.total);
    const partial = !ready && !conditional && numeric(provisional.known_subtotal ?? calc.known_subtotal);
    const value = ready ? calc.total : conditional ? provisional.total : partial ? provisional.known_subtotal ?? calc.known_subtotal : null;
    const inferred = included.some(offer => offer.unit_inference), deliveryRequested = !!(request.delivery_required || request.delivery_address);
    const deliveryUnknown = deliveryRequested && included.some(offer => !offer.delivery), preliminary = inferred || deliveryUnknown;
    const id = state.job?.id;
    const reportLink = format => {
      if (!id) return null;
      const link = safeReportURL(window.QuoteLens.reportURL(id,format));
      const revision = state.job?.revision ?? result.review?.revision;
      if (!link || link.startsWith('blob:') || !numeric(revision)) return link;
      const url = new URL(link,location.href); url.searchParams.set('revision',String(revision)); return url.href;
    };
    const links = Object.fromEntries(['pdf','docx','html','json'].map(format => [format,reportLink(format)]));
    const minSuppliers = result.search?.policy?.min_suppliers ?? result.policy?.min_suppliers ?? state.config?.defaults?.min_suppliers;
    const insufficient = calc.reason || (!included.length ? 'Нет предложений, принятых для расчёта.' : numeric(minSuppliers) && numeric(calc.independent_suppliers) && Number(calc.independent_suppliers) < Number(minSuppliers) ? `Для расчёта нужно ${number(minSuppliers,0)} поставщика. Сейчас принято: ${number(calc.independent_suppliers,0)}.` : 'Принятых данных недостаточно для расчёта. Проверьте предложения или уточните запрос.');
    const warnings = [...new Set([...(provisional.warnings || []),...(ready && inferred ? ['Единицу цены нужно подтвердить у поставщиков.'] : []),...(ready && deliveryUnknown ? ['Стоимость доставки требует подтверждения.'] : [])])];
    const missingIDs = provisional.missing_item_ids || calc.missing_item_ids || [];
    const missingNames = items.filter(item => missingIDs.includes(item.id)).map(item => item.title);
    const budget = result.search?.budget || {}, stop = result.search?.stop || {};
    const policy = result.search?.search_policy || request.search_policy;
    const condition = policy ? policy.mode === 'target' ? `По 3 сопоставимых предложения на позицию, не дольше ${number(policy.max_seconds / 60)} мин.` : policy.mode === 'time' ? `По времени: ${number(policy.max_seconds / 60)} мин.` : `До ${number(policy.max_offers,0)} уникальных предложений, не дольше ${number(policy.max_seconds / 60)} мин.` : '';
    const itemCalculations = items.length ? `<section class="item-calculations" aria-label="Расчёт по позициям">${items.map(item => {
      const calculation = item.calculation || {}, estimate = calculation.provisional || {}, amount = itemEstimate(item);
      return `<details class="item-calculation" data-calculation-item="${escape(item.id)}"><summary><span>${escape(item.title)}</span><strong>${numeric(amount.value) ? `${amount.ready ? '' : '≈ '}${money(amount.value)}` : 'Нет расчёта'}</strong></summary><p>${number(item.quantity,6)} ${escape(item.unit || item.request?.unit || 'ед.')} · ${amount.ready ? 'Подтверждено' : 'Требует проверки'}</p>${numeric(calculation.unit_price ?? estimate.unit_price) ? `<p>За единицу: ${unitMoney(calculation.unit_price ?? estimate.unit_price)}</p>` : ''}<p>${escape(amount.ready ? calculation.rule || '' : estimate.reason || calculation.reason || 'Недостаточно сопоставимых цен.')}</p><button type="button" class="text-button" data-select-item="${escape(item.id)}">Открыть предложения →</button></details>`;
    }).join('')}</section>` : '';
    const editable = state.job?.status !== 'running';
    const incomplete = items.some(item => item.target_group && !item.target_group.ready) || result.target_group && !result.target_group.ready || !items.length && calc.status !== 'ready';
    const continueAction = editable && id && incomplete && serverHistoryEnabled() ? '<div class="continue-search"><button id="continue-search" type="button" class="button button-secondary">Продолжить поиск</button><details><summary>Время продолжения</summary><label for="continue-minutes">Минут<input id="continue-minutes" type="number" min="0.016666666666" max="60" step="any" value="10"></label></details></div>' : '';
    const shareAction = editable && id && serverHistoryEnabled() ? '<div class="share-block"><button id="share-result" type="button" class="text-button">Поделиться обоснованием</button><div id="share-result-link" hidden></div><p id="share-feedback" class="report-feedback" role="status" hidden></p></div>' : '';
    const reportActions = !editable ? '<p class="review-note">Промежуточный результат · только просмотр</p>' : id ? `<div class="report-block"><h2>Расчёт закупки</h2><div class="report-actions">${window.QuoteLens.savedCatalog !== true && links.pdf ? `<a id="report-pdf" class="button button-primary" data-report-download="pdf" href="${escape(links.pdf)}" download="quotelens-report.pdf">${icon('doc')}Скачать PDF</a>` : ''}${window.QuoteLens.savedCatalog !== true && links.docx ? `<a id="report-docx" class="button button-secondary" data-report-download="docx" href="${escape(links.docx)}" download="quotelens-report.docx">DOCX</a>` : ''}</div><p id="report-feedback" class="report-feedback" role="status" hidden></p></div>` : '<p class="report-unavailable">Отчёт недоступен: результат не сохранён.</p>';
    const label = ready && !preliminary ? 'НМЦК по профилю' : 'Предварительная стоимость';
    const estimatedCount = items.filter(item => numeric(itemEstimate(item).value)).length;
    const explanation = ready && !preliminary ? 'Начальная максимальная цена контракта по выбранным правилам расчёта. Требует утверждения заказчиком.' : 'Оценка по найденным ценам. Неподтверждённые условия и недостающие позиции требуют проверки.';
    $('#calculation-summary').innerHTML = `${reportActions}${shareAction}${continueAction}${!items.length && result.target_group ? targetMarkup(result) : ''}<div class="cost-overview"><div class="total-label">${label}<span class="term-help"><button type="button" class="term-help-button" aria-label="Что означает ${label}" aria-expanded="false" aria-controls="cost-term" aria-describedby="cost-term">?</button><span id="cost-term" class="term-tooltip" role="tooltip" hidden>${explanation}</span></span></div><div class="total-price${numeric(value) ? '' : ' no-data'}">${numeric(value) ? money(value) : 'Пока не определена'}</div><p class="cost-status ${ready && !preliminary ? 'ready' : ''}">${partial ? `Часть закупки · ${estimatedCount} из ${items.length} позиций` : ready && !preliminary ? 'Подтверждённые данные' : numeric(value) ? 'Требует проверки' : 'Недостаточно подтверждённых цен'}</p>${partial ? '<p class="partial-warning">Полная стоимость пока неизвестна.</p>' : ''}</div><details class="calculation-details"><summary>Состав и расчёт ${icon('chevron')}</summary>${!ready ? `<p>${escape(insufficient)}</p>` : ''}${warnings.length ? `<ul class="calculation-note">${warnings.map(w => `<li>${escape(w)}</li>`).join('')}</ul>` : ''}${missingNames.length ? `<p>Без расчёта: ${escape(missingNames.join('; '))}.</p>` : ''}${!items.length && numeric(calc.unit_price ?? provisional.unit_price) ? `<div class="calculation-row"><span>За 1 ${escape(request.unit || 'ед.')}</span><strong>${unitMoney(calc.unit_price ?? provisional.unit_price)}</strong></div>` : ''}${!items.length ? `<div class="calculation-row"><span>Количество</span><strong>${number(request.quantity,6)} ${escape(request.unit || 'ед.')}</strong></div>` : ''}<div class="calculation-row"><span>В расчёте</span><strong>${numeric(calc.n) ? number(calc.n,0) : 'Не указано'} предложений</strong></div>${conditional && numeric(provisional.n) ? `<div class="calculation-row"><span>В оценке</span><strong>${number(provisional.n,0)} предложений</strong></div>` : ''}${itemCalculations}${numeric(calc.cv ?? provisional.cv) ? `<div class="calculation-row"><div>Разброс цен ${cvHelp()}</div><strong>${number(calc.cv ?? provisional.cv)}%</strong></div>` : ''}<p>${escape(calc.rule || 'Средняя цена принятых предложений × количество.')}</p><div class="calculation-row"><span>Поставщиков в расчёте</span><strong>${number(calc.independent_suppliers,0)}</strong></div></details><details id="research-audit" class="research-details" tabindex="-1"><summary>Источники и ход поиска</summary><p>${escape(stop.label || 'Поиск завершён')}</p>${$('#validation-notice').textContent ? `<p>${escape($('#validation-notice').textContent)}</p>` : ''}${condition ? `<p>${escape(condition)}</p>` : ''}${stop.detail ? `<p>${escape(stop.detail)}</p>` : ''}${numeric(budget.pages_used) ? `<p>Прочитано страниц: ${number(budget.pages_used,0)}.</p>` : ''}${searchAuditMarkup(result.search || {})}${actualUsageMarkup(budget)}</details>${links.html || links.json ? `<details class="research-details"><summary>Другие форматы</summary><div class="extra-report-actions">${links.html ? `<a id="report-html" href="${escape(links.html)}" download="quotelens-report.html">HTML</a>` : ''}${links.json ? `<a id="report-json" href="${escape(links.json)}" download="quotelens-report.json">JSON</a>` : ''}</div></details>` : ''}`;
  }

  async function continueSearch() {
    if (!state.job?.id || state.job.status === 'running' || state.continueBusy) return;
    const seconds = Math.round(Number($('#continue-minutes')?.value ?? 10) * 60);
    if (!Number.isInteger(seconds) || seconds < 1 || seconds > 3600) { showError('Выберите время от 1 секунды до 60 минут.'); return; }
    const id = state.job.id, sequence = state.resumeSequence;
    state.continueBusy = true; $('#continue-search').disabled = true; hideError();
    try {
      const job = await api(`/api/jobs/${encodeURIComponent(id)}/continue`,{method:'POST',body:JSON.stringify({max_seconds:seconds,expected_revision:state.job.revision ?? state.result?.review?.revision ?? 0})});
      if (sequence !== state.resumeSequence || state.job?.id !== id) return;
      state.job = job; state.started = Date.now();
      if (job.result) renderResult(job.result,job,true);
      setFormBusy(job.status === 'running'); $('#progress-screen').hidden = job.status !== 'running'; startClock();
      await pollJob();
    } catch(error) { if (sequence === state.resumeSequence && state.job?.id === id) showError(error.message || 'Не удалось продолжить поиск.',pollJob); }
    finally { state.continueBusy = false; if ($('#continue-search')) $('#continue-search').disabled = false; }
  }
  async function shareResult() {
    if (!state.job?.id || state.job.status === 'running' || state.shareBusy) return;
    const id = state.job.id, sequence = state.resumeSequence, button = $('#share-result'), feedback = $('#share-feedback');
    state.shareBusy = true; button.disabled = true; feedback.hidden = false; feedback.textContent = 'Создаём ссылку…';
    try {
      const result = await api(`/api/jobs/${encodeURIComponent(id)}/share`,{method:'POST',body:JSON.stringify({expected_revision:state.job.revision ?? state.result?.review?.revision ?? 0})});
      if (sequence !== state.resumeSequence || state.job?.id !== id) return;
      const url = safeURL(result.url);
      if (!url) throw new Error('Сервер не вернул ссылку на обоснование.');
      const node = $('#share-result-link'); node.hidden = false;
      node.innerHTML = `<input id="share-url" aria-label="Ссылка на обоснование" readonly value="${escape(url)}"><div><button type="button" class="text-button" data-copy-share>Скопировать ссылку</button><a href="${escape(url)}" target="_blank" rel="noopener noreferrer">Открыть</a></div>`;
      feedback.textContent = 'Сохранённая версия · только чтение';
      try { await navigator.clipboard.writeText(url); feedback.textContent = 'Ссылка скопирована · сохранённая версия, только чтение'; } catch {}
    } catch(error) { if (feedback.isConnected) feedback.textContent = error.message || 'Не удалось создать ссылку.'; }
    finally { state.shareBusy = false; if (button.isConnected) button.disabled = false; }
  }
  async function openCosts() {
    const dialog = $('#costs-dialog'), body = $('#costs-body'), sequence = ++state.costsSequence;
    body.textContent = 'Загружаем расходы…'; if (!dialog.open) dialog.showModal();
    try {
      const data = await api('/api/costs'); if (sequence !== state.costsSequence) return;
      const total = data.totals || {}, jobs = data.jobs || [];
      const tokensKnown = row => row.tokens_known === true || row.tokens_known !== false && row.cost_known === true;
      const allTokensKnown = total.tokens_known === true || total.tokens_known !== false && (jobs.length ? jobs.every(tokensKnown) : total.cost_known === true);
      const costLabel = (row,isTotal = false) => confirmedCostMarkup(row.cost_known === true || isTotal ? row.cost_usd : row.known_cost_usd ?? row.cost_usd,row.cost_known);
      body.innerHTML = `<div class="costs-total"><span>Стоимость всех поисков</span><strong>${costLabel(total,true)}</strong></div><p class="muted">Вызовов: ${number(total.model_calls,0)} · ${allTokensKnown ? 'токенов' : 'токенов по ответам'}: ${tokenFact(total.total_tokens,allTokensKnown)}${missingResponsesMarkup(total)}</p>${data.source ? `<p class="muted">${escape(data.source)}</p>` : ''}${jobs.length ? `<div class="costs-table-wrap"><table class="costs-table"><thead><tr><th>Поиск</th><th>Вызовы</th><th>Токены: вход / выход</th><th>Стоимость</th></tr></thead><tbody>${jobs.map(job => `<tr><th><a href="#job=${encodeURIComponent(job.id)}" data-cost-job="${escape(job.id)}">${escape(job.title || 'Поиск предложений')}</a><small>${escape(historyTime(job.created_at) || '')}${Array.isArray(job.passes) ? ` · проходов: ${job.passes.length}` : numeric(job.passes) ? ` · проходов: ${number(job.passes,0)}` : ''}</small>${costPassesMarkup(job.passes)}</th><td>${number(job.model_calls,0)}${missingResponsesMarkup(job)}</td><td>${numeric(job.total_tokens) && Number(job.total_tokens) > 0 || tokensKnown(job) ? `${number(job.prompt_tokens,0)} / ${number(job.completion_tokens,0)}${tokensKnown(job) ? '' : '<small>По ответам провайдера</small>'}` : 'Неизвестно'}</td><td>${costLabel(job)}</td></tr>`).join('')}</tbody></table></div>` : '<p>Сохранённых расходов пока нет.</p>'}`;
    } catch(error) { if (sequence === state.costsSequence) body.textContent = error.message || 'Не удалось загрузить расходы.'; }
  }
  async function downloadReport(event) {
    const link = event.target.closest('[data-report-download]'); if (!link) return;
    event.preventDefault(); if (state.reportBusy) return;
    const format = link.dataset.reportDownload, id = state.job?.id;
    const feedback = $('#report-feedback');
    state.reportBusy = true; feedback.hidden = false; feedback.textContent = `Готовим ${format.toUpperCase()}…`;
    $$('[data-report-download]').forEach(node => node.setAttribute('aria-disabled','true'));
    try {
      const response = await fetch(link.href,{cache:'no-store'});
      if (!response.ok) {
        let value; try { value = await response.json(); } catch {}
        throw new Error(value?.error || 'Не удалось подготовить отчёт. Попробуйте другой формат.');
      }
      const expected = format === 'pdf' ? 'application/pdf' : 'application/vnd.openxmlformats-officedocument.wordprocessingml.document';
      if (!response.headers.get('content-type')?.startsWith(expected)) throw new Error('Сервер вернул неожиданный формат отчёта. Повторите попытку.');
      const blob = await response.blob(), url = URL.createObjectURL(blob);
      const download = document.createElement('a'); download.href = url; download.download = `quotelens-${id || 'report'}.${format}`; document.body.append(download); download.click(); download.remove(); setTimeout(() => URL.revokeObjectURL(url),30000);
      if (state.job?.id === id) { feedback.textContent = 'Отчёт готов.'; feedback.setAttribute('role','status'); }
    } catch(error) { if (state.job?.id === id) { feedback.textContent = error.message; feedback.setAttribute('role','alert'); } }
    finally { state.reportBusy = false; $$('[data-report-download]').forEach(node => node.removeAttribute('aria-disabled')); }
  }
  function manuallyExcluded(offer) {
    const ids = state.result?.review?.excluded_ids || state.job?.excluded_ids || state.job?.manual_excluded_ids || [];
    return offer.manual_excluded === true || Array.isArray(ids) && ids.some(id => String(id) === String(offer.id));
  }
  function reviewMarkup(offer) {
    if (!state.job?.id || state.job.status === 'running') return '';
    const supported = window.QuoteLens.reviewSupported === true || state.result?.search?.mode !== 'snapshot' && window.QuoteLens.request === defaultRequest;
    if (!supported || window.QuoteLens.reviewSupported === false) return '<p class="review-note">Ручная проверка недоступна для сохранённого примера.</p>';
    const excluded = manuallyExcluded(offer);
    return `<form id="offer-review-form" class="offer-review-form" data-offer-id="${escape(offer.id)}" data-action="${excluded ? 'reset' : 'exclude'}"><label for="review-reason">Причина ${excluded ? 'возврата' : 'исключения'} <span class="muted">(необязательно)</span></label><textarea id="review-reason" rows="2" maxlength="300" placeholder="Комментарий к решению"></textarea><p class="review-note">${excluded ? 'Вернётся исходное решение по доказательствам. Расчёт обновится.' : 'Предложение останется в отчёте, но не будет участвовать в расчёте.'}</p><button id="review-submit" type="submit" class="button button-secondary">${excluded ? 'Вернуть исходное решение' : 'Исключить из расчёта'}</button><p id="review-feedback" class="review-feedback" role="status" hidden></p></form>`;
  }
  async function submitReview(event) {
    const form = event.target.closest('#offer-review-form'); if (!form) return;
    event.preventDefault(); if (state.reviewBusy || !state.job?.id || state.job.status === 'running') return;
    const id = state.job.id, offerId = form.dataset.offerId;
    const reason = $('#review-reason',form).value.trim() || 'Проверка пользователем';
    state.reviewBusy = true; $('#review-submit',form).disabled = true; $('#review-reason',form).disabled = true;
    const feedback = $('#review-feedback',form); feedback.hidden = false; feedback.textContent = 'Обновляем расчёт…';
    try {
      const job = await api(`/api/jobs/${encodeURIComponent(id)}/review`,{method:'POST',body:JSON.stringify({offer_id:offerId,action:form.dataset.action,reason,expected_revision:state.job.revision ?? state.result?.review?.revision ?? 0})});
      if (state.job?.id !== id) return;
      if (!job.result) throw new Error('Сервер не вернул обновлённый расчёт. Повторите попытку.');
      const stillOpen = $('#evidence-dialog').open && state.evidenceId === offerId;
      renderResult(job.result,job,true);
      if (stillOpen) {
        openEvidence(offerId,state.lastFocused,false);
        $('#review-feedback').hidden = false; $('#review-feedback').textContent = 'Решение сохранено. Расчёт и отчёт обновлены.';
        $('#review-submit')?.focus();
      }
    } catch(error) {
      if (state.job?.id === id && form.isConnected) { feedback.textContent = error.message || 'Не удалось сохранить решение. Повторите попытку.'; feedback.setAttribute('role','alert'); }
    } finally {
      state.reviewBusy = false;
      if (form.isConnected) { $('#review-submit',form).disabled = false; $('#review-reason',form).disabled = false; }
    }
  }
  async function loadSourceDocument(event) {
    const details = event.target.closest('[data-source-document]');
    if (!details?.open || details.dataset.loaded || !state.job?.id) return;
    details.dataset.loaded = 'loading'; const body = $('pre',details), id = state.job.id;
    try {
      const document = await api(`/api/jobs/${encodeURIComponent(id)}/documents/${encodeURIComponent(details.dataset.sourceDocument)}`);
      body.textContent = (typeof document.text === 'string' ? document.text : typeof document.document?.text === 'string' ? document.document.text : 'Текст источника не сохранён.') + (document.structured_sections || []).map(section=>`\n\nСохранённый HTML · вкладка «${section.title}»\n${section.text}`).join('');
      details.dataset.loaded = 'true';
    } catch(error) { body.textContent = error.message || 'Не удалось открыть текст источника.'; delete details.dataset.loaded; }
  }
  function restoreEvidenceFocus() {
    const trigger = state.lastFocused?.isConnected ? state.lastFocused : $$('[data-evidence]').find(node => node.dataset.evidence === state.evidenceId) || $('#result-filters summary');
    trigger?.focus();
  }
  const checkField = (check, kind = 'attributes') => /^(attributes|conditions)\./.test(check.key) ? check.key : `${kind}.${check.key}`;
  const fieldCheck = (offer, field) => (field.startsWith('conditions.') ? offer.condition_checks || [] : offer.requirement_checks || []).find(item => checkField(item,field.startsWith('conditions.') ? 'conditions' : 'attributes') === field);
  const reviewField = (offer, field) => (offer.review_fields || []).find(item => item.key === field);
  const isManualField = descriptor => descriptor?.confirmation?.kind === 'manual' || descriptor?.status === 'manual_confirmed';
  const fieldAcceptance = (offer, field) => reviewField(offer,field)?.acceptance || fieldCheck(offer,field)?.acceptance || offer.manual_acceptances?.[field];
  const sourceFact = (offer,field) => reviewField(offer,field) ? reviewField(offer,field).original_value : fieldValue(offer,field);
  function firstReviewField(offer) {
    const problem = ['mismatch','unconfirmed'].map(status => (offer.requirement_checks || []).find(item => item.status === status && (!offer.review_fields?.length || reviewField(offer,`attributes.${item.key}`)))).find(Boolean);
    if (problem) return `attributes.${problem.key}`;
    const editable = (offer.review_fields || []).find(item => item.required !== false && ['unconfirmed','mismatch'].includes(item.status));
    if (editable) return editable.key;
    if (offer.review_fields?.length) return null;
    const check = (offer.requirement_checks || []).find(item => ['unconfirmed','mismatch'].includes(item.status));
    if (check) return `attributes.${check.key}`;
    const requirement = (requestForOffer(offer).requirements || []).find(item => !hasFieldValue(offer.attributes?.[item.key]) || !offer.evidence?.[`attributes.${item.key}`]?.quote);
    if (requirement) return `attributes.${requirement.key}`;
    return ['unit','tax','delivery','currency','price'].find(field => !hasFieldValue(offer[field]) || field === 'unit' && offer.unit_inference) || null;
  }
  const fieldValue = (offer, field) => field.startsWith('attributes.') ? offer.attributes?.[field.slice(11)] ?? fieldCheck(offer,field)?.actual : field.startsWith('conditions.') ? offer.conditions?.[field.slice(11)] ?? fieldCheck(offer,field)?.actual : offer[field];
  const contextHint = (offer,field) => field.startsWith('attributes.') ? offer.attribute_hints?.[field.slice(11)]?.[0] : null;
  const acceptedSuggestion = (offer,field) => reviewField(offer,field)?.confirmation?.origin === 'accepted_suggestion' ? reviewField(offer,field).confirmation : null;
  const fieldEvidence = (offer, field) => acceptedSuggestion(offer,field)?.supporting_source || offer.evidence?.[field] || contextHint(offer,field) || (field === 'price' && offer.validation?.price_state === 'on_request' ? offer.evidence?.price_availability : null) || fieldCheck(offer,field)?.evidence;
  const hasFieldValue = value => value !== null && value !== undefined && value !== '' && value !== 'unknown';
  const fieldLabel = field => evidenceLabels[field] || (field.startsWith('attributes.') ? attributeLabels[field.slice(11)] || field.slice(11) : field);
  const isCIEHint = hint => ['inferred_from_CIE_whiteness','inferred_from_CIE'].includes(hint?.basis);
  function confirmableSuggestion(offer,field) {
    const descriptor = reviewField(offer,field), hint = descriptor?.confirmable_suggestion || contextHint(offer,field);
    if (!descriptor || descriptor.required === false || descriptor.confirmation || !field.startsWith('attributes.') || hasFieldValue(descriptor.value ?? fieldValue(offer,field)) || !hint?.quote || !safeURL(hint.url || offer.url) || hint.unit_reason || offer.evidence?.[field]?.unit_reason || Array.isArray(hint.value)) return null;
    const type = descriptor.type, value = hint.value;
    if (type === 'string' && typeof value !== 'string' || type === 'boolean' && typeof value !== 'boolean' || ['number','integer'].includes(type) && (typeof value !== 'number' || !Number.isFinite(value) || type === 'integer' && !Number.isInteger(value)) || !['string','boolean','number','integer'].includes(type) || !hasFieldValue(value)) return null;
    if (['number','integer'].includes(type) && !descriptor.confirmable_suggestion && (!descriptor.unit || (hint.normalized_unit || hint.source_unit || hint.unit) !== descriptor.unit)) return null;
    return hint;
  }
  function shownValueMarkup(offer,field) {
    if (!state.job?.id || state.job.status === 'running') return '';
    const accepted = acceptedSuggestion(offer,field), hint = confirmableSuggestion(offer,field);
    if (!accepted && !hint) return '';
    return `<form id="shown-value-form" class="shown-value-form" data-field="${escape(field)}" data-action="${accepted ? 'reset_field' : 'confirm_value'}"><div class="shown-value-actions">${accepted ? '<span class="shown-value-accepted">Принято</span><button type="submit" class="text-button">Отменить принятие</button>' : '<button type="submit" class="button button-primary">Принять</button>'}</div>${accepted ? '<p class="manual-confirmation">Подтверждено вами по показанному источнику.</p>' : ''}<p id="shown-value-feedback" class="review-feedback" role="status" hidden></p></form>`;
  }
  async function submitShownValue(event) {
    const form = event.target.closest('#shown-value-form'); if (!form) return;
    event.preventDefault();
    await saveCorrection({offer_id:state.evidenceId,action:form.dataset.action,fields:{[form.dataset.field]:true},expected_revision:state.job?.revision ?? state.result?.review?.revision ?? 0},form);
  }
  function displayFact(offer, field) {
    const value = reviewField(offer,field)?.value ?? fieldValue(offer,field);
    if (!hasFieldValue(value) && contextHint(offer,field)) return `${plain(contextHint(offer,field).value)} · ${isCIEHint(contextHint(offer,field)) ? 'вывод по CIE' : 'из контекста'}, проверить`;
    if (!hasFieldValue(value)) return field === 'price' ? offer.validation?.price_state === 'on_request' ? 'По запросу у продавца' : 'Цена не извлечена' : 'Не извлечено';
    if (field === 'price') return `${unitNumber(value)} ${offer.currency || 'валюта не указана'}`;
    if (field === 'tax') return {included:'Включён',excluded:'Не включён'}[value] || plain(value);
    if (Array.isArray(value) && value.length === 2 && value.every(numeric)) { const proof=fieldEvidence(offer,field); return proof?.raw_value ? displayRequirement(proof.raw_value,proof.source_unit) : requirementText(value); }
    return plain(value);
  }
  function deviationMarkup(offer,field) {
    if (!state.job?.id || state.job.status === 'running') return '';
    const request = requestForOffer(offer), descriptor = reviewField(offer,field), check = fieldCheck(offer,field);
    const conditions = Array.isArray(request.conditions) ? request.conditions : request.conditions ? [request.conditions] : [];
    const requested = field.startsWith('attributes.') ? (request.requirements || []).some(item => `attributes.${item.key}` === field) : field.startsWith('conditions.') && (!!descriptor || conditions.some((item,index) => `conditions.${typeof item === 'object' ? item.key || item.type || `condition_${index+1}` : `condition_${index+1}`}` === field));
    const acceptance = fieldAcceptance(offer,field), status = check?.status || descriptor?.status;
    if (!requested || !acceptance && (!['mismatch','unconfirmed'].includes(status) || status === 'unconfirmed' && confirmableSuggestion(offer,field))) return '';
    return `<form id="deviation-form" class="deviation-form" data-field="${escape(field)}" data-accepted="${acceptance ? 'true' : 'false'}"><h4>${acceptance ? 'Отклонение принято вручную' : 'Решение по отклонению'}</h4><p>${acceptance ? 'В расчёте учтено решение пользователя. Исходное значение и статус проверки сохранены.' : 'Принятие разрешает использовать это предложение при расчёте. Найденное значение и отсутствие подтверждения сохраняются.'}</p>${acceptance?.reason ? `<p>Причина: ${escape(acceptance.reason)}</p>` : ''}<label for="deviation-reason">Причина решения</label><textarea id="deviation-reason" rows="2" maxlength="300"${acceptance ? '' : ' required'} placeholder="Обоснуйте принятие отклонения"></textarea><button type="submit" class="button button-secondary">${acceptance ? 'Отменить принятие отклонения' : 'Принять отклонение для расчёта'}</button><p id="deviation-feedback" class="review-feedback" role="status" hidden></p></form>`;
  }
  async function submitDeviation(event) {
    const form = event.target.closest('#deviation-form'); if (!form) return;
    event.preventDefault(); if (state.reviewBusy || !state.job?.id || state.job.status === 'running') return;
    await saveCorrection({offer_id:state.evidenceId,action:'accept_deviation',fields:{[form.dataset.field]:form.dataset.accepted !== 'true'},reason:$('#deviation-reason',form).value.trim() || 'Принятие отклонения отменено',expected_revision:state.job.revision ?? state.result?.review?.revision ?? 0},form);
  }
  const supplierKey = offer => JSON.stringify([state.job?.id,offer.id,offer.document_id,offer.source_hash]);
  function supplierMarkup(offer) {
    const details = offer.supplier_details || supplierCache.get(supplierKey(offer))?.details;
    if (!details) return '';
    const fields = [['legal_name','Юридическое наименование'],['inn','ИНН'],['email','Электронная почта'],['phone','Телефон'],['website','Сайт']];
    const statuses = {not_checked:'Реестр не проверен',format_valid:'Формат ИНН корректен · реестр не проверен',invalid:'ИНН некорректен',conflict:'Противоречивые данные поставщика'};
    const status = details.verification?.status || 'not_checked';
    return `<section class="supplier-details" aria-label="Сведения о поставщике"><h3>Поставщик</h3><dl>${fields.map(([key,label]) => {
      const proof = details.evidence?.[key], value = details[key];
      return `<div><dt>${label}</dt><dd>${key === 'website' && value ? sourceLink(value,value) || escape(value) : escape(value || 'Не найдено')}${proof?.quote ? `<details><summary>Подтверждение из источника</summary><blockquote>${escape(proof.quote)}</blockquote>${sourceLink(proof.url || offer.url,'Источник')}${proof.retrieved_at ? `<small>${escape(historyTime(proof.retrieved_at) || '')}</small>` : ''}</details>` : ''}</dd></div>`;
    }).join('')}</dl><p class="supplier-verification">${escape(statuses[status] || 'Реестр не проверен')}</p>${details.inn ? sourceLink(details.verification?.registry_url || 'https://egrul.nalog.ru/','Проверить в реестре ФНС') : ''}</section>`;
  }
  async function loadSupplierDetails(offer) {
    const slot = $('#supplier-detail-slot');
    if (!slot || offer.supplier_details || !offer.document_id || !validJobID(state.job?.id) || !serverHistoryEnabled() || state.result?.search?.mode === 'snapshot') return;
    const key = supplierKey(offer), jobID = state.job.id, entry = supplierCache.get(key) || {};
    if (entry.details) { slot.innerHTML = supplierMarkup(offer); return; }
    slot.innerHTML = '<p class="muted" role="status">Загружаем сведения о поставщике…</p>';
    try {
      if (!entry.pending) {
        entry.pending = api(`/api/jobs/${encodeURIComponent(jobID)}/suppliers/${encodeURIComponent(offer.id)}`).then(payload => {
          if (String(payload.offer_id) !== String(offer.id) || !payload.supplier_details || typeof payload.supplier_details !== 'object') throw new Error('Не удалось прочитать сведения о поставщике.');
          entry.details = payload.supplier_details; return entry.details;
        });
        supplierCache.set(key,entry);
        if (supplierCache.size > 128) supplierCache.delete(supplierCache.keys().next().value);
      }
      await entry.pending;
      if (slot.isConnected && state.job?.id === jobID && state.evidenceId === String(offer.id) && supplierKey(offer) === key) slot.innerHTML = supplierMarkup(offer);
    } catch {
      supplierCache.delete(key);
      if (slot.isConnected && state.job?.id === jobID && state.evidenceId === String(offer.id)) slot.innerHTML = '<p class="muted">Сведения о поставщике пока недоступны. <button type="button" class="text-button" data-retry-supplier>Повторить</button></p>';
    }
  }
  function observedTermsMarkup(offer) {
    const kinds={delivery:'Доставка',payment:'Оплата',warranty:'Гарантия',terms:'Общие условия'};
    const groups=Object.entries(kinds).filter(([key])=>offer.observed_terms?.[key]?.length);
    const constraints=offer.pricing_conditions || [];
    if(!groups.length && !constraints.length) return '';
    return `<section class="observed-seller-terms"><h3>Условия продавца</h3>${constraints.length ? `<details><summary>Ограничения цены</summary>${constraints.map(item=>`<blockquote>${escape(item.context || item.quote)}</blockquote>${sourceLink(item.url,'Источник')}`).join('')}</details>` : ''}${groups.map(([key,label])=>`<details><summary>${label} · ${offer.observed_terms[key].length} цитат</summary>${offer.observed_terms[key].map(item=>`<blockquote>${escape(item.quote)}</blockquote>${sourceLink(item.url,'Источник')}`).join('')}</details>`).join('')}</section>`;
  }
  function fieldCorrectionMarkup(offer,field) {
    const descriptor = reviewField(offer,field);
    if (!descriptor || !state.job?.id || state.job.status === 'running') return '';
    const value = descriptor.value, choices = descriptor.options || [];
    const labels = {included:'НДС включён',excluded:'Без НДС',unknown:'Не указан',RUB:'Рубли (RUB)',true:'Да',false:'Нет'};
    let input;
    if (choices.length || descriptor.type === 'boolean') {
      const options = choices.length ? choices : [true,false];
      input = `<select id="correction-value" name="value" required>${!hasFieldValue(value) ? '<option value="">Выберите значение</option>' : ''}${options.map(option => { const val = typeof option === 'object' ? option.value : option; return `<option value="${escape(val)}"${String(val) === String(value) ? ' selected' : ''}>${escape(typeof option === 'object' ? option.label || labels[val] || val : labels[val] || val)}</option>`; }).join('')}</select>`;
    } else {
      const numericType = ['number','integer'].includes(descriptor.type);
      input = `<input id="correction-value" name="value" type="${numericType ? 'number' : 'text'}"${numericType ? ` step="${descriptor.type === 'integer' ? '1' : 'any'}"` : ' maxlength="2000"'} value="${escape(numericType && Array.isArray(value) ? '' : Array.isArray(value) ? value.join('; ') : hasFieldValue(value) ? value : '')}" required${descriptor.type === 'array' ? ' placeholder="Значения через точку с запятой"' : ''}>`;
    }
    return `<details id="field-edit" class="field-edit"><summary>Изменить значение</summary><form id="field-correction-form" class="field-correction-form" data-field="${escape(field)}" data-type="${escape(descriptor.type)}">${isManualField(descriptor) ? '<p class="manual-confirmation">Ручное подтверждение</p>' : ''}<label for="correction-value">${escape(descriptor.label || fieldLabel(field))}${descriptor.unit ? `, ${escape(descriptor.unit)}` : ''}</label>${input}<details class="correction-comment"><summary>Комментарий</summary><textarea id="correction-reason" maxlength="300" rows="2" placeholder="Необязательно"></textarea></details><div class="correction-actions"><button type="submit" class="button button-primary">Сохранить уточнение</button>${offer.manual_review ? '<button type="button" class="text-button" data-reset-corrections>Вернуть исходные данные</button>' : ''}</div>${isManualField(descriptor) ? '' : '<p class="correction-audit-note">Ручное подтверждение будет отмечено в расчёте и отчёте.</p>'}${isManualField(descriptor) && ['mismatch','unconfirmed'].includes(descriptor.status) ? `<p class="proof-missing">${descriptor.status === 'mismatch' ? 'Уточнённое значение не соответствует требованию.' : 'Уточнённое значение требует проверки.'}</p>` : ''}<p id="correction-feedback" class="review-feedback" role="status" hidden></p></form></details>`;
  }
  async function submitCorrection(event) {
    const form = event.target.closest('#field-correction-form'); if (!form) return;
    event.preventDefault(); if (state.reviewBusy || !state.job?.id || state.job.status === 'running') return;
    const input = $('#correction-value',form), type = form.dataset.type;
    let value = input.value.trim();
    if (['number','integer'].includes(type)) value = Number(value);
    else if (type === 'boolean') value = value === 'true';
    else if (type === 'array') value = value.split(';').map(item => item.trim()).filter(Boolean);
    await saveCorrection({offer_id:state.evidenceId,action:'update',fields:{[form.dataset.field]:value},reason:$('#correction-reason',form)?.value.trim() || 'Уточнено пользователем',expected_revision:state.job.revision ?? state.result?.review?.revision ?? 0},form);
  }
  async function saveCorrection(payload,form) {
    if (!state.job?.id || state.job.status === 'running' || state.reviewBusy || !form) return;
    const id = state.job.id, offerID = state.evidenceId, field = state.evidenceField;
    state.reviewBusy = true; $$('input,select,textarea,button',form).forEach(node => node.disabled = true);
    const feedback = $('#correction-feedback, #deviation-feedback, #shown-value-feedback',form); feedback.hidden = false; feedback.textContent = 'Сохраняем…';
    try {
      const job = await api(`/api/jobs/${encodeURIComponent(id)}/review`,{method:'POST',body:JSON.stringify(payload)});
      if (state.job?.id !== id) return;
      if (!job.result) throw new Error('Сервер не вернул обновлённый расчёт.');
      renderResult(job.result,job,true); openEvidence(offerID,state.lastFocused,false,field); selectEvidenceField(field,true);
      if (form.id === 'field-correction-form' && $('#field-edit')) $('#field-edit').open = true;
      const savedFeedback = form.id === 'deviation-form' ? $('#deviation-feedback') : form.id === 'shown-value-form' ? $('#shown-value-feedback') : $('#correction-feedback');
      if (savedFeedback) { savedFeedback.hidden = false; savedFeedback.textContent = 'Сохранено. Расчёт и отчёт обновлены.'; }
    } catch(error) {
      if (!form.isConnected || state.job?.id !== id) return;
      feedback.setAttribute('role','alert');
      if (error.status === 409 || error.payload?.error === 'revision_conflict') feedback.innerHTML = 'Результат изменился. <button type="button" class="text-button" data-reload-revision>Загрузить актуальные данные</button> и повторить действие.';
      else feedback.textContent = error.message || 'Не удалось сохранить уточнение.';
    } finally { state.reviewBusy = false; if (form.isConnected) $$('input,select,textarea,button',form).forEach(node => node.disabled = false); }
  }
  function requirementMatch(actual, requirement) {
    if (!hasFieldValue(actual)) return null;
    const expected = requirement.value, op = requirement.op || 'eq';
    if (['gte','>=','lte','<=','gt','>','lt','<'].includes(op)) {
      if (!numeric(actual) || !numeric(expected)) return null;
      const a = Number(actual), b = Number(expected);
      return {gte:a >= b,'>=':a >= b,lte:a <= b,'<=':a <= b,gt:a > b,'>':a > b,lt:a < b,'<':a < b}[op];
    }
    if (op === 'between') return Array.isArray(expected) && expected.length === 2 && numeric(actual) ? Number(actual) >= Number(expected[0]) && Number(actual) <= Number(expected[1]) : null;
    if (op === 'in') return Array.isArray(expected) ? expected.some(item => String(item).toLocaleLowerCase() === String(actual).toLocaleLowerCase()) : null;
    if (op === 'contains') return String(actual).toLocaleLowerCase().includes(String(expected).toLocaleLowerCase());
    if (['eq','='].includes(op)) return numeric(actual) && numeric(expected) ? Number(actual) === Number(expected) : String(actual).toLocaleLowerCase() === String(expected).toLocaleLowerCase();
    return null;
  }
  function evidenceRows(offer,conditionsOnly=false) {
    const request = requestForOffer(offer), requirements = request.requirements || [];
    const operators = {eq:'=',gte:'≥',lte:'≤',gt:'>',lt:'<',between:'в диапазоне',contains:'содержит',in:'одно из'};
    const rows = requirements.filter(item => item?.key).map(item => ({field:`attributes.${item.key}`,label:item.label || attributeLabels[item.key] || item.key,requested:`${operators[item.op] || item.op || '='} ${displayRequirement(item.value,item.unit)}`.trim(),requirement:item}));
    for (const [kind,checks] of [['attributes',offer.requirement_checks || []],['conditions',offer.condition_checks || []]]) for (const check of checks) {
      if (!check?.key) continue;
      const field = checkField(check,kind), row = rows.find(item => item.field === field);
      if (row) row.check = check;
      else rows.unshift({field,label:kind === 'conditions' ? conditionLabel({key:check.key,label:check.label,text:requirementText(check.expected)}) : check.label || attributeLabels[check.key] || check.key,requested:`${operators[check.operator] || check.operator || '='} ${displayRequirement(check.expected,check.unit)}`.trim(),check});
    }
    const missing = request.missing_critical_fields || [];
    for (const key of missing) {
      if (!rows.some(row => row.field === `attributes.${key}`)) rows.push({field:`attributes.${key}`,label:attributeLabels[key] || key,requested:'Не задано в запросе',requestMissing:true});
    }
    for (const field of ['price','currency','unit','tax','delivery','pack_size',...Object.keys(offer.attributes || {}).map(key => `attributes.${key}`),...Object.keys(offer.evidence || {})]) {
      if (!rows.some(row => row.field === field)) rows.push({field,label:fieldLabel(field),requested:field === 'unit' ? request.unit || 'Не задано' : '—'});
    }
    for (const descriptor of offer.review_fields || []) if (!rows.some(row => row.field === descriptor.key)) rows.push({field:descriptor.key,label:descriptor.label,requested:hasFieldValue(descriptor.expected) ? plain(descriptor.expected) : '—'});
    const visibleRows = rows.filter(row => {
      if (['image_url','image_source_url','price_availability'].includes(row.field)) return false;
      const descriptor = reviewField(offer,row.field), proof = fieldEvidence(offer,row.field);
      if (descriptor?.required === false && !isManualField(descriptor)) {
        return hasFieldValue(fieldValue(offer,row.field)) && !!proof?.quote && !!safeURL(proof.url || offer.url);
      }
      return true;
    });
    return visibleRows.filter(row => conditionsOnly ? row.field.startsWith('conditions.') : !row.field.startsWith('conditions.')).sort((a,b)=>Number(!a.field.startsWith('attributes.'))-Number(!b.field.startsWith('attributes.'))).map(row => {
      const value = fieldValue(offer,row.field), item = fieldEvidence(offer,row.field);
      const quote = typeof item === 'object' && item ? item.quote : typeof item === 'string' ? item : '';
      const confirmed = hasFieldValue(value) && !!quote && !!safeURL(item?.url || offer.url);
      const rejected = (offer.validation?.rejected_fields || []).includes(row.field);
      const match = row.check ? {matches:true,mismatch:false,unconfirmed:null}[row.check.status] ?? null : row.requirement ? requirementMatch(value,row.requirement) : true;
      const kind = contextHint(offer,row.field) && !hasFieldValue(value) ? 'contextual' : row.requestMissing ? 'request-missing' : rejected && !confirmed ? 'rejected' : !hasFieldValue(value) ? 'missing' : match === false ? 'mismatch' : confirmed && match !== null ? 'confirmed' : 'unconfirmed';
      const acceptance = fieldAcceptance(offer,row.field);
      const manualLabel = acceptedSuggestion(offer,row.field) ? 'Принято' : 'Уточнено';
      const label = acceptance ? 'Отклонение принято вручную' : isManualField(reviewField(offer,row.field)) ? reviewField(offer,row.field).status === 'mismatch' ? `${manualLabel} · не соответствует` : reviewField(offer,row.field).status === 'unconfirmed' ? `${manualLabel} · нужна проверка` : `${manualLabel} вручную` : {contextual:isCIEHint(contextHint(offer,row.field)) ? 'Вывод по CIE · подтвердить' : 'Из контекста · подтвердить','request-missing':'Уточнить запрос',rejected:'Проверить',missing:row.field.startsWith('conditions.') ? 'Подтвердить для закупки' : 'Не извлечено',mismatch:'Проверить',confirmed:row.requirement || row.check ? 'Соответствует' : 'Есть цитата',unconfirmed:confirmed ? 'Есть в источнике · проверить' : 'Уточнить'}[kind];
      return `<tr data-fact-field="${escape(row.field)}" class="fact-${acceptance ? 'confirmed' : kind}"><th scope="row"><span class="fact-label">${escape(row.label)}</span><span class="fact-request">${escape(row.field.startsWith('conditions.') && row.requested.length > 90 ? 'Точное условие — в проверке' : row.requested)}</span></th><td data-label="Найдено">${escape(displayFact(offer,row.field))}${row.field === 'unit' && offer.unit_inference ? '<span class="fact-inference">Вывод, требует подтверждения</span>' : ''}</td><td data-label="Подтверждение"><button type="button" class="fact-proof" data-proof-field="${escape(row.field)}" aria-controls="field-proof" aria-pressed="false"><span class="proof-dot" aria-hidden="true"></span>${escape(label)}${quote ? '<span class="proof-open">Цитата →</span>' : ''}</button></td></tr>`;
    }).join('');
  }
  function selectEvidenceField(field, focus = false) {
    const offer = (state.result?.offers || []).find(item => String(item.id) === state.evidenceId);
    if (!offer) return;
    if (['image_url','image_source_url'].includes(field)) field = firstReviewField(offer) || 'price';
    state.evidenceField = field;
    if (field.startsWith('conditions.')) { const conditions=$('#contract-conditions'); if(conditions) conditions.open=true; }
    const item = fieldEvidence(offer,field), quote = typeof item === 'object' && item ? item.quote : typeof item === 'string' ? item : '';
    const rejected = (offer.validation?.rejected_fields || []).includes(field);
    const missing = !hasFieldValue(fieldValue(offer,field));
    const descriptor = reviewField(offer,field), check = fieldCheck(offer,field), requirement = (requestForOffer(offer).requirements || []).find(item => `attributes.${item.key}` === field);
    const hint = acceptedSuggestion(offer,field)?.supporting_source || descriptor?.confirmable_suggestion || contextHint(offer,field);
    const note = descriptor?.required === false ? '' : hint?.reason || descriptor?.reason || (rejected && !quote ? 'Сохранённый источник не подтвердил извлечённое значение.' : missing ? 'В сохранённых данных значение не найдено.' : !quote ? 'Подтверждающая цитата не сохранена.' : '');
    const expected = descriptor?.expected ?? check?.expected ?? requirement?.value;
    const sourceValue = descriptor ? hasFieldValue(descriptor.original_value) ? field === 'price' ? `${unitNumber(descriptor.original_value)} ${sourceFact(offer,'currency') || 'валюта не указана'}` : Array.isArray(descriptor.original_value) ? displayFact(offer,field) : plain(descriptor.original_value) : hint ? `${plain(hint.value)} · ${isCIEHint(hint) ? 'вывод по CIE' : 'из контекста'}` : 'Не указано' : displayFact(offer,field);
    $('#field-proof').innerHTML = `<div class="proof-heading"><h3>${escape(descriptor?.label || check?.label || requirement?.label || fieldLabel(field))}</h3></div><dl class="field-comparison">${hasFieldValue(expected) ? `<div><dt>Требуется</dt><dd>${escape(displayRequirement(expected,descriptor?.unit || requirement?.unit))}</dd></div>` : ''}<div><dt>В карточке</dt><dd>${escape(sourceValue)}</dd></div>${isManualField(descriptor) ? `<div><dt>${acceptedSuggestion(offer,field) ? 'Принято' : 'Уточнено'}</dt><dd>${escape(displayFact(offer,field))}</dd></div>` : ''}</dl>${hint ? `<p class="proof-origin">${isCIEHint(hint) ? 'Вывод по CIE' : 'Из контекста источника'}${acceptedSuggestion(offer,field) ? ' · подтверждено вами' : ' · требует подтверждения'}</p>` : ''}${item?.visibility === 'reachable_tab' ? `<p class="proof-origin">Сохранённый HTML · вкладка «${escape(item.tab_label || 'Характеристики')}»</p>` : ''}${item?.unit_reason ? '<p class="proof-missing">В этой строке источника единица не указана.</p>' : ''}${quote ? `<blockquote class="field-quote">${escape(quote)}</blockquote>${sourceLink(item?.url || offer.url,'Открыть источник','proof-source-link')}` : ''}${field === 'attributes.color' && (isCIEHint(hint) || hasFieldValue(offer.attributes?.whiteness_cie)) ? '<p class="proof-missing">Цвет — отдельная характеристика; CIE показывает степень белизны.</p>' : ''}${note && (!isManualField(descriptor) || acceptedSuggestion(offer,field)) ? `<p class="proof-missing">${escape(note)}</p>` : ''}${field === 'unit' && offer.unit_inference ? `<p class="proof-missing">Предполагаемая единица: ${escape(offer.unit_inference)}</p>` : ''}${shownValueMarkup(offer,field)}${deviationMarkup(offer,field)}${fieldCorrectionMarkup(offer,field)}${!descriptor && !quote ? `<p class="proof-missing">Проверьте страницу поставщика${sourceLink(offer.url,'Открыть предложение') ? `: ${sourceLink(offer.url,'Открыть предложение')}` : '.'}</p>` : ''}`;
    $$('[data-proof-field]').forEach(button => {
      const selected = button.dataset.proofField === field;
      button.setAttribute('aria-pressed',String(selected)); button.closest('tr').classList.toggle('selected',selected);
    });
    if (focus) { setEvidencePane('source'); $('#field-proof').focus({preventScroll:true}); }
  }
  function setEvidencePane(pane) {
    $('#evidence-body').dataset.pane = pane;
    $$('[data-evidence-pane]').forEach(button => button.setAttribute('aria-pressed',String(button.dataset.evidencePane === pane)));
  }
  function openEvidence(id, trigger, focus = true, requestedField = null, background = false) {
    const offer = (state.result?.offers || []).find(item => String(item.id) === String(id));
    if (!offer) return;
    const current = state.evidenceId === String(id), field = requestedField || (current ? state.evidenceField : firstReviewField(offer) || 'price');
    const renderKey = JSON.stringify([offer,requestForOffer(offer),state.job?.status,state.job?.revision]);
    if (background && current && $('#evidence-dialog').open && state.evidenceRenderKey === renderKey) return;
    const reading = background && current && $('#evidence-dialog').open ? viewSnapshot($('#evidence-dialog')) : null;
    const pane = $('#evidence-body').dataset.pane || 'facts';
    state.evidenceRenderKey = renderKey;
    const captured = date(offer.retrieved_at), offers = state.result.offers || [], index = offers.indexOf(offer);
    const priceDescriptor = reviewField(offer,'price'), manualPrice = isManualField(priceDescriptor);
    const observedPrice = priceDescriptor ? priceDescriptor.original_value : offer.price;
    const manualBasis = ['price','currency','unit','pack_size'].some(key => isManualField(reviewField(offer,key)));
    const sourcePriceLine = manualBasis || !offer.unit ? `<p>Цена в источнике: ${numeric(observedPrice) ? `${unitNumber(observedPrice)} ${escape(sourceFact(offer,'currency') || 'валюта не указана')}` : priceUnconfirmed(offer) ? 'не подтверждена' : 'не указана'} / ${escape(sourceFact(offer,'unit') || 'единица не указана')}</p>` : '';
    $('#evidence-position').textContent = `${index + 1} / ${offers.length}`;
    $('#previous-evidence').disabled = index <= 0; $('#next-evidence').disabled = index >= offers.length - 1;
    $('#evidence-body').innerHTML = `<header class="evidence-summary"><div class="evidence-heading"><div class="evidence-status">${statusPill(offer.status)}${offer.provenance === 'synthetic' ? '<span class="synthetic-label">Учебные данные</span>' : ''}</div><h2 id="evidence-title">${escape(offer.title || 'Предложение без названия')}</h2><p class="supplier-label">${escape(offer.supplier || offer.domain || 'Поставщик не указан')}${offer.supplier && offer.domain && offer.supplier !== offer.domain ? ` · ${escape(offer.domain)}` : ''}</p></div><div class="evidence-price"><strong>${numeric(offer.price) ? `${rubles(offer) ? unitMoney(offer.price) : unitNumber(offer.price) + ' ' + escape(offer.currency || 'валюта не указана')}${offer.unit ? ' / ' + escape(offer.unit) : ''}` : priceUnconfirmed(offer) ? 'Цена не подтверждена' : offer.validation?.price_state === 'on_request' ? 'Цена по запросу' : 'Цена не извлечена'}</strong>${manualBasis ? '<span class="manual-confirmation">Цена для расчёта · уточнено вручную</span>' : ''}${sourcePriceLine}${numeric(offer.normalized_price) && rubles(offer) && Number(offer.normalized_price) !== Number(offer.price) ? `<p>За 1 ${escape(requestForOffer(offer).unit || 'ед.')}: ${unitMoney(offer.normalized_price)}</p>` : ''}<span class="evidence-captured">${captured ? `Сохранено ${escape(captured)}` : 'Дата сохранения не указана'}</span>${sourceLink(offer.url,'Открыть предложение','evidence-source-link')}</div></header><nav class="evidence-mobile-nav" aria-label="Раздел проверки"><button type="button" data-evidence-pane="facts" aria-pressed="true">Параметры и решение</button><button type="button" data-evidence-pane="source" aria-pressed="false">Источник и цитата</button></nav><div class="evidence-workspace"><section class="evidence-facts" aria-labelledby="facts-heading"><div class="workspace-section-heading"><h3 id="facts-heading">Сопоставление с запросом</h3><span>Выберите поле для проверки</span></div><table class="evidence-table"><thead><tr><th scope="col">Параметр / требуется</th><th scope="col">Найдено</th><th scope="col">Подтверждение</th></tr></thead><tbody>${evidenceRows(offer)}</tbody></table>${observedTermsMarkup(offer)}${(offer.condition_checks || []).length ? `<details id="contract-conditions" class="contract-conditions"><summary>Условия этой закупки · ${offer.condition_checks.length}</summary><p>Сравните требования договора с условиями продавца и подтвердите подходящие.</p><table class="evidence-table"><tbody>${evidenceRows(offer,true)}</tbody></table></details>` : ''}<section class="evidence-decision" aria-labelledby="decision-heading"><h3 id="decision-heading">Решение по предложению</h3><ul class="reason-list">${([...(offer.review_blockers || []),...(offer.reasons || [])].length ? [...new Set([...(offer.review_blockers || []),...(offer.reasons || [])])] : ['Причина не указана.']).map(reason => `<li>${escape(reason)}</li>`).join('')}</ul>${offer.duplicate_of ? `<p class="muted">Повтор предложения: ${escape(offer.duplicate_of)}</p>` : ''}${reviewMarkup(offer)}</section><div id="supplier-detail-slot">${supplierMarkup(offer)}</div></section><aside class="evidence-source" aria-label="Подтверждения из источника"><div class="workspace-section-heading"><h3>Источник и цитата</h3>${sourceLink(offer.url,offer.domain || 'Страница товара')}</div><section id="field-proof" tabindex="-1" aria-live="polite"></section><div id="captured-source"></div>${offer.image_url || offer.image_source_url ? `<p class="image-proof-link">${sourceLink(offer.image_source_url || offer.image_url,'Источник фотографии')}</p>` : ''}</aside></div>`;
    if (offer.document_id && state.job?.id && state.result?.search?.mode !== 'snapshot') {
      const section = document.createElement('details'); section.className = 'source-document'; section.dataset.sourceDocument = String(offer.document_id);
      const summary = document.createElement('summary'); summary.textContent = 'Сохранённый текст источника';
      const text = document.createElement('pre'); text.textContent = 'Загружаем текст источника…'; section.append(summary,text);
      section.addEventListener('toggle',loadSourceDocument); $('#captured-source').append(section);
    } else {
      $('#captured-source').innerHTML = '<p class="source-unavailable">Сохранённый текст недоступен для этого предложения. Подтверждения приведены в цитатах.</p>';
    }
    state.evidenceId = String(id); state.lastFocused = trigger || document.activeElement;
    setEvidencePane(background && current ? pane : 'facts'); selectEvidenceField(field);
    const dialog = $('#evidence-dialog');
    // A normal document layer keeps the shared feedback widget reachable.
    // showModal() would put this panel in the top layer and make its host inert.
    if (!dialog.open) {
      state.evidenceOverflow = document.body.style.overflow;
      if (typeof dialog.show === 'function') dialog.show(); else dialog.setAttribute('open','');
    }
    $('#evidence-backdrop').hidden = false;
    document.body.style.overflow = 'hidden';
    if (!background && requestedField) selectEvidenceField(field,true); else if (focus && !background) $('#close-evidence').focus();
    restoreView(dialog,reading);
    loadSupplierDetails(offer);
  }
  function closeEvidence() {
    const dialog = $('#evidence-dialog'); if (!dialog.open) return;
    if (typeof dialog.close === 'function') dialog.close(); else { dialog.removeAttribute('open'); cleanUpEvidence(); }
  }
  function cleanUpEvidence() {
    $('#evidence-backdrop').hidden = true;
    document.body.style.overflow = state.evidenceOverflow ?? ''; state.evidenceOverflow = null;
    restoreEvidenceFocus();
  }
  function navigateEvidence(offset) {
    const offers = state.result?.offers || [], index = offers.findIndex(offer => String(offer.id) === state.evidenceId);
    if (offers[index + offset]) openEvidence(offers[index + offset].id,state.lastFocused,false);
  }
  async function init() {
    state.initialized = true;
    renderSourcePicker(); renderSearchPolicy(); renderSpecPreview();
    let opening;
    if (!state.job && state.view === 'home') {
      const id = new URLSearchParams(location.hash.slice(1)).get('job');
      if (validJobID(id) && serverHistoryEnabled()) opening = resumeJob(id);
      else { renderHistory(); opening = loadHistory(); }
    }
    const results = await Promise.allSettled([api('/api/config'),api('/api/sources')]);
    if (results[0].status === 'fulfilled') state.config = results[0].value;
    if (results[1].status === 'fulfilled') {
      state.sources = (Array.isArray(results[1].value) ? results[1].value : []).filter(source => source.id);
      if (state.requestSourceIDs !== null) restoreLegacyScope();
      state.sourcesLoaded = true; renderSourcePicker();
      if (state.result) { renderFacets(); renderOffers(); }
    } else {
      // The current search scope does not depend on the legacy source registry.
      renderSourcePicker();
    }
    await opening;
  }
  function recoverView() {
    if (!state.initialized || document.hidden) return;
    clearTimeout(state.recoveryTimer);
    state.recoveryTimer = setTimeout(() => {
      state.recoveryTimer = null;
      if (document.hidden) return;
      if (state.view === 'home') loadHistory();
      else if (state.view === 'job') pollJob();
    },150);
  }
  function ensureWorkflowControls() {
    const form = $('#search-form'); form.noValidate = true;
    if (!$('#request-input')) {
      const input = document.createElement('div'); input.id = 'request-input';
      form.prepend(input);
      for (const selector of ['label[for="description"]','#description','.spec-upload-row','#data-boundary','#spec-file-list']) {
        const node = $(selector,form); if (node) input.append(node);
      }
    }
    if (!$('#research-settings')) {
      const settings = document.createElement('section'); settings.id = 'research-settings';
      form.append(settings);
      for (const selector of ['.search-target','.search-options','.form-controls']) { const node = $(selector,form); if (node) settings.append(node); }
    }
    if (!$('#draft-stage-actions')) { const actions = document.createElement('div'); actions.id = 'draft-stage-actions'; $('#spec-preview').after(actions); }
    const actions = $('#draft-stage-actions'); actions.classList.add('stage-action-row');
    actions.innerHTML = `<button id="review-back" type="button" class="button button-secondary">Назад к ТЗ</button><button id="review-next" type="button" class="button button-primary">Далее к поиску</button><button id="research-back" type="button" class="button button-secondary button-back">${icon('arrow-left')}<span>Назад к позициям</span></button>`;
    if (!$('#draft-item-navigation')) { const nav = document.createElement('nav'); nav.id = 'draft-item-navigation'; nav.setAttribute('aria-label','Позиции ТЗ'); $('#spec-items').before(nav); }
    if (!$('#offer-pagination-top')) { const nav = document.createElement('nav'); nav.id = 'offer-pagination-top'; nav.className = 'offer-pagination'; nav.setAttribute('aria-label','Страницы предложений сверху'); $('#offer-list').before(nav); }
    if (!$('#stop-mode-help')) { const help = document.createElement('details'); help.id = 'stop-mode-help'; help.className = 'inline-help'; $('#search-policy-hint').after(help); }
    if (!$('[data-open-research]')) { const button = document.createElement('button'); button.type = 'button'; button.className = 'text-button'; button.dataset.openResearch = ''; button.textContent = 'Источники и ход поиска'; $('.results-topline').append(button); }
    if (!$('[data-show-rejected]')) { const button = document.createElement('button'); button.type = 'button'; button.className = 'text-button'; button.dataset.showRejected = ''; button.textContent = 'Отклонённые'; $('.results-topline').append(button); }
    $('#evidence-dialog .drawer-header > span').textContent = 'Разбор предложения';
    $('#prepare-spec').textContent = 'Проверить позиции';
    $('#search-submit').innerHTML = `${icon('search')}Начать поиск`;
    const options = $('#stop-mode').options;
    for (const option of options) option.textContent = {target:'По готовности позиций',offers:'По числу найденных предложений',time:'По времени'}[option.value] || option.textContent;
    $('#spec-preview-heading').textContent = 'Позиции закупки';
    $('#review-back').addEventListener('click',() => { hideError(); setDraftStage('input',true); });
    $('#review-next').addEventListener('click',advanceDraft);
    $('#research-back').addEventListener('click',() => { hideError(); setDraftStage('review',true); });
    $('#draft-item-navigation').addEventListener('click',event => { const button = event.target.closest('[data-draft-item]'); if (button) selectDraftItem(Number(button.dataset.draftItem)); });
    $('#description').addEventListener('input',() => { if (draft.items.length && draft.description !== $('#description').value.trim()) specStatus('Текст изменён. Проверьте позиции заново перед поиском.'); else specStatus(''); });
  }
  ensureWorkflowControls();
  $('#attach-files').addEventListener('click',() => $('#spec-files').click());
  $('#spec-files').addEventListener('change',event => addFiles([...event.target.files]));
  $('#prepare-spec').addEventListener('click',prepareDraft);
  $('#stop-mode').addEventListener('change',renderSearchPolicy);
  $('#stop-mode-cards')?.addEventListener('change',event => {
    if (!event.target.matches('input[name=stop_mode_radio]') || state.formBusy) return;
    $('#stop-mode').value = event.target.value; renderSearchPolicy();
  });
  $('#source-picker').addEventListener('click',event => {
    const all = event.target.closest('[data-source-select-all]'), clear = event.target.closest('[data-source-clear-all]');
    if ((!all && !clear) || state.formBusy) return;
    state.sourceScope.mode = 'domains'; state.requestSourceIDs = null;
    $$('#source-options input[name=source_scope_mode]').forEach(input => { input.checked = input.value === 'domains'; });
    $('#source-domain-field').hidden = false;
    $$('#source-options [data-source-domain]').forEach(input => { if (input.dataset.sourceUnavailable !== 'true') input.checked = !!all; });
    collectSourceDomains(); renderWorkflow();
  });
  $('#search-minutes').addEventListener('input',renderSearchPolicy);
  $('#spec-file-list').addEventListener('click',async event => {
    if (state.formBusy || draft.preparing) return;
    const saved = event.target.closest('[data-remove-document]');
    if (saved) {
      const key = saved.dataset.removeDocument;
      const removed = draft.documents.find(doc => String(doc.id || doc.sha256 || doc.name) === key);
      draft.documents = draft.documents.filter(doc => String(doc.id || doc.sha256 || doc.name) !== key);
      if (removed) {
        const belongs = item => itemComesFromFile(item,removed);
        draft.savedItems = draft.savedItems.filter(item => !belongs(item)); draft.items = draft.items.filter(item => !belongs(item));
        if (removed.id) draft.removedItemIDs = draft.removedItemIDs.filter(id => !id.startsWith(removed.id + '-'));
      }
      pruneDraftDocuments();
      if (!draft.items.length) draft.stage = 'input';
      renderSpecPreview(); specStatus(draft.items.length ? 'Файл убран. Проверьте оставшиеся позиции.' : ''); return;
    }
    const button = event.target.closest('[data-remove-file]'); if (!button) return;
    draft.files.splice(Number(button.dataset.removeFile),1); ++draft.generation; renderFiles();
    setDraftStage('input'); specStatus('');
  });
  $('#spec-items').addEventListener('input',event => {
    const article = event.target.closest('[data-item-index]'), item = article && draft.items[Number(article.dataset.itemIndex)];
    if (!item) return;
    const field = event.target.dataset.itemField;
    if (field) {
      item[field] = field === 'quantity' ? event.target.value === '' ? null : Number(event.target.value) : event.target.value;
      if (field === 'description') { item._descriptionDirty = true; $('[data-item-dirty]',article).hidden = false; }
      const label = $(`[data-draft-item="${article.dataset.itemIndex}"] > span:last-child`);
      if (label && field === 'title') label.childNodes[0].textContent = item.title || 'Без названия';
      return;
    }
    const row = event.target.closest('[data-requirement-index]'); if (!row) return;
    const req = item.requirements[Number(row.dataset.requirementIndex)], key = event.target.dataset.requirementField;
    if (!req || !key) return;
    try {
      event.target.setCustomValidity('');
      if (key === 'value') { req._inputValue = event.target.value; req.value = parseRequirementValue(event.target.value,req); }
      else { req[key] = event.target.value; if (key === 'op') { const input = $('[data-requirement-field="value"]',row); input.setCustomValidity(''); req.value = parseRequirementValue(input.value,req); } }
      if (key === 'value' || key === 'op') delete req._error;
      req.manual_override = true;
    } catch(error) { req._error = error.message; (key === 'op' ? $('[data-requirement-field="value"]',row) : event.target).setCustomValidity(error.message); }
  });
  $('#spec-items').addEventListener('click',event => {
    const article = event.target.closest('[data-item-index]'), index = article ? Number(article.dataset.itemIndex) : -1;
    const remove = event.target.closest('[data-remove-item]');
    if (remove) { const [item] = draft.items.splice(Number(remove.dataset.removeItem),1); if (item?.id && !draft.removedItemIDs.includes(item.id)) draft.removedItemIDs.push(item.id); draft.savedItems = draft.savedItems.filter(saved => saved.id !== item?.id); pruneDraftDocuments(); if (!draft.items.length) draft.stage = 'input'; renderSpecPreview(); return; }
    const reparse = event.target.closest('[data-reparse-item]'); if (reparse) { reparseItem(Number(reparse.dataset.reparseItem)); return; }
    const removeReq = event.target.closest('[data-remove-requirement]');
    if (removeReq && index >= 0) { draft.items[index].requirements.splice(Number(removeReq.dataset.removeRequirement),1); renderSpecPreview(); return; }
    const addReq = event.target.closest('[data-add-requirement]');
    if (addReq && index >= 0) { const item = draft.items[index]; item.requirements ||= []; item.requirements.push({key:'manual_' + Date.now().toString(36),label:'',op:'eq',value:'',unit:null,quote:'',manual_override:true}); renderSpecPreview(); const article = $(`[data-item-index="${index}"]`); $('details',article).open = true; $$('[data-requirement-field="label"]',article).at(-1)?.focus(); }
  });
  $('#item-filter').addEventListener('change',event => selectItem(event.target.value));
  $('#offer-search').addEventListener('input',event => { state.text = event.target.value.trim(); state.page = 1; renderFacets(); renderOffers(); });
  $('#calculation-summary').addEventListener('click',downloadReport);
  $('#calculation-summary').addEventListener('click',async event => {
    if (event.target.closest('#continue-search')) continueSearch();
    if (event.target.closest('#share-result')) shareResult();
    if (event.target.closest('[data-open-costs]')) openCosts();
    if (event.target.closest('[data-copy-share]')) {
      const input = $('#share-url'); input.select();
      try { await navigator.clipboard.writeText(input.value); $('#share-feedback').textContent = 'Ссылка скопирована'; }
      catch { $('#share-feedback').textContent = 'Ссылка выделена — скопируйте её'; }
    }
  });
  $('#open-costs').addEventListener('click',openCosts);
  $('#close-costs').addEventListener('click',() => $('#costs-dialog').close());
  $('#costs-body').addEventListener('click',event => { if (event.target.closest('[data-cost-job]')) $('#costs-dialog').close(); });
  $('#search-form').addEventListener('submit',startSearch);
  function toggleQuery() {
    const expand = $('#search-form').hidden;
    $('#search-form').hidden = !expand;
    $('#edit-query').innerHTML = `${expand ? 'Свернуть запрос' : 'Запрос и ТЗ'}${icon('chevron')}`;
    $('#edit-query').setAttribute('aria-expanded',String(expand));
    $('#query-heading').setAttribute('aria-expanded',String(expand));
    if (expand) setDraftStage('input',true); else renderWorkflow();
  }
  $('#edit-query').addEventListener('click',toggleQuery);
  $('#query-heading').addEventListener('click',toggleQuery);
  $('#source-options').addEventListener('change',event => {
    if (event.target.matches('[data-source-domain]')) { state.requestSourceIDs = null; collectSourceDomains(); return; }
    const input = event.target.closest('input[name="source_scope_mode"]'); if (!input) return;
    state.requestSourceIDs = null; state.sourceScope.mode = input.value;
    $('#source-domain-field').hidden = input.value !== 'domains';
    updateSourceSummary();
    $('#source-error').hidden = true;
  });
  $('#source-options').addEventListener('input',event => {
    if (event.target.id !== 'source-domains') return;
    state.requestSourceIDs = null; collectSourceDomains();
  });
  $('#status-filter').addEventListener('change',event => { state.status = event.target.value; state.page = 1; renderFacets(); renderOffers(); });
  $('#source-filter').addEventListener('change',event => { state.source = event.target.value; state.page = 1; renderFacets(); renderOffers(); });
  for (const pagination of [$('#offer-pagination'),$('#offer-pagination-top')].filter(Boolean)) {
    pagination.addEventListener('click',event => {
      const button = event.target.closest('[data-page]'); if (!button || button.disabled) return;
      state.page = Number(button.dataset.page); renderOffers();
      $('#selected-item-heading').scrollIntoView({block:'start',behavior:'instant'});
    });
    pagination.addEventListener('change',event => { if (event.target.matches('[data-page-size]')) { state.pageSize = Number(event.target.value); state.page = 1; renderOffers(); } });
  }
  for (const layout of ['list','grid']) $(`#layout-${layout}`).addEventListener('click',() => {
    state.layout = layout;
    for (const option of ['list','grid']) { $(`#layout-${option}`).classList.toggle('active',option === layout); $(`#layout-${option}`).setAttribute('aria-pressed',String(option === layout)); }
    renderOffers();
  });
  $('#cancel-search').addEventListener('click',cancelSearch);
  $('#all-searches').addEventListener('click',event => { event.preventDefault(); showHome(); });
  $('.brand').addEventListener('click',event => { event.preventDefault(); showHome(); });
  $('#history-refresh').addEventListener('click',() => loadHistory());
  $('#history-more').addEventListener('click',() => loadHistory(true));
  $('#close-evidence').addEventListener('click',closeEvidence);
  $('#previous-evidence').addEventListener('click',() => navigateEvidence(-1));
  $('#next-evidence').addEventListener('click',() => navigateEvidence(1));
  $('#evidence-body').addEventListener('submit',submitReview);
  $('#evidence-body').addEventListener('submit',submitCorrection);
  $('#evidence-body').addEventListener('submit',submitDeviation);
  $('#evidence-body').addEventListener('submit',submitShownValue);
  $('#evidence-dialog').addEventListener('close',cleanUpEvidence);
  $('#evidence-backdrop').addEventListener('click',closeEvidence);
  $('#evidence-body').addEventListener('click',event => {
    if (event.target.closest('[data-retry-supplier]')) {
      const offer = (state.result?.offers || []).find(item => String(item.id) === state.evidenceId);
      if (offer) loadSupplierDetails(offer);
    }
    const proof = event.target.closest('[data-proof-field]'); if (proof) selectEvidenceField(proof.dataset.proofField,true);
    const pane = event.target.closest('[data-evidence-pane]'); if (pane) setEvidencePane(pane.dataset.evidencePane);
    if (event.target.closest('[data-reset-corrections]')) saveCorrection({offer_id:state.evidenceId,action:'reset',reason:'Возврат исходных данных',expected_revision:state.job?.revision ?? 0},$('#field-correction-form'));
    if (event.target.closest('[data-reload-revision]')) {
      const id = state.job?.id, offer = state.evidenceId, field = state.evidenceField;
      api(`/api/jobs/${encodeURIComponent(id)}`).then(job => { if (state.job?.id === id && job.result) { renderResult(job.result,job,true); openEvidence(offer,state.lastFocused,false,field); selectEvidenceField(field,true); } }).catch(error => { const feedback = $('#shown-value-feedback, #correction-feedback, #deviation-feedback'); if (feedback) feedback.textContent = error.message; });
    }
  });
  document.addEventListener('click',event => {
    // Shadow DOM retargets widget events to the host. Keep the visible region
    // unchanged while a person opens feedback or selects it on the page.
    const path = event.composedPath();
    const selectingFeedbackArea = $$('public-feedback-widget').some(host => [...(host.shadowRoot?.querySelectorAll('.picker') || [])].some(picker => picker.getClientRects().length > 0));
    if (event.defaultPrevented || selectingFeedbackArea || path.some(node => node?.localName === 'public-feedback-widget')) return;
    const item = event.target.closest('[data-select-item]'); if (item) selectItem(item.dataset.selectItem);
    if (event.target.closest('[data-show-rejected]')) { state.status = state.status === 'excluded' ? 'active' : 'excluded'; state.page = 1; renderFacets(); renderOffers(); }
    if (event.target.closest('[data-open-research]')) { const audit = $('#research-audit'); if (audit) { audit.open = true; audit.scrollIntoView({block:'start',behavior:'instant'}); $('summary',audit).focus(); } }
    const term = event.target.closest('.term-help-button');
    if (term) {
      const tooltip = $('#cost-term'), close = term.getAttribute('aria-expanded') === 'true';
      tooltip.hidden = close; term.setAttribute('aria-expanded',String(!close)); term.closest('.term-help').dataset.tooltipDismissed = String(close);
    } else if (!event.target.closest('.term-help')) dismissTermHelp();
    const evidence = event.target.closest('[data-evidence]'); if (evidence) openEvidence(evidence.dataset.evidence,evidence,true,evidence.dataset.evidenceField || null);
    if (!event.target.closest('#source-picker')) $('#source-picker').open = false;
    if (!event.target.closest('#result-filters')) $('#result-filters').open = false;
  });
  document.addEventListener('keydown',event => {
    // Events from the feedback widget are retargeted to its shadow host.
    // Escape belongs to the active widget or picker, never to this workspace.
    if (event.defaultPrevented || event.composedPath().some(node => node?.localName === 'public-feedback-widget')) return;
    if (event.key === 'Escape' && $('#cost-term')?.getClientRects().length) { event.preventDefault(); dismissTermHelp(); return; }
    if (event.key === 'Escape' && $('#evidence-dialog').open && $('#evidence-dialog').contains(document.activeElement)) {
      event.preventDefault(); closeEvidence(); return;
    }
    if (event.key === 'Escape' && $('#source-picker').open) { $('#source-picker').open = false; $('#source-picker-summary').focus(); }
    if (event.key === 'Escape' && $('#result-filters').open) { $('#result-filters').open = false; $('#result-filters summary').focus(); }
  });
  function dismissTermHelp() {
    const help = $('.term-help'); if (!help) return;
    help.dataset.tooltipDismissed = 'true';
    $('.term-tooltip',help).hidden = true; $('.term-help-button',help).setAttribute('aria-expanded','false');
  }
  for (const eventName of ['pointerover','focusin']) document.addEventListener(eventName,event => {
    const help = event.target.closest('.term-help');
    if (help && !help.contains(event.relatedTarget)) delete help.dataset.tooltipDismissed;
  });
  window.addEventListener('hashchange',() => {
    const id = new URLSearchParams(location.hash.slice(1)).get('job');
    if (validJobID(id) && (id !== state.job?.id || state.view !== 'job')) resumeJob(id);
    else if (!location.hash && state.view !== 'home') showHome(false);
  });
  window.addEventListener('pageshow',recoverView);
  window.addEventListener('focus',recoverView);
  document.addEventListener('visibilitychange',() => { if (document.hidden) clearTimers(); else recoverView(); });
  window.addEventListener('pagehide',clearTimers);
  window.QuoteLens.renderResult = renderResult;
  window.QuoteLens.refresh = init;
  const boot = window.QUOTE_LENS_BOOT || {};
  if (typeof boot.description === 'string') $('#description').value = boot.description;
  if (numeric(boot.quantity) && Number(boot.quantity) > 0) $('#quantity').value = boot.quantity;
  init();
})();
