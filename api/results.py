"""
GET /api/results?player_id=<id>&regions=12,13&min_price=1&crafting=true

Fetches buy orders for all items the player can gather/craft.
Uses pre-built recipes.json for filtering; fetches live market + order data.

Market fetch strategy:
  - No region filter: uses stats baked into the market list (0 extra requests).
  - Region filter: fetches per-item orders concurrently with a token bucket
    rate limiter (230 req/min) and up to 20 parallel workers.
"""

import json
import sys
import os
import threading
import time
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, os.path.dirname(__file__))
from _lib import (
    api_get, get_toolbelt, load_recipes_cache,
    classify_items, cors_headers,
)

MAX_WORKERS = 20   # parallel order fetches for the region-filtered path
RATE_LIMIT  = 230  # req/min — leaves headroom for the two initial requests


# ── Token bucket (module-level so it persists across warm lambda instances) ─

class _TokenBucket:
    def __init__(self, rate_per_min: int, burst: int = 20):
        self.rate     = rate_per_min / 60.0
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
                time.sleep((1.0 - self.tokens) / self.rate)
                self.tokens = 0.0
            else:
                self.tokens -= 1.0


_bucket = _TokenBucket(RATE_LIMIT)


# ── Helpers ─────────────────────────────────────────────────────────────────

def _stats_from_market_item(m: dict):
    """Extract highest buy price and total buy qty from a market list item."""
    stats       = m.get('marketStats') or {}
    highest_buy = stats.get('highestBuyPrice') or m.get('highestBuyPrice') or 0
    total_qty   = stats.get('totalBuyQuantity') or m.get('totalBuyQuantity') or 0
    return int(highest_buy), int(total_qty)


def fetch_orders(item_id, region_ids):
    """Fetch per-item buy orders (rate-limited). Used only for region filtering."""
    try:
        _bucket.acquire()
        data   = api_get(f'/api/market/item/{item_id}')
        orders = data.get('buyOrders', [])
        if region_ids:
            orders = [o for o in orders if o.get('regionId') in region_ids]
        if not orders:
            return item_id, None
        prices = [int(o['priceThreshold']) for o in orders]
        qtys   = [int(o['quantity'])        for o in orders]
        return item_id, {
            'highest_buy': max(prices),
            'total_qty':   sum(qtys),
        }
    except Exception:
        return item_id, None


# ── Handler ──────────────────────────────────────────────────────────────────

class handler(BaseHTTPRequestHandler):
    def do_OPTIONS(self):
        self._send(200, {})

    def do_GET(self):
        params = parse_qs(urlparse(self.path).query)

        player_id   = params.get('player_id',  [''])[0].strip()
        regions_raw = params.get('regions',     [''])[0].strip()
        min_price   = int(params.get('min_price', ['1'])[0] or 1)
        crafting    = params.get('crafting', ['true'])[0].lower() != 'false'
        debug       = params.get('debug',    ['false'])[0].lower() == 'true'

        if not player_id:
            self._send(400, {'error': 'player_id is required'})
            return

        region_ids = set()
        if regions_raw:
            try:
                region_ids = {int(r) for r in regions_raw.split(',') if r.strip()}
            except ValueError:
                self._send(400, {'error': 'regions must be comma-separated integers'})
                return

        try:
            # 1. Fetch toolbelt + market list in parallel (2 requests total)
            with ThreadPoolExecutor(max_workers=2) as ex:
                tools_f  = ex.submit(get_toolbelt, player_id)
                market_f = ex.submit(
                    api_get, '/api/market', {'hasBuyOrders': 'true', 'limit': 1000}
                )
                tools       = tools_f.result()
                market_data = market_f.result()

            if not tools:
                self._send(200, {'error': 'No tools found in player toolbelt', 'items': []})
                return

            market_items = market_data.get('data', {}).get('items', [])
            market_by_id = {str(item['id']): item for item in market_items}
            market_ids   = set(market_by_id.keys())

            # 2. Load recipe cache (market items + intermediates + ingredients)
            all_recipes = {
                iid: r for iid, r in load_recipes_cache().items()
                if iid in market_ids or r.get('intermediate') or r.get('ingredient')
            }

            # 3. Classify obtainable items
            extractable, craftable, source_map = classify_items(
                all_recipes, tools, include_crafting=crafting
            )
            obtainable = extractable | craftable

            # 4. Collect buy order stats
            order_results = {}

            if not region_ids:
                # ── Fast path: no region filter ─────────────────────────────
                # Stats are already embedded in the market list — 0 extra requests.
                needs_fetch = []
                for iid in obtainable:
                    m = market_by_id.get(iid, {})
                    highest_buy, total_qty = _stats_from_market_item(m)
                    if highest_buy:
                        order_results[iid] = {
                            'highest_buy': highest_buy,
                            'total_qty':   total_qty,
                        }
                    else:
                        needs_fetch.append(iid)   # fallback for items missing stats

                if needs_fetch:
                    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
                        futures = {ex.submit(fetch_orders, iid, set()): iid
                                   for iid in needs_fetch}
                        for future in as_completed(futures):
                            iid, data = future.result()
                            if data:
                                order_results[iid] = data
            else:
                # ── Region path: fetch per-item orders (rate-limited) ────────
                with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
                    futures = {ex.submit(fetch_orders, iid, region_ids): iid
                               for iid in obtainable}
                    for future in as_completed(futures):
                        iid, data = future.result()
                        if data:
                            order_results[iid] = data

            # 5. Build result list
            items = []
            for iid, order in order_results.items():
                if order['highest_buy'] < min_price:
                    continue
                recipes = all_recipes.get(iid, {})
                score   = order['highest_buy'] * order['total_qty']
                items.append({
                    'id':          iid,
                    'name':        recipes.get('name', iid),
                    'tier':        recipes.get('tier', -1),
                    'tag':         recipes.get('tag', ''),
                    'source':      source_map.get(iid, 'craft'),
                    'highest_buy': order['highest_buy'],
                    'total_qty':   order['total_qty'],
                    'score':       score,
                })

            items.sort(key=lambda x: x['score'], reverse=True)

            # Debug: append unobtainable items (no extra fetches)
            if debug:
                for iid, recipes in all_recipes.items():
                    if recipes.get('intermediate') or recipes.get('ingredient'):
                        continue
                    if iid in obtainable:
                        continue
                    m = market_by_id.get(iid, {})
                    highest_buy, total_qty = _stats_from_market_item(m)
                    items.append({
                        'id':          iid,
                        'name':        recipes.get('name', iid),
                        'tier':        recipes.get('tier', -1),
                        'tag':         recipes.get('tag', ''),
                        'source':      'none',
                        'highest_buy': highest_buy or None,
                        'total_qty':   total_qty or m.get('buyOrders'),
                        'score':       0,
                    })

            self._send(200, {
                'items': items,
                'stats': {
                    'total_market':   len(market_items),
                    'cached_recipes': len(all_recipes),
                    'extractable':    len(extractable),
                    'craftable':      len(craftable),
                    'with_orders':    len(items),
                    'unobtainable':   sum(1 for i in items if i['source'] == 'none'),
                    'regions':        sorted(region_ids) if region_ids else 'all',
                },
            })

        except Exception as e:
            self._send(500, {'error': str(e)})

    def _send(self, status, body):
        payload = json.dumps(body).encode()
        self.send_response(status)
        for k, v in cors_headers().items():
            self.send_header(k, v)
        self.send_header('Content-Length', str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format, *args):
        pass
