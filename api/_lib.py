"""
Shared logic for Bitcraft Market Advisor API functions.
"""

import json
import os
import urllib.request
import urllib.parse

API_BASE = 'https://bitjita.com'
HEADERS = {
    'User-Agent': 'BitJita (Billard)',
    'Accept': 'application/json',
}

# Path to pre-built recipe cache (populated by GitHub Actions)
RECIPES_PATH = os.path.join(os.path.dirname(__file__), '..', 'data', 'recipes.json')


def api_get(path, params=None):
    url = f'{API_BASE}{path}'
    if params:
        url += '?' + urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


def load_recipes_cache():
    """Load the pre-built recipe cache from disk. Returns empty dict if not found."""
    try:
        with open(RECIPES_PATH) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def get_player_id(username):
    """Look up a player by username and return their entity ID, or None if not found."""
    data = api_get('/api/players', {'q': username, 'limit': 5})
    players = data.get('players', [])
    # Exact match first, then partial
    for p in players:
        if p.get('username', '').lower() == username.lower():
            return str(p['entityId'])
    if players:
        return str(players[0]['entityId'])
    return None


def get_toolbelt(player_id):
    """
    Fetch player inventories and extract tools from the Toolbelt.
    Returns {tool_type_int: {level, power, name, tier}}.
    """
    data = api_get(f'/api/players/{player_id}/inventories')
    items_lookup = data.get('items', {})
    tools = {}

    for bag in data.get('inventories', []):
        if bag.get('inventoryName', '').lower() != 'toolbelt':
            continue
        for pocket in bag.get('pockets', []):
            contents = pocket.get('contents')
            if not contents:
                continue
            item_id = str(contents['itemId'])
            info = items_lookup.get(item_id, {})
            tool_type = info.get('toolType')
            tool_level = info.get('toolLevel')
            tool_power = info.get('toolPower') or 0
            if tool_type is None or tool_level is None:
                continue
            if tool_type not in tools or tool_level > tools[tool_type]['level']:
                tools[tool_type] = {
                    'level': tool_level,
                    'power': tool_power,
                    'name': info.get('name', f'item_{item_id}'),
                    'tier': info.get('tier'),
                }
    return tools


def can_extract(recipes, tools):
    for recipe in recipes.get('extraction', []):
        reqs = recipe.get('toolRequirements', [])
        if not reqs:
            return True
        if all(
            tools.get(r['tool_type'], {}).get('level', 0) >= r['level'] and
            tools.get(r['tool_type'], {}).get('power', 0) >= r['power']
            for r in reqs
        ):
            return True
    return False


def is_unpack_recipe(recipe):
    return 'unpack' in recipe.get('name', '').lower()


def can_craft(recipes, obtainable):
    for recipe in recipes.get('crafting', []):
        if is_unpack_recipe(recipe):
            continue
        item_ings = [i for i in recipe.get('consumedItemStacks', []) if i.get('item_type') == 'item']
        if not item_ings:
            continue
        if all(str(i['item_id']) in obtainable for i in item_ings):
            return True
    return False


def find_craftable_reverse(all_recipes, obtainable):
    """
    Find market items craftable via 'recipesUsingItem' on obtainable items.
    Iterates until stable so crafting chains are resolved.
    """
    market_item_ids = set(all_recipes.keys())
    craftable = set()

    changed = True
    while changed:
        changed = False
        current = obtainable | craftable
        for item_id, recipes in all_recipes.items():
            if item_id not in current:
                continue
            for recipe in recipes.get('using', []):
                if is_unpack_recipe(recipe):
                    continue
                item_ings = [
                    i for i in recipe.get('consumedItemStacks', [])
                    if i.get('item_type') == 'item'
                ]
                if not item_ings:
                    continue
                if not all(str(i['item_id']) in current for i in item_ings):
                    continue
                for output in recipe.get('craftedItemStacks', []):
                    out_id = str(output['item_id'])
                    if out_id in market_item_ids and out_id not in current:
                        craftable.add(out_id)
                        changed = True
    return craftable


def classify_items(all_recipes, tools, include_crafting=True):
    """
    Given a recipe dict and tools, return (extractable, craftable) sets of item IDs.
    """
    extractable = {iid for iid, r in all_recipes.items() if can_extract(r, tools)}
    craftable = set()
    if include_crafting:
        for iid, r in all_recipes.items():
            if iid not in extractable and can_craft(r, extractable):
                craftable.add(iid)
        craftable |= find_craftable_reverse(all_recipes, extractable)
    return extractable, craftable


def cors_headers():
    return {
        'Access-Control-Allow-Origin': '*',
        'Access-Control-Allow-Methods': 'GET, OPTIONS',
        'Access-Control-Allow-Headers': 'Content-Type',
        'Content-Type': 'application/json',
    }
