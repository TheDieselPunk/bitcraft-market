"""
GET /api/results?player_id=<id>&regions=12,13&min_price=1&crafting=true

Fetches buy orders for all items the player can gather/craft.
Uses pre-built recipes.json for filtering; fetches live market + order data.
"""

import json
import sys
import os
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, os.path.dirname(__file__))
from _lib import (
    api_get, get_toolbelt, load_recipes_cache,
    classify_items, cors_headers,
)

MAX_WORKERS = 5  # parallel order fetches — stay within bitjita rate limits


def fetch_orders(item_id, region_ids):
    """Fetch buy orders for one item, filtered to requested regions."""
    try:
        data = api_get(f'/api/market/item/{item_id}')
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

        # Parse region IDs (comma-separated ints; empty = all regions)
        region_ids = set()
        if regions_raw:
            try:
                region_ids = {int(r) for r in regions_raw.split(',') if r.strip()}
            except ValueError:
                self._send(400, {'error': 'regions must be comma-separated integers'})
                return

        try:
            # 1. Fetch toolbelt and market items in parallel
            with ThreadPoolExecutor(max_workers=2) as ex:
                tools_future  = ex.submit(get_toolbelt, player_id)
                market_future = ex.submit(
                    api_get, '/api/market', {'hasBuyOrders': 'true', 'limit': 1000}
                )
                tools        = tools_future.result()
                market_data  = market_future.result()

            if not tools:
                self._send(200, {'error': 'No tools found in player toolbelt', 'items': []})
                return

            market_items = market_data.get('data', {}).get('items', [])
            market_ids   = {str(item['id']) for item in market_items}

            # 2. Load recipe cache — keep market items, intermediates (loot chains),
            #    and ingredients (raw mats needed for crafting ingredient checks)
            all_recipes = {
                iid: r for iid, r in load_recipes_cache().items()
                if iid in market_ids or r.get('intermediate') or r.get('ingredient')
            }

            # 3. Classify obtainable items
            extractable, craftable, source_map = classify_items(all_recipes, tools, include_crafting=crafting)
            obtainable = extractable | craftable

            # 4. Fetch buy orders in parallel
            order_results = {}
            with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
                futures = {
                    ex.submit(fetch_orders, iid, region_ids): iid
                    for iid in obtainable
                }
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
                source  = source_map.get(iid, 'craft')
                score   = order['highest_buy'] * order['total_qty']
                items.append({
                    'id':          iid,
                    'name':        recipes.get('name', iid),
                    'tier':        recipes.get('tier', -1),
                    'tag':         recipes.get('tag', ''),
                    'source':      source,
                    'highest_buy': order['highest_buy'],
                    'total_qty':   order['total_qty'],
                    'score':       score,
                })

            items.sort(key=lambda x: x['score'], reverse=True)

            # Debug mode: append unobtainable items (no order fetch — use bulk count only)
            if debug:
                market_by_id = {str(m['id']): m for m in market_items}
                for iid, recipes in all_recipes.items():
                    if iid in obtainable:
                        continue
                    m = market_by_id.get(iid, {})
                    items.append({
                        'id':          iid,
                        'name':        recipes.get('name', iid),
                        'tier':        recipes.get('tier', -1),
                        'tag':         recipes.get('tag', ''),
                        'source':      'none',
                        'highest_buy': None,
                        'total_qty':   m.get('buyOrders', None),
                        'score':       0,
                    })

            self._send(200, {
                'items':       items,
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
