#!/usr/bin/env python3
"""
build_recipes.py — Recipe cache builder for GitHub Actions.

Fetches all items that currently have market orders and caches their
extraction, crafting, recipesUsingItem, and itemListPossibilities data
into data/recipes.json.

Also fetches:
  - Intermediate items (e.g. "Briny Argus Products") produced by
    recipesUsingItem recipes — needed for loot-table chains.
  - Ingredient items (e.g. "Emarium Ore Chunk") used in crafting recipes
    but not sold on the market — needed so can_craft() works correctly.

Uses a thread-safe token bucket rate limiter + ThreadPoolExecutor to run
as many concurrent requests as possible without exceeding the 250 req/min cap.

Run from the web/ directory:
    python scripts/build_recipes.py
"""

import json
import time
import threading
import urllib.request
import urllib.parse
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

API_BASE   = 'https://bitjita.com'
HEADERS    = {'User-Agent': 'BitJita (Billard)', 'Accept': 'application/json'}
OUT_FILE        = Path(__file__).parent.parent / 'data' / 'recipes.json'
TOOL_PRICES_FILE = Path(__file__).parent.parent / 'data' / 'tool_prices.json'
MAX_WORKERS = 10          # concurrent HTTP connections
RATE_LIMIT  = 240         # req/min — comfortably under the 250 cap
BURST       = 15          # token bucket burst size

# Ingredient IDs for every tool upgrade step (T1 craft through T7→T8).
# These are always refreshed so tool_prices.json stays current.
TOOL_INGREDIENT_IDS = [
    # T1 initial craft
    '1050001', '1090004', '1020003', '1070004',
    # T1→T2  (Pyrelite)
    '2050001', '2090004', '2020003', '2070004',
    # T2→T3  (Emarium)
    '3050001', '3090004', '3020003', '3070004',
    # T3→T4  (Elenvar)
    '4050001', '4090004', '4020003', '4070004',
    # T4→T5  (Luminite)
    '5050001', '5090004', '5020003', '5070004',
    # T5→T6  (Rathium)
    '6050001', '6090004', '6020003', '6070004',
    # T6→T7  (Aurumite)
    '1899017490', '625147590', '1639308227', '806992520',
    # T7→T8  (Celestium)
    '1464752960', '1224328894', '28056473', '1743778001',
]


# ── Rate limiter ────────────────────────────────────────────────────────────

class TokenBucket:
    """Thread-safe token bucket. Blocks callers until a token is available."""

    def __init__(self, rate_per_min: int, burst: int = BURST):
        self.rate     = rate_per_min / 60.0   # tokens / second
        self.capacity = float(burst)
        self.tokens   = float(burst)
        self.last     = time.monotonic()
        self._lock    = threading.Lock()

    def acquire(self):
        with self._lock:
            now = time.monotonic()
            self.tokens = min(self.capacity,
                              self.tokens + (now - self.last) * self.rate)
            self.last = now
            if self.tokens < 1:
                wait = (1.0 - self.tokens) / self.rate
                time.sleep(wait)
                self.tokens = 0.0
            else:
                self.tokens -= 1.0


_bucket = TokenBucket(RATE_LIMIT)


# ── HTTP helper ─────────────────────────────────────────────────────────────

def api_get(path, params=None):
    _bucket.acquire()
    url = f'{API_BASE}{path}'
    if params:
        url += '?' + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read())


# ── Per-item fetch functions ────────────────────────────────────────────────

def fetch_market_item(item_id: str) -> dict:
    d = api_get(f'/api/items/{item_id}')
    return {
        'name':                  d['item']['name'],
        'tier':                  d['item']['tier'],
        'tag':                   d['item'].get('tag', ''),
        'extraction':            d.get('extractionRecipes', []),
        'crafting':              d.get('craftingRecipes', []),
        'using':                 d.get('recipesUsingItem', []),
        'itemListPossibilities': d.get('itemListPossibilities', []),
    }


def fetch_intermediate_item(item_id: str) -> dict:
    d = api_get(f'/api/items/{item_id}')
    return {
        'name':                  d['item']['name'],
        'tier':                  d['item']['tier'],
        'tag':                   d['item'].get('tag', ''),
        'intermediate':          True,
        'itemListPossibilities': d.get('itemListPossibilities', []),
    }


def fetch_ingredient_item(item_id: str) -> dict:
    d = api_get(f'/api/items/{item_id}')
    return {
        'name':       d['item']['name'],
        'tier':       d['item']['tier'],
        'tag':        d['item'].get('tag', ''),
        'ingredient': True,
        'extraction': d.get('extractionRecipes', []),
    }


# ── Concurrent batch fetcher ────────────────────────────────────────────────

def fetch_batch(ids: list, fetch_fn, label: str) -> dict:
    """
    Fetch a list of item IDs concurrently using ThreadPoolExecutor.
    Returns {item_id: data_dict}.
    """
    if not ids:
        return {}

    results = {}
    errors  = 0
    done    = 0
    total   = len(ids)

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futures = {ex.submit(fetch_fn, iid): iid for iid in ids}
        for future in as_completed(futures):
            iid   = futures[future]
            done += 1
            try:
                results[iid] = future.result()
            except Exception as e:
                errors += 1
                print(f'  ✗ {label} {iid}: {e}')
            if done % 50 == 0 or done == total:
                print(f'  {label}: {done}/{total} ({errors} errors)')

    return results


# ── Main ────────────────────────────────────────────────────────────────────

def main():
    OUT_FILE.parent.mkdir(parents=True, exist_ok=True)

    # Load existing cache so we only re-fetch stale / new items
    existing: dict = {}
    if OUT_FILE.exists():
        try:
            existing = json.loads(OUT_FILE.read_text())
            print(f'Loaded {len(existing)} existing cached entries.')
        except Exception:
            pass

    # ── Pass 1: fetch market item list ──────────────────────────────────────
    print('\nFetching market item list…')
    data  = api_get('/api/market', {'hasBuyOrders': 'true', 'limit': 1000})
    items = data.get('data', {}).get('items', [])
    print(f'  {len(items)} items with active buy orders.')

    # Items are stale if they're missing any required field
    required  = {'extraction', 'crafting', 'using', 'itemListPossibilities'}
    to_fetch  = [
        str(item['id']) for item in items
        if str(item['id']) not in existing
        or not required.issubset(existing[str(item['id'])])
    ]
    cached_ok = len(items) - len(to_fetch)
    print(f'  {cached_ok} already cached, {len(to_fetch)} need fetching.')

    updated = dict(existing)

    # ── Pass 2: fetch stale/new market items concurrently ───────────────────
    if to_fetch:
        print(f'\nPass 2 — market items ({len(to_fetch)} items, '
              f'up to {MAX_WORKERS} concurrent)…')
        t0 = time.monotonic()
        results = fetch_batch(to_fetch, fetch_market_item, 'market')
        updated.update(results)
        OUT_FILE.write_text(json.dumps(updated))
        elapsed = time.monotonic() - t0
        print(f'  Done in {elapsed:.1f}s '
              f'({len(to_fetch)/elapsed*60:.0f} req/min effective).')

    # ── Pass 3: intermediate items (Products / loot boxes) ──────────────────
    intermediate_ids = {
        str(out['item_id'])
        for r in updated.values()
        if not r.get('intermediate') and not r.get('ingredient')
        for recipe in r.get('using', [])
        for out in recipe.get('craftedItemStacks', [])
        if str(out['item_id']) not in updated
    }

    if intermediate_ids:
        print(f'\nPass 3 — intermediate items ({len(intermediate_ids)})…')
        t0 = time.monotonic()
        results = fetch_batch(list(intermediate_ids),
                              fetch_intermediate_item, 'intermediate')
        updated.update(results)
        OUT_FILE.write_text(json.dumps(updated))
        print(f'  Done in {time.monotonic()-t0:.1f}s.')

    # ── Pass 4: ingredient items (raw mats used in crafting or extraction) ───
    # Collect items consumed in crafting recipes
    ingredient_ids = {
        str(ing['item_id'])
        for r in updated.values()
        if not r.get('intermediate') and not r.get('ingredient')
        for recipe in r.get('crafting', [])
        if 'unpack' not in recipe.get('name', '').lower()
        for ing in recipe.get('consumedItemStacks', [])
        if ing.get('item_type') == 'item'
        and str(ing['item_id']) not in updated
    }
    # Also collect items consumed in extraction recipes (e.g. bait)
    ingredient_ids |= {
        str(ing['item_id'])
        for r in updated.values()
        if not r.get('intermediate') and not r.get('ingredient')
        for recipe in r.get('extraction', [])
        for ing in recipe.get('consumedItemStacks', [])
        if ing.get('item_type') == 'item'
        and str(ing['item_id']) not in updated
    }

    if ingredient_ids:
        print(f'\nPass 4 — ingredient items ({len(ingredient_ids)})…')
        t0 = time.monotonic()
        results = fetch_batch(list(ingredient_ids),
                              fetch_ingredient_item, 'ingredient')
        updated.update(results)

    # ── Pass 5: ingredients of extraction-consumed items (e.g. bait crafting) ─
    # One more pass to pull in items needed to craft bait / other extraction inputs.
    bait_ingredient_ids = {
        str(ing['item_id'])
        for iid, r in updated.items()
        if r.get('ingredient')
        for recipe in r.get('crafting', [])
        if 'unpack' not in recipe.get('name', '').lower()
        for ing in recipe.get('consumedItemStacks', [])
        if ing.get('item_type') == 'item'
        and str(ing['item_id']) not in updated
    }

    if bait_ingredient_ids:
        print(f'\nPass 5 — bait/extraction ingredient items ({len(bait_ingredient_ids)})…')
        t0 = time.monotonic()
        results = fetch_batch(list(bait_ingredient_ids),
                              fetch_ingredient_item, 'bait-ingredient')
        updated.update(results)

    # ── Summary + metadata ───────────────────────────────────────────────────
    n_market = sum(1 for v in updated.values()
                   if not v.get('intermediate') and not v.get('ingredient'))
    n_inter  = sum(1 for v in updated.values() if v.get('intermediate'))
    n_ing    = sum(1 for v in updated.values() if v.get('ingredient'))

    updated['__meta__'] = {
        'built_at':     time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
        'market':       n_market,
        'intermediates': n_inter,
        'ingredients':  n_ing,
    }

    OUT_FILE.write_text(json.dumps(updated))

    print(f'\n{"─"*50}')
    print(f'Total entries : {len(updated) - 1}')   # exclude __meta__
    print(f'  Market items : {n_market}')
    print(f'  Intermediates: {n_inter}')
    print(f'  Ingredients  : {n_ing}')
    print(f'  Built at     : {updated["__meta__"]["built_at"]}')
    print(f'Written to {OUT_FILE}')

    # ── Tool prices pass (always fresh, not cached) ──────────────────────────
    print(f'\nTool prices — fetching {len(TOOL_INGREDIENT_IDS)} ingredient items…')
    t0 = time.monotonic()
    tool_prices = {}
    errors = 0
    for iid in TOOL_INGREDIENT_IDS:
        try:
            _bucket.acquire()
            d = api_get(f'/api/items/{iid}')
            stats = d.get('marketStats') or {}
            tool_prices[iid] = {
                'name':            d['item']['name'],
                'medianSellPrice': stats.get('medianSellPrice'),
                'lowestSellPrice': stats.get('lowestSellPrice'),
                'medianBuyPrice':  stats.get('medianBuyPrice'),
                'totalSellOrders': stats.get('totalSellOrders'),
                'totalBuyOrders':  stats.get('totalBuyOrders'),
            }
        except Exception as e:
            errors += 1
            print(f'  ✗ {iid}: {e}')

    tool_prices['__meta__'] = {'built_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}
    TOOL_PRICES_FILE.write_text(json.dumps(tool_prices))
    print(f'  Done in {time.monotonic()-t0:.1f}s ({errors} errors). Written to {TOOL_PRICES_FILE}')


if __name__ == '__main__':
    main()
