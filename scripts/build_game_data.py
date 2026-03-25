#!/usr/bin/env python3
"""
build_game_data.py — Game data cache builder using static BitCraftToolBox files.

Fetches 6 static JSON files from BitCraftToolBox/BitCraft_GameData and produces
web/data/game_data.json with:
  - extraction_by_item:  {item_id -> [{prob_per_hp, stamina, time, tools, consumed, ...}]}
  - cargo_by_item:       {item_id -> [{recipe_name, cargo_input_id, ...}]}
  - resource_max_health: {resource_id -> max_health}
  - cargo_extraction:    {cargo_id -> [{resource_id, prob_per_hp, ...}]}
  - __meta__:            build statistics

Run from the web/ directory:
    python scripts/build_game_data.py
"""

import json
import time
import urllib.request
from pathlib import Path

STATIC_BASE = (
    'https://raw.githubusercontent.com/BitCraftToolBox/'
    'BitCraft_GameData/cereal/cs/static'
)
OUT_FILE = Path(__file__).parent.parent / 'data' / 'game_data.json'


def fetch_json(filename):
    url = f'{STATIC_BASE}/{filename}'
    print(f'  Fetching {url} ...')
    req = urllib.request.Request(url, headers={'Accept': 'application/json'})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())


def to_list(data):
    """Normalise a JSON value that may be a list or a dict of objects."""
    if isinstance(data, list):
        return data
    return list(data.values())


def build_lookup(items, key='id'):
    """Build {item[key] -> item} mapping."""
    return {entry[key]: entry for entry in items}


def build_cargo_extraction(extraction_list):
    """
    Build {cargo_id_str -> [{resource_id, prob_per_hp, stamina_per_cast, ...}]}
    from extraction recipes whose extracted item stacks have item_type == 'Cargo'.
    """
    by_cargo = {}
    for r in extraction_list:
        resource_id = str(r.get('resource_id', ''))
        stamina = r.get('stamina_requirement', 0.0)
        time_req = r.get('time_requirement', 1.6)
        tools = [
            {'tool_type': t.get('tool_type'), 'level': t.get('level', 1), 'power': t.get('power', 1)}
            for t in r.get('tool_requirements', [])
        ]
        levels = [
            {'skill_id': l.get('skill_id'), 'level': l.get('level', 1)}
            for l in r.get('level_requirements', [])
        ]
        for stack_entry in r.get('extracted_item_stacks', []):
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


def build_extraction_by_item(extraction_list, item_list_by_id, item_by_id):
    """
    Build {item_id_str -> [{resource_id, prob_per_hp, output_quantity, ...}]}

    For each extracted item stack:
    - If the item has a non-zero item_list_id, it is a wrapper. Resolve it via
      item_list_desc: for each possibility, add an entry per actual item with
      prob_per_hp adjusted by possibility_probability. Each actual item may be
      an Item or Cargo type.
    - Otherwise, index directly by item_id.
    """
    by_item = {}

    for r in extraction_list:
        resource_id = str(r.get('resource_id', ''))
        stamina = r.get('stamina_requirement', 0.0)
        time_req = r.get('time_requirement', 1.6)
        tools = [
            {'tool_type': t.get('tool_type'), 'level': t.get('level', 1), 'power': t.get('power', 1)}
            for t in r.get('tool_requirements', [])
        ]
        levels = [
            {'skill_id': l.get('skill_id'), 'level': l.get('level', 1)}
            for l in r.get('level_requirements', [])
        ]
        consumed = [
            {
                'item_id': str(c['item_id']),
                'consumption_chance': c.get('consumption_chance', 1.0),
            }
            for c in r.get('consumed_item_stacks', [])
        ]

        for stack_entry in r.get('extracted_item_stacks', []):
            stack = stack_entry.get('item_stack', {})
            item_id = stack.get('item_id', 0)
            item_type = stack.get('item_type', 'Item')
            prob = stack_entry.get('probability', 0.0)
            qty = stack.get('quantity', 1)

            if not item_id or prob <= 0:
                continue

            # Skip Cargo-type stacks here — they are handled by build_cargo_extraction
            if item_type == 'Cargo':
                continue

            # Check if this item is a wrapper (item_list_id != 0)
            item_info = item_by_id.get(item_id, {})
            list_id = item_info.get('item_list_id', 0)

            base_entry = {
                'resource_id': resource_id,
                'stamina_per_cast': stamina,
                'time_per_cast': time_req,
                'tool_requirements': tools,
                'level_requirements': levels,
                'consumed': consumed,
            }

            if list_id and list_id != 0:
                # Wrapper item — resolve via item_list_desc
                item_list = item_list_by_id.get(list_id)
                if item_list:
                    for possibility in item_list.get('possibilities', []):
                        poss_prob = possibility.get('probability', 1.0)
                        for actual in possibility.get('items', []):
                            actual_id = str(actual.get('item_id', ''))
                            actual_qty = actual.get('quantity', 1)
                            actual_type = actual.get('item_type', 'Item')
                            if not actual_id:
                                continue
                            entry = dict(base_entry)
                            entry['prob_per_hp'] = prob * poss_prob
                            entry['output_quantity'] = actual_qty
                            if actual_type == 'Cargo':
                                entry['cargo_input_id'] = actual_id
                            by_item.setdefault(actual_id, []).append(entry)
                else:
                    # item_list not found, fall back to wrapper id
                    entry = dict(base_entry)
                    entry['prob_per_hp'] = prob
                    entry['output_quantity'] = qty
                    by_item.setdefault(str(item_id), []).append(entry)
            else:
                # Direct item drop
                entry = dict(base_entry)
                entry['prob_per_hp'] = prob
                entry['output_quantity'] = qty
                by_item.setdefault(str(item_id), []).append(entry)

    return by_item


def build_cargo_by_item(crafting_list, cargo_extraction, extraction_by_item,
                        cargo_by_id, item_list_by_id, item_by_id):
    """
    Build {item_id_str -> [{recipe_name, cargo_input_id, ...}]}

    Only includes recipes where a Cargo-type input is obtainable from the world —
    either directly in cargo_extraction OR resolved via item wrappers into
    extraction_by_item (e.g. ocean fish 6000-6005 via Oceanfish Output wrappers).

    If an output item is a wrapper (item_list_id != 0), resolve it via
    item_list_desc and re-key by actual item IDs.
    """
    # All cargo IDs that can be obtained in-world (directly or via wrapper)
    obtainable_cargo = set(cargo_extraction.keys()) | set(extraction_by_item.keys())

    by_item = {}

    for r in crafting_list:
        cargo_inputs = [
            i for i in r.get('consumed_item_stacks', [])
            if i.get('item_type') == 'Cargo'
        ]
        if not cargo_inputs:
            continue

        item_outputs = [
            o for o in r.get('crafted_item_stacks', [])
            if o.get('item_type') == 'Item'
        ]
        if not item_outputs:
            continue

        for cargo_input in cargo_inputs:
            cargo_id = str(cargo_input.get('item_id', ''))
            if cargo_id not in obtainable_cargo:
                continue  # skip market packages (not gatherable)

            cargo_name = cargo_by_id.get(cargo_input.get('item_id', 0), {}).get('name', cargo_id)

            for output in item_outputs:
                out_item_id = output.get('item_id', 0)
                out_qty = output.get('quantity', 1)

                if not out_item_id:
                    continue

                entry_base = {
                    'recipe_name':        r.get('name', ''),
                    'stamina_per_action': r.get('stamina_requirement', 0.0),
                    'time_per_action':    r.get('time_requirement', 1.6),
                    'actions_required':   r.get('actions_required', 1),
                    'tool_requirements':  r.get('tool_requirements', []),
                    'level_requirements': r.get('level_requirements', []),
                    'cargo_input_id':     cargo_id,
                    'cargo_input_qty':    cargo_input.get('quantity', 1),
                    'cargo_input_name':   cargo_name,
                }

                # Check if output is a wrapper item
                out_item_info = item_by_id.get(out_item_id, {})
                list_id = out_item_info.get('item_list_id', 0)

                if list_id and list_id != 0:
                    # Wrapper — resolve via item_list_desc
                    item_list = item_list_by_id.get(list_id)
                    if item_list:
                        # Accumulate expected output per actual item across ALL possibilities
                        # (multiple possibilities may yield the same item — sum them)
                        item_outputs = {}  # actual_id -> total expected qty per recipe run
                        for possibility in item_list.get('possibilities', []):
                            poss_prob = possibility.get('probability', 1.0)
                            for actual in possibility.get('items', []):
                                actual_id = str(actual.get('item_id', ''))
                                actual_qty = actual.get('quantity', 1)
                                if not actual_id:
                                    continue
                                item_outputs[actual_id] = (
                                    item_outputs.get(actual_id, 0.0)
                                    + out_qty * poss_prob * actual_qty
                                )
                        for actual_id, total_qty in item_outputs.items():
                            entry = dict(entry_base)
                            entry['output_quantity'] = total_qty
                            by_item.setdefault(actual_id, []).append(entry)
                        continue  # skip the wrapper key itself
                    # fallthrough: list not found, use wrapper key
                entry = dict(entry_base)
                entry['output_quantity'] = out_qty
                by_item.setdefault(str(out_item_id), []).append(entry)

    return by_item


def build_item_chain_by_item(crafting_list, extraction_by_item, item_list_by_id, item_by_id):
    """
    Build {output_item_id_str -> [{input_item_id, input_item_name, output_quantity, ...}]}

    For crafting recipes where the consumed input is an Item type that is
    world-extractable (in extraction_by_item), e.g. lake fish → process → oil/filet.
    Output wrappers (item_list_id != 0) are resolved and quantities aggregated.
    """
    by_item = {}

    for r in crafting_list:
        item_inputs = [
            i for i in r.get('consumed_item_stacks', [])
            if i.get('item_type') == 'Item'
               and str(i.get('item_id', '')) in extraction_by_item
        ]
        if not item_inputs:
            continue

        crafted_outputs = [
            o for o in r.get('crafted_item_stacks', [])
            if o.get('item_type') == 'Item'
        ]
        if not crafted_outputs:
            continue

        for item_input in item_inputs:
            input_id = str(item_input.get('item_id', ''))
            input_name = item_by_id.get(item_input.get('item_id', 0), {}).get('name', input_id)

            entry_base = {
                'recipe_name':        r.get('name', ''),
                'stamina_per_action': r.get('stamina_requirement', 0.0),
                'time_per_action':    r.get('time_requirement', 1.6),
                'actions_required':   r.get('actions_required', 1),
                'tool_requirements':  r.get('tool_requirements', []),
                'level_requirements': r.get('level_requirements', []),
                'input_item_id':      input_id,
                'input_item_qty':     item_input.get('quantity', 1),
                'input_item_name':    input_name,
            }

            for output in crafted_outputs:
                out_item_id = output.get('item_id', 0)
                out_qty = output.get('quantity', 1)
                if not out_item_id:
                    continue

                out_item_info = item_by_id.get(out_item_id, {})
                list_id = out_item_info.get('item_list_id', 0)

                if list_id and list_id != 0:
                    item_list = item_list_by_id.get(list_id)
                    if item_list:
                        # Accumulate expected output per actual item across all possibilities
                        resolved = {}
                        for possibility in item_list.get('possibilities', []):
                            poss_prob = possibility.get('probability', 1.0)
                            for actual in possibility.get('items', []):
                                actual_id = str(actual.get('item_id', ''))
                                actual_qty = actual.get('quantity', 1)
                                if not actual_id:
                                    continue
                                resolved[actual_id] = (
                                    resolved.get(actual_id, 0.0)
                                    + out_qty * poss_prob * actual_qty
                                )
                        for actual_id, total_qty in resolved.items():
                            entry = dict(entry_base)
                            entry['output_quantity'] = total_qty
                            by_item.setdefault(actual_id, []).append(entry)
                        continue
                    # fallthrough: list not found, use wrapper key
                entry = dict(entry_base)
                entry['output_quantity'] = out_qty
                by_item.setdefault(str(out_item_id), []).append(entry)

    return by_item


def build_resource_max_health(resource_list):
    """Build {resource_id_str -> max_health} mapping."""
    result = {}
    for res in resource_list:
        rid = str(res.get('id', ''))
        health = res.get('max_health')
        if rid and health is not None:
            result[rid] = health
    return result


def main():
    OUT_FILE.parent.mkdir(parents=True, exist_ok=True)

    print('Fetching static game data files ...')

    print('Fetching extraction_recipe_desc.json ...')
    extraction_list = to_list(fetch_json('extraction_recipe_desc.json'))
    print(f'  {len(extraction_list)} extraction recipes.')

    print('Fetching resource_desc.json ...')
    resource_list = to_list(fetch_json('resource_desc.json'))
    print(f'  {len(resource_list)} resource nodes.')

    print('Fetching crafting_recipe_desc.json ...')
    crafting_list = to_list(fetch_json('crafting_recipe_desc.json'))
    print(f'  {len(crafting_list)} crafting recipes.')

    print('Fetching cargo_desc.json ...')
    cargo_list = to_list(fetch_json('cargo_desc.json'))
    cargo_by_id = build_lookup(cargo_list)
    print(f'  {len(cargo_list)} cargo types.')

    print('Fetching item_desc.json ...')
    item_list_raw = to_list(fetch_json('item_desc.json'))
    item_by_id = build_lookup(item_list_raw)
    print(f'  {len(item_list_raw)} items.')

    print('Fetching item_list_desc.json ...')
    item_list_raw2 = to_list(fetch_json('item_list_desc.json'))
    item_list_by_id = build_lookup(item_list_raw2)
    print(f'  {len(item_list_raw2)} item lists (wrapper resolvers).')

    print('\nBuilding cargo_extraction ...')
    cargo_extraction = build_cargo_extraction(extraction_list)
    print(f'  {len(cargo_extraction)} gatherable cargo types.')

    print('Building extraction_by_item ...')
    extraction_by_item = build_extraction_by_item(
        extraction_list, item_list_by_id, item_by_id
    )
    print(f'  {len(extraction_by_item)} unique items from extraction.')

    print('Building resource_max_health ...')
    resource_max_health = build_resource_max_health(resource_list)
    print(f'  {len(resource_max_health)} resource nodes with max_health.')

    print('Building cargo_by_item ...')
    cargo_by_item = build_cargo_by_item(
        crafting_list, cargo_extraction, extraction_by_item,
        cargo_by_id, item_list_by_id, item_by_id
    )
    print(f'  {len(cargo_by_item)} unique items produced from cargo processing.')

    print('Building item_chain_by_item ...')
    item_chain_by_item = build_item_chain_by_item(
        crafting_list, extraction_by_item, item_list_by_id, item_by_id
    )
    print(f'  {len(item_chain_by_item)} unique items produced via item processing chains.')

    output = {
        'extraction_by_item':  extraction_by_item,
        'cargo_by_item':       cargo_by_item,
        'item_chain_by_item':  item_chain_by_item,
        'resource_max_health': resource_max_health,
        'cargo_extraction':    cargo_extraction,
        '__meta__': {
            'built_at':           time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
            'extraction_items':   len(extraction_by_item),
            'resources':          len(resource_max_health),
            'cargo_types':        len(cargo_extraction),
            'cargo_items':        len(cargo_by_item),
            'item_chain_items':   len(item_chain_by_item),
        },
    }

    OUT_FILE.write_text(json.dumps(output))

    print(f'\n{"-"*50}')
    print(f'Extraction item entries : {len(extraction_by_item)}')
    print(f'Resource nodes          : {len(resource_max_health)}')
    print(f'Cargo extraction types  : {len(cargo_extraction)}')
    print(f'Items via cargo process : {len(cargo_by_item)}')
    print(f'Items via item chains   : {len(item_chain_by_item)}')
    print(f'Written to {OUT_FILE}')

    # Spot-checks
    for check_id, label in [('4110017', 'Azure Sphyra'), ('4220030', 'T4 Lakefish Output wrapper')]:
        if check_id in extraction_by_item:
            e = extraction_by_item[check_id][0]
            print(f'\nSpot-check {label} ({check_id}):')
            print(f'  prob_per_hp={e["prob_per_hp"]}, stamina={e["stamina_per_cast"]}, '
                  f'resource={e["resource_id"]}')
        else:
            print(f'\nNote: {label} ({check_id}) not in extraction_by_item '
                  f'(may be expected if wrapper-only).')


if __name__ == '__main__':
    main()
