"""Read-only acceptance evidence for an explicitly started real staging search.

No POST, model stub, seeded result or provider credential is used here. The
coordinator owns the bounded live launch and supplies its persisted job ID.
"""
import argparse
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import ipaddress
from io import BytesIO
import json
from pathlib import Path
import re
import time
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen
from zipfile import ZipFile


def read_json(path):
    return json.loads(Path(path).read_text())


def write_json(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')


def normalized_space(value):
    return ' '.join(str(value).split())


def timestamp(value):
    return datetime.fromisoformat(value.replace('Z', '+00:00'))


def public_base(value):
    parsed = urlsplit(value)
    if parsed.scheme not in {'http', 'https'} or parsed.username or parsed.password:
        raise ValueError('Use the coordinator-owned staging URL without credentials.')
    try:
        private = ipaddress.ip_address(parsed.hostname).is_private
    except ValueError:
        private = parsed.hostname == 'localhost'
    if not private:
        raise ValueError('This runner is restricted to a private/loopback staging host.')
    return value.rstrip('/') + '/'


def fetch(base, path, *, binary=False, timeout=45):
    request = Request(base + path.lstrip('/'), method='GET', headers={'Accept':'*/*' if binary else 'application/json'})
    with urlopen(request, timeout=timeout) as response:
        body = response.read(32 * 1024 * 1024 + 1)
        if len(body) > 32 * 1024 * 1024:
            raise ValueError('Staging response exceeded the bounded evidence size.')
    return body if binary else json.loads(body)


def expected_payload(path):
    value = read_json(path)
    if 'job' in value and 'payload_json' in value['job']:
        value = value['job']['payload_json']
        value = json.loads(value) if isinstance(value, str) else value
    return value


def backed_number(value, text):
    try:
        expected = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return False
    for raw in re.findall(r'\d(?:[\d\s\u00a0\u202f]*\d)?(?:[.,]\d+)?', str(text)):
        try:
            if Decimal(re.sub(r'[\s\u00a0\u202f]', '', raw).replace(',', '.')) == expected:
                return True
        except InvalidOperation:
            pass
    return False


def inspect_offer(base, job_id, offer, expected_keys, after):
    problems, sources = [], {}
    if offer.get('provenance') in {'synthetic', 'snapshot'}:
        problems.append('Synthetic or snapshot offer cannot satisfy real acceptance.')
    if offer.get('product_identity', {}).get('status') != 'matches':
        problems.append('Product identity is not confirmed.')
    if offer.get('unit_inference'):
        problems.append('Price unit is inferred rather than confirmed.')
    checks = {row['key']:row for row in offer.get('requirement_checks', []) if row.get('key')}
    technical_gaps = {key:checks.get(key, {}).get('status', 'missing') for key in expected_keys if checks.get(key, {}).get('status') != 'matches'}
    if offer.get('manual_acceptances') or offer.get('manual_confirmations'):
        problems.append('Manual acceptance/confirmation is present; it is not a primary-source fact.')
    for field in ('price', 'unit', 'tax'):
        proof = offer.get('evidence', {}).get(field) or {}
        if not isinstance(proof, dict):
            proof = {}
        text = proof.get('quote')
        document_id = proof.get('document_id') or offer.get('document_id')
        if not text or not proof.get('url') or not document_id:
            problems.append(field + ': missing source quote, URL or capture ID.')
            continue
        if document_id not in sources:
            try:
                sources[document_id] = fetch(base, f'api/jobs/{job_id}/documents/{quote(str(document_id), safe="")}')
            except Exception as error:
                problems.append(field + ': saved capture read failed: ' + str(error)[:100])
                continue
        document = sources[document_id]
        if document.get('document'):
            document = document['document']
        if normalized_space(text) not in normalized_space(document.get('text', '')) and not any(normalized_space(text) in normalized_space(section.get('text', '')) for section in document.get('structured_sections', [])):
            problems.append(field + ': exact quote absent from saved source text.')
        if field == 'price' and document.get('page_kind') in {'article','listing','category','search'}:
            problems.append('price: saved source is not a primary product offer page.')
        retrieved = document.get('retrieved_at')
        if not retrieved or timestamp(retrieved) < after:
            problems.append(field + ': source was not captured in the real acceptance run.')
        digest = proof.get('source_hash')
        if digest and digest != document.get('sha256'):
            problems.append(field + ': capture hash mismatch.')
        if field == 'price' and not backed_number(offer.get('price'), text):
            problems.append('price: numeric price is not backed by its quote.')
    if offer.get('tax') != 'included':
        problems.append('VAT-inclusive comparison is not confirmed.')
    details = offer.get('supplier_details')
    if not details:
        try:
            payload = fetch(base, f'api/jobs/{job_id}/suppliers/{quote(str(offer["id"]), safe="")}')
            details = payload.get('supplier_details') or {} if payload.get('offer_id') == offer['id'] else {}
        except Exception as error:
            details = {}; problems.append('Supplier capture read failed: ' + str(error)[:100])
    identity_proof = details.get('evidence', {}).get('inn') or {}
    identity = details.get('inn') if identity_proof.get('quote') else None
    if identity:
        identity_doc_id = identity_proof.get('document_id') or offer.get('document_id')
        try:
            if identity_doc_id not in sources:
                sources[identity_doc_id] = fetch(base, f'api/jobs/{job_id}/documents/{quote(str(identity_doc_id), safe="")}')
            identity_doc = sources[identity_doc_id]
            identity_doc = identity_doc.get('document') or identity_doc
            if normalized_space(identity_proof['quote']) not in normalized_space(identity_doc.get('text','')) or str(identity) not in re.sub(r'\D','',identity_proof['quote']):
                identity = None
        except Exception:
            identity = None
    if not identity:
        problems.append('Supplier INN is not backed by a saved quote; legal distinctness is not proven.')
    return {
        'id':offer['id'], 'title':offer.get('title'), 'url':offer.get('url'),
        'price':offer.get('price'), 'normalized_price':offer.get('normalized_price'),
        'unit':offer.get('unit'), 'tax':offer.get('tax'),
        'supplier':details.get('legal_name') or offer.get('supplier'), 'confirmed_inn':identity,
        'supplier_identity_proof':identity_proof,
        'technical_gaps':technical_gaps,
        'condition_gaps':[{'key':row.get('key'), 'label':row.get('label'), 'status':row.get('status')}
                          for row in offer.get('condition_checks', []) if row.get('status') != 'matches'],
        'source_quotes':{field:offer.get('evidence', {}).get(field) for field in ('price','unit','tax')},
        'capture_metadata':[{key:doc.get(key) for key in ('id','url','title','sha256','retrieved_at')} for doc in sources.values()],
        'problems':problems,
    }


def browser_proof(base, job_id, output):
    from playwright.sync_api import sync_playwright
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path='/usr/bin/google-chrome', headless=True,
                                             args=['--no-sandbox','--disable-dev-shm-usage'])
        try:
            context = browser.new_context(viewport={'width':1440,'height':1000}, accept_downloads=True)
            page = context.new_page()
            errors, posts = [], []
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.on('request', lambda request: posts.append(request.url) if request.method == 'POST' else None)
            page.goto(base + '#job=' + job_id, wait_until='domcontentloaded')
            page.locator('#result-screen:not([hidden])').wait_for(timeout=30000)
            page.screenshot(path=str(output/'real-staging-result.png'), full_page=True)
            if page.locator('#offer-list [data-evidence]').count():
                page.locator('#offer-list [data-evidence]').first.click()
                page.screenshot(path=str(output/'real-staging-analysis.png'), full_page=True)
            return {'page_errors':errors, 'posts':posts, 'read_only':not posts}
        finally:
            browser.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-url', required=True)
    parser.add_argument('--job', required=True)
    parser.add_argument('--expected-payload', required=True)
    parser.add_argument('--target-item')
    parser.add_argument('--target-only', action='store_true', help='Explicitly authorized demo of the exact original target item only')
    parser.add_argument('--after', required=True, help='Earliest real acceptance start, RFC3339')
    parser.add_argument('--out', required=True)
    parser.add_argument('--wait-seconds', type=int, default=0)
    parser.add_argument('--browser', action='store_true')
    args = parser.parse_args()
    if not re.fullmatch(r'[a-f0-9]{24}', args.job) or not 0 <= args.wait_seconds <= 660:
        parser.error('Use one existing job ID and a bounded observation interval (0–660 seconds).')
    base = public_base(args.base_url)
    output = Path(args.out); output.mkdir(parents=True, exist_ok=True)
    expected = expected_payload(args.expected_payload)
    expected_item = next((item for item in expected.get('items', []) if item.get('id') == args.target_item), None) if args.target_item else expected['items'][0]
    if not expected_item:
        parser.error('Target item does not exist in the original payload.')
    deadline = time.monotonic() + args.wait_seconds
    while args.wait_seconds and time.monotonic() < deadline:
        # History is a bounded metadata query, not the potentially large saved
        # result. One full job read is made only after observation finishes.
        history = fetch(base, 'api/jobs?limit=20')
        brief = next((row for row in history.get('items', []) if row.get('id') == args.job), None)
        if brief is None:
            raise ValueError('Observed real job is absent from the staging history page.')
        with (output/'metadata-observations.jsonl').open('a') as log:
            log.write(json.dumps({'observed_at':datetime.now(timezone.utc).isoformat(), **brief},ensure_ascii=False)+'\n')
        if brief.get('status') not in {'running','queued'}:
            break
        time.sleep(min(60, max(0, deadline-time.monotonic())))
    job = fetch(base, 'api/jobs/' + args.job)
    write_json(output/'real-job.private.json', job)
    request = job.get('request', {})
    actual_item = next((item for item in request.get('items', []) if item.get('id') == expected_item['id']), None)
    result = job.get('result') or {}
    target = next((item for item in result.get('items', []) if item.get('id') == expected_item['id']), {})
    group = target.get('target_group') or {}
    selected_ids = set(group.get('offer_ids') or target.get('calculation', {}).get('selected_offer_ids') or [])
    selected = [offer for offer in result.get('offers', []) if offer.get('item_id') == expected_item['id']
                and (offer['id'] in selected_ids if selected_ids else offer.get('status') == 'included')]
    priced_candidates = [offer for offer in result.get('offers', []) if offer.get('item_id') == expected_item['id']
                         and offer.get('price') is not None and offer.get('product_identity', {}).get('status') == 'matches']
    reviews = {}
    for offer in [*selected, *priced_candidates[:10]]:
        if offer['id'] not in reviews:
            reviews[offer['id']] = inspect_offer(base, args.job, offer, {row['key'] for row in expected_item.get('requirements', [])}, timestamp(args.after))
    checks = [reviews[offer['id']] for offer in selected]
    inn = {offer['confirmed_inn'] for offer in checks if offer['confirmed_inn']}
    payload_same = request.get('items') == expected.get('items')
    target_same = actual_item == expected_item
    target_only_same = args.target_only and request.get('items') == [expected_item]
    fresh = bool(job.get('created_at')) and timestamp(job['created_at']) >= timestamp(args.after)
    strict = (job.get('status') == 'complete' and fresh and (payload_same or target_only_same) and target_same and len(checks) >= 3 and len(inn) >= 3
              and target.get('calculation', {}).get('status') == 'ready' and group.get('ready') is True
              and all(offer.get('status') == 'included' for offer in selected)
              and all(not offer['problems'] and not offer['technical_gaps'] and not offer['condition_gaps'] for offer in checks))
    summary = {
        'job_id':args.job, 'status':job.get('status'), 'created_at':job.get('created_at'),
        'target_id':expected_item['id'], 'target_title':expected_item['title'],
        'request_all_items_unchanged':payload_same, 'target_item_unchanged':target_same,
        'run_scope':'authorized_exact_target_only' if target_only_same else 'full_original' if payload_same else 'unexpected_input_change',
        'omitted_original_items':[{'id':item['id'],'title':item['title']} for item in expected.get('items', []) if item['id'] not in {actual['id'] for actual in request.get('items', [])}],
        'expected_requirement_count':len(expected_item.get('requirements', [])),
        'fresh_real_job':fresh, 'search_mode':result.get('search', {}).get('mode'),
        'budget':result.get('search', {}).get('budget'), 'stop':result.get('search', {}).get('stop'),
        'target_calculation':target.get('calculation'), 'target_group':group,
        'priced_primary_candidates':[{key:offer.get(key) for key in ('id','title','url','price','unit','tax','status')} for offer in priced_candidates],
        'priced_candidate_reviews':[reviews[offer['id']] for offer in priced_candidates[:10]],
        'selected_count':len(checks), 'confirmed_distinct_inn_count':len(inn),
        'offers':checks, 'strict_acceptance_pass':strict,
        'notes':['No source/model fixture or POST was used by this observer.',
                 'Different domains alone are not legal independence evidence.',
                 'Unknown technical/contract conditions remain explicit; no requirement is silently relaxed.'],
    }
    downloads, export_errors = [], []
    if job.get('status') not in {'running','queued'} and result:
        for extension in ('html','json','docx','pdf'):
            try:
                body = fetch(base, f'api/jobs/{args.job}/report.{extension}', binary=True, timeout=60)
                path = output / ('real-report.' + extension)
                path.write_bytes(body)
                if extension == 'pdf' and not body.startswith(b'%PDF-'):
                    raise ValueError('Downloaded PDF signature is invalid.')
                if extension == 'docx':
                    with ZipFile(BytesIO(body)) as archive:
                        archive.read('word/document.xml')
                if extension == 'json':
                    json.loads(body)
                if extension == 'html' and b'/feedback/widget.js' in body:
                    raise ValueError('Application feedback shell leaked into the standalone report.')
                downloads.append({'format':extension, 'bytes':len(body), 'sha256':hashlib.sha256(body).hexdigest(), 'path':str(path)})
            except Exception as error:
                export_errors.append({'format':extension, 'error':str(error)[:200]})
    summary['downloads'] = downloads
    summary['export_errors'] = export_errors
    strict = strict and len(downloads) == 4 and not export_errors
    if args.browser and job.get('status') in {'running','queued'}:
        summary['browser'] = {'pending':'Native browser proof waits for terminal status to avoid one-second full-result polling.'}
        strict = False
    elif args.browser:
        try:
            summary['browser'] = browser_proof(base, args.job, output)
            strict = strict and not summary['browser']['page_errors'] and summary['browser']['read_only']
        except Exception as error:
            summary['browser'] = {'error':str(error)[:300]}
            strict = False
    summary['strict_acceptance_pass'] = strict
    write_json(output/'acceptance.json', summary)
    print(json.dumps({key:summary[key] for key in ('job_id','status','target_title','selected_count','confirmed_distinct_inn_count','strict_acceptance_pass')}, ensure_ascii=False))
    return 0 if strict else 2


if __name__ == '__main__':
    raise SystemExit(main())
