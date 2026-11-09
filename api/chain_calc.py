"""
GET /api/chain_calc?item_id=X&quantity=Y&gather_speed=W&craft_speed=V&rod_power=Z&pick_power=A&...[&market=0]

Each profession tool takes a <tool>_power param (see TOOL_PARAM_MAP); missing ones default to 1.

Returns all extraction/cargo-chain methods for producing a given item, with
stamina, time and cast/action breakdowns per method, plus each method's
products (target + processing co-products) valued at live BitJita prices.

gather_speed  multiplier applied to fishing/extraction time (e.g. 1.05 = +5%)
craft_speed   multiplier applied to crafting/processing time (separate stat)
sailing_level, ship_speed (Ocean, raw units/s), sail_region (id, blank = fastest)
              add a sailing cargo run when a box can hold the item
"""

import json
import math
import re
import sys
import os
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

sys.path.insert(0, os.path.dirname(__file__))
from _lib import load_game_data, load_recipes_cache, load_sailing_data, cors_headers
from _effort_calc import activation_yield
from _market import (SALES_WINDOWS, all_recent_sales, order_books,
                     sell_capped, sell_instant, sell_listed)


# tool_type int (tool_type_desc.json) -> param name, one per profession tool
TOOL_PARAM_MAP = {
    1:  'axe_power',       # Forestry
    2:  'saw_power',       # Carpentry
    3:  'chisel_power',    # Masonry
    4:  'pick_power',      # Mining
    5:  'hammer_power',    # Smithing
    6:  'knife_power',     # Leatherworking
    7:  'bow_power',       # Hunting
    8:  'scissors_power',  # Tailoring
    9:  'hoe_power',       # Farming
    10: 'rod_power',       # Fishing
    11: 'pot_power',       # Cooking
    12: 'machete_power',   # Foraging
    13: 'quill_power',     # Scholar
    14: 'mallet_power',    # Construction
}

# Fishing node resource_id patterns -> human label
_NODE_PATTERNS = [
    (re.compile(r'^(\d)110000$'), 'T{tier} Lake (small)'),
    (re.compile(r'^(\d)110001$'), 'T{tier} Lake (large/bait)'),
    (re.compile(r'^(\d)110003$'), 'T{tier} Ocean'),
    (re.compile(r'^(\d)110004$'), 'T{tier} Tiny Lake (bait)'),
]


def node_label(node_id, game_data=None):
    nid = str(node_id)
    for pattern, tmpl in _NODE_PATTERNS:
        m = pattern.match(nid)
        if m:
            return tmpl.replace('{tier}', m.group(1))
    activation = (game_data or {}).get('node_activation', {}).get(nid)
    if activation:
        return activation['name']
    return f'Node {nid}'


def item_name(item_id, recipes, game_data=None):
    """Look up human-readable name from recipes cache, falling back to game_data item_names."""
    sid = str(item_id)
    entry = recipes.get(sid)
    if entry:
        return entry.get('name', sid)
    if game_data:
        name = game_data.get('item_names', {}).get(sid)
        if name:
            return name
    return sid


def _tool_power(tool_reqs, tool_powers):
    """Return tool power for the first tool requirement, default 1."""
    tool_type = tool_reqs[0]['tool_type'] if tool_reqs else None
    return tool_powers.get(tool_type, 1) if tool_type is not None else 1


def _yield_per_cast(entry, power, game_data):
    """Items one cast yields: each cast removes `power` HP, but never more than the node has."""
    hp = game_data.get('resource_max_health', {}).get(str(entry.get('resource_id')))
    return entry['prob_per_hp'] * (min(power, hp) if hp else power)


# extraction_by_item also holds wrapper-resolved cargo under its cargo id, and
# cargo ids overlap item ids (cargo 3002 Emarium Ore Chunk, item 3002 Ancient
# Journal Page #3), so every lookup keeps only the entries for the kind it wants.
def _item_sources(ebi, item_id):
    """Nodes that drop the item itself."""
    return [e for e in ebi.get(item_id, []) if not e.get('cargo_input_id')]


def _cargo_sources(ebi, cex, cargo_id):
    """Nodes that drop the cargo, directly (cargo_extraction) or via a wrapper item."""
    return [e for e in ebi.get(cargo_id, []) if e.get('cargo_input_id') == cargo_id] + cex.get(cargo_id, [])


def compute_bait_cost(bait_id, consumption_chance, total_fish_casts,
                      tool_powers, gather_speed, craft_speed, game_data, recipes):
    """
    Compute the extra cost of producing bait consumed during total_fish_casts casts.
    Uses the first small-fish→bait recipe found in item_chain_by_item that has
    no circular dependency (i.e. bait is not also a required input).

    Returns (extra_steps, extra_stamina, extra_time_sec).
    """
    bait_needed = total_fish_casts * consumption_chance
    icbi = game_data.get('item_chain_by_item', {})
    ebi  = game_data.get('extraction_by_item', {})

    for bait_recipe in icbi.get(str(bait_id), []):
        # Skip circular recipes (e.g. Enrich: bait + Hexmoth → bait)
        if any(c['item_id'] == str(bait_id)
               for c in bait_recipe.get('other_consumed', [])):
            continue

        bait_per_run = bait_recipe['output_quantity']
        if bait_per_run <= 0:
            continue

        bait_fish_id   = str(bait_recipe['input_item_id'])
        bait_fish_name = bait_recipe.get('input_item_name', bait_fish_id)
        craft_runs     = bait_needed / bait_per_run
        total_work     = craft_runs * bait_recipe['actions_required']
        craft_power    = _tool_power(bait_recipe.get('tool_requirements', []), tool_powers)
        craft_attempts = total_work / craft_power
        craft_stamina  = craft_attempts * bait_recipe.get('stamina_per_action', 0.0)
        craft_time     = craft_attempts * bait_recipe.get('time_per_action', 1.6) / craft_speed

        for fe in _item_sources(ebi, bait_fish_id):
            power         = _tool_power(fe.get('tool_requirements', []), tool_powers)
            prob          = _yield_per_cast(fe, power, game_data)
            if prob <= 0:
                continue
            fish_casts    = craft_runs / prob
            fish_stamina  = fish_casts * fe['stamina_per_cast']
            fish_time     = fish_casts * fe['time_per_cast'] / gather_speed

            return (
                [
                    {
                        'type':     'bait_fish',
                        'label':    f'Catch {bait_fish_name} (for bait)',
                        'qty_out':  craft_runs,
                        'qty_label': bait_fish_name,
                        'casts':    fish_casts,
                        'stamina':  fish_stamina,
                        'time_sec': fish_time,
                    },
                    {
                        'type':     'bait_craft',
                        'label':    f'Process {bait_fish_name} → {item_name(bait_id, recipes, game_data)}',
                        'qty_out':  bait_needed,
                        'qty_label': item_name(bait_id, recipes, game_data),
                        'actions':  craft_attempts,
                        'stamina':  craft_stamina,
                        'time_sec': craft_time,
                    },
                ],
                fish_stamina + craft_stamina,
                fish_time    + craft_time,
            )
    return [], 0.0, 0.0


def compute_activation_cost(fe, items_needed, power, tool_powers, gather_speed, craft_speed,
                            game_data, recipes, visited=frozenset()):
    """
    Compute the bait/chum cost of activating enough of fe's nodes to gather
    items_needed (Hidden Spot / Dormant School baited, ocean School chummed —
    see node_activation in build_game_data.py). The bait/chum itself is
    resolved through resolve_item_crafting_chain.

    Returns (extra_steps, extra_stamina, extra_time_sec, external_ingredients).
    """
    rid = str(fe['resource_id'])
    activation = game_data.get('node_activation', {}).get(rid)
    if not activation:
        return [], 0.0, 0.0, []
    per_node = activation_yield(
        activation, game_data.get('resource_max_health', {}).get(rid),
        fe['prob_per_hp'], power, fe['time_per_cast'], gather_speed,
    )
    if per_node <= 0:
        return [], 0.0, 0.0, []

    nodes = items_needed / per_node
    kind  = 'chum' if activation['verb'] == 'Chum' else 'bait'
    act_stamina = activation['stamina'] * nodes
    act_time    = activation['time'] * nodes / gather_speed

    extra_steps, extra_stamina, extra_time, external = [], act_stamina, act_time, []
    for it in activation['items']:
        qty = it['quantity'] * nodes
        sub = resolve_item_crafting_chain(
            it['item_id'], qty, tool_powers, gather_speed, craft_speed,
            game_data, recipes, visited
        )
        if sub is None:
            external.append({'item_id': str(it['item_id']), 'item_name': it['item_name'], 'quantity': qty})
            continue
        sub_steps, sub_stam, sub_time, sub_ext = sub
        # Tag as bait_/chum_ steps so the page groups them under the activation
        extra_steps += [
            dict(s, type=f'{kind}_fish' if s['type'] in ('extract', 'fish') or s['type'].endswith('_fish')
                 else f'{kind}_craft')
            for s in sub_steps
        ]
        extra_stamina += sub_stam
        extra_time    += sub_time
        external      += sub_ext

    window = activation.get('duration_sec')
    extra_steps.append({
        'type':      f'{kind}_craft',
        'label':     f"{activation['verb']} {activation['source_name']}"
                     + (f' (open {window // 60} min, ~{per_node:.1f} per node solo)' if window else ''),
        'qty_out':   nodes,
        'qty_label': 'nodes',
        'actions':   nodes,
        'stamina':   act_stamina,
        'time_sec':  act_time,
    })
    return extra_steps, extra_stamina, extra_time, external


def _loop_recipe(sid, recipe):
    """Enrich (consumes its own output) or Unpack (consumes a market package)."""
    return any(c['item_id'] == sid or c.get('item_type') == 'Cargo' and 'Unpack' in recipe.get('recipe_name', '')
               for c in recipe.get('consumed', []))


def _recipe_unit_time(recipe, times, tool_powers, craft_speed):
    """Seconds per unit of recipe's output, its inputs costed from times (inf if one can't be made)."""
    out_qty = recipe.get('output_quantity', 1)
    if out_qty <= 0:
        return math.inf
    passive = recipe.get('stamina_per_action', 0) == 0 and not recipe.get('tool_requirements')
    craft = 0.0 if passive else (recipe.get('actions_required', 1)
                                 / _tool_power(recipe.get('tool_requirements', []), tool_powers)
                                 * recipe.get('time_per_action', 1.6) / craft_speed)
    return (craft + sum(c['quantity'] * times.get(str(c['item_id']), math.inf)
                        for c in recipe.get('consumed', []))) / out_qty


_unit_times_cache = {'key': None, 'times': {}}


def unit_times(tool_powers, gather_speed, craft_speed, game_data):
    """
    {item_id: rough seconds to make one}, made the way resolve_item_crafting_chain
    makes it (extract, else unpack cargo, else craft with the quickest recipe), so it
    can try recipes quickest first instead of resolving every one: chains branch at
    each level, and resolving them all takes minutes. Bait/chum isn't counted.
    """
    key = (id(game_data), tuple(sorted(tool_powers.items())), gather_speed, craft_speed)
    cache = _unit_times_cache
    if cache['key'] == key:
        return cache['times']

    i2i = game_data.get('item_to_item_crafting', {})
    cbi = game_data.get('cargo_by_item', {})
    ebi = game_data.get('extraction_by_item', {})
    cex = game_data.get('cargo_extraction', {})

    def gather_time(fe):
        prob = _yield_per_cast(fe, _tool_power(fe.get('tool_requirements', []), tool_powers), game_data)
        return fe['time_per_cast'] / gather_speed / prob if prob > 0 else None

    times = {}
    for sid in ebi:
        direct = _item_sources(ebi, sid)
        t = direct and gather_time(max(direct, key=lambda e: e['prob_per_hp']))
        if t:
            times[sid] = t
    for sid, entries in cbi.items():
        ce = entries[0]
        if sid in times or ce['output_quantity'] <= 0:
            continue
        for fe in _cargo_sources(ebi, cex, str(ce['cargo_input_id'])):
            t = gather_time(fe)
            if t:
                unpack = (ce['actions_required'] / _tool_power(ce.get('tool_requirements', []), tool_powers)
                          * ce.get('time_per_action', 1.6) / craft_speed)
                times[sid] = (ce.get('cargo_input_qty', 1) * t + unpack) / ce['output_quantity']
                break

    # Crafted items: relax until no recipe gets an item 0.1% quicker. Productive
    # loops (seeds → plant → more seeds) shrink a little each pass, hence the cap.
    crafted = [(sid, r) for sid, rs in i2i.items() if sid not in times
               for r in rs if not _loop_recipe(sid, r)]
    for _ in range(50):
        changed = False
        for sid, r in crafted:
            t = _recipe_unit_time(r, times, tool_powers, craft_speed)
            if t < times.get(sid, math.inf) * 0.999:
                times[sid] = t
                changed = True
        if not changed:
            break
    cache.update(key=key, times=times)
    return times


def resolve_item_crafting_chain(item_id, quantity, tool_powers, gather_speed, craft_speed,
                               game_data, recipes, visited=None, demand=None):
    """
    Recursively resolve a multi-step production chain for item_id.
    Returns (steps, total_stamina, total_time_sec, external_ingredients) or None if unresolvable.
    demand: {item_id: quantity} requested by each crafting level up the chain.

    Resolution order:
      1. Direct extraction (non-cargo ebi entry) → extract step
      2. Cargo unpack chain (cbi entry) → mine + unpack steps
      3. Item-to-item crafting (i2i) → recurse on inputs, append craft step
         (recipes tried quickest first, by unit_times)
      None → caller treats as external ingredient
    """
    if visited is None:
        visited = frozenset()
    if item_id in visited:
        return None  # cycle guard

    i2i = game_data.get('item_to_item_crafting', {})
    cbi = game_data.get('cargo_by_item', {})
    ebi = game_data.get('extraction_by_item', {})
    cex = game_data.get('cargo_extraction', {})

    sid = str(item_id)

    # ── Path 1: directly extractable item ────────────────────────────────────
    direct = _item_sources(ebi, sid)
    if direct:
        # Highest-yield source (e.g. Baited Spot over Baited School)
        fe = max(direct, key=lambda e: e['prob_per_hp'])
        power = _tool_power(fe.get('tool_requirements', []), tool_powers)
        prob  = _yield_per_cast(fe, power, game_data)
        if prob > 0:
            casts   = quantity / prob
            stamina = casts * fe['stamina_per_cast']
            time_s  = casts * fe['time_per_cast'] / gather_speed
            iname   = item_name(sid, recipes, game_data)
            tt      = fe.get('tool_requirements', [{}])[0].get('tool_type') if fe.get('tool_requirements') else None
            verb    = 'Chop' if tt == 1 else 'Mine' if tt == 4 else 'Extract'
            a_steps, a_stam, a_time, a_ext = compute_activation_cost(
                fe, quantity, power, tool_powers, gather_speed, craft_speed,
                game_data, recipes, visited | {sid}
            )
            return (
                a_steps + [{
                    'type':     'extract',
                    'label':    f'{verb} {iname}',
                    'qty_out':  quantity,
                    'qty_label': iname,
                    'casts':    casts,
                    'stamina':  stamina,
                    'time_sec': time_s,
                }],
                stamina + a_stam, time_s + a_time, a_ext
            )

    # ── Path 2: from cargo unpack (e.g. Ore Chunk → Ore Piece) ───────────────
    if sid in cbi:
        ce            = cbi[sid][0]
        cargo_id      = str(ce['cargo_input_id'])
        out_qty       = ce['output_quantity']
        cargo_input_qty = ce.get('cargo_input_qty', 1)
        if out_qty > 0:
            craft_runs    = quantity / out_qty
            cargo_needed  = craft_runs * cargo_input_qty
            total_work    = craft_runs * ce['actions_required']
            proc_power    = _tool_power(ce.get('tool_requirements', []), tool_powers)
            proc_attempts = total_work / proc_power
            proc_stamina  = proc_attempts * ce.get('stamina_per_action', 0.0)
            proc_time     = proc_attempts * ce.get('time_per_action', 1.6) / craft_speed

            for fe in _cargo_sources(ebi, cex, cargo_id):
                power = _tool_power(fe.get('tool_requirements', []), tool_powers)
                prob  = _yield_per_cast(fe, power, game_data)
                if prob <= 0:
                    continue
                casts       = cargo_needed / prob
                mine_stam   = casts * fe['stamina_per_cast']
                mine_time   = casts * fe['time_per_cast'] / gather_speed
                cargo_name  = ce.get('cargo_input_name', cargo_id)
                iname_out   = item_name(sid, recipes, game_data)
                tt          = fe.get('tool_requirements', [{}])[0].get('tool_type') if fe.get('tool_requirements') else None
                verb        = 'Chop' if tt == 1 else 'Mine' if tt == 4 else 'Extract'
                a_steps, a_stam, a_time, a_ext = compute_activation_cost(
                    fe, cargo_needed, power, tool_powers, gather_speed, craft_speed,
                    game_data, recipes, visited | {sid}
                )
                return (
                    a_steps + [
                        {
                            'type':     'extract',
                            'label':    f'{verb} {cargo_name}',
                            'qty_out':  cargo_needed,
                            'qty_label': cargo_name,
                            'casts':    casts,
                            'stamina':  mine_stam,
                            'time_sec': mine_time,
                        },
                        {
                            'type':     'process',
                            'label':    f'Extract {cargo_name} \u2192 {iname_out}',
                            'qty_out':  quantity,
                            'qty_label': iname_out,
                            'actions':  proc_attempts,
                            'stamina':  proc_stamina,
                            'time_sec': proc_time,
                        },
                    ],
                    mine_stam + proc_stamina + a_stam,
                    mine_time + proc_time + a_time,
                    a_ext
                )

    # ── Path 3: item-to-item crafting ─────────────────────────────────────────
    if sid in i2i:
        new_visited = visited | {sid}
        new_demand  = {**(demand or {}), sid: quantity}
        # Prefer real recipes over Enrich (consumes its own output) and
        # Unpack (consumes a market package) when both exist, then the quickest:
        # many items have several (Chemical Base from any tier of catalyst, or Tree Sap)
        def _is_loop(r):
            return _loop_recipe(sid, r)
        times = unit_times(tool_powers, gather_speed, craft_speed, game_data)
        fallback = None
        for recipe in sorted(i2i[sid], key=lambda r: (_is_loop(r), _recipe_unit_time(r, times, tool_powers, craft_speed))):
            out_qty = recipe.get('output_quantity', 1)
            if out_qty <= 0:
                continue

            craft_runs     = quantity / out_qty
            total_work     = craft_runs * recipe.get('actions_required', 1)
            craft_power    = _tool_power(recipe.get('tool_requirements', []), tool_powers)
            craft_attempts = total_work / craft_power
            craft_stamina  = craft_attempts * recipe.get('stamina_per_action', 0.0)
            # Passive recipes (no stamina, no tool — e.g. furnace smelt) take real-world time
            # but don't consume player time, so exclude from total.
            is_passive  = (recipe.get('stamina_per_action', 0) == 0
                           and not recipe.get('tool_requirements'))
            craft_time  = 0.0 if is_passive else craft_attempts * recipe.get('time_per_action', 1.6) / craft_speed

            pre_steps   = []
            pre_stamina = 0.0
            pre_time    = 0.0
            external    = []

            for consumed in recipe.get('consumed', []):
                c_id         = consumed['item_id']
                c_qty_needed = craft_runs * consumed['quantity']
                c_name       = consumed.get('item_name', c_id)

                sub = resolve_item_crafting_chain(
                    c_id, c_qty_needed, tool_powers, gather_speed, craft_speed,
                    game_data, recipes, new_visited, new_demand
                )
                if sub is None:
                    external.append({'item_id': str(c_id), 'item_name': c_name, 'quantity': c_qty_needed})
                else:
                    sub_steps, sub_stam, sub_time, sub_ext = sub
                    pre_steps   += sub_steps
                    pre_stamina += sub_stam
                    pre_time    += sub_time
                    external    += sub_ext

            iname_out  = item_name(sid, recipes, game_data)
            craft_step = {
                'type':     'process',
                'label':    f'Craft {iname_out}',
                'qty_out':  quantity,
                'qty_label': iname_out,
                'actions':  craft_attempts,
                'stamina':  craft_stamina,
                'time_sec': craft_time,
            }
            result = (
                pre_steps + [craft_step],
                pre_stamina + craft_stamina,
                pre_time + craft_time,
                external
            )
            # A recipe whose inputs lead back to an item already up the chain
            # returns some of that item as "external". A fraction of what that
            # level makes is a productive loop (seeds → plant → more seeds), but
            # needing at least as much as it makes can't pay for itself (Split
            # Refined Rough Leather to get Rough Leather for Refined Rough
            # Leather) — try the next recipe. Enrich/Unpack are only a fallback.
            starved = any(x.get('item_id') in new_demand and x['quantity'] >= new_demand[x['item_id']]
                          for x in external)
            if not _is_loop(recipe) and not starved:
                return result
            fallback = fallback or result
        return fallback

    return None


_recipe_outputs_cache = {'game_data': None, 'outputs': {}}


def recipe_outputs(game_data):
    """
    {(table, recipe_id, input_id): {output_id: qty per run}} for every processing
    recipe in cargo_by_item / item_chain_by_item. Processing a fish yields a
    Products box, so one run gives filets, scales, ... alongside the oil.
    """
    cache = _recipe_outputs_cache
    if cache['game_data'] is not game_data:
        outputs = {}
        for table, input_key in (('cargo_by_item', 'cargo_input_id'), ('item_chain_by_item', 'input_item_id')):
            for out_id, entries in game_data.get(table, {}).items():
                for e in entries:
                    if e.get('recipe_id') is None:
                        continue
                    per_run = outputs.setdefault((table, e['recipe_id'], str(e[input_key])), {})
                    per_run[out_id] = per_run.get(out_id, 0) + e['output_quantity']
        cache.update(game_data=game_data, outputs=outputs)
    return cache['outputs']


def _products(table, entry, input_id, runs, item_id, quantity, game_data):
    """Everything `runs` runs of entry's processing recipe yield (just item_id if unknown)."""
    per_run = recipe_outputs(game_data).get((table, entry.get('recipe_id'), input_id))
    if not per_run:
        return {str(item_id): quantity}
    return {out_id: qty * runs for out_id, qty in per_run.items()}


def sea_route(resource_id, nodes, sailing):
    """
    Sailing between nodes that stand at sea (sailing_cargo.json sea_nodes: Salt
    Deposits sit in shallow sea, so you farm them from a ship). Returns ([sail step],
    seconds) for visiting `nodes` of them nearest-first in sailing['region'], or the
    region with the shortest hops; ([], 0.0) without a ship or for any other node.
    """
    data = load_sailing_data()
    sea = (data or {}).get('sea_nodes', {}).get(str(resource_id))
    if not sea or not sailing or sailing['ship_speed'] <= 0 or not nodes:
        return [], 0.0
    regions = {rid: r for rid, r in sea['regions'].items() if not sailing['region'] or rid == sailing['region']}
    if not regions:
        return [], 0.0
    rid, route = min(regions.items(), key=lambda kv: kv[1]['avg_hop'])
    sail_time = nodes * route['avg_hop'] / sailing['ship_speed']
    region = data['regions'].get(rid, {}).get('name', 'Region')
    return [{
        'type':      'sail',
        'label':     f"Sail to {nodes:,.0f} {sea['name']}s in {region} (R{rid}), "
                     f"{route['avg_hop'] / 3:.0f} tiles apart on average",
        'qty_out':   nodes,
        'qty_label': 'nodes',
        'stamina':   0.0,
        'time_sec':  sail_time,
    }], sail_time


def resolve_sailing_method(item_id, quantity, sailing, gather_speed, craft_speed, recipes, game_data):
    """
    Sailing cargo run (Method S): sail nearest-node-first between Lost Wreckage /
    Shipment / Treasure, open each for one box, then unpack the boxes.

    sailing = {'level': Sailing level, 'ship_speed': Ocean speed (raw units/s),
               'region': region id, or '' for whichever region is fastest}.
    The run takes every node type the level allows, in the share it has on the
    region's route (sailing_cargo.json), and every box picked up is unpacked, so
    all their contents are products. Wanting the boxes themselves skips unpacking.
    """
    data = load_sailing_data()
    if not data or not sailing or sailing['ship_speed'] <= 0:
        return None
    sid = str(item_id)
    types = {t['key']: t for t in data['node_types']}
    want_boxes = any(t['box_item_id'] == sid for t in types.values())

    best = None
    for rid, region in data['regions'].items():
        if sailing['region'] and rid != sailing['region']:
            continue
        brackets = [b for b in region['brackets'] if b['min_level'] <= sailing['level']]
        if not brackets:
            continue
        route = brackets[-1]
        share = route['share']
        if want_boxes:
            per_node = share.get(next(k for k, t in types.items() if t['box_item_id'] == sid), 0)
        else:
            per_node = sum(s * types[k]['unpack']['outputs'].get(sid, 0) for k, s in share.items())
        if per_node <= 0:
            continue

        nodes      = quantity / per_node
        boxes      = {k: nodes * s for k, s in share.items()}
        sail_time  = nodes * route['avg_hop'] / sailing['ship_speed']
        open_time  = sum(n * types[k]['open_time'] for k, n in boxes.items()) / gather_speed
        open_stam  = sum(n * types[k]['open_stamina'] for k, n in boxes.items())
        if want_boxes:
            unpack_actions = unpack_time = unpack_stam = 0.0
            products = {types[k]['box_item_id']: n for k, n in boxes.items()}
        else:
            unpack_actions = sum(n * types[k]['unpack']['actions'] for k, n in boxes.items())
            unpack_time = sum(n * types[k]['unpack']['actions'] * types[k]['unpack']['time_per_action']
                              for k, n in boxes.items()) / craft_speed
            unpack_stam = sum(n * types[k]['unpack']['actions'] * types[k]['unpack']['stamina_per_action']
                              for k, n in boxes.items())
            products = {}
            for k, n in boxes.items():
                for out_id, qty in types[k]['unpack']['outputs'].items():
                    products[out_id] = products.get(out_id, 0) + n * qty

        total_time = sail_time + open_time + unpack_time
        if best and best['total_time_seconds'] <= total_time:
            continue

        mix   = ', '.join(f"{types[k]['name']} {s:.0%}" for k, s in share.items())
        label = f"{region['name']} (R{rid})"
        steps = [
            {
                'type':     'sail',
                'label':    f"Sail to {nodes:,.0f} nodes in {label}, "
                            f"{route['avg_hop'] / 3:.0f} tiles apart on average ({mix})",
                'qty_out':  nodes,
                'qty_label': 'nodes',
                'stamina':  0.0,
                'time_sec': sail_time,
            },
            {
                'type':     'extract',
                'label':    'Open ' + ', '.join(f"{n:,.0f} {types[k]['name']}" for k, n in boxes.items()),
                'qty_out':  nodes,
                'qty_label': 'boxes',
                'casts':    nodes,
                'stamina':  open_stam,
                'time_sec': open_time,
            },
        ]
        if not want_boxes:
            steps.append({
                'type':     'process',
                'label':    f'Unpack {nodes:,.0f} boxes',
                'qty_out':  quantity,
                'qty_label': item_name(sid, recipes, game_data),
                'actions':  unpack_actions,
                'stamina':  unpack_stam,
                'time_sec': unpack_time,
            })
        best = {
            'method_name':          f"Sailing cargo — {label} → {item_name(sid, recipes, game_data)}",
            'source_node':          None,
            'node_label':           label,
            'fish_name':            None,
            'steps':                steps,
            'total_stamina':        open_stam + unpack_stam,
            'total_time_seconds':   total_time,
            'total_casts':          nodes,
            'total_actions':        unpack_actions,
            'items_per_full_node':  None,
            'external_ingredients': {},
            'products':             products,
        }
    return best


def resolve_all_methods(item_id, quantity, tool_powers, gather_speed, craft_speed, game_data, recipes,
                        sailing=None):
    """
    Return a list of method dicts for producing `quantity` of `item_id`.

    Each method dict:
      method_name, source_node, node_label, fish_name,
      steps, total_stamina, total_time_seconds, total_casts, total_actions,
      items_per_full_node, external_ingredients,
      products ({item_id: qty} — the target plus processing co-products)
    """
    methods = []
    ebi  = game_data.get('extraction_by_item', {})
    cbi  = game_data.get('cargo_by_item', {})
    cex  = game_data.get('cargo_extraction', {})
    icbi = game_data.get('item_chain_by_item', {})
    rmh = game_data.get('resource_max_health', {})

    sid = str(item_id)

    # ── Method A: Direct extraction ───────────────────────────────────────────
    for e in _item_sources(ebi, sid):
        tool_reqs = e.get('tool_requirements', [])
        tool_type = tool_reqs[0]['tool_type'] if tool_reqs else None
        power = tool_powers.get(tool_type, 1) if tool_type is not None else 1

        prob = _yield_per_cast(e, power, game_data)
        if prob <= 0:
            continue

        casts_per_item = 1.0 / prob
        total_casts = quantity * casts_per_item
        stamina = total_casts * e['stamina_per_cast']
        time_sec = total_casts * e['time_per_cast'] / gather_speed

        rid = str(e['resource_id'])
        node_health = rmh.get(rid, 0)
        # items per full node = base prob only (tool power speeds up but doesn't change total yield)
        items_per_node = float(node_health) * e['prob_per_hp'] if node_health else None

        label = node_label(rid, game_data)
        iname = item_name(item_id, recipes, game_data)

        act_steps, act_stamina, act_time, act_external = compute_activation_cost(
            e, quantity, power, tool_powers, gather_speed, craft_speed, game_data, recipes
        )
        sail_steps, sail_time = sea_route(rid, quantity / items_per_node if items_per_node else 0, sailing)

        methods.append({
            'method_name': f'Direct — {label}',
            'source_node': rid,
            'node_label':  label,
            'fish_name':   None,
            'steps': act_steps + sail_steps + [{
                'type':     'extract',
                'label':    f'Extract {iname}',
                'qty_out':  quantity,
                'qty_label': iname,
                'casts':    total_casts,
                'stamina':  stamina,
                'time_sec': time_sec,
            }],
            'total_stamina':       stamina + act_stamina,
            'total_time_seconds':  time_sec + act_time + sail_time,
            'total_casts':         total_casts,
            'total_actions':       0.0,
            'items_per_full_node': items_per_node,
            'external_ingredients': act_external,
            'products':            {sid: quantity},
        })

    # ── Method B: Cargo chain ─────────────────────────────────────────────────
    for ce in cbi.get(sid, []):
        fish_id = str(ce['cargo_input_id'])
        oil_per_fish = ce['output_quantity']
        if oil_per_fish <= 0:
            continue

        fish_needed   = quantity / oil_per_fish
        total_work    = fish_needed * ce['actions_required']
        proc_power    = _tool_power(ce.get('tool_requirements', []), tool_powers)
        proc_attempts = total_work / proc_power
        proc_stamina  = proc_attempts * ce.get('stamina_per_action', 0.75)
        proc_time     = proc_attempts * ce.get('time_per_action', 1.6) / craft_speed
        cargo_name    = ce.get('cargo_input_name', item_name(fish_id, recipes, game_data))

        fish_sources = _cargo_sources(ebi, cex, fish_id)
        for fe in fish_sources:
            f_tool_reqs = fe.get('tool_requirements', [])
            f_tool_type = f_tool_reqs[0]['tool_type'] if f_tool_reqs else None
            f_power = tool_powers.get(f_tool_type, 1) if f_tool_type is not None else 1

            f_prob = _yield_per_cast(fe, f_power, game_data)
            if f_prob <= 0:
                continue

            fish_casts_per_unit = 1.0 / f_prob
            total_fish_casts = fish_needed * fish_casts_per_unit
            fish_stamina = total_fish_casts * fe['stamina_per_cast']
            fish_time    = total_fish_casts * fe['time_per_cast'] / gather_speed

            rid = str(fe['resource_id'])
            node_health = rmh.get(rid, 0)
            # fish per full node = base prob only
            fish_per_node = float(node_health) * fe['prob_per_hp'] if node_health else None
            # output items from one full node depletion
            items_per_node = fish_per_node * oil_per_fish if fish_per_node else None

            label = node_label(rid, game_data)

            # Chum cost for ocean nodes (any tier with a node_activation entry)
            chum_steps, chum_stamina, chum_time, chum_external = compute_activation_cost(
                fe, fish_needed, f_power, tool_powers, gather_speed, craft_speed, game_data, recipes
            )

            total_stamina = fish_stamina + proc_stamina + chum_stamina
            total_time    = fish_time    + proc_time    + chum_time

            fish_step = {
                'type':     'fish',
                'label':    f'Catch {cargo_name}',
                'qty_out':  fish_needed,
                'qty_label': cargo_name,
                'casts':    total_fish_casts,
                'stamina':  fish_stamina,
                'time_sec': fish_time,
            }
            iname_out = item_name(item_id, recipes, game_data)
            proc_step = {
                'type':     'process',
                'label':    f'Process {cargo_name} → {iname_out}',
                'qty_out':  quantity,
                'qty_label': iname_out,
                'actions':  proc_attempts,
                'stamina':  proc_stamina,
                'time_sec': proc_time,
            }

            methods.append({
                'method_name':         f'Cargo — {cargo_name} → {item_name(item_id, recipes, game_data)} via {label}',
                'source_node':         rid,
                'node_label':          label,
                'fish_name':           cargo_name,
                'steps':               chum_steps + [fish_step, proc_step],
                'total_stamina':       total_stamina,
                'total_time_seconds':  total_time,
                'total_casts':         total_fish_casts,
                'total_actions':       proc_attempts,
                'items_per_full_node': items_per_node,
                'external_ingredients': chum_external,
                'products': _products('cargo_by_item', ce, fish_id, fish_needed, sid, quantity, game_data),
            })

    # ── Method C: Item chain (extract item → craft → output) ─────────────────
    # e.g. lake fish (Item) → process → oil/filet/products
    for ic in icbi.get(sid, []):
        input_id = str(ic['input_item_id'])
        out_per_run = ic['output_quantity']
        if out_per_run <= 0:
            continue

        runs          = quantity / out_per_run
        fish_needed   = runs * ic.get('input_item_qty', 1)
        oil_per_fish  = out_per_run / ic.get('input_item_qty', 1)
        total_work    = runs * ic['actions_required']
        proc_power    = _tool_power(ic.get('tool_requirements', []), tool_powers)
        proc_attempts = total_work / proc_power
        proc_stamina  = proc_attempts * ic.get('stamina_per_action', 0.0)
        proc_time     = proc_attempts * ic.get('time_per_action', 1.6) / craft_speed
        input_name    = ic.get('input_item_name', item_name(input_id, recipes, game_data))

        # The recipe's other ingredients (vials, ink, relics, ...): crafted where a
        # chain exists, otherwise bought
        ing_steps, ing_stamina, ing_time, ing_external = [], 0.0, 0.0, []
        for c in ic.get('other_consumed', []):
            c_qty = runs * c['quantity']
            sub = resolve_item_crafting_chain(
                c['item_id'], c_qty, tool_powers, gather_speed, craft_speed,
                game_data, recipes, frozenset({sid}), {sid: quantity}
            )
            if sub is None:
                ing_external.append({'item_id': str(c['item_id']), 'item_name': c['item_name'], 'quantity': c_qty})
                continue
            sub_steps, sub_stam, sub_time, sub_ext = sub
            ing_steps    += sub_steps
            ing_stamina  += sub_stam
            ing_time     += sub_time
            ing_external += sub_ext

        for fe in _item_sources(ebi, input_id):
            f_tool_reqs = fe.get('tool_requirements', [])
            f_tool_type = f_tool_reqs[0]['tool_type'] if f_tool_reqs else None
            f_power = tool_powers.get(f_tool_type, 1) if f_tool_type is not None else 1

            f_prob = _yield_per_cast(fe, f_power, game_data)
            if f_prob <= 0:
                continue

            fish_casts_per_unit = 1.0 / f_prob
            total_fish_casts = fish_needed * fish_casts_per_unit
            fish_stamina = total_fish_casts * fe['stamina_per_cast']
            fish_time    = total_fish_casts * fe['time_per_cast'] / gather_speed

            rid = str(fe['resource_id'])
            node_health = rmh.get(rid, 0)
            fish_per_node = float(node_health) * fe['prob_per_hp'] if node_health else None
            items_per_node = fish_per_node * oil_per_fish if fish_per_node else None

            label = node_label(rid, game_data)

            # Bait cost for nodes that consume bait per cast
            bait_steps, bait_stamina, bait_time = [], 0.0, 0.0
            for b in fe.get('consumed', []):
                bs, bstam, btime = compute_bait_cost(
                    b['item_id'], b['consumption_chance'], total_fish_casts,
                    tool_powers, gather_speed, craft_speed, game_data, recipes
                )
                bait_steps  += bs
                bait_stamina += bstam
                bait_time    += btime

            # Bait cost for Baited Spots/Schools (bait spent once per node)
            act_steps, act_stamina, act_time, act_external = compute_activation_cost(
                fe, fish_needed, f_power, tool_powers, gather_speed, craft_speed,
                game_data, recipes, frozenset({sid})
            )
            bait_steps   += act_steps
            bait_stamina += act_stamina
            bait_time    += act_time

            # Sailing between the nodes, for ones that stand at sea (Salt Deposits)
            sail_steps, sail_time = sea_route(rid, fish_needed / fish_per_node if fish_per_node else 0, sailing)
            bait_steps += sail_steps
            bait_time  += sail_time

            total_stamina = fish_stamina + proc_stamina + bait_stamina + ing_stamina
            total_time    = fish_time    + proc_time    + bait_time    + ing_time

            fish_step = {
                'type':     'fish',
                'label':    f'Catch {input_name}',
                'qty_out':  fish_needed,
                'qty_label': input_name,
                'casts':    total_fish_casts,
                'stamina':  fish_stamina,
                'time_sec': fish_time,
            }
            iname_out = item_name(item_id, recipes, game_data)
            proc_step = {
                'type':     'process',
                'label':    f'Process {input_name} → {iname_out}',
                'qty_out':  quantity,
                'qty_label': iname_out,
                'actions':  proc_attempts,
                'stamina':  proc_stamina,
                'time_sec': proc_time,
            }

            methods.append({
                'method_name':          f'Item chain — {input_name} → {item_name(item_id, recipes, game_data)} via {label}',
                'source_node':          rid,
                'node_label':           label,
                'fish_name':            input_name,
                'steps':                bait_steps + [fish_step] + ing_steps + [proc_step],
                'total_stamina':        total_stamina,
                'total_time_seconds':   total_time,
                'total_casts':          total_fish_casts,
                'total_actions':        proc_attempts,
                'items_per_full_node':  items_per_node,
                'external_ingredients': act_external + ing_external,
                'products': _products('item_chain_by_item', ic, input_id, runs, sid, quantity, game_data),
            })

    # ── Method D: Multi-step crafting chain (e.g. ingots) ─────────────────────
    # Triggers for items reachable via item-to-item crafting but not by A/B/C.
    # Items that only come out of sailing boxes get Method S instead.
    i2i = game_data.get('item_to_item_crafting', {})
    box_ids = {t['box_item_id'] for t in (load_sailing_data() or {}).get('node_types', [])}
    craftable = [e for e in i2i.get(sid, [])
                 if not any(c['item_id'] in box_ids for c in e.get('consumed', []))]
    if craftable and sid not in ebi and sid not in cbi and sid not in icbi:
        result = resolve_item_crafting_chain(
            sid, quantity, tool_powers, gather_speed, craft_speed, game_data, recipes
        )
        if result:
            steps, total_stamina, total_time, external = result
            total_casts   = sum(s.get('casts',   0) for s in steps)
            total_actions = sum(s.get('actions', 0) for s in steps)
            iname = item_name(sid, recipes, game_data)
            methods.append({
                'method_name':         f'Crafting chain — {iname}',
                'source_node':         None,
                'node_label':          'Multi-step crafting',
                'fish_name':           None,
                'steps':               steps,
                'total_stamina':       total_stamina,
                'total_time_seconds':  total_time,
                'total_casts':         total_casts,
                'total_actions':       total_actions,
                'items_per_full_node': None,
                'external_ingredients': external,
                'products':            {sid: quantity},
            })

    # ── Method S: Sailing cargo boxes ─────────────────────────────────────────
    sail = resolve_sailing_method(sid, quantity, sailing, gather_speed, craft_speed, recipes, game_data)
    if sail:
        methods.append(sail)

    return methods


def price_methods(methods, recipes, game_data):
    """
    Attach live market value to each method (m['market']): what its products
    earn sold instantly into buy orders, listed at the lowest ask, or listed
    but capped by recent sales (see _market.py), and what buying its external
    ingredients at the ask costs. Returns False if no order book could be fetched.
    """
    product_ids = {str(i) for m in methods for i in m.get('products', {})}
    ids = product_ids | {x['item_id'] for m in methods
                         for x in m.get('external_ingredients', []) if x.get('item_id')}
    if not ids:
        return False
    with ThreadPoolExecutor(max_workers=2) as pool:
        books_f = pool.submit(order_books, ids)
        sales_f = pool.submit(all_recent_sales, product_ids)
        books, sales = books_f.result(), sales_f.result()

    for m in methods:
        products = []
        for iid, qty in m.get('products', {}).items():
            book = books[str(iid)]
            gold, sold = sell_instant(book, qty)
            name = item_name(iid, recipes, game_data)
            recent = sales.get(str(iid))
            capped = None
            if recent:
                # Nobody listing it right now: price at what it last traded for
                list_price = book['ask'] or recent['avg_30d']
                capped = {d: sell_capped(book, qty, recent['units'][d], list_price)[0]
                          for d in SALES_WINDOWS}
            products.append({
                'item_id':       str(iid),
                'name':          book['name'] if name == str(iid) and book['name'] else name,
                'quantity':      qty,
                'ask':           book['ask'],
                'bid':           book['bid'],
                'instant_gold':  gold,
                'instant_units': sold,
                'listed_gold':   sell_listed(book, qty),
                'sold_recent':   recent['units'] if recent else None,
                'capped_gold':   capped,
            })
        external_cost, unpriced = 0.0, []
        for x in m.get('external_ingredients', []):
            ask = books.get(x.get('item_id') or '', {}).get('ask')
            if ask:
                external_cost += ask * x['quantity']
            else:
                unpriced.append(x['item_name'])
        m['market'] = {
            'products':          sorted(products, key=lambda p: -p['listed_gold']),
            'revenue_instant':   sum(p['instant_gold'] for p in products),
            'revenue_listed':    sum(p['listed_gold'] for p in products),
            # None unless every product's trade history came back
            'revenue_capped':    ({d: sum(p['capped_gold'][d] for p in products) for d in SALES_WINDOWS}
                                  if all(p['capped_gold'] for p in products) else None),
            'external_cost':     external_cost,
            'external_unpriced': unpriced,
        }
    return any(b['ask'] is not None or b['bids'] for b in books.values())


class handler(BaseHTTPRequestHandler):
    def do_OPTIONS(self):
        self._send(200, {})

    def do_GET(self):
        params = parse_qs(urlparse(self.path).query)

        def _p(name, default=None):
            v = params.get(name, [None])[0]
            return v if v is not None else default

        # Search mode: ?q=<text> returns matching items from local data
        q = _p('q', '')
        if q:
            game_data = load_game_data()
            recipes   = load_recipes_cache()
            q_lo = q.lower()
            # Build a set of item IDs that have known chains
            known_ids = (set(game_data.get('extraction_by_item', {}).keys()) |
                         set(game_data.get('cargo_by_item', {}).keys())        |
                         set(game_data.get('item_chain_by_item', {}).keys())   |
                         set(game_data.get('item_to_item_crafting', {}).keys()))
            results = []
            seen = set()
            for iid, r in recipes.items():
                name = r.get('name', '')
                if q_lo in name.lower() and iid not in seen:
                    seen.add(iid)
                    has_chain = iid in known_ids
                    results.append({'id': iid, 'name': name,
                                    'tier': r.get('tier'), 'has_chain': has_chain})
            results.sort(key=lambda x: (not x['has_chain'], x['name'].lower()))
            self._send(200, {'items': results[:20]})
            return

        item_id_raw = _p('item_id', '')
        if not item_id_raw:
            self._send(400, {'error': 'item_id parameter is required'})
            return

        try:
            quantity      = float(_p('quantity', '1'))
            tool_powers   = {tt: float(_p(param, '1')) for tt, param in TOOL_PARAM_MAP.items()}
            sailing       = {'level':      int(float(_p('sailing_level', '0'))),
                             'ship_speed': float(_p('ship_speed', '0')),
                             'region':     _p('sail_region', '')}
            gather_speed  = float(_p('gather_speed', '1.0'))
            craft_speed   = float(_p('craft_speed',  '1.0'))
            if gather_speed <= 0:
                gather_speed = 1.0
            if craft_speed <= 0:
                craft_speed = 1.0
            if quantity <= 0:
                quantity = 1.0
        except ValueError as exc:
            self._send(400, {'error': f'Invalid numeric parameter: {exc}'})
            return

        try:
            game_data = load_game_data()
            recipes   = load_recipes_cache()
        except Exception as exc:
            self._send(500, {'error': f'Failed to load data: {exc}'})
            return

        iname = item_name(item_id_raw, recipes, game_data)

        try:
            methods = resolve_all_methods(
                item_id_raw, quantity, tool_powers, gather_speed, craft_speed, game_data, recipes,
                sailing
            )
        except Exception as exc:
            self._send(500, {'error': f'Calculation error: {exc}'})
            return

        # Live prices (?market=0 skips the BitJita order-book calls)
        market_ok = False
        if methods and _p('market', '1') != '0':
            try:
                market_ok = price_methods(methods, recipes, game_data)
            except Exception:
                market_ok = False

        error = None if methods else 'No chain found for this item'
        self._send(200, {
            'item_id':   item_id_raw,
            'item_name': iname,
            'quantity':  quantity,
            'methods':   methods,
            'market_ok': market_ok,
            'error':     error,
        })

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
