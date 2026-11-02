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
import re
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


def possibility_chances(item_list):
    """(chance, possibility) for each entry of an item list. One entry is picked per
    roll; lists whose chances total more than 1 are weights (Lost Treasure's add up
    to ~100, legendary pies' to ~29,600), so those are scaled to sum to 1."""
    possibilities = item_list.get('possibilities', [])
    total = sum(p.get('probability', 1.0) for p in possibilities)
    scale = 1 / total if total > 1 else 1
    return [(p.get('probability', 1.0) * scale, p) for p in possibilities]


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
                'experience_per_progress': r.get('experience_per_progress', []),
                'consumed': consumed,
            }

            if list_id and list_id != 0:
                # Wrapper item — resolve via item_list_desc
                item_list = item_list_by_id.get(list_id)
                if item_list:
                    for poss_prob, possibility in possibility_chances(item_list):
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
    # All cargo IDs that can be obtained in-world (directly or via wrapper).
    # extraction_by_item is keyed by item id, and wrapper cargo shares it, so only
    # entries tagged with the cargo count: item 1 Hex Coin drops from loot, but
    # that doesn't make cargo 1 (Sagi Bird) gatherable.
    obtainable_cargo = set(cargo_extraction) | {
        cid for cid, entries in extraction_by_item.items()
        if any(e.get('cargo_input_id') == cid for e in entries)
    }

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
                    'recipe_id':          r.get('id'),  # groups a recipe's co-products
                    'recipe_name':        r.get('name', ''),
                    'stamina_per_action': r.get('stamina_requirement', 0.0),
                    'time_per_action':    r.get('time_requirement', 1.6),
                    'actions_required':   r.get('actions_required', 1),
                    'tool_requirements':  r.get('tool_requirements', []),
                    'level_requirements': r.get('level_requirements', []),
                    'experience_per_progress': r.get('experience_per_progress', []),
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
                        for poss_prob, possibility in possibility_chances(item_list):
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
                            entry['output_item_name'] = item_by_id.get(int(actual_id), {}).get('name', actual_id)
                            by_item.setdefault(actual_id, []).append(entry)
                        continue  # skip the wrapper key itself
                    # fallthrough: list not found, use wrapper key
                entry = dict(entry_base)
                entry['output_quantity'] = out_qty
                entry['output_item_name'] = item_by_id.get(out_item_id, {}).get('name', str(out_item_id))
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

        # IDs of the extractable inputs for this recipe
        extractable_ids = {str(i.get('item_id', '')) for i in item_inputs}

        # Other consumed items (not extractable — external ingredients like raw meat)
        other_consumed = [
            {
                'item_id':   str(i.get('item_id', '')),
                'item_name': item_by_id.get(i.get('item_id', 0), {}).get('name', str(i.get('item_id', ''))),
                'quantity':  i.get('quantity', 1),
            }
            for i in r.get('consumed_item_stacks', [])
            if i.get('item_type') == 'Item'
               and str(i.get('item_id', '')) not in extractable_ids
        ]

        for item_input in item_inputs:
            input_id = str(item_input.get('item_id', ''))
            input_name = item_by_id.get(item_input.get('item_id', 0), {}).get('name', input_id)

            entry_base = {
                'recipe_id':          r.get('id'),  # groups a recipe's co-products
                'recipe_name':        r.get('name', ''),
                'stamina_per_action': r.get('stamina_requirement', 0.0),
                'time_per_action':    r.get('time_requirement', 1.6),
                'actions_required':   r.get('actions_required', 1),
                'tool_requirements':  r.get('tool_requirements', []),
                'level_requirements': r.get('level_requirements', []),
                'input_item_id':      input_id,
                'input_item_qty':     item_input.get('quantity', 1),
                'input_item_name':    input_name,
                'other_consumed':     other_consumed,
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
                        for poss_prob, possibility in possibility_chances(item_list):
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


def build_item_to_item_crafting(crafting_list, item_by_id, item_list_by_id, cargo_by_id):
    """
    Build {output_item_id_str -> [{recipe info with all consumed items}]}

    Covers ALL crafting recipes producing Item outputs, regardless of input type.
    This enables recursive chain resolution for multi-step chains (e.g. ingots).
    """
    by_item = {}

    for r in crafting_list:
        crafted_outputs = [
            o for o in r.get('crafted_item_stacks', [])
            if o.get('item_type') == 'Item'
        ]
        if not crafted_outputs:
            continue

        consumed = []
        for i in r.get('consumed_item_stacks', []):
            itype = i.get('item_type', 'Item')
            iid   = i.get('item_id', 0)
            if itype == 'Item':
                name = item_by_id.get(iid, {}).get('name', str(iid))
            elif itype == 'Cargo':
                name = cargo_by_id.get(iid, {}).get('name', str(iid))
            else:
                name = str(iid)
            consumed.append({
                'item_id':   str(iid),
                'item_name': name,
                'quantity':  i.get('quantity', 1),
                'item_type': itype,
            })

        entry_base = {
            'recipe_name':        r.get('name', ''),
            'stamina_per_action': r.get('stamina_requirement', 0.0),
            'time_per_action':    r.get('time_requirement', 1.6),
            'actions_required':   r.get('actions_required', 1),
            'tool_requirements':  r.get('tool_requirements', []),
            'level_requirements': r.get('level_requirements', []),
            'experience_per_progress': r.get('experience_per_progress', []),
            'consumed':           consumed,
        }

        for output in crafted_outputs:
            out_item_id = output.get('item_id', 0)
            out_qty     = output.get('quantity', 1)
            if not out_item_id:
                continue

            out_item_info = item_by_id.get(out_item_id, {})
            list_id = out_item_info.get('item_list_id', 0)

            if list_id and list_id != 0:
                item_list = item_list_by_id.get(list_id)
                if item_list:
                    resolved = {}
                    for poss_prob, possibility in possibility_chances(item_list):
                        for actual in possibility.get('items', []):
                            actual_id  = str(actual.get('item_id', ''))
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
                    continue  # skip wrapper key itself
                # fallthrough: list not found, use wrapper key

            entry = dict(entry_base)
            entry['output_quantity'] = out_qty
            by_item.setdefault(str(out_item_id), []).append(entry)

    return by_item


# Chum descriptions say "Once chummed, nodes remain fishable for 15 minutes."
_DURATION_RE = re.compile(r'for (\d+) minutes')
FISHING_SKILL_ID = 12

# Resource tags still in the static data but no longer reachable in game.
REMOVED_RESOURCE_TAGS = {'Paranensis'}

# Nodes nobody can gather on a loop: dungeon nodes, and one-shot 1-HP
# "Uncharted" finds (an Old Anvil yields 60 Salvaged Pirate's Weapons once).
# Counted as sources, they made routes like dungeon-mined Emarium look best.
UNFARMABLE_RESOURCE_TAGS = {
    'Dungeon Resource', 'Profession Dungeon Resource',
    'Uncharted Resource', 'Uncharted Scholar', 'Uncharted Tailoring Resource',
}

# Sailing cargo is farmed by sailing node to node for one box each, not by standing
# at a node; the Chain Calculator models it from sailing_cargo.json instead.
SAILING_CARGO_TAGS = {'Sailing Cargo'}
SKIPPED_RESOURCE_TAGS = REMOVED_RESOURCE_TAGS | UNFARMABLE_RESOURCE_TAGS | SAILING_CARGO_TAGS


def build_node_activation(extraction_list, resource_list, item_by_id):
    """
    Build {activated_resource_id_str -> {verb, source_name, items, ...}}

    Some fishing nodes must be activated before they can be fished. A 1-HP
    node (Hidden Spot, Dormant School, ocean School) has an extraction recipe
    that consumes items but extracts nothing (verb "Bait" or "Chum"); one
    action destroys it and spawns on_destroy_yield_resource_id, the node you
    actually fish.

    Only Fishing activations are included. Other skills use the same pattern
    (Sparkstone lava, beehive smoker, vault locks) with multi-stage mechanics
    this model hasn't been checked against.

    name:         the activated node (e.g. Baited Briny Argus Spot)
    items:        [{item_id, item_name, quantity}] consumed per activation
    stamina/time: for the activation action
    duration_sec: how long the activated node stays fishable, parsed from the
                  consumed item's description, or None if it lasts until depleted
    """
    res_by_id = {r['id']: r for r in resource_list}
    by_resource = {}
    for r in extraction_list:
        if r.get('extracted_item_stacks') or not r.get('consumed_item_stacks'):
            continue
        if not any(l.get('skill_id') == FISHING_SKILL_ID for l in r.get('level_requirements', [])):
            continue
        src = res_by_id.get(r.get('resource_id')) or {}
        target = src.get('on_destroy_yield_resource_id') or 0
        if not target:
            continue

        consumed = r['consumed_item_stacks']
        durations = [
            int(m.group(1)) * 60
            for c in consumed
            for m in [_DURATION_RE.search(item_by_id.get(c['item_id'], {}).get('description', ''))]
            if m
        ]
        by_resource[str(target)] = {
            'name':               res_by_id.get(target, {}).get('name', str(target)),
            'verb':               r.get('verb_phrase', ''),
            'source_resource_id': str(src['id']),
            'source_name':        src.get('name', ''),
            'items': [
                {
                    'item_id':   str(c['item_id']),
                    'item_name': item_by_id.get(c['item_id'], {}).get('name', str(c['item_id'])),
                    'quantity':  c.get('quantity', 1) * c.get('consumption_chance', 1.0),
                }
                for c in consumed
            ],
            'stamina':            r.get('stamina_requirement', 0.0),
            'time':               r.get('time_requirement', 1.6),
            'tool_requirements': [
                {'tool_type': t.get('tool_type'), 'level': t.get('level', 1), 'power': t.get('power', 1)}
                for t in r.get('tool_requirements', [])
            ],
            'level_requirements': [
                {'skill_id': l.get('skill_id'), 'level': l.get('level', 1)}
                for l in r.get('level_requirements', [])
            ],
            'duration_sec':       min(durations) if durations else None,
        }
    return by_resource


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

    removed = {r['id'] for r in resource_list if r.get('tag') in SKIPPED_RESOURCE_TAGS}
    extraction_list = [r for r in extraction_list if r.get('resource_id') not in removed]
    print(f'  Dropped {len(removed)} removed or unfarmable nodes ({", ".join(sorted(SKIPPED_RESOURCE_TAGS))}).')

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

    print('Building node_activation ...')
    node_activation = build_node_activation(extraction_list, resource_list, item_by_id)
    print(f'  {len(node_activation)} nodes that need bait/chum before gathering.')

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

    print('Building item_to_item_crafting ...')
    item_to_item_crafting = build_item_to_item_crafting(
        crafting_list, item_by_id, item_list_by_id, cargo_by_id
    )
    print(f'  {len(item_to_item_crafting)} unique items via item-to-item crafting.')

    # Build a name lookup for all known items (for chain_calc label resolution)
    item_names = {str(item['id']): item['name'] for item in item_list_raw if item.get('name')}
    cargo_names = {str(cargo['id']): cargo['name'] for cargo in cargo_list if cargo.get('name')}

    output = {
        'extraction_by_item':    extraction_by_item,
        'cargo_by_item':         cargo_by_item,
        'item_chain_by_item':    item_chain_by_item,
        'item_to_item_crafting': item_to_item_crafting,
        'resource_max_health':   resource_max_health,
        'node_activation':       node_activation,
        'cargo_extraction':      cargo_extraction,
        'item_names':            item_names,
        'cargo_names':           cargo_names,
        '__meta__': {
            'built_at':              time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
            'extraction_items':      len(extraction_by_item),
            'resources':             len(resource_max_health),
            'activated_nodes':       len(node_activation),
            'cargo_types':           len(cargo_extraction),
            'cargo_items':           len(cargo_by_item),
            'item_chain_items':      len(item_chain_by_item),
            'item_to_item_crafting': len(item_to_item_crafting),
        },
    }

    OUT_FILE.write_text(json.dumps(output))

    print(f'\n{"-"*50}')
    print(f'Extraction item entries   : {len(extraction_by_item)}')
    print(f'Resource nodes            : {len(resource_max_health)}')
    print(f'Bait/chum activated nodes : {len(node_activation)}')
    print(f'Cargo extraction types    : {len(cargo_extraction)}')
    print(f'Items via cargo process   : {len(cargo_by_item)}')
    print(f'Items via item chains     : {len(item_chain_by_item)}')
    print(f'Items via item-to-item craft: {len(item_to_item_crafting)}')
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
