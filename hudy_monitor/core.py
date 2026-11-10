"""HUDY deal monitor core: conservative discovery, JSON-LD parsing and diagnostics.

Does not invent original prices or infer in-stock sizes from a size selector.
"""
import json
import re
import sqlite3
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from urllib.parse import urljoin, urlparse, urldefrag
from urllib.robotparser import RobotFileParser
from xml.etree import ElementTree

import requests
from bs4 import BeautifulSoup

USER_AGENT = 'HudyDealMonitor/1.0 (personal price tracking; respectful crawling)'
PRODUCT_TYPES = {'Product'}
SIZE_RE = re.compile(r'(?<!\w)(?:XXL|2XL|45(?:[.,]0|[.,]5|\s*1/3|⅓)?|46(?:[.,]0)?)(?!\w)', re.I)


def money(value):
    if value is None or isinstance(value, bool):
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
    if not url:
        return None
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
        for key in ('@graph', 'itemListElement', 'item'):
            if key in node:
                yield from iter_json_nodes(node[key])


def _offers(offers):
    if isinstance(offers, list):
        return [x for x in offers if isinstance(x, dict)]
    return [offers] if isinstance(offers, dict) else []


def parse_products(html, url):
    """Parse explicit Product JSON-LD only; avoid guessed prices and availability."""
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
            if not isinstance(types, list) or not any(str(t).split('/')[-1] == 'Product' for t in types):
                continue
            brand = item.get('brand') or ''
            if isinstance(brand, dict):
                brand = brand.get('name', '')
            for offer in _offers(item.get('offers')):
                specification = offer.get('priceSpecification') or {}
                if isinstance(specification, list):
                    specification = next((s for s in specification if isinstance(s, dict)), {})
                price = money(offer.get('price') if offer.get('price') is not None else specification.get('price'))
                if price is None:
                    continue
                sizes = []
                for source in (item, offer):
                    for key in ('size', 'sizeName'):
                        v = source.get(key)
                        if isinstance(v, str):
                            sizes.append(v)
                        elif isinstance(v, list):
                            sizes.extend(str(x) for x in v)
                found.append({
                    'url': normalize_url(item.get('url') or url, url) or url,
                    'name': item.get('name') or '', 'brand': str(brand),
                    'price': price, 'currency': offer.get('priceCurrency', 'EUR'),
                    'availability': str(offer.get('availability', '')),
                    'sizes': sizes, 'sku': item.get('sku', ''),
                    'original': None,
                })
    return found


def matching_brand(product, brands):
    text = (str(product.get('brand', '')) + ' ' + str(product.get('name', ''))).casefold()
    return any(str(b).casefold() in text for b in brands)


def _norm_size(value):
    return re.sub(r'\s+', '', str(value)).upper().replace('⅓', '1/3').replace(',', '.')


def size_match(product, config):
    """True means size label matches, NOT that stock is confirmed."""
    sizes = product.get('sizes') or []
    if not sizes:
        return None
    wanted = {_norm_size(x) for x in config.get('clothing_sizes', []) + config.get('shoe_sizes', [])}
    return bool(wanted.intersection({_norm_size(x) for x in sizes}))


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
                return False
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
                if r.status_code in (429, 503) and attempt < self.retries:
                    time.sleep(min(30, 2 ** attempt * 5))
                    continue
                r.raise_for_status()
                return r.text
            except requests.RequestException:
                if attempt == self.retries:
                    raise
                time.sleep(min(15, 2 ** attempt * 2))


def diagnose_size_selector(html, url):
    """Log bounded, sanitized size-control markup for a specific product page.

    This is diagnostic only: never treat a visible size label as in stock.
    """
    if 'alto-hs-hooded-jacket-men' not in url.lower():
        return
    soup = BeautifulSoup(html, 'html.parser')
    print('DEBUG SIZE TEST:', url, flush=True)
    matches = 0
    for node in soup.find_all(string=re.compile(r'(?<![A-Za-z])(?:XXL|2XL|XL)(?![A-Za-z])', re.I)):
        parent = node.parent
        if parent is None or parent.name in ('script', 'style', 'noscript'):
            continue
        text = parent.get_text(' ', strip=True)
        if len(text) > 80:
            continue
        # Avoid printing arbitrary scripts, inline event handlers or user data.
        attrs = {}
        for key in ('class', 'disabled', 'aria-disabled', 'aria-selected', 'data-size', 'data-stock', 'title'):
            if key in parent.attrs:
                value = parent.get(key)
                attrs[key] = str(value)[:100]
        print('DEBUG SIZE ELEMENT:', parent.name, 'text=', repr(text[:80]),
              'attrs=', json.dumps(attrs, ensure_ascii=False), flush=True)
        matches += 1
        if matches >= 12:
            break
    print('DEBUG SIZE MATCHES:', matches, '(diagnostic only; stock not verified)', flush=True)


def discover(fetcher, seeds, max_pages=35, max_sitemap_urls=150):
    """Bounded, robots-aware discovery; log page structure without dumping HTML."""
    host = fetcher.host
    queue = []
    for seed in seeds:
        u = normalize_url(seed, fetcher.base)
        if u and is_same_host(u, host):
            queue.append(u)
    try:
        xml = fetcher.get(urljoin(fetcher.base, '/sitemap.xml'))
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
            print('SKIP', url, exc, flush=True)
            continue
        soup = BeautifulSoup(html, 'html.parser')
        scripts = soup.select('script[type="application/ld+json"]')
        products = parse_products(html, url)
        print('DEBUG PAGE:', url, 'HTML chars:', len(html),
              'JSON-LD blocks:', len(scripts), 'Product blocks:', len(products), flush=True)
        for script in scripts:
            try:
                data = json.loads(script.string or script.get_text())
                types = []
                for node in iter_json_nodes(data):
                    t = node.get('@type')
                    if t:
                        types.extend(t if isinstance(t, list) else [t])
                print('DEBUG JSON-LD types:', sorted(set(str(x) for x in types))[:12], flush=True)
            except (ValueError, TypeError):
                print('DEBUG JSON-LD: invalid JSON', flush=True)
        print('DEBUG LINKS:', len(soup.select('a[href]')), flush=True)
        diagnose_size_selector(html, url)
        yield url, html
        # Follow only same-host links from configured category/sale pages.
        if any(word in url.lower() for word in ('vypredaj', 'outlet', 'sale')):
            for a in soup.select('a[href]'):
                candidate = normalize_url(a.get('href'), url)
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
    old = db.execute('SELECT MIN(CAST(price AS REAL)) FROM observations WHERE url=?',
                     (product['url'],)).fetchone()[0]
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
    cur = db.execute('INSERT OR IGNORE INTO alerts VALUES(?,?,?,?)',
                     (url, alert_type, str(price), now))
    db.commit()
    return cur.rowcount == 1


def send_telegram(token, chat_id, message):
    response = requests.post('https://api.telegram.org/bot' + token + '/sendMessage',
                             json={'chat_id': chat_id, 'text': message,
                                   'disable_web_page_preview': True}, timeout=20)
    response.raise_for_status()
    data = response.json()
    if not data.get('ok'):
        raise RuntimeError('Telegram API rejected the message')


def compare_exact(product, entries, timeout=20):
    """Compare only manually verified matching variants; no fuzzy guesses."""
    results = []
    for entry in entries:
        if not entry.get('verified_same_variant') or not entry.get('size'):
            continue
        try:
            url = entry['url']
            r = requests.get(url, headers={'User-Agent': USER_AGENT}, timeout=timeout)
            r.raise_for_status()
            for other in parse_products(r.text, url):
                if other['currency'] == product['currency']:
                    results.append({'shop': entry.get('name', urlparse(url).hostname),
                                    'price': other['price'], 'url': url, 'size': entry['size']})
                    break
        except (requests.RequestException, ValueError, KeyError) as exc:
            print('Competitor unavailable:', entry.get('url'), exc)
    return results
