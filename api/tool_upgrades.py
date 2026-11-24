"""
GET /api/tool_upgrades?username=<name>[&all_tiers=1]

For each tool type, finds the player's best tool in their toolbelt and the market listings of
the same type with higher power. By default only tiers the player's skill allows are listed
(tier T needs level 10*T in the tool's skill; T1 is always usable); all_tiers=1 drops that filter.

Returns:
  {
    player: {id, username},
    fetched_at,
    types: [{type, name, skill_id, skill_name, skill_level, max_tier,
             current: {item_id, name, tier, rarity, power} | null,
             candidates: [{item_id, name, tier, rarity, power,
                           orders: [{price, qty, regionId, regionName, claimName, seller}]}]}]
  }

The fair-value maths runs in the page (tool_upgrades.html), from live ingredient prices.
"""

import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

sys.path.insert(0, os.path.dirname(__file__))
from _lib import api_get, get_player_id, cors_headers, SKILL_NAMES

CATALOG_PATH = os.path.join(os.path.dirname(__file__), '..', 'data', 'tool_catalog.json')
MAX_CANDIDATES = 120   # listed tools to fetch orders for, cheapest tiers first

_CATALOG = None
_LEVELS = None


def load_catalog():
    global _CATALOG
    if _CATALOG is None:
        with open(CATALOG_PATH, encoding='utf-8') as f:
            _CATALOG = json.load(f)
    return _CATALOG


def load_levels():
    global _LEVELS
    if _LEVELS is None:
        _LEVELS = sorted(api_get('/static/experience/levels.json'), key=lambda e: e['level'])
    return _LEVELS


def xp_to_level(xp, table):
    level = 1
    for entry in table:
        if entry['xp'] <= xp:
            level = entry['level']
        else:
            break
    return level


def max_tier_for(level):
    """Highest tool tier usable at a skill level: tier T needs level 10*T (T2 at 20, T5 at 50)."""
    return max(1, min(10, level // 10))


def toolbelt_item_ids(inv):
    ids = []
    for bag in inv.get('inventories', []):
        if (bag.get('inventoryName') or '').lower() != 'toolbelt':
            continue
        for pocket in bag.get('pockets', []):
            contents = pocket.get('contents')
            if contents:
                ids.append(str(contents['itemId']))
    return ids


def fetch_orders(item_id):
    try:
        data = api_get(f'/api/market/item/{item_id}')
    except Exception:
        return item_id, []
    orders = []
    for o in data.get('sellOrders', []):
        price = float(o.get('priceThreshold') or 0)
        if price <= 0:
            continue
        orders.append({
            'price':      price,
            'qty':        int(o.get('quantity') or 1),
            'regionId':   o.get('regionId'),
            'regionName': o.get('regionName', ''),
            'claimName':  o.get('claimName', ''),
            'seller':     o.get('ownerUsername', ''),
        })
    orders.sort(key=lambda o: o['price'])
    return item_id, orders


def tool_summary(item_id, t):
    return {'item_id': item_id, 'name': t['name'], 'tier': t['tier'],
            'rarity': t['rarity'], 'power': t['power']}


def build(username, all_tiers):
    catalog = load_catalog()
    tools, types = catalog['tools'], catalog['types']

    player_id = get_player_id(username)
    if not player_id:
        return 404, {'error': f'Player "{username}" not found'}

    with ThreadPoolExecutor(max_workers=4) as ex:
        f_inv = ex.submit(api_get, f'/api/players/{player_id}/inventories')
        f_player = ex.submit(api_get, f'/api/players/{player_id}')
        f_levels = ex.submit(load_levels)
        f_market = ex.submit(api_get, '/api/market', {'hasSellOrders': 'true', 'limit': 5000})
        inv, player, levels, market = f_inv.result(), f_player.result(), f_levels.result(), f_market.result()

    xp = {int(e['skill_id']): e.get('quantity', 0)
          for e in player.get('player', {}).get('experience', []) if e.get('skill_id') is not None}
    listed = {it['id'] for it in market.get('data', {}).get('items', []) if it['id'] in tools}

    # Best owned tool per type (by power)
    current = {}
    for iid in toolbelt_item_ids(inv):
        t = tools.get(iid)
        if t and (t['type'] not in current or t['power'] > tools[current[t['type']]]['power']):
            current[t['type']] = iid

    out_types, wanted = [], []
    for type_id, info in sorted(types.items(), key=lambda kv: kv[1]['name']):
        type_id = int(type_id)
        level = xp_to_level(xp.get(info['skill_id'], 0), levels)
        max_tier = 10 if all_tiers else max_tier_for(level)
        cur = current.get(type_id)
        cur_power = tools[cur]['power'] if cur else 0
        cands = [iid for iid in listed
                 if tools[iid]['type'] == type_id and tools[iid]['power'] > cur_power
                 and tools[iid]['tier'] <= max_tier]
        wanted.extend(cands)
        out_types.append({
            'type': type_id, 'name': info['name'], 'skill_id': info['skill_id'],
            'skill_name': SKILL_NAMES.get(info['skill_id'], ''), 'skill_level': level,
            'max_tier': max_tier,
            'current': tool_summary(cur, tools[cur]) if cur else None,
            'candidates': cands,
        })

    wanted.sort(key=lambda iid: (tools[iid]['tier'], tools[iid]['power']))
    truncated = len(wanted) > MAX_CANDIDATES
    wanted = wanted[:MAX_CANDIDATES]
    with ThreadPoolExecutor(max_workers=16) as ex:
        orders = dict(ex.map(fetch_orders, wanted))

    for t in out_types:
        t['candidates'] = sorted(
            ({**tool_summary(iid, tools[iid]), 'orders': orders[iid]}
             for iid in t['candidates'] if orders.get(iid)),
            key=lambda c: (c['power'], c['orders'][0]['price']))

    return 200, {
        'player': {'id': player_id, 'username': username},
        'fetched_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
        'truncated': truncated,
        'types': out_types,
    }


class handler(BaseHTTPRequestHandler):
    def do_OPTIONS(self):
        self._send(200, {})

    def do_GET(self):
        params = parse_qs(urlparse(self.path).query)
        username = params.get('username', [''])[0].strip()
        if not username:
            self._send(400, {'error': 'username parameter is required'})
            return
        try:
            status, body = build(username, params.get('all_tiers', [''])[0] == '1')
            self._send(status, body)
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
