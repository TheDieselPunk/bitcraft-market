#!/usr/bin/env python3
"""
build_recipes.py — Recipe cache builder for GitHub Actions.

Fetches the live BitJita market item list, then reads static BitCraftToolBox
desc tables for item details, extraction, crafting, recipesUsingItem, and
itemListPossibilities data into data/recipes.json.

Also fetches:
  - Intermediate items (e.g. "Briny Argus Products") produced by
    recipesUsingItem recipes — needed for loot-table chains.
  - Ingredient items (e.g. "Emarium Ore Chunk") used in crafting recipes
    but not sold on the market — needed so can_craft() works correctly.

Uses a thread-safe token bucket rate limiter for the remaining live market
requests without exceeding the 250 req/min cap.

Run from the web/ directory:
    python scripts/build_recipes.py
"""

import json
import time
import threading
import urllib.request
import urllib.parse
from pathlib import Path

API_BASE   = 'https://bitjita.com'
HEADERS    = {'User-Agent': 'BitJita (Billard)', 'Accept': 'application/json'}
STATIC_BASE = (
    'https://raw.githubusercontent.com/BitCraftToolBox/'
    'BitCraft_GameData/cereal/cs/static'
)
OUT_FILE        = Path(__file__).parent.parent / 'data' / 'recipes.json'
TOOL_PRICES_FILE = Path(__file__).parent.parent / 'data' / 'tool_prices.json'
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
    # T8→T9  (Umbracite)
    '445742898', '471802228', '1227914325', '478917',
    # T9→T10 (Astralite)
    '2069757207', '547017087', '117329467', '944952036',
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


# ── Static table helpers ────────────────────────────────────────────────────

def fetch_static_json(filename):
    url = f'{STATIC_BASE}/{filename}'
    print(f'  Fetching {filename} ...')
    req = urllib.request.Request(url, headers={'Accept': 'application/json'})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())


def to_list(data):
    return data if isinstance(data, list) else list(data.values())


def build_lookup(rows, key='id'):
    return {entry[key]: entry for entry in rows}


def item_type_name(item_type):
    if isinstance(item_type, str):
        return item_type.lower()
    return 'cargo' if item_type == 1 else 'item'


def normalize_item_stack(stack):
    return {
        'item_id':            stack.get('item_id'),
        'quantity':           stack.get('quantity', 1),
        'item_type':          item_type_name(stack.get('item_type', 'Item')),
        'durability':         stack.get('durability', 0),
        'discovery_score':    stack.get('discovery_score', 0),
        'consumption_chance': stack.get('consumption_chance', 1),
    }


def normalize_level_requirements(reqs):
    return [
        {
            'level':    req.get('level', 1),
            'skill_id': req.get('skill_id') or (req.get('skill') or {}).get('id'),
        }
        for req in reqs
    ]


def normalize_tool_requirements(reqs):
    return [
        {
            'level':     req.get('level', 1),
            'power':     req.get('power', 1),
            'tool_type': req.get('tool_type'),
        }
        for req in reqs
    ]


def normalize_experience(entries):
    return [
        {'quantity': entry.get('quantity', 0), 'skill_id': entry.get('skill_id')}
        for entry in entries
    ]


def item_summary(item_id, item_by_id):
    item = item_by_id.get(int(item_id), {})
    return {
        'id':            str(item_id),
        'name':          item.get('name', str(item_id)),
        'iconAssetName': item.get('icon_asset_name', ''),
        'tier':          item.get('tier'),
        'rarity':        item.get('rarity'),
    }


def stack_summaries(stacks, item_by_id, cargo_by_id):
    result = []
    for stack in stacks:
        item_id = stack.get('item_id')
        item_type = item_type_name(stack.get('item_type', 'Item'))
        if item_type == 'cargo':
            source = cargo_by_id.get(item_id, {})
            item_type_int = 1
        else:
            source = item_by_id.get(item_id, {})
            item_type_int = 0
        result.append({
            'id':            item_id,
            'quantity':      stack.get('quantity', 1),
            'itemType':      item_type_int,
            'name':          source.get('name', str(item_id)),
            'iconAssetName': source.get('icon_asset_name', ''),
        })
    return result


def normalize_crafting_recipe(recipe, item_by_id, cargo_by_id):
    consumed = [normalize_item_stack(s) for s in recipe.get('consumed_item_stacks', [])]
    crafted = [normalize_item_stack(s) for s in recipe.get('crafted_item_stacks', [])]
    direct_output = next((s for s in crafted if s.get('item_type') == 'item'), None)
    return {
        'id':                       recipe.get('id'),
        'name':                     recipe.get('name', 'Craft'),
        'timeRequirement':          recipe.get('time_requirement', 1.6),
        'staminaRequirement':       recipe.get('stamina_requirement', 0),
        'toolDurabilityLost':       recipe.get('tool_durability_lost', 0),
        'buildingRequirementType':  recipe.get('building_requirement_type', 0),
        'buildingRequirementTier':  recipe.get('building_requirement_tier', 0),
        'levelRequirements':        normalize_level_requirements(recipe.get('level_requirements', [])),
        'toolRequirements':         normalize_tool_requirements(recipe.get('tool_requirements', [])),
        'consumedItemStacks':       consumed,
        'discoveryTriggers':        recipe.get('discovery_triggers', []),
        'requiredClaimTechId':      recipe.get('required_claim_tech_id', 0),
        'fullDiscoveryScore':       str(recipe.get('full_discovery_score', '1')),
        'experiencePerProgress':    normalize_experience(recipe.get('experience_per_progress', [])),
        'craftedItemStacks':        crafted,
        'actionsRequired':          recipe.get('actions_required', 1),
        'toolMeshIndex':            recipe.get('tool_mesh_index', 0),
        'recipePerformanceId':      recipe.get('recipe_performance_id', 0),
        'requiredKnowledges':       recipe.get('required_knowledges', []),
        'blockingKnowledges':       recipe.get('blocking_knowledges', []),
        'hideWithoutRequiredKnowledge': recipe.get('hide_without_required_knowledge', False),
        'hideWithBlockingKnowledges':   recipe.get('hide_with_blocking_knowledges', False),
        'allowUseHands':            recipe.get('allow_use_hands', False),
        'isPassive':                recipe.get('is_passive', False),
        'consumedItems':            stack_summaries(recipe.get('consumed_item_stacks', []), item_by_id, cargo_by_id),
        'craftedItems':             stack_summaries(recipe.get('crafted_item_stacks', []), item_by_id, cargo_by_id),
        'outputQuantity':           direct_output.get('quantity', 1) if direct_output else 1,
        'targetId':                 direct_output.get('item_id') if direct_output else 0,
        'buildingType':             recipe.get('building_requirement_type', 0),
        'buildingTier':             recipe.get('building_requirement_tier', 0),
    }


def normalize_extraction_recipe(recipe, item_by_id, cargo_by_id):
    extracted = [
        {
            'item_stack': normalize_item_stack(entry.get('item_stack', {})),
            'probability': entry.get('probability', 0),
        }
        for entry in recipe.get('extracted_item_stacks', [])
    ]
    consumed = [normalize_item_stack(s) for s in recipe.get('consumed_item_stacks', [])]
    direct_output = next(
        (
            entry['item_stack']
            for entry in extracted
            if entry.get('item_stack', {}).get('item_type') == 'item'
        ),
        None,
    )
    return {
        'id':                    recipe.get('id'),
        'resourceId':            recipe.get('resource_id', 0),
        'cargoId':               recipe.get('cargo_id', 0),
        'discoveryTriggers':     recipe.get('discovery_triggers', []),
        'requiredKnowledges':    recipe.get('required_knowledges', []),
        'timeRequirement':       recipe.get('time_requirement', 1.6),
        'staminaRequirement':    recipe.get('stamina_requirement', 0),
        'toolDurabilityLost':    recipe.get('tool_durability_lost', 0),
        'extractedItemStacks':   extracted,
        'consumedItemStacks':    consumed,
        'range':                 recipe.get('range', 0),
        'toolRequirements':      normalize_tool_requirements(recipe.get('tool_requirements', [])),
        'allowUseHands':         recipe.get('allow_use_hands', True),
        'levelRequirements':     normalize_level_requirements(recipe.get('level_requirements', [])),
        'experiencePerProgress': normalize_experience(recipe.get('experience_per_progress', [])),
        'verbPhrase':            recipe.get('verb_phrase', 'Extract'),
        'toolMeshIndex':         recipe.get('tool_mesh_index', 0),
        'recipePerformanceId':   recipe.get('recipe_performance_id', 0),
        'consumedItems':         stack_summaries(recipe.get('consumed_item_stacks', []), item_by_id, cargo_by_id),
        'extractedItems':        stack_summaries(
            [entry.get('item_stack', {}) for entry in recipe.get('extracted_item_stacks', [])],
            item_by_id,
            cargo_by_id,
        ),
        'outputQuantity':        direct_output.get('quantity', 1) if direct_output else 1,
        'targetId':              direct_output.get('item_id') if direct_output else 0,
        'averageOutputs':        [],
    }


def index_item_list_possibilities(item_list_rows, item_by_id):
    indexed = {}
    for item_list in item_list_rows:
        entries = []
        for possibility in item_list.get('possibilities', []):
            chance = possibility.get('probability', 1)
            for stack in possibility.get('items', []):
                target_id = str(stack.get('item_id', ''))
                if not target_id:
                    continue
                entries.append({
                    'targetId':   target_id,
                    'targetItem': item_summary(target_id, item_by_id),
                    'quantity':   stack.get('quantity', 1),
                    'chance':     chance,
                    'isCargo':    item_type_name(stack.get('item_type', 'Item')) == 'cargo',
                })
        indexed[item_list.get('id')] = entries
    return indexed


def resolve_item_outputs(stack, item_by_id, item_list_possibilities):
    if item_type_name(stack.get('item_type', 'Item')) != 'item':
        return []
    item_id = stack.get('item_id')
    item = item_by_id.get(item_id, {})
    list_id = item.get('item_list_id', 0)
    if list_id:
        return [
            int(entry['targetId'])
            for entry in item_list_possibilities.get(list_id, [])
            if not entry.get('isCargo') and str(entry.get('targetId', '')).isdigit()
        ]
    return [item_id] if item_id else []


def build_static_indexes():
    print('\nFetching static BitCraftToolBox desc tables ...')
    item_rows = to_list(fetch_static_json('item_desc.json'))
    cargo_rows = to_list(fetch_static_json('cargo_desc.json'))
    crafting_rows = to_list(fetch_static_json('crafting_recipe_desc.json'))
    extraction_rows = to_list(fetch_static_json('extraction_recipe_desc.json'))
    item_list_rows = to_list(fetch_static_json('item_list_desc.json'))

    item_by_id = build_lookup(item_rows)
    cargo_by_id = build_lookup(cargo_rows)
    item_list_possibilities = index_item_list_possibilities(item_list_rows, item_by_id)

    crafting_by_output = {}
    using_by_input = {}
    for recipe in crafting_rows:
        normalized = normalize_crafting_recipe(recipe, item_by_id, cargo_by_id)
        for stack in recipe.get('crafted_item_stacks', []):
            for output_id in resolve_item_outputs(stack, item_by_id, item_list_possibilities):
                crafting_by_output.setdefault(str(output_id), []).append(normalized)
        for stack in recipe.get('consumed_item_stacks', []):
            if item_type_name(stack.get('item_type', 'Item')) == 'item' and stack.get('item_id'):
                using_by_input.setdefault(str(stack['item_id']), []).append(normalized)

    extraction_by_output = {}
    for recipe in extraction_rows:
        normalized = normalize_extraction_recipe(recipe, item_by_id, cargo_by_id)
        for entry in recipe.get('extracted_item_stacks', []):
            for output_id in resolve_item_outputs(
                entry.get('item_stack', {}), item_by_id, item_list_possibilities
            ):
                extraction_by_output.setdefault(str(output_id), []).append(normalized)

    return {
        'items': item_by_id,
        'item_list_possibilities': item_list_possibilities,
        'crafting_by_output': crafting_by_output,
        'using_by_input': using_by_input,
        'extraction_by_output': extraction_by_output,
    }


# ── Per-item builders from static indexes ───────────────────────────────────

def build_market_item(item_id: str, static_indexes: dict) -> dict:
    item = static_indexes['items'].get(int(item_id), {})
    return {
        'name':                  item.get('name', item_id),
        'tier':                  item.get('tier'),
        'tag':                   item.get('tag', ''),
        'extraction':            static_indexes['extraction_by_output'].get(item_id, []),
        'crafting':              static_indexes['crafting_by_output'].get(item_id, []),
        'using':                 static_indexes['using_by_input'].get(item_id, []),
        'itemListPossibilities': static_indexes['item_list_possibilities'].get(item.get('item_list_id', 0), []),
    }


def build_intermediate_item(item_id: str, static_indexes: dict) -> dict:
    item = static_indexes['items'].get(int(item_id), {})
    return {
        'name':                  item.get('name', item_id),
        'tier':                  item.get('tier'),
        'tag':                   item.get('tag', ''),
        'intermediate':          True,
        'crafting':              static_indexes['crafting_by_output'].get(item_id, []),
        'itemListPossibilities': static_indexes['item_list_possibilities'].get(item.get('item_list_id', 0), []),
    }


def build_ingredient_item(item_id: str, static_indexes: dict) -> dict:
    item = static_indexes['items'].get(int(item_id), {})
    return {
        'name':       item.get('name', item_id),
        'tier':       item.get('tier'),
        'tag':        item.get('tag', ''),
        'ingredient': True,
        'extraction': static_indexes['extraction_by_output'].get(item_id, []),
        'crafting':   static_indexes['crafting_by_output'].get(item_id, []),
        'using':      static_indexes['using_by_input'].get(item_id, []),
    }


# ── Concurrent batch fetcher ────────────────────────────────────────────────

def fetch_batch(ids: list, fetch_fn, label: str) -> dict:
    """
    Fetch a list of item IDs concurrently using ThreadPoolExecutor.
    Returns {item_id: data_dict}.
    """
    raise RuntimeError('fetch_batch is unused; recipes are built from static tables')
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

def median(values):
    if not values:
        return None
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2


def fetch_sell_order_prices(item_id):
    data = api_get(f'/api/market/item/{item_id}')
    listing_list = (
        data if isinstance(data, list)
        else data.get('sellOrders', data.get('orders', []))
    )
    prices = []
    for listing in listing_list:
        price = listing.get('priceThreshold') or listing.get('price')
        if price is None or float(price) <= 0:
            continue
        prices.append(float(price))
    return prices


def main():
    OUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    static_indexes = build_static_indexes()

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

    market_ids = [str(item['id']) for item in items]
    print(f'  Rebuilding {len(market_ids)} market entries from static tables.')

    t0 = time.monotonic()
    updated = {
        iid: build_market_item(iid, static_indexes)
        for iid in market_ids
    }
    OUT_FILE.write_text(json.dumps(updated))
    print(f'  Done in {time.monotonic()-t0:.1f}s.')

    # ── Pass 3: intermediate items (Products / loot boxes) ──────────────────
    intermediate_ids = {
        str(out['item_id'])
        for r in updated.values()
        if not r.get('intermediate') and not r.get('ingredient')
        for recipe in r.get('using', [])
        for out in recipe.get('craftedItemStacks', [])
        # Re-fetch if not cached at all, or if cached without crafting data
        if str(out['item_id']) not in updated
        or (updated.get(str(out['item_id']), {}).get('intermediate')
            and 'crafting' not in updated.get(str(out['item_id']), {}))
    }

    if intermediate_ids:
        print(f'\nPass 3 — intermediate items ({len(intermediate_ids)})…')
        t0 = time.monotonic()
        updated.update({
            iid: build_intermediate_item(iid, static_indexes)
            for iid in intermediate_ids
        })
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
        updated.update({
            iid: build_ingredient_item(iid, static_indexes)
            for iid in ingredient_ids
        })
        print(f'  Done in {time.monotonic()-t0:.1f}s.')

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
        updated.update({
            iid: build_ingredient_item(iid, static_indexes)
            for iid in bait_ingredient_ids
        })
        print(f'  Done in {time.monotonic()-t0:.1f}s.')

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
            prices = fetch_sell_order_prices(iid)
            item = static_indexes['items'].get(int(iid), {})
            tool_prices[iid] = {
                'name':            item.get('name', iid),
                'medianSellPrice': median(prices),
                'lowestSellPrice': min(prices) if prices else None,
                'medianBuyPrice':  None,
                'totalSellOrders': len(prices),
                'totalBuyOrders':  None,
            }
        except Exception as e:
            errors += 1
            print(f'  ✗ {iid}: {e}')

    tool_prices['__meta__'] = {'built_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}
    TOOL_PRICES_FILE.write_text(json.dumps(tool_prices))
    print(f'  Done in {time.monotonic()-t0:.1f}s ({errors} errors). Written to {TOOL_PRICES_FILE}')


if __name__ == '__main__':
    main()
