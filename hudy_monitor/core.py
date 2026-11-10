import json
import re
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from urllib.parse import urljoin, urlparse, urldefrag
from urllib.robotparser import RobotFileParser
from xml.etree import ElementTree

import requests
from bs4 import BeautifulSoup

USER_AGENT = 'HudyDealMonitor/1.0 (personal price tracking; respectful crawling)'
PRODUCT_TYPES = {'Product'}
SIZE_RE = re.compile(r'(?<!\d)(?:XXL|2XL|45(?:[.,]0|[.,]5|\s*1/3|⅓)?|46(?:[.,]0)?)(?!\d)', re.I)


def money(value):
    if value is None:
        return None
    if isinstance(value, (int, float, Decimal)):
        return Decimal(str(value))
    s = str(value).replace('\u00a0', ' ').replace('€', '').replace('EUR', '').strip()
    s = re.sub(r'[^\d,.\-]', '', s)
    if not s:
        return None
    if ',' in s and '.' in s:
        s = s.replace('.', '').replace(',', '.') if s.rfind(',') > s.rfind('.') else s.replace(',', '')
    elif ',' in s:
        s = s.replace(',', '.')
    try:
        return Decimal(s)
    except InvalidOperation:
        return None


def normalize_url(url, base):
    absolute = urldefrag(urljoin(base, url))[0]
    parsed = urlparse(absolute)
    if parsed.scheme not in ('https', 'http'):
        return None
    return parsed._replace(query='', fragment='').geturl()


def is_same_host(url, host):
    return urlparse(url).hostname == host


def iter_json_nodes(node):
    if isinstance(node, list):
        for child in node:
            yield from iter_json_nodes(child)
    elif isinstance(node, dict):
        yield node
        if '@graph' in node:
            yield from iter_json_nodes(node['@graph'])


def parse_products(html, url):
    soup = BeautifulSoup(html, 'html.parser')
    found = []
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            raw = json.loads(script.string or script.get_text())
        except (ValueError, TypeError):
            continue
        for item in iter_json_nodes(raw):
            types = item.get('@type', [])
            types = [types] if isinstance(types, str) else types
            if not PRODUCT_TYPES.intersection(types):
                continue
            offers = item.get('offers') or {}
            offers = offers if isinstance(offers, list) else [offers]
            for offer in offers:
                if not isinstance(offer, dict):
                    continue
                price = money(offer.get('price') or (offer.get('priceSpecification') or {}).get('price'))
                if price is None:
                    continue
                brand = item.get('brand') or ''
                if isinstance(brand, dict):
                    brand = brand.get('name', '')
                sizes = []
                for source in (item, offer):
                    for key in ('size', 'sizeName', 'sizeSystem'):
                        v = source.get(key)
                        if isinstance(v, str):
                            sizes.append(v)
                        elif isinstance(v, list):
                            sizes.extend(str(x) for x in v)
                found.append({
                    'url': url, 'name': item.get('name') or '', 'brand': str(brand),
                    'price': price, 'currency': offer.get('priceCurrency', 'EUR'),
                    'availability': str(offer.get('availability', '')),
                    'sizes': sizes, 'sku': item.get('sku', ''),
                    'original': None,  # Never infer a crossed-out price from ambiguous HTML.
                })
    return found


def matching_brand(product, brands):
    text = (product['brand'] + ' ' + product['name']).casefold()
    return any(b.casefold() in text for b in brands)


def size_match(product, config):
    """Return True, False or None (not verifiable)."""
    sizes = product.get('sizes') or []
    if not sizes:
        return None
    normalized = [re.sub(r'\s+', '', str(x)).upper().replace('⅓', '1/3').replace(',', '.') for x in sizes]
    wanted = config['clothing_sizes'] + config['shoe_sizes']
    wanted = {re.sub(r'\s+', '', str(x)).upper().replace('⅓', '1/3').replace(',', '.') for x in wanted}
    return bool(wanted.intersection(normalized))


def discount_percent(product):
    old, new = product.get('original'), product['price']
    if old is None or old <= new or new < 0:
        return None
    return (Decimal('100') * (old - new) / old).quantize(Decimal('0.1'))


class Fetcher:
    def __init__(self, base, delay=3, timeout=20, retries=2):
        self.base = base
        self.host = urlparse(base).hostname
        self.delay = max(1.0, float(delay))
        self.timeout = timeout
        self.retries = retries
        self.session = requests.Session()
        self.session.headers.update({'User-Agent': USER_AGENT, 'Accept-Language': 'sk-SK,sk;q=0.9'})
        self.robots = RobotFileParser()
        self.robots.set_url(urljoin(base, '/robots.txt'))
        self.robots_loaded = False
        self.last_request = 0

    def allowed(self, url):
        if not is_same_host(url, self.host):
            return False
        if not self.robots_loaded:
            try:
                r = self.session.get(urljoin(self.base, '/robots.txt'), timeout=self.timeout)
                if r.status_code == 200:
                    self.robots.parse(r.text.splitlines())
                elif r.status_code in (401, 403):
                    return False
                else:
                    self.robots.parse(['User-agent: *', 'Disallow:'])
                self.robots_loaded = True
            except requests.RequestException:
                return False  # Fail closed if robots policy cannot be checked.
        return self.robots.can_fetch(USER_AGENT, url)

    def get(self, url):
        if not self.allowed(url):
            raise PermissionError('Blocked by robots.txt or outside allowed host: ' + url)
        for attempt in range(self.retries + 1):
            wait = self.delay - (time.monotonic() - self.last_request)
            if wait > 0:
                time.sleep(wait)
            self.last_request = time.monotonic()
            try:
                r = self.session.get(url, timeout=self.timeout, allow_redirects=True)
                if not is_same_host(r.url, self.host):
                    raise PermissionError('Redirected to external host')
                if r.status_code in (429, 503):
                    if attempt < self.retries:
                        time.sleep(min(30, 2 ** attempt * 5))
                        continue
                r.raise_for_status()
                return r.text
            except requests.RequestException:
                if attempt == self.retries:
                    raise
                time.sleep(min(15, 2 ** attempt * 2))


def discover(fetcher, seeds, max_pages=35, max_sitemap_urls=150):
    """Conservative discovery via seeds, on-site links and optional sitemap."""
    host = fetcher.host
    queue = []
    for seed in seeds:
        u = normalize_url(seed, fetcher.base)
        if u and is_same_host(u, host):
            queue.append(u)
    # Sitemaps are hints only; bounded to avoid crawling entire site.
    for sitemap in [urljoin(fetcher.base, '/sitemap.xml')]:
        try:
            xml = fetcher.get(sitemap)
            root = ElementTree.fromstring(xml)
            locs = [e.text for e in root.iter() if e.tag.endswith('loc') and e.text]
            for loc in locs[:max_sitemap_urls]:
                u = normalize_url(loc, fetcher.base)
                if u and is_same_host(u, host) and not u.endswith('.xml'):
                    queue.append(u)
        except (ValueError, ElementTree.ParseError, PermissionError, requests.RequestException):
            pass
    seen = set()
    while queue and len(seen) < max_pages:
        url = queue.pop(0)
        if url in seen:
            continue
        seen.add(url)
        try:
            html = fetcher.get(url)
        except (requests.RequestException, PermissionError) as exc:
            print('SKIP', url, exc)
            continue
        
        soup_debug = BeautifulSoup(html, 'html.parser')
        jsonld = soup_debug.select('script[type="application/ld+json"]')
        print(
            "DEBUG PAGE:",
            
        for script in jsonld:
            try:
                data = json.loads(
                    script.string or script.get_text()
                )
                if isinstance(data, dict):
                    print(
                        "DEBUG JSON-LD:",
                        data.get("@type"),
                        list(data.keys())[:10],
                        flush=True
                    )
                elif isinstance(data, list):
                    print(
                        "DEBUG JSON-LD list:",
                        len(data),
                        flush=True
                    )
            except (ValueError, TypeError):
                pass

        print(
            "DEBUG LINKS:",
            len(soup_debug.select("a[href]")),
            flush=True
        )
        
            url,
            "HTML bytes:", len(html),
            "JSON-LD blocks:", len(jsonld),
            "Product blocks:", len(parse_products(html, url)),
            flush=True
        )
        
        yield url, html
        soup = BeautifulSoup(html, 'html.parser')
        # Only follow links from configured sale/category pages, not every product page.
        if any(word in url.lower() for word in ('vypredaj', 'outlet', 'sale')):
            for a in soup.select('a[href]'):
                candidate = normalize_url(a['href'], url)
                if candidate and is_same_host(candidate, host) and candidate not in seen and candidate not in queue:
                    if len(queue) < max_pages * 5:
                        queue.append(candidate)


def init_db(db):
    db.executescript('''
    CREATE TABLE IF NOT EXISTS products (
      url TEXT PRIMARY KEY, name TEXT NOT NULL, brand TEXT,
      first_seen TEXT NOT NULL, last_seen TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS observations (
      id INTEGER PRIMARY KEY AUTOINCREMENT, url TEXT NOT NULL,
      checked_at TEXT NOT NULL, price TEXT NOT NULL, currency TEXT,
      original_price TEXT, discount TEXT, sizes_json TEXT,
      availability TEXT, FOREIGN KEY(url) REFERENCES products(url)
    );
    CREATE INDEX IF NOT EXISTS idx_obs_url ON observations(url, checked_at);
    CREATE TABLE IF NOT EXISTS alerts (
      url TEXT NOT NULL, alert_type TEXT NOT NULL, price TEXT NOT NULL,
      created_at TEXT NOT NULL, PRIMARY KEY(url, alert_type, price)
    );
    CREATE TABLE IF NOT EXISTS competitor_observations (
      id INTEGER PRIMARY KEY AUTOINCREMENT, hudy_url TEXT, competitor_url TEXT,
      shop TEXT, price TEXT, checked_at TEXT, size TEXT
    );
    ''')
    db.commit()


def store(db, product):
    now = datetime.now(timezone.utc).isoformat()
    old = db.execute('SELECT MIN(CAST(price AS REAL)) FROM observations WHERE url=?', (product['url'],)).fetchone()[0]
    db.execute('''INSERT INTO products(url,name,brand,first_seen,last_seen) VALUES(?,?,?,?,?)
        ON CONFLICT(url) DO UPDATE SET name=excluded.name,brand=excluded.brand,last_seen=excluded.last_seen''',
        (product['url'], product['name'], product['brand'], now, now))
    discount = discount_percent(product)
    db.execute('''INSERT INTO observations(url,checked_at,price,currency,original_price,discount,sizes_json,availability)
        VALUES(?,?,?,?,?,?,?,?)''', (product['url'], now, str(product['price']), product['currency'],
        str(product['original']) if product['original'] is not None else None,
        str(discount) if discount is not None else None,
        json.dumps(product['sizes']), product['availability']))
    db.commit()
    return old is not None and product['price'] < Decimal(str(old))


def mark_alert(db, url, alert_type, price):
    now = datetime.now(timezone.utc).isoformat()
    cur = db.execute('INSERT OR IGNORE INTO alerts VALUES(?,?,?,?)', (url, alert_type, str(price), now))
    db.commit()
    return cur.rowcount == 1


def send_telegram(token, chat_id, message):
    response = requests.post('https://api.telegram.org/bot' + token + '/sendMessage',
                             json={'chat_id': chat_id, 'text': message, 'disable_web_page_preview': True}, timeout=20)
    response.raise_for_status()
    data = response.json()
    if not data.get('ok'):
        raise RuntimeError('Telegram API rejected the message')


def compare_exact(product, entries, timeout=20):
    """Only compare manually matched URLs with same size and verified variant.
    A competitor is never scraped unless the URL is explicitly configured.
    """
    results = []
    for entry in entries:
        if not entry.get('verified_same_variant') or not entry.get('size'):
            continue
        try:
            url = entry['url']
            r = requests.get(url, headers={'User-Agent': USER_AGENT}, timeout=timeout)
            r.raise_for_status()
            candidates = parse_products(r.text, url)
            for other in candidates:
                if other['currency'] != product['currency']:
                    continue
                # Explicitly verified same product/variant by operator.
                results.append({'shop': entry.get('name', urlparse(url).hostname),
                                'price': other['price'], 'url': url, 'size': entry['size']})
                break
        except (requests.RequestException, ValueError, KeyError) as exc:
            print('Competitor unavailable:', entry.get('url'), exc)
    return results
