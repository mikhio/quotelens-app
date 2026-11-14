"""Public supplier captures and explicitly synthetic requests; no network/model.

The 24-field request below is synthetic. No owner job, original specification,
request identifiers, review values or production database enter these fixtures.
"""
from copy import deepcopy
import base64
import gzip
import hashlib
import json
from pathlib import Path
import struct
import time
import unittest
from unittest.mock import patch

from quotelens.acquisition import (Acquisition, AcquisitionError, MAX_IMAGE_BYTES,
                                   _PageParser, _Response, _raster_mime, _space, acquire_image)
from quotelens.extraction import extract_product_offer
from quotelens.price_context import primary_attribute_rows, primary_product_images, primary_requirement_observations
from quotelens.selection import validate_candidate


FIXTURES = Path(__file__).parent / 'fixtures/feedback-sources'


def capture(name='lider-20261113'):
    return json.loads(gzip.decompress((FIXTURES / (name + '.json.gz')).read_bytes()))


# Synthetic independent keys and placeholder targets: extraction must report
# source facts, including contradictions, rather than copy these target values.
PUBLIC_FIELDS = [
    ('country', 'Страна происхождения', None, 'Китай'),
    ('maker', 'Производитель', None, 'Synthetic target brand'),
    ('single_volume', 'Печать одним экструдером', 'мм', '1x1x1 мм'),
    ('dual_volume', 'Печать двумя экструдерами', 'мм', '2x2x2 мм'),
    ('layer', 'Толщина слоя', 'мм', [0.01, 1]),
    ('filament', 'Диаметр используемой нити пластика', 'мм', 9),
    ('enclosure', 'Тип корпуса', None, 'Synthetic target enclosure'),
    ('materials', 'Поддерживаемые пластики', None, 'Synthetic target materials'),
    ('nozzles', 'Диаметр сопел', 'мм', '9/10 мм'),
    ('plate', 'Материал рабочей платформы', None, 'Synthetic target platform'),
    ('formats', 'Поддержка форматов', None, 'Synthetic formats'),
    ('bed_temp', 'Максимальный нагрев рабочей платформы', 'градусов', 9),
    ('extruder_temp', 'Максимальный нагрев экструдера', 'град', 9),
    ('chamber_temp', 'Максимальный нагрев рабочей камеры', 'град', 9),
    ('dryer_temp', 'Максимальный нагрев отсека для сушки пластика', 'град', 9),
    ('optimal_speed', 'Оптимальная скорость печати', 'мм/с', 9),
    ('max_speed', 'Максимальная скорость печати', 'мм/с', 9),
    ('interfaces', 'Интерфейсы подключения', None, 'Synthetic target interface'),
    ('xy_accuracy', 'Точность позиционирования X/Y', 'μm', 9),
    ('z_accuracy', 'Точность позиционирования Z', 'μm', 9),
    ('dimensions', 'Габариты принтера без упаковки (ШхГхВ)', 'мм', '1x2x3 мм'),
    ('weight', 'Вес принтера без упаковки', 'кг', 9),
    ('supply', 'Электропитание', 'Гц', '1 В, 2 Гц'),
    ('power', 'Номинальная мощность', 'Вт', 9),
]


def request(fields=PUBLIC_FIELDS):
    return {'category': 'other', 'product': '3D принтер CreatBot D600 Pro',
            'description': 'Synthetic public-capture extraction QA', 'quantity': 1, 'unit': 'шт',
            'requirements': [{'key': key, 'label': label, 'unit': unit, 'value': value, 'op': 'eq'}
                             for key, label, unit, value in fields], 'missing_critical_fields': []}


def document(body, title='Прибор FixtureD600'):
    markup = '<main itemscope itemtype="https://schema.org/Product"><h1 itemprop="name">' + title + '</h1>' + body + '</main>'
    parser = _PageParser(); parser.feed(markup)
    return {'url': 'https://supplier.example/product/', 'title': title, 'primary_title': title,
            'html': markup, 'text': _space(' '.join(parser.parts)), 'document_id': 'synthetic-qa',
            'sha256': hashlib.sha256(markup.encode()).hexdigest(), 'retrieved_at': '2026-11-13T00:00:00Z'}


class CompleteCaptureTests(unittest.TestCase):
    def test_fresh_public_capture_agrees_on_all_24_fields_without_replacing_saved_capture(self):
        saved, fresh = capture(), capture('lider-fresh-20261113')
        self.assertNotEqual(saved['sha256'], fresh['sha256'])
        self.assertNotEqual(saved['retrieved_at'], fresh['retrieved_at'])
        original = primary_requirement_observations(saved, request())
        current = primary_requirement_observations(fresh, request())
        self.assertEqual(len(current), 14)
        for requirement in request()['requirements']:
            key = requirement['key']
            with self.subTest(field=key):
                self.assertEqual(current.get(key, {}).get('value'), original.get(key, {}).get('value'))
                if key in current:
                    self.assertIn(current[key]['quote'], _space(fresh['text']))
        self.assertEqual(primary_product_images(saved)[0]['url'], primary_product_images(fresh)[0]['url'])

    def test_all_24_fields_have_an_explicit_observed_or_unknown_expectation(self):
        source, target = capture(), request()
        before = deepcopy(source)
        offer = extract_product_offer(source, target)
        observed = {
            'country': 'Китай', 'layer': 0.05, 'filament': 1.75,
            'enclosure': 'Закрытый',
            'materials': 'PLA, ABS, Carbon Fiber, Wood, Nylon, PC, PETG, HIPS, PP, Flexible, TPU, PVA и другие',
            'plate': 'боросиликатного стекла на металлическом основании и имеет специальное покрытие BuildTak для улучшения адгезии',
            'bed_temp': 100, 'extruder_temp': 420, 'chamber_temp': 70, 'dryer_temp': 65,
            'max_speed': 200, 'interfaces': 'USB (Флеш-накопитель), USB (Кабель)',
            'dimensions': '915x845x1085 мм', 'weight': 125,
        }
        unknown = {'maker', 'single_volume', 'dual_volume', 'nozzles', 'formats', 'optimal_speed',
                   'xy_accuracy', 'z_accuracy', 'supply', 'power'}
        self.assertEqual(len(target['requirements']), 24)
        self.assertEqual({r['key'] for r in target['requirements']}, set(observed) | unknown)
        for key, value in observed.items():
            with self.subTest(field=key):
                self.assertEqual(offer['attributes'][key], value)
                proof = offer['evidence']['attributes.' + key]
                self.assertIn(proof['quote'], _space(source['text']))
                self.assertEqual(proof['url'], source['url'])
                self.assertEqual(proof['source_hash'], source['sha256'])
                self.assertEqual(proof['document_id'], source['document_id'])
                self.assertEqual(proof['retrieved_at'], source['retrieved_at'])
                self.assertTrue(proof['raw_label'])
                self.assertTrue(proof['raw_value'])
                self.assertTrue(proof['locator'])
        for key in unknown:
            with self.subTest(field=key):
                self.assertNotIn(key, offer['attributes'])
                self.assertNotIn('attributes.' + key, offer['evidence'])
        self.assertEqual(offer['price'], 1250000)
        self.assertEqual(offer['unit'], 'шт')
        self.assertEqual(source, before)

    def test_units_and_source_multiple_values_are_preserved_without_target_copy(self):
        offer = extract_product_offer(capture(), request())
        proof = offer['evidence']['attributes.layer']
        self.assertEqual(proof['raw_value'], '50 мкм')
        self.assertEqual(proof['source_unit'], 'мкм')
        self.assertEqual(proof['normalized_unit'], 'мм')
        self.assertIn('unit_conversion', proof['normalization'])
        self.assertEqual(offer['evidence']['attributes.extruder_temp']['raw_value'], '260 °С / 420 °С')
        self.assertEqual(offer['evidence']['attributes.max_speed']['raw_value'], '10 - 200 мм/с')
        self.assertEqual(offer['evidence']['attributes.dryer_temp']['raw_value'], '45 или 65 °С')

    def test_primary_photo_comes_from_gallery_dom_not_a_text_quote_or_logo(self):
        source = capture(); offer = extract_product_offer(source, request())
        self.assertEqual(offer['image_url'], 'https://lider-3d.ru/upload/iblock/049/0497fe2bf5e84c7eadd38cafcd31e1c3.jpg')
        self.assertEqual(offer['image_source_url'], source['url'])
        proof = offer['evidence']['image_url']
        self.assertNotIn('quote', proof)
        self.assertEqual(proof['basis'], 'primary_product_dom')
        self.assertEqual(proof['attribute'], 'href')
        self.assertIn(proof['raw_value'], source['html'])
        self.assertEqual(proof['source_hash'], source['sha256'])


class GeneralAttributeGuardTests(unittest.TestCase):
    def test_labelled_table_definition_list_and_property_blocks_use_arbitrary_keys(self):
        target = request([('random_country', 'Страна происхождения', None, 'Wrong target'),
                          ('random_mass', 'Вес принтера без упаковки', 'кг', 1),
                          ('random_brand', 'Производитель', None, 'Wrong target')])
        source = document('<table><tr><th>Страна производитель</th><td>Япония</td></tr></table>'
                          '<dl><dt>Вес нетто</dt><dd>2500 г</dd></dl>'
                          '<div class="property"><span class="property-name">Бренд</span><span class="property-value">Fixture Maker</span></div>')
        attrs = extract_product_offer(source, target)['attributes']
        self.assertEqual(attrs['random_country'], 'Япония')
        self.assertEqual(attrs['random_mass'], 2.5)
        self.assertNotIn('random_brand', attrs)  # A brand does not prove a manufacturer.
        self.assertEqual(attrs['brand'], 'Fixture Maker')

    def test_old_hidden_recommended_and_foreign_product_values_never_fill_a_gap(self):
        for foreign in [
            '<div hidden><table><tr><td>Вес нетто</td><td>900 кг</td></tr></table></div>',
            '<div style="display:none"><div>Вес нетто: 900 кг</div></div>',
            '<del><div>Вес нетто: 900 кг</div></del>',
            '<div class="related-products"><div>Вес нетто: 900 кг</div></div>',
            '<section><h2>Другой прибор Other900</h2><table><tr><td>Вес нетто</td><td>900 кг</td></tr></table></section>',
            '<div itemscope itemtype="https://schema.org/Product"><h2 itemprop="name">Другой прибор Other900</h2><div>Вес нетто: 900 кг</div></div>',
        ]:
            with self.subTest(markup=foreign):
                source = document('<div class="price">100 ₽ за 1 шт.</div>' + foreign)
                target = request([('mass', 'Вес принтера без упаковки', 'кг', 900)])
                offer = extract_product_offer(source, target)
                self.assertNotIn('mass', offer['attributes'])
                candidate = {'title': source['title'], 'attributes': {'mass': 900},
                             'evidence': {'attributes.mass': {'quote': 'Вес нетто 900 кг'}}}
                self.assertNotIn('mass', validate_candidate(candidate, source, target)['attributes'])

    def test_same_number_with_wrong_unit_or_label_is_not_the_requested_fact(self):
        target = request([('power', 'Номинальная мощность', 'Вт', 2000)])
        for row in ['<tr><td>Номинальная мощность</td><td>2000 Гц</td></tr>',
                    '<tr><td>Вес нетто</td><td>2000 кг</td></tr>']:
            with self.subTest(row=row):
                self.assertNotIn('power', extract_product_offer(document('<table>' + row + '</table>'), target)['attributes'])
        source = document('<table><tr><td>Номинальная мощность</td><td>2000 кВт</td></tr></table>')
        candidate = {'title': source['title'], 'attributes': {'power': 2000},
                     'evidence': {'attributes.power': {'quote': 'Номинальная мощность 2000 кВт'}}}
        self.assertNotIn('power', validate_candidate(candidate, source, target)['attributes'])
        self.assertEqual(extract_product_offer(source, target)['attributes']['power'], 2000000)
        target = request([('temp', 'Температура камеры печати', 'град', -10)])
        source = document('<table><tr><td>Температура камеры печати:</td><td>−10 °C</td></tr></table>')
        self.assertEqual(extract_product_offer(source, target)['attributes']['temp'], -10)

    def test_conflicting_primary_rows_and_unspecified_multiple_values_stay_unknown(self):
        target = request([('mass', 'Вес принтера без упаковки', 'кг', 125)])
        source = document('<table><tr><td>Вес нетто</td><td>125 кг</td></tr><tr><td>Вес нетто</td><td>145 кг</td></tr></table>')
        self.assertNotIn('mass', extract_product_offer(source, target)['attributes'])
        candidate = {'title': source['title'], 'attributes': {'mass': 125},
                     'evidence': {'attributes.mass': {'quote': 'Вес нетто 125 кг'}}}
        self.assertNotIn('mass', validate_candidate(candidate, source, target)['attributes'])
        target = request([('temp', 'Температура экструдера', 'град', 420)])
        source = document('<table><tr><td>Температура экструдера</td><td>260 °C / 420 °C</td></tr></table>')
        self.assertNotIn('temp', extract_product_offer(source, target)['attributes'])
        target = request([('max_speed', 'Максимальная скорость печати', 'мм/с', 45)])
        source = document('<table><tr><td>Оптимальная скорость печати</td><td>45 мм/с</td></tr></table>')
        self.assertNotIn('max_speed', extract_product_offer(source, target)['attributes'])

    def test_product_gallery_excludes_unsafe_hidden_icon_and_neighbour_images(self):
        for image in [
            '<img itemprop="image" src="http://supplier.example/a.jpg">',
            '<img itemprop="image" src="https://127.0.0.1/a.jpg">',
            '<img itemprop="image" src="https://supplier.example/a.jpg?token=x&amp;api_key=secret">',
            '<img itemprop="image" src="https://supplier.example/logo.png">',
            '<img itemprop="image" src="https://supplier.example/image.svg">',
            '<img hidden itemprop="image" src="https://supplier.example/a.jpg">',
            '<div class="recommend"><img itemprop="image" src="https://supplier.example/a.jpg"></div>',
        ]:
            with self.subTest(image=image):
                source = document(image)
                self.assertEqual(primary_product_images(source), [])
                self.assertNotIn('image_url', extract_product_offer(source, request([])))
        source = document('<div class="product-gallery"><img src="/loader.svg" data-src="/actual.png"></div>')
        self.assertEqual(primary_product_images(source)[0]['url'], 'https://supplier.example/actual.png')


class ImageTransportGuardTests(unittest.TestCase):
    PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+j/wAAAABJRU5ErkJggg==')

    def test_acquires_checked_raster_and_returns_the_final_response_hash(self):
        response = _Response('https://cdn.example/photo.png', 200, self.PNG, {'content-type': 'image/png'})
        with patch.object(Acquisition, '_request', return_value=response) as read:
            result = acquire_image('https://supplier.example/photo.png', timeout=2)
        self.assertEqual(result, {'body': self.PNG, 'mime': 'image/png', 'url': response.url,
                                  'sha256': hashlib.sha256(self.PNG).hexdigest()})
        self.assertEqual(read.call_args.kwargs['max_bytes'], MAX_IMAGE_BYTES)
        self.assertFalse(read.call_args.kwargs['robots'])
        self.assertLess(read.call_args.args[1] - time.monotonic(), 2)

    def test_html_svg_mime_confusion_bad_status_and_huge_canvas_are_rejected(self):
        huge = bytearray(self.PNG); huge[16:20] = struct.pack('>I', 16001)
        for body, mime, status in [(b'<svg xmlns="http://www.w3.org/2000/svg"/>', 'image/svg+xml', 200),
                                   (b'<html>Login</html>', 'image/png', 200),
                                   (self.PNG, 'text/html', 200), (self.PNG, 'image/png', 404),
                                   (bytes(huge), 'image/png', 200)]:
            with self.subTest(mime=mime, status=status), patch.object(Acquisition, '_request', return_value=_Response('https://supplier.example/a.png', status, body, {'content-type': mime})), self.assertRaises(AcquisitionError):
                acquire_image('https://supplier.example/a.png')

    def test_private_redirect_is_rejected_by_the_shared_transport_before_second_connection(self):
        class Reply:
            status = 302
            def getheaders(self):
                return [('Location', 'https://127.0.0.1/private.png')]
        class Connection:
            sock = None
            def request(self, *args, **kwargs): pass
            def getresponse(self): return Reply()
            def close(self): pass
        with patch.object(Acquisition, '_check_robots'), patch('quotelens.acquisition._public_addresses', return_value=['93.184.216.34']) as dns, patch('quotelens.acquisition._PinnedHTTPS', return_value=Connection()) as connection, self.assertRaises(AcquisitionError):
            acquire_image('https://supplier.example/a.png')
        self.assertEqual(dns.call_count, 1)
        self.assertEqual(connection.call_count, 1)


if __name__ == '__main__':
    unittest.main()
