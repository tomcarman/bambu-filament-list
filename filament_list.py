#!/usr/bin/env python3
"""List configured filament slots in a Bambu Studio project, without modifying it."""
import argparse
import csv
import json
from html import escape
import re
import sys
import zipfile
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import Request, urlopen
from urllib.error import URLError
from html.parser import HTMLParser

DEFAULT_CATALOG = Path('/Applications/BambuStudio.app/Contents/Resources/profiles/BBL/filament/filaments_color_codes.json')
FIELDS = ['slot', 'profile', 'material', 'vendor', 'hex', 'colour_name', 'product_code', 'match', 'filament_id', 'store_url']

DEFAULT_STORE = 'https://uk.store.bambulab.com'
STORE_PRODUCTS = {
    'PLA Basic': '/products/pla-basic-filament',
    'PLA Matte': '/products/pla-matte',
}

def rgba(value):
    value = str(value).strip().upper()
    if re.fullmatch(r'#[0-9A-F]{6}', value):
        return value + 'FF'
    return value

class ProductDataParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.active = False
        self.parts = []
        self.documents = []

    def handle_starttag(self, tag, attrs):
        if tag == 'script':
            self.active = dict(attrs).get('type') == 'application/ld+json'
            self.parts = []

    def handle_data(self, data):
        if self.active:
            self.parts.append(data)

    def handle_endtag(self, tag):
        if tag == 'script' and self.active:
            self.active = False
            try:
                self.documents.append(json.loads(''.join(self.parts)))
            except ValueError:
                pass


def variant_links(page, product_url, store_type):
    """Read exact colour/type variants from the store's structured product data."""
    parser = ProductDataParser()
    parser.feed(page)
    candidates = {}
    wanted_type = 'Refill' if store_type == 'refill' else 'Filament with spool'
    def visit(value):
        if isinstance(value, list):
            for item in value:
                visit(item)
        elif isinstance(value, dict):
            if value.get('@type') == 'Product':
                name = value.get('name', '')
                code = re.search(r'\((\d{5})\)', name)
                options = [part.strip() for part in name.split('/')]
                offers = value.get('offers', [])
                if isinstance(offers, dict):
                    offers = [offers]
                for offer in offers:
                    url = offer.get('url', '')
                    actual, expected = urlsplit(url), urlsplit(product_url)
                    if (code and wanted_type in options and '1 kg' in options
                            and actual.scheme == 'https' and actual.netloc == expected.netloc
                            and actual.path == expected.path and re.fullmatch(r'id=\d+', actual.query)):
                        candidates.setdefault(code.group(1), set()).add(url)
            for child in value.values():
                if isinstance(child, (dict, list)):
                    visit(child)
    visit(parser.documents)
    return {code: next(iter(urls)) for code, urls in candidates.items() if len(urls) == 1}


def fetch_variant_links(product_url, store_type):
    request = Request(product_url, headers={'User-Agent': 'bambu-filament-list/0.1'})
    with urlopen(request, timeout=15) as response:
        page = response.read(8_000_001)
    if len(page) > 8_000_000:
        raise ValueError('Store page exceeds size limit')
    return variant_links(page.decode('utf-8'), product_url, store_type)


def add_store_links(rows, store_base, store_type, loader=fetch_variant_links):
    cache = {}
    for row in rows:
        if row['vendor'] != 'Bambu Lab' or row['match'] != 'exact catalogue match':
            continue
        family = row['profile'].removeprefix('Bambu ').split(' @', 1)[0]
        path = STORE_PRODUCTS.get(family)
        if not path:
            continue
        product_url = store_base.rstrip('/') + path
        if product_url not in cache:
            try:
                cache[product_url] = loader(product_url, store_type)
            except (OSError, URLError, ValueError) as error:
                print(f'Could not resolve store variants for {family}: {error}', file=sys.stderr)
                cache[product_url] = {}
        row['store_url'] = cache[product_url].get(row['product_code'], '')


def validate_store_base(value):
    parsed = urlsplit(value)
    if parsed.scheme not in {'http', 'https'} or not parsed.netloc or parsed.query or parsed.fragment:
        raise ValueError('--store-base must be an http(s) store URL without a query or fragment')
    return value


def extract(project, catalog):
    with zipfile.ZipFile(project) as archive:
        info = archive.getinfo('Metadata/project_settings.config')
        if info.file_size > 20_000_000:
            raise ValueError('Project settings are unexpectedly large')
        settings = json.loads(archive.read(info))
    if not isinstance(settings, dict):
        raise ValueError('Project settings must be a JSON object')
    colours = settings.get('filament_colour', [])
    if not isinstance(colours, list) or not colours:
        raise ValueError('No configured filament colours found')
    def at(key, index):
        values = settings.get(key, [])
        return str(values[index]) if isinstance(values, list) and index < len(values) else ''
    rows = []
    for i, colour in enumerate(colours):
        fid = at('filament_ids', i)
        multi = at('filament_multi_colour', i)
        # Match the full colour sequence for multicolour products; never approximate.
        components = [rgba(x) for x in multi.split(';') if x.strip()] or [rgba(colour)]
        matches = [entry for entry in catalog
                   if fid and entry.get('fila_id') == fid
                   and [rgba(x) for x in entry.get('fila_color', [])] == components]
        identities = sorted(set((e.get('fila_color_name', {}).get('en', ''),
                                 str(e.get('fila_color_code', ''))) for e in matches))
        profile = at('filament_settings_id', i)
        vendor = at('filament_vendor', i)
        match = ('exact catalogue match' if len(identities) == 1 else
                 'ambiguous catalogue match' if identities else 'unresolved')
        rows.append(dict(zip(FIELDS, [i + 1, profile,
            at('filament_type', i), vendor, colour,
            ' / '.join(x[0] for x in identities), ' / '.join(x[1] for x in identities),
            match, fid, ''])))
    return rows

def render_html(rows, project_name):
    """Render a standalone, offline report; treat project text as untrusted."""
    def text(value):
        return escape(str(value), quote=True)

    body = []
    for row in rows:
        colour = str(row['hex'])
        safe_colour = colour if re.fullmatch(r'#[0-9a-fA-F]{6}(?:[0-9a-fA-F]{2})?', colour) else '#eeeeee'
        status = 'exact' if row['match'] == 'exact catalogue match' else 'other'
        store_url = row.get('store_url', '')
        store_link = (f'<a class="store" href="{text(store_url)}" target="_blank" '
                      f'rel="noopener noreferrer">View in Bambu Store</a>' if store_url else '—')
        body.append(
            '<tr>'
            f'<td class="slot">{text(row["slot"])}</td>'
            f'<td><span class="swatch" style="background-color:{safe_colour}" aria-hidden="true"></span>'
            f'<strong>{text(row["colour_name"] or "Unresolved colour")}</strong>'
            f'<code>{text(colour)}</code></td>'
            f'<td>{text(row["profile"])}<small>{text(row["vendor"])} · {text(row["material"])}</small></td>'
            f'<td class="product">{text(row["product_code"] or "—")}</td>'
            f'<td>{store_link}</td>'
            f'<td><span class="status {status}">{text(row["match"])}</span></td>'
            '</tr>'
        )
    return """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Filament list — """ + text(project_name) + """</title>
<style>
:root{color-scheme:light;font-family:system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;color:#24342e;background:#f3f6f4}
*{box-sizing:border-box}body{margin:0;padding:48px 24px}main{max-width:1120px;margin:auto}
.eyebrow{font-size:12px;font-weight:750;letter-spacing:.15em;text-transform:uppercase;color:#43735d}
h1{font-size:36px;letter-spacing:-.035em;margin:10px 0}p{line-height:1.6;color:#54665c}
.filename{overflow-wrap:anywhere}.count{display:inline-block;background:#dfece3;padding:5px 12px;border-radius:20px;font-size:13px;font-weight:650}
.table-wrap{margin-top:26px;overflow-x:auto;background:white;border:1px solid #d9e2dc;border-radius:14px;box-shadow:0 8px 30px #263c2e08}
table{border-collapse:collapse;width:100%;text-align:left}caption{text-align:left;padding:20px 22px;font-weight:650}
th{font-size:11px;text-transform:uppercase;letter-spacing:.08em;background:#f7f9f7;color:#56685d}
th,td{padding:17px 20px;border-top:1px solid #e6ece7;vertical-align:middle}td{font-size:14px}tbody tr:hover{background:#fafcfb}
.slot{color:#738278;font-variant-numeric:tabular-nums}.swatch{float:left;width:38px;height:38px;border-radius:9px;border:1px solid #0002;margin-right:12px}
strong{display:block;font-weight:650;white-space:nowrap}code,small{display:block;margin-top:5px;font-size:12px;color:#6b7b70}.product{font-variant-numeric:tabular-nums}
.store{color:#176b45;font-weight:650;text-decoration:none;white-space:nowrap}.store:hover{text-decoration:underline}
.status{display:inline-block;border-radius:6px;padding:5px 8px;font-size:11px}.exact{background:#edf5ee;color:#326343}.other{background:#fff3dc;color:#805811}
footer{font-size:12px;margin-top:22px;max-width:800px}footer p{margin:8px 0}
@media(max-width:600px){body{padding:24px 12px}h1{font-size:28px}th,td{padding:14px 12px}}
@media print{body{background:white;padding:0}main{max-width:none}.table-wrap{overflow:visible;box-shadow:none}th,td{padding:10px 8px}.swatch{print-color-adjust:exact;-webkit-print-color-adjust:exact}tr{break-inside:avoid}}
</style>
</head>
<body><main>
<div class="eyebrow">Bambu project</div><h1>Filament list</h1>
<p class="filename">""" + text(project_name) + """</p>
<span class="count">""" + str(len(rows)) + """ configured filament slots</span>
<div class="table-wrap"><table>
<caption>Project colours and materials</caption>
<thead><tr><th scope="col">Slot</th><th scope="col">Colour</th><th scope="col">Filament</th><th scope="col">Product code</th><th scope="col">Store</th><th scope="col">Catalogue match</th></tr></thead>
<tbody>""" + ''.join(body) + """</tbody></table></div>
<footer><p>Names and product codes are matched by material ID and exact colour values against the supplied Bambu Studio catalogue. Custom or unknown colours remain unresolved.</p>
<p>This report lists configured project slots, not per-plate usage or required spool quantities. Swatches are screen colour references.</p></footer>
</main></body></html>"""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', required=True, type=Path, help='Bambu Studio .3mf project')
    parser.add_argument('--format', choices=['table', 'csv', 'json', 'html'], default='table')
    parser.add_argument('--catalog', type=Path, default=DEFAULT_CATALOG,
                        help='Bambu filaments_color_codes.json; defaults to installed macOS app')
    parser.add_argument('--store-base', default=DEFAULT_STORE,
                        help='regional Bambu Store base URL (default: UK store)')
    parser.add_argument('--store-type', choices=['refill', 'spool'], default='refill',
                        help='exact 1 kg store variant to link (default: refill)')
    parser.add_argument('--no-store-links', action='store_true',
                        help='skip online store lookups for fully offline extraction')
    args = parser.parse_args()
    try:
        if args.catalog.is_file():
            catalog = json.loads(args.catalog.read_text(encoding='utf-8'))['data']
        else:
            print('Colour catalogue unavailable; names remain unresolved. Use --catalog PATH.', file=sys.stderr)
            catalog = []
        store_base = validate_store_base(args.store_base)
        rows = extract(args.project, catalog)
        if not args.no_store_links:
            add_store_links(rows, store_base, args.store_type)
        if args.format == 'json':
            print(json.dumps(rows, indent=2, ensure_ascii=False))
        elif args.format == 'html':
            print(render_html(rows, args.project.name))
        elif args.format == 'csv':
            writer = csv.DictWriter(sys.stdout, fieldnames=FIELDS)
            writer.writeheader()
            writer.writerows(rows)
        else:
            columns = ['slot', 'profile', 'hex', 'colour_name', 'product_code']
            widths = {key: max(len(key), *(len(str(r[key])) for r in rows)) for key in columns}
            print('  '.join(key.ljust(widths[key]) for key in columns))
            for row in rows:
                print('  '.join(str(row[key]).ljust(widths[key]) for key in columns))
    except (OSError, ValueError, KeyError, TypeError, zipfile.BadZipFile) as error:
        parser.exit(1, f'Error: {error}\n')

if __name__ == '__main__':
    main()
