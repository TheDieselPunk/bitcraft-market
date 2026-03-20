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
RATE_LIMIT = 230  # req/min — comfortably under the 250 cap
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

    # Fetch only items with active buy orders (455 vs 1388 total)
    print('Fetching market item list…')
    data  = api_get('/api/market', {'hasBuyOrders': 'true', 'limit': 1000})
    items = data.get('data', {}).get('items', [])
    print(f'  {len(items)} items with buy orders found.')

    updated = dict(existing)
    fetched = 0
    errors  = 0

    for i, item in enumerate(items):
        item_id = str(item['id'])

        # Skip if already cached with all required fields
        required = {'extraction', 'crafting', 'using', 'itemListPossibilities'}
        if item_id in existing and required.issubset(existing[item_id]):
            continue

        try:
            d = api_get(f'/api/items/{item_id}')
            updated[item_id] = {
                'name':                 d['item']['name'],
                'tier':                 d['item']['tier'],
                'tag':                  d['item'].get('tag', ''),
                'extraction':           d.get('extractionRecipes', []),
                'crafting':             d.get('craftingRecipes', []),
                'using':                d.get('recipesUsingItem', []),
                'itemListPossibilities': d.get('itemListPossibilities', []),
            }
            fetched += 1
        except Exception as e:
            print(f'  Error fetching {item_id}: {e}')
            errors += 1

        # Save incrementally every 25 items so cancellations don't lose progress
        if (i + 1) % 25 == 0:
            OUT_FILE.write_text(json.dumps(updated))
            print(f'  {i + 1}/{len(items)} processed ({fetched} fetched, {errors} errors)… [saved]')

    # Second pass: fetch intermediate items (e.g. "Briny Argus Products") that are
    # produced by recipesUsingItem recipes but are not market items themselves.
    # These may have itemListPossibilities that produce market items (e.g. Basic Fish Oil).
    intermediate_ids = set()
    for item_data in updated.values():
        if item_data.get('intermediate'):
            continue
        for recipe in item_data.get('using', []):
            for output in recipe.get('craftedItemStacks', []):
                out_id = str(output['item_id'])
                if out_id not in updated:
                    intermediate_ids.add(out_id)

    print(f'\nFetching {len(intermediate_ids)} intermediate items (Products boxes etc.)…')
    for int_id in intermediate_ids:
        try:
            d = api_get(f'/api/items/{int_id}')
            updated[int_id] = {
                'name':                  d['item']['name'],
                'tier':                  d['item']['tier'],
                'tag':                   d['item'].get('tag', ''),
                'intermediate':          True,
                'itemListPossibilities': d.get('itemListPossibilities', []),
            }
        except Exception as e:
            print(f'  Error fetching intermediate {int_id}: {e}')

    # Third pass: fetch ingredient items used in crafting recipes for market items.
    # These are items like "Emarium Ore Chunk" — extractable by the player but
    # not sold on the market — whose absence breaks the can_craft() check.
    ingredient_ids = set()
    for item_data in updated.values():
        if item_data.get('intermediate') or item_data.get('ingredient'):
            continue
        for recipe in item_data.get('crafting', []):
            if 'unpack' in recipe.get('name', '').lower():
                continue
            for ing in recipe.get('consumedItemStacks', []):
                if ing.get('item_type') == 'item':
                    ing_id = str(ing['item_id'])
                    if ing_id not in updated:
                        ingredient_ids.add(ing_id)

    print(f'\nFetching {len(ingredient_ids)} ingredient items (raw mats not on market)…')
    for ing_id in ingredient_ids:
        try:
            d = api_get(f'/api/items/{ing_id}')
            updated[ing_id] = {
                'name':       d['item']['name'],
                'tier':       d['item']['tier'],
                'tag':        d['item'].get('tag', ''),
                'ingredient': True,
                'extraction': d.get('extractionRecipes', []),
            }
        except Exception as e:
            print(f'  Error fetching ingredient {ing_id}: {e}')

    OUT_FILE.write_text(json.dumps(updated))
    print(f'\nDone. {fetched} fetched, {errors} errors. Total cached: {len(updated)} '
          f'({len(intermediate_ids)} intermediates, {len(ingredient_ids)} ingredients).')
    print(f'Written to {OUT_FILE}')


if __name__ == '__main__':
    main()
