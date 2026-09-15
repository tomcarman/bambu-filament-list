import json
import unittest
from filament_list import variant_links, add_store_links

URL = 'https://uk.store.bambulab.com/products/pla-basic-filament'


def product(code, kind, variant_id):
    return {'@type': 'Product', 'name': f'PLA Basic - Example ({code}) / {kind} / 1 kg',
            'offers': {'url': f'{URL}?id={variant_id}'}}


class StoreLinksTest(unittest.TestCase):
    def test_exact_colour_and_type(self):
        data = {'@type': 'ProductGroup', 'hasVariant': [
            product('10502', 'Refill', '41465096077372'),
            product('10502', 'Filament with spool', '558068580753551360'),
            product('10100', 'Refill', '40206189035580')]}
        page = '<script type="application/ld+json">' + json.dumps(data) + '</script>'
        self.assertEqual(variant_links(page, URL, 'refill')['10502'], URL + '?id=41465096077372')
        self.assertEqual(variant_links(page, URL, 'spool')['10502'], URL + '?id=558068580753551360')
        self.assertNotIn('10100', variant_links(page, URL, 'spool'))

    def test_ambiguity_and_foreign_urls(self):
        data = [product('10502', 'Refill', '1'), product('10502', 'Refill', '2')]
        other = product('10100', 'Refill', '3')
        other['offers']['url'] = 'https://example.com/products/pla-basic-filament?id=3'
        page = '<script type="application/ld+json">' + json.dumps(data + [other]) + '</script>'
        self.assertEqual(variant_links(page, URL, 'refill'), {})

    def test_one_lookup_per_family_and_unresolved_rows(self):
        calls = []
        def loader(url, kind):
            calls.append((url, kind))
            return {'10502': URL + '?id=41465096077372'}
        row = {'vendor': 'Bambu Lab', 'match': 'exact catalogue match',
               'profile': 'Bambu PLA Basic @BBL X1C', 'product_code': '10502', 'store_url': ''}
        rows = [dict(row), dict(row), dict(row, match='unresolved'), dict(row, vendor='Generic')]
        add_store_links(rows, 'https://uk.store.bambulab.com', 'refill', loader)
        self.assertEqual(len(calls), 1)
        self.assertTrue(rows[0]['store_url'].endswith('?id=41465096077372'))
        self.assertEqual([r['store_url'] for r in rows[2:]], ['', ''])


if __name__ == '__main__':
    unittest.main()
