import argparse
import os
import sqlite3
from pathlib import Path

import yaml
from dotenv import load_dotenv

from .core import (Fetcher, compare_exact, discount_percent, discover,
                   init_db, mark_alert, matching_brand, parse_products,
                   send_telegram, size_match, store)


def main():
    parser = argparse.ArgumentParser(description='HUDY sale monitor')
    parser.add_argument('--dry-run', action='store_true', help='Do not send Telegram messages')
    parser.add_argument('--report', action='store_true', help='Show recent observations')
    args = parser.parse_args()
    load_dotenv()
    config = yaml.safe_load(Path(os.getenv('HUDY_CONFIG', 'config.yaml')).read_text(encoding='utf-8'))
    db = sqlite3.connect(os.getenv('HUDY_DB', 'monitor.sqlite3'))
    init_db(db)
    if args.report:
        for row in db.execute('''SELECT p.name, o.price, o.discount, o.checked_at, p.url
              FROM observations o JOIN products p ON p.url=o.url
              WHERE o.id IN (SELECT MAX(id) FROM observations GROUP BY url)
              ORDER BY o.checked_at DESC LIMIT 100'''):
            print(' | '.join(str(x or '-') for x in row))
        return
    token = os.getenv('TELEGRAM_BOT_TOKEN', '')
    chat_id = os.getenv('TELEGRAM_CHAT_ID', '')
    if not args.dry_run and (not token or not chat_id):
        raise SystemExit('Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID, or use --dry-run')
    fetcher = Fetcher(config['site'], config.get('request_delay_seconds', 3),
                      config.get('request_timeout_seconds', 20), config.get('max_retries', 2))
    count = 0
    for url, html in discover(fetcher, config['seed_urls'],
                              config.get('max_pages_per_run', 35), config.get('max_sitemap_urls', 150)):
        for p in parse_products(html, url):
            if not matching_brand(p, config['brands']):
                continue
            size = size_match(p, config)
            # A listed size is not proof that this variant is in stock.
            stock_verified = p.get('stock_verified') is True
            new_low = store(db, p)
            count += 1
            discount = discount_percent(p)
            print(p['name'], p['price'], 'EUR', 'discount=', discount, 'size=', size, 'stock_verified=', stock_verified, 'product_url=', p['url'])
            # Safety: unknown sizes or missing original price do not qualify for sale alerts.
            if size is not True or not stock_verified:
                continue
            is_sale = discount is not None and discount >= config['min_discount_percent']
            if not is_sale and not (new_low and config.get('alert_on_new_low', True)):
                continue
            entries = config.get('competitors', {}).get(url, [])
            competitors = compare_exact(p, entries) if entries else []
            comparison = '\n'.join(f"{c['shop']}: {c['price']} EUR ({c['size']}) {c['url']}" for c in competitors)
            kind = 'discount' if is_sale else 'new_low'
            message = (f"HUDY: {p['name']}\nCena: {p['price']} EUR\n"
                       f"Zlava: {discount if discount is not None else 'neoverena'} %\n"
                       f"Velkost: {', '.join(p['sizes'])}\n{url}")
            if new_low:
                message += '\nNove historicke minimum v lokalnej databaze!'
            if comparison:
                message += '\nOverene konkurencne URL (cena bez dopravy):\n' + comparison
            if args.dry_run:
                print('DRY RUN ALERT:', message)
            else:
                # Only mark as sent AFTER Telegram accepts it.
                if not db.execute('SELECT 1 FROM alerts WHERE url=? AND alert_type=? AND price=?',
                                  (url, kind, str(p['price']))).fetchone():
                    send_telegram(token, chat_id, message)
                    mark_alert(db, url, kind, p['price'])
    print('Processed product observations:', count)
    if count == 0:
        print('WARNING: no matching structured product data found. Check robots, seed URLs, JSON-LD and site layout.')
    db.close()


if __name__ == '__main__':
    main()
