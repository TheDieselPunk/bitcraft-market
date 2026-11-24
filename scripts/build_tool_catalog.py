#!/usr/bin/env python3
"""
build_tool_catalog.py — catalog of every tiered tool, for the Tool Upgrades page.

Reads tool_desc / item_desc / tool_type_desc from the BitCraftToolBox static tables and
writes data/tool_catalog.json:

    {
      "types": {"<tool_type>": {"name": "Pickaxe", "skill_id": 5}},
      "tools": {"<item_id>": {"name", "type", "tier", "rarity", "power", "level", "rollable",
                              "price": {"median", "lowest", "listings"}?}},
      "__meta__": {...}
    }

Also caches each listed tool's sell prices from BitJita, so the page can credit the leftover
tools from rolling without fetching every listing live.

Only T1-T10 tools of the regular kinds are kept (each tool type, tier and rarity is its own
item). Every tool type is crafted and upgraded from the same ingredients at each tier, which is
what lets the page price any of them with the Tool Rolling model.

Run from the project root:
    python scripts/build_tool_catalog.py
"""

import json
import statistics
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

STATIC_BASE = (
    'https://raw.githubusercontent.com/BitCraftToolBox/'
    'BitCraft_GameData/cereal/cs/static'
)
API_BASE = 'https://bitjita.com'
API_HEADERS = {'User-Agent': 'BitJita (Billard)', 'Accept': 'application/json'}
OUT_FILE = Path(__file__).parent.parent / 'data' / 'tool_catalog.json'

# Tool types with a tier ladder (Axe ... Quill). The pot, Mallet, Hexite Tuner and Combat
# Weapon have none.
TIERED_TYPES = set(range(1, 14)) - {11}
RARITIES = ['Common', 'Uncommon', 'Rare', 'Epic', 'Legendary', 'Mythic']


def fetch_static(name):
    req = urllib.request.Request(f'{STATIC_BASE}/{name}', headers={'User-Agent': 'bitcraft-effort-build'})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())


def api_get(path, params=None):
    url = f'{API_BASE}{path}'
    if params:
        url += '?' + urllib.parse.urlencode(params)
    with urllib.request.urlopen(urllib.request.Request(url, headers=API_HEADERS), timeout=20) as r:
        return json.loads(r.read())


def fetch_prices(tool_ids):
    """{item_id: {median, lowest, listings}} for every tool that has sell orders right now."""
    listed = [it['id'] for it in api_get('/api/market', {'hasSellOrders': 'true', 'limit': 5000})['data']['items']
              if it['id'] in tool_ids]

    def one(iid):
        try:
            orders = api_get(f'/api/market/item/{iid}').get('sellOrders', [])
            prices = sorted(float(o['priceThreshold']) for o in orders if float(o.get('priceThreshold') or 0) > 0)
            return iid, prices
        except Exception as e:
            print(f'  failed {iid}: {e}')
            return iid, []

    out = {}
    with ThreadPoolExecutor(max_workers=6) as ex:
        for iid, prices in ex.map(one, listed):
            if prices:
                out[iid] = {'median': statistics.median(prices), 'lowest': prices[0], 'listings': len(prices)}
    return out


def main():
    tool_rows = fetch_static('tool_desc.json')
    items = {row['id']: row for row in fetch_static('item_desc.json')}
    type_rows = fetch_static('tool_type_desc.json')

    types = {
        str(t['id']): {'name': t['name'], 'skill_id': t['skill_id']}
        for t in type_rows if t['id'] in TIERED_TYPES
    }

    tools = {}
    for t in tool_rows:
        item = items.get(t['item_id'])
        if not item or t['tool_type'] not in TIERED_TYPES:
            continue
        tier, rarity = item.get('tier'), item.get('rarity')
        if not isinstance(tier, int) or not 1 <= tier <= 10 or rarity not in RARITIES:
            continue
        if t['power'] >= 10000:  # admin/debug tools
            continue
        tools[str(t['item_id'])] = {
            'name':   item['name'],
            'type':   t['tool_type'],
            'tier':   tier,
            'rarity': rarity,
            'power':  t['power'],
            'level':  t['level'],
            # Flint tools are starter tools, not part of the craft-and-upgrade ladder
            'rollable': not (tier == 1 and t['level'] == 1),
        }

    try:
        prices = fetch_prices(set(tools))
    except Exception as e:  # keep the last prices if BitJita is down
        print(f'Price fetch failed ({e}); keeping previous prices')
        try:
            old = json.loads(OUT_FILE.read_text(encoding='utf-8'))['tools']
            prices = {k: v['price'] for k, v in old.items() if v.get('price')}
        except Exception:
            prices = {}
    for iid, p in prices.items():
        if iid in tools:
            tools[iid]['price'] = p

    OUT_FILE.write_text(json.dumps({
        'types': types,
        'tools': tools,
        '__meta__': {
            'built_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
            'count': len(tools),
            'priced': len(prices),
        },
    }), encoding='utf-8')
    print(f'{len(tools)} tools ({len(prices)} with listings) across {len(types)} types written to {OUT_FILE}')


if __name__ == '__main__':
    main()
