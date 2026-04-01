"""
GET /api/chain_calc?item_id=X&quantity=Y&rod_power=Z&gather_speed=W&craft_speed=V&pick_power=A&axe_power=B

Returns all extraction/cargo-chain methods for producing a given item, with
stamina, time and cast/action breakdowns per method.

gather_speed  multiplier applied to fishing/extraction time (e.g. 1.05 = +5%)
craft_speed   multiplier applied to crafting/processing time (separate stat)
"""

import json
import re
import sys
import os
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

sys.path.insert(0, os.path.dirname(__file__))
from _lib import load_game_data, load_recipes_cache, cors_headers


# tool_type int -> param name
TOOL_PARAM_MAP = {
    10: 'rod_power',
    4:  'pick_power',
    1:  'axe_power',
    5:  'hammer_power',
}

# Fishing node resource_id patterns -> human label
_NODE_PATTERNS = [
    (re.compile(r'^(\d)110000$'), 'T{tier} Lake (small)'),
    (re.compile(r'^(\d)110001$'), 'T{tier} Lake (large/bait)'),
    (re.compile(r'^(\d)110003$'), 'T{tier} Ocean'),
    (re.compile(r'^(\d)110004$'), 'T{tier} Tiny Lake (bait)'),
]


def node_label(node_id):
    nid = str(node_id)
    for pattern, tmpl in _NODE_PATTERNS:
        m = pattern.match(nid)
        if m:
            return tmpl.replace('{tier}', m.group(1))
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


CHUM_DURATION_SECS = 900  # 15 minutes per chum


def _tool_power(tool_reqs, tool_powers):
    """Return tool power for the first tool requirement, default 1."""
    tool_type = tool_reqs[0]['tool_type'] if tool_reqs else None
    return tool_powers.get(tool_type, 1) if tool_type is not None else 1


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

        for fe in ebi.get(bait_fish_id, []):
            power         = _tool_power(fe.get('tool_requirements', []), tool_powers)
            prob          = fe['prob_per_hp'] * power
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
                        'label':    f'Process {bait_fish_name} → {item_name(bait_id, recipes)}',
                        'qty_out':  bait_needed,
                        'qty_label': item_name(bait_id, recipes),
                        'actions':  craft_attempts,
                        'stamina':  craft_stamina,
                        'time_sec': craft_time,
                    },
                ],
                fish_stamina + craft_stamina,
                fish_time    + craft_time,
            )
    return [], 0.0, 0.0


def compute_chum_cost(tier, total_ocean_casts, time_per_cast,
                      tool_powers, gather_speed, craft_speed, game_data, recipes):
    """
    Compute the extra cost of producing chum for total_ocean_casts ocean casts.
    Each chum lasts CHUM_DURATION_SECS seconds of fishing.

    Returns (extra_steps, extra_stamina, extra_time_sec, external_ingredients).
    external_ingredients = list of {item_name, quantity} for items not calculable
    (e.g. raw meat from hunting).
    """
    casts_per_chum = CHUM_DURATION_SECS / max(time_per_cast, 0.1)
    chum_needed    = total_ocean_casts / casts_per_chum
    chum_id        = str(tier * 1_000_000 + 110_026)   # e.g. 5110026

    icbi = game_data.get('item_chain_by_item', {})
    ebi  = game_data.get('extraction_by_item', {})

    for chum_recipe in icbi.get(chum_id, []):
        chum_per_run    = chum_recipe.get('output_quantity', 1)
        if chum_per_run <= 0:
            continue

        lake_fish_id    = str(chum_recipe['input_item_id'])
        lake_fish_name  = chum_recipe.get('input_item_name', lake_fish_id)
        lake_fish_qty   = chum_recipe.get('input_item_qty', 10)
        craft_runs      = chum_needed / chum_per_run
        lake_fish_needed = craft_runs * lake_fish_qty

        total_work      = craft_runs * chum_recipe['actions_required']
        craft_power     = _tool_power(chum_recipe.get('tool_requirements', []), tool_powers)
        craft_attempts  = total_work / craft_power
        craft_stamina   = craft_attempts * chum_recipe.get('stamina_per_action', 0.0)
        craft_time      = craft_attempts * chum_recipe.get('time_per_action', 1.6) / craft_speed

        # External ingredients (raw meat etc.)
        external = [
            {
                'item_name': c['item_name'],
                'quantity':  chum_needed * c['quantity'],
            }
            for c in chum_recipe.get('other_consumed', [])
        ]

        extra_steps = []
        extra_stamina = craft_stamina
        extra_time    = craft_time

        for fe in ebi.get(lake_fish_id, []):
            power         = _tool_power(fe.get('tool_requirements', []), tool_powers)
            prob          = fe['prob_per_hp'] * power
            if prob <= 0:
                continue
            fish_casts    = lake_fish_needed / prob
            fish_stamina  = fish_casts * fe['stamina_per_cast']
            fish_time     = fish_casts * fe['time_per_cast'] / gather_speed

            extra_steps   += [{
                'type':     'chum_fish',
                'label':    f'Catch {lake_fish_name} (for chum)',
                'qty_out':  lake_fish_needed,   # lake fish caught for chum
                'qty_label': lake_fish_name,
                'casts':    fish_casts,
                'stamina':  fish_stamina,
                'time_sec': fish_time,
            }]
            extra_stamina += fish_stamina
            extra_time    += fish_time

            # Bait cost for the chum lake fishing
            for b in fe.get('consumed', []):
                b_steps, b_stam, b_time = compute_bait_cost(
                    b['item_id'], b['consumption_chance'], fish_casts,
                    tool_powers, gather_speed, craft_speed, game_data, recipes
                )
                extra_steps   = b_steps + extra_steps   # bait steps come first
                extra_stamina += b_stam
                extra_time    += b_time
            break  # use first extraction source

        extra_steps += [{
            'type':     'chum_craft',
            'label':    f'Craft {item_name(chum_id, recipes)} (chum)',
            'qty_out':  chum_needed,
            'qty_label': item_name(chum_id, recipes),
            'actions':  craft_attempts,
            'stamina':  craft_stamina,
            'time_sec': craft_time,
        }]

        return extra_steps, extra_stamina, extra_time, external

    return [], 0.0, 0.0, []


def resolve_item_crafting_chain(item_id, quantity, tool_powers, gather_speed, craft_speed,
                               game_data, recipes, visited=None):
    """
    Recursively resolve a multi-step production chain for item_id.
    Returns (steps, total_stamina, total_time_sec, external_ingredients) or None if unresolvable.

    Resolution order:
      1. Direct extraction (non-cargo ebi entry) → extract step
      2. Cargo unpack chain (cbi entry) → mine + unpack steps
      3. Item-to-item crafting (i2i) → recurse on inputs, append craft step
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
    direct = [e for e in ebi.get(sid, []) if not e.get('cargo_input_id')]
    if direct:
        fe = direct[0]
        power = _tool_power(fe.get('tool_requirements', []), tool_powers)
        prob  = fe['prob_per_hp'] * power
        if prob > 0:
            casts   = quantity / prob
            stamina = casts * fe['stamina_per_cast']
            time_s  = casts * fe['time_per_cast'] / gather_speed
            iname   = item_name(sid, recipes, game_data)
            tt      = fe.get('tool_requirements', [{}])[0].get('tool_type') if fe.get('tool_requirements') else None
            verb    = 'Chop' if tt == 1 else 'Mine' if tt == 4 else 'Extract'
            return (
                [{
                    'type':     'extract',
                    'label':    f'{verb} {iname}',
                    'qty_out':  quantity,
                    'qty_label': iname,
                    'casts':    casts,
                    'stamina':  stamina,
                    'time_sec': time_s,
                }],
                stamina, time_s, []
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

            for fe in ebi.get(cargo_id, []) + cex.get(cargo_id, []):
                power = _tool_power(fe.get('tool_requirements', []), tool_powers)
                prob  = fe['prob_per_hp'] * power
                if prob <= 0:
                    continue
                casts       = cargo_needed / prob
                mine_stam   = casts * fe['stamina_per_cast']
                mine_time   = casts * fe['time_per_cast'] / gather_speed
                cargo_name  = ce.get('cargo_input_name', cargo_id)
                iname_out   = item_name(sid, recipes, game_data)
                tt          = fe.get('tool_requirements', [{}])[0].get('tool_type') if fe.get('tool_requirements') else None
                verb        = 'Chop' if tt == 1 else 'Mine' if tt == 4 else 'Extract'
                return (
                    [
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
                    mine_stam + proc_stamina,
                    mine_time + proc_time,
                    []
                )

    # ── Path 3: item-to-item crafting ─────────────────────────────────────────
    if sid in i2i:
        new_visited = visited | {sid}
        for recipe in i2i[sid]:
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
                    game_data, recipes, new_visited
                )
                if sub is None:
                    external.append({'item_name': c_name, 'quantity': c_qty_needed})
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
            return (
                pre_steps + [craft_step],
                pre_stamina + craft_stamina,
                pre_time + craft_time,
                external
            )

    return None


def resolve_all_methods(item_id, quantity, tool_powers, gather_speed, craft_speed, game_data, recipes):
    """
    Return a list of method dicts for producing `quantity` of `item_id`.

    Each method dict:
      method_name, source_node, node_label, fish_name,
      steps, total_stamina, total_time_seconds, total_casts, total_actions,
      items_per_full_node
    """
    methods = []
    ebi  = game_data.get('extraction_by_item', {})
    cbi  = game_data.get('cargo_by_item', {})
    cex  = game_data.get('cargo_extraction', {})
    icbi = game_data.get('item_chain_by_item', {})
    rmh = game_data.get('resource_max_health', {})

    sid = str(item_id)

    # ── Method A: Direct extraction ───────────────────────────────────────────
    for e in ebi.get(sid, []):
        tool_reqs = e.get('tool_requirements', [])
        tool_type = tool_reqs[0]['tool_type'] if tool_reqs else None
        power = tool_powers.get(tool_type, 1) if tool_type is not None else 1

        prob = e['prob_per_hp'] * power
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

        label = node_label(rid)
        iname = item_name(item_id, recipes)

        methods.append({
            'method_name': f'Direct — {label}',
            'source_node': rid,
            'node_label':  label,
            'fish_name':   None,
            'steps': [{
                'type':     'extract',
                'label':    f'Extract {iname}',
                'qty_out':  quantity,
                'qty_label': iname,
                'casts':    total_casts,
                'stamina':  stamina,
                'time_sec': time_sec,
            }],
            'total_stamina':       stamina,
            'total_time_seconds':  time_sec,
            'total_casts':         total_casts,
            'total_actions':       0.0,
            'items_per_full_node': items_per_node,
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
        cargo_name    = ce.get('cargo_input_name', item_name(fish_id, recipes))

        # Cargo source may be in extraction_by_item (wrapper-resolved) OR cargo_extraction (direct)
        fish_sources = ebi.get(fish_id, []) + cex.get(fish_id, [])
        for fe in fish_sources:
            f_tool_reqs = fe.get('tool_requirements', [])
            f_tool_type = f_tool_reqs[0]['tool_type'] if f_tool_reqs else None
            f_power = tool_powers.get(f_tool_type, 1) if f_tool_type is not None else 1

            f_prob = fe['prob_per_hp'] * f_power
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

            label = node_label(rid)
            total_stamina = fish_stamina + proc_stamina
            total_time    = fish_time + proc_time

            # Chum cost for ocean nodes (resource pattern *110003)
            chum_steps, chum_stamina, chum_time, chum_external = [], 0.0, 0.0, []
            tier_match = re.match(r'^(\d)110003$', rid)
            if tier_match:
                tier = int(tier_match.group(1))
                chum_steps, chum_stamina, chum_time, chum_external = compute_chum_cost(
                    tier, total_fish_casts, fe['time_per_cast'],
                    tool_powers, gather_speed, craft_speed, game_data, recipes
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
            iname_out = item_name(item_id, recipes)
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
                'method_name':         f'Cargo — {cargo_name} → {item_name(item_id, recipes)} via {label}',
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
            })

    # ── Method C: Item chain (extract item → craft → output) ─────────────────
    # e.g. lake fish (Item) → process → oil/filet/products
    for ic in icbi.get(sid, []):
        input_id = str(ic['input_item_id'])
        oil_per_fish = ic['output_quantity']
        if oil_per_fish <= 0:
            continue

        fish_needed   = quantity / oil_per_fish
        total_work    = fish_needed * ic['actions_required']
        proc_power    = _tool_power(ic.get('tool_requirements', []), tool_powers)
        proc_attempts = total_work / proc_power
        proc_stamina  = proc_attempts * ic.get('stamina_per_action', 0.0)
        proc_time     = proc_attempts * ic.get('time_per_action', 1.6) / craft_speed
        input_name    = ic.get('input_item_name', item_name(input_id, recipes))

        for fe in ebi.get(input_id, []):
            f_tool_reqs = fe.get('tool_requirements', [])
            f_tool_type = f_tool_reqs[0]['tool_type'] if f_tool_reqs else None
            f_power = tool_powers.get(f_tool_type, 1) if f_tool_type is not None else 1

            f_prob = fe['prob_per_hp'] * f_power
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

            label = node_label(rid)

            # Bait cost for large-lake nodes that consume bait per cast
            bait_steps, bait_stamina, bait_time = [], 0.0, 0.0
            for b in fe.get('consumed', []):
                bs, bstam, btime = compute_bait_cost(
                    b['item_id'], b['consumption_chance'], total_fish_casts,
                    tool_powers, gather_speed, craft_speed, game_data, recipes
                )
                bait_steps  += bs
                bait_stamina += bstam
                bait_time    += btime

            total_stamina = fish_stamina + proc_stamina + bait_stamina
            total_time    = fish_time    + proc_time    + bait_time

            fish_step = {
                'type':     'fish',
                'label':    f'Catch {input_name}',
                'qty_out':  fish_needed,
                'qty_label': input_name,
                'casts':    total_fish_casts,
                'stamina':  fish_stamina,
                'time_sec': fish_time,
            }
            iname_out = item_name(item_id, recipes)
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
                'method_name':          f'Item chain — {input_name} → {item_name(item_id, recipes)} via {label}',
                'source_node':          rid,
                'node_label':           label,
                'fish_name':            input_name,
                'steps':                bait_steps + [fish_step, proc_step],
                'total_stamina':        total_stamina,
                'total_time_seconds':   total_time,
                'total_casts':          total_fish_casts,
                'total_actions':        proc_attempts,
                'items_per_full_node':  items_per_node,
                'external_ingredients': [],
            })

    # ── Method D: Multi-step crafting chain (e.g. ingots) ─────────────────────
    # Triggers for items reachable via item-to-item crafting but not by A/B/C.
    i2i = game_data.get('item_to_item_crafting', {})
    if sid in i2i and sid not in ebi and sid not in cbi and sid not in icbi:
        result = resolve_item_crafting_chain(
            sid, quantity, tool_powers, gather_speed, craft_speed, game_data, recipes
        )
        if result:
            steps, total_stamina, total_time, external = result
            total_casts   = sum(s.get('casts',   0) for s in steps)
            total_actions = sum(s.get('actions', 0) for s in steps)
            iname = item_name(sid, recipes)
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
            })

    return methods


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
            rod_power     = float(_p('rod_power',    '1'))
            pick_power    = float(_p('pick_power',   '1'))
            axe_power     = float(_p('axe_power',    '1'))
            hammer_power  = float(_p('hammer_power', '1'))
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

        tool_powers = {
            10: rod_power,
            4:  pick_power,
            1:  axe_power,
            5:  hammer_power,
        }

        try:
            game_data = load_game_data()
            recipes   = load_recipes_cache()
        except Exception as exc:
            self._send(500, {'error': f'Failed to load data: {exc}'})
            return

        iname = item_name(item_id_raw, recipes)

        try:
            methods = resolve_all_methods(
                item_id_raw, quantity, tool_powers, gather_speed, craft_speed, game_data, recipes
            )
        except Exception as exc:
            self._send(500, {'error': f'Calculation error: {exc}'})
            return

        error = None if methods else 'No chain found for this item'
        self._send(200, {
            'item_id':   item_id_raw,
            'item_name': iname,
            'quantity':  quantity,
            'methods':   methods,
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
