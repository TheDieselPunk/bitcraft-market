#!/usr/bin/env python3
"""
build_game_data.py — Supplement game data cache builder.

Fetches extraction mechanics data from BitCraftToolBox/BitCraft_GameData that
the bitjita API doesn't expose (e.g. T4 fish extraction recipes).

Produces web/data/game_data.json with:
  - extraction_by_item: {item_id -> [{prob_per_hp, stamina, time, tools, consumed}]}
  - resource_max_health: {resource_id -> max_health}

Run from the web/ directory:
    python scripts/build_game_data.py
"""

import json
import time
import urllib.request
from pathlib import Path

GAMEDATA_BASE = (
    'https://raw.githubusercontent.com/BitCraftToolBox/'
    'BitCraft_GameData/cereal/cs/static'
)
BITJITA_BASE = 'https://bitjita.com'
BITJITA_HEADERS = {'User-Agent': 'BitJita (research)', 'Accept': 'application/json'}
OUT_FILE = Path(__file__).parent.parent / 'data' / 'game_data.json'


def fetch_json(filename):
    url = f'{GAMEDATA_BASE}/{filename}'
    print(f'  Fetching {url} ...')
    req = urllib.request.Request(url, headers={'Accept': 'application/json'})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def build_wrapper_to_actual(resource_list):
    """
    Build a mapping from wrapper item IDs to their actual item IDs.

    Some extraction recipes drop wrapper items (e.g. 4220030 "T4 Lakefish Output")
    that resolve 1:1 to the actual item (e.g. 4110017 Azure Sphyra). The connection
    is established via the resource node: the resource's on_destroy_yield gives the
    actual item_id (which doubles as the item_list ID), while the per-cast extraction
    drops the wrapper. We map wrapper → actual using the same resource node.

    This is then used to re-key extraction_by_item by actual item_id.
    """
    # resource_id -> list of on_destroy item_ids (the actual fish IDs)
    resource_to_actual = {}
    for res in resource_list:
        rid = str(res.get('id', ''))
        yields = res.get('on_destroy_yield', [])
        if yields:
            resource_to_actual[rid] = [str(y['item_id']) for y in yields]
    return resource_to_actual


def build_extraction_by_item(extraction_recipes, resource_to_actual):
    """
    Convert extraction_recipe_desc list into a dict keyed by actual item_id.

    The game data structure has each extraction recipe targeting a resource node,
    and the recipe's extracted_item_stacks list which items drop and at what
    probability per health-point of the node damaged.

    When a drop item is a wrapper (e.g. T4 Lakefish Output), we re-key by the
    actual item_id from resource_to_actual, since the wrapper resolves 1:1.
    We also keep the original wrapper key so the calculator can find it either way.
    """
    by_item = {}

    for recipe in extraction_recipes:
        resource_id = str(recipe.get('resource_id', recipe.get('id', '')))
        stamina = recipe.get('stamina_requirement', 0.0)
        time_req = recipe.get('time_requirement', 1.6)
        tool_reqs = recipe.get('tool_requirements', [])
        level_reqs = recipe.get('level_requirements', [])

        # Consumed items (e.g. bait)
        consumed = [
            {
                'item_id': str(c['item_id']),
                'consumption_chance': c.get('consumption_chance', 1.0),
            }
            for c in recipe.get('consumed_item_stacks', [])
        ]

        # Normalise tool_requirements keys (snake_case from raw data)
        tools = [
            {
                'tool_type': t.get('tool_type'),
                'level': t.get('level', 1),
                'power': t.get('power', 1),
            }
            for t in tool_reqs
        ]

        levels = [
            {
                'skill_id': l.get('skill_id'),
                'level': l.get('level', 1),
            }
            for l in level_reqs
        ]

        # The actual item IDs for this resource (from on_destroy_yield)
        actual_ids = resource_to_actual.get(resource_id, [])

        # Each extracted item stack is a separate drop
        for stack_entry in recipe.get('extracted_item_stacks', []):
            stack = stack_entry.get('item_stack', {})
            wrapper_id = str(stack.get('item_id', ''))
            prob = stack_entry.get('probability', 0.0)
            qty = stack.get('quantity', 1)

            if not wrapper_id or prob <= 0:
                continue

            entry = {
                'resource_id': resource_id,
                'prob_per_hp': prob,
                'output_quantity': qty,
                'stamina_per_cast': stamina,
                'time_per_cast': time_req,
                'tool_requirements': tools,
                'level_requirements': levels,
                'consumed': consumed,
            }

            # Index by wrapper item_id (e.g. 4220030)
            by_item.setdefault(wrapper_id, []).append(entry)

            # Also index by actual item_ids for this resource (e.g. 4110017 Azure Sphyra)
            # These are the items the wrapper resolves to 1:1 per cast.
            for actual_id in actual_ids:
                if actual_id != wrapper_id:
                    by_item.setdefault(actual_id, []).append(entry)

    return by_item


def build_resource_max_health(resource_desc):
    """Build {resource_id_str -> max_health} mapping."""
    result = {}
    resources = resource_desc if isinstance(resource_desc, list) else resource_desc.values()
    for res in resources:
        rid = str(res.get('id', ''))
        health = res.get('max_health')
        if rid and health is not None:
            result[rid] = health
    return result


def build_cargo_extraction(extraction_recipes):
    """
    Build extraction data for Cargo items from resource nodes.

    Some extraction recipes yield cargo items (e.g. Ferralith Ore Chunk) instead
    of regular items. They use the same prob_per_hp model.

    Returns {cargo_id_str -> [{resource_id, prob_per_hp, stamina_per_cast, ...}]}
    """
    by_cargo = {}
    for recipe in extraction_recipes:
        resource_id = str(recipe.get('resource_id', ''))
        stamina = recipe.get('stamina_requirement', 0.0)
        time_req = recipe.get('time_requirement', 1.6)
        tools = [
            {'tool_type': t.get('tool_type'), 'level': t.get('level', 1), 'power': t.get('power', 1)}
            for t in recipe.get('tool_requirements', [])
        ]
        levels = [
            {'skill_id': l.get('skill_id'), 'level': l.get('level', 1)}
            for l in recipe.get('level_requirements', [])
        ]
        for stack_entry in recipe.get('extracted_item_stacks', []):
            stack = stack_entry.get('item_stack', {})
            if stack.get('item_type', '') != 'Cargo':
                continue
            cargo_id = str(stack.get('item_id', ''))
            prob = stack_entry.get('probability', 0.0)
            if not cargo_id or prob <= 0:
                continue
            entry = {
                'resource_id': resource_id,
                'prob_per_hp': prob,
                'output_quantity': stack.get('quantity', 1),
                'stamina_per_cast': stamina,
                'time_per_cast': time_req,
                'tool_requirements': tools,
                'level_requirements': levels,
            }
            by_cargo.setdefault(cargo_id, []).append(entry)
    return by_cargo


def build_cargo_by_item(crafting_recipes, cargo_extraction, cargo_names):
    """
    Build crafting-from-cargo data for items produced by processing gatherable cargo.

    Only includes recipes where the Cargo ingredient exists in cargo_extraction
    (i.e. it can actually be gathered). This naturally excludes market-package
    "Unpack" recipes since those cargo IDs have no extraction data.

    Returns {item_id_str -> [{recipe_name, actions_required, stamina_per_action, ...}]}
    """
    by_item = {}
    for recipe in crafting_recipes:
        cargo_inputs = [
            i for i in recipe.get('consumed_item_stacks', [])
            if i.get('item_type') == 'Cargo'
        ]
        if not cargo_inputs:
            continue
        item_outputs = [
            o for o in recipe.get('crafted_item_stacks', [])
            if o.get('item_type') == 'Item'
        ]
        if not item_outputs:
            continue
        for cargo_input in cargo_inputs:
            cargo_id = str(cargo_input.get('item_id', ''))
            if cargo_id not in cargo_extraction:
                continue  # skip market packages (not gatherable)
            for output in item_outputs:
                item_id = str(output.get('item_id', ''))
                if not item_id:
                    continue
                entry = {
                    'recipe_name':      recipe.get('name', ''),
                    'stamina_per_action': recipe.get('stamina_requirement', 0.0),
                    'time_per_action':  recipe.get('time_requirement', 1.6),
                    'actions_required': recipe.get('actions_required', 1),
                    'output_quantity':  output.get('quantity', 1),
                    'tool_requirements': recipe.get('tool_requirements', []),
                    'level_requirements': recipe.get('level_requirements', []),
                    'cargo_input_id':   cargo_id,
                    'cargo_input_qty':  cargo_input.get('quantity', 1),
                    'cargo_input_name': cargo_names.get(cargo_id, cargo_id),
                }
                by_item.setdefault(item_id, []).append(entry)
    return by_item


def bitjita_get(path):
    url = f'{BITJITA_BASE}{path}'
    req = urllib.request.Request(url, headers=BITJITA_HEADERS)
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read())


def fetch_item_recipe(item_id):
    """Fetch crafting/extraction recipe data for an item from the bitjita API."""
    d = bitjita_get(f'/api/items/{item_id}')
    return {
        'name':      d['item']['name'],
        'tier':      d['item']['tier'],
        'tag':       d['item'].get('tag', ''),
        'ingredient': True,
        'extraction': d.get('extractionRecipes', []),
        'crafting':   d.get('craftingRecipes', []),
        'using':      d.get('recipesUsingItem', []),
    }


def collect_extraction_consumed_ids(extraction_by_item):
    """Return set of item_ids consumed by extraction recipes (e.g. bait)."""
    ids = set()
    for entries in extraction_by_item.values():
        for entry in entries:
            for c in entry.get('consumed', []):
                ids.add(str(c['item_id']))
    return ids


def main():
    OUT_FILE.parent.mkdir(parents=True, exist_ok=True)

    print('Fetching extraction_recipe_desc.json …')
    extraction_recipes = fetch_json('extraction_recipe_desc.json')
    if isinstance(extraction_recipes, dict):
        extraction_recipes = list(extraction_recipes.values())
    print(f'  {len(extraction_recipes)} extraction recipes.')

    print('Fetching resource_desc.json …')
    resource_desc = fetch_json('resource_desc.json')
    if isinstance(resource_desc, dict):
        resource_list = list(resource_desc.values())
    else:
        resource_list = resource_desc
    print(f'  {len(resource_list)} resource nodes.')

    print('Fetching crafting_recipe_desc.json …')
    crafting_recipes = fetch_json('crafting_recipe_desc.json')
    if isinstance(crafting_recipes, dict):
        crafting_recipes = list(crafting_recipes.values())
    print(f'  {len(crafting_recipes)} crafting recipes.')

    print('Fetching cargo_desc.json …')
    cargo_desc = fetch_json('cargo_desc.json')
    if isinstance(cargo_desc, dict):
        cargo_desc = list(cargo_desc.values())
    cargo_names = {str(c['id']): c['name'] for c in cargo_desc}
    print(f'  {len(cargo_desc)} cargo types.')

    resource_to_actual = build_wrapper_to_actual(resource_list)
    extraction_by_item = build_extraction_by_item(extraction_recipes, resource_to_actual)
    resource_max_health = build_resource_max_health(resource_list)
    cargo_extraction   = build_cargo_extraction(extraction_recipes)
    cargo_by_item      = build_cargo_by_item(crafting_recipes, cargo_extraction, cargo_names)
    print(f'  {len(cargo_extraction)} gatherable cargo types, {len(cargo_by_item)} items via cargo processing.')

    # Collect actual fish/node item IDs (from on_destroy_yield mappings)
    actual_item_ids = set()
    for actual_list in resource_to_actual.values():
        actual_item_ids.update(actual_list)

    # Fetch recipe data for:
    #   1. Items consumed by extraction recipes (bait etc.)
    #   2. Actual fish/gatherable items (needed for their using-recipes, e.g. Azure Minni → Fine Bait)
    consumed_ids = collect_extraction_consumed_ids(extraction_by_item)
    to_fetch_ids = (consumed_ids | actual_item_ids)
    print(f'\nFetching recipes for {len(to_fetch_ids)} items (bait + actual fish)...')
    extra_recipes = {}
    for iid in sorted(to_fetch_ids):
        try:
            extra_recipes[iid] = fetch_item_recipe(iid)
            name = extra_recipes[iid]['name']
            crafting_count = len(extra_recipes[iid].get('crafting', []))
            using_count = len(extra_recipes[iid].get('using', []))
            print(f'  {iid} {name} ({crafting_count} crafting, {using_count} using recipes)')
        except Exception as e:
            print(f'  WARNING: could not fetch {iid}: {e}')

    # Fetch intermediates produced by extra_recipe using-recipes (e.g. 4220019 "Fine Bait and Shells")
    intermediate_ids = {
        str(out['item_id'])
        for rec in extra_recipes.values()
        for urec in rec.get('using', [])
        for out in urec.get('craftedItemStacks', [])
        if str(out['item_id']) not in extra_recipes
    }
    if intermediate_ids:
        print(f'\nFetching {len(intermediate_ids)} intermediates produced by extra_recipe using-recipes...')
        for iid in sorted(intermediate_ids):
            try:
                d = bitjita_get(f'/api/items/{iid}')
                extra_recipes[iid] = {
                    'name':      d['item']['name'],
                    'tier':      d['item']['tier'],
                    'tag':       d['item'].get('tag', ''),
                    'intermediate': True,
                    'itemListPossibilities': d.get('itemListPossibilities', []),
                }
                print(f'  {iid} {extra_recipes[iid]["name"]} ({len(extra_recipes[iid]["itemListPossibilities"])} loot entries)')
            except Exception as e:
                print(f'  WARNING: could not fetch {iid}: {e}')

    output = {
        'extraction_by_item': extraction_by_item,
        'resource_max_health': resource_max_health,
        'extra_recipes': extra_recipes,
        'cargo_extraction': cargo_extraction,
        'cargo_by_item': cargo_by_item,
        '__meta__': {
            'built_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
            'extraction_items': len(extraction_by_item),
            'resources': len(resource_max_health),
            'extra_recipes': len(extra_recipes),
            'cargo_types': len(cargo_extraction),
            'cargo_items': len(cargo_by_item),
        },
    }

    OUT_FILE.write_text(json.dumps(output))

    print(f'\n{"-"*50}')
    print(f'Extraction item entries : {len(extraction_by_item)}')
    print(f'Resource nodes          : {len(resource_max_health)}')
    print(f'Extra recipes fetched   : {len(extra_recipes)}')
    print(f'Cargo extraction types  : {len(cargo_extraction)}')
    print(f'Items via cargo process : {len(cargo_by_item)}')
    print(f'Written to {OUT_FILE}')

    # Spot-check Azure Sphyra
    if '4110017' in extraction_by_item:
        e = extraction_by_item['4110017'][0]
        print(f'\nSpot-check Azure Sphyra (4110017):')
        print(f'  prob_per_hp={e["prob_per_hp"]}, stamina={e["stamina_per_cast"]}, '
              f'resource={e["resource_id"]}')
    else:
        print('\nWARNING: Azure Sphyra (4110017) not found in extraction data.')


if __name__ == '__main__':
    main()
