#!/usr/bin/env python3
"""
build_recipes.py — Recipe cache builder for GitHub Actions.

Fetches all items that currently have market orders and caches their
extraction, crafting, and recipesUsingItem data into data/recipes.json.

Run from the web/ directory:
    python scripts/build_recipes.py
"""

import json
import time
import urllib.request
import urllib.parse
from pathlib import Path

API_BASE = 'https://bitjita.com'
HEADERS  = {'User-Agent': 'BitJita (Billard)', 'Accept': 'application/json'}
OUT_FILE = Path(__file__).parent.parent / 'data' / 'recipes.json'
RATE_LIMIT = 180  # req/min — conservative to avoid hitting the 250 cap
_last = 0.0


def api_get(path, params=None):
    global _last
    gap = 60.0 / RATE_LIMIT
    elapsed = time.monotonic() - _last
    if elapsed < gap:
        time.sleep(gap - elapsed)
    url = f'{API_BASE}{path}'
    if params:
        url += '?' + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=15) as r:
        data = json.loads(r.read())
    _last = time.monotonic()
    return data


def main():
    OUT_FILE.parent.mkdir(parents=True, exist_ok=True)

    # Load existing cache so we only re-fetch changed/new items
    existing = {}
    if OUT_FILE.exists():
        try:
            existing = json.loads(OUT_FILE.read_text())
            print(f'Loaded {len(existing)} existing cached recipes.')
        except Exception:
            pass

    # Fetch all items with any market orders
    print('Fetching market item list…')
    data  = api_get('/api/market', {'hasOrders': 'true', 'limit': 1000})
    items = data.get('data', {}).get('items', [])
    print(f'  {len(items)} items found.')

    updated = dict(existing)
    fetched = 0
    errors  = 0

    for i, item in enumerate(items):
        item_id = str(item['id'])

        # Skip if already cached (recipes don't change frequently)
        if item_id in existing and 'using' in existing[item_id]:
            continue

        try:
            d = api_get(f'/api/items/{item_id}')
            updated[item_id] = {
                'name':       d['item']['name'],
                'tier':       d['item']['tier'],
                'tag':        d['item'].get('tag', ''),
                'extraction': d.get('extractionRecipes', []),
                'crafting':   d.get('craftingRecipes', []),
                'using':      d.get('recipesUsingItem', []),
            }
            fetched += 1
        except Exception as e:
            print(f'  Error fetching {item_id}: {e}')
            errors += 1

        if (i + 1) % 50 == 0:
            print(f'  {i + 1}/{len(items)} processed ({fetched} fetched, {errors} errors)…')

    OUT_FILE.write_text(json.dumps(updated))
    print(f'\nDone. {fetched} fetched, {errors} errors. Total cached: {len(updated)}.')
    print(f'Written to {OUT_FILE}')


if __name__ == '__main__':
    main()
