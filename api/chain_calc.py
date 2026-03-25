"""
GET /api/chain_calc?item_id=X&quantity=Y&rod_power=Z&gather_speed=W&pick_power=A&axe_power=B

Returns all extraction/cargo-chain methods for producing a given item, with
stamina, time and cast/action breakdowns per method.
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


def item_name(item_id, recipes):
    """Look up human-readable name from recipes cache."""
    entry = recipes.get(str(item_id))
    if entry:
        return entry.get('name', str(item_id))
    return str(item_id)


def resolve_all_methods(item_id, quantity, tool_powers, gather_speed, game_data, recipes):
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
                'type':    'extract',
                'label':   f'Extract {iname}',
                'casts':   total_casts,
                'stamina': stamina,
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

        fish_needed = quantity / oil_per_fish
        proc_actions = fish_needed * ce['actions_required']
        proc_stamina = proc_actions * ce.get('stamina_per_action', 0.75)
        proc_time    = proc_actions * ce.get('time_per_action', 1.6)
        cargo_name   = ce.get('cargo_input_name', item_name(fish_id, recipes))

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

            methods.append({
                'method_name': f'Cargo — {cargo_name} → {item_name(item_id, recipes)} via {label}',
                'source_node': rid,
                'node_label':  label,
                'fish_name':   cargo_name,
                'steps': [
                    {
                        'type':    'fish',
                        'label':   f'Catch {cargo_name}',
                        'casts':   total_fish_casts,
                        'stamina': fish_stamina,
                        'time_sec': fish_time,
                    },
                    {
                        'type':    'process',
                        'label':   f'Process {cargo_name} → {item_name(item_id, recipes)}',
                        'actions': proc_actions,
                        'stamina': proc_stamina,
                        'time_sec': proc_time,
                    },
                ],
                'total_stamina':       total_stamina,
                'total_time_seconds':  total_time,
                'total_casts':         total_fish_casts,
                'total_actions':       proc_actions,
                'items_per_full_node': items_per_node,
            })

    # ── Method C: Item chain (extract item → craft → output) ─────────────────
    # e.g. lake fish (Item) → process → oil/filet/products
    for ic in icbi.get(sid, []):
        input_id = str(ic['input_item_id'])
        oil_per_fish = ic['output_quantity']
        if oil_per_fish <= 0:
            continue

        fish_needed = quantity / oil_per_fish
        proc_actions = fish_needed * ic['actions_required']
        proc_stamina = proc_actions * ic.get('stamina_per_action', 0.0)
        proc_time    = proc_actions * ic.get('time_per_action', 1.6)
        input_name   = ic.get('input_item_name', item_name(input_id, recipes))

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
            total_stamina = fish_stamina + proc_stamina
            total_time    = fish_time + proc_time

            methods.append({
                'method_name': f'Item chain — {input_name} → {item_name(item_id, recipes)} via {label}',
                'source_node': rid,
                'node_label':  label,
                'fish_name':   input_name,
                'steps': [
                    {
                        'type':    'fish',
                        'label':   f'Catch {input_name}',
                        'casts':   total_fish_casts,
                        'stamina': fish_stamina,
                        'time_sec': fish_time,
                    },
                    {
                        'type':    'process',
                        'label':   f'Process {input_name} → {item_name(item_id, recipes)}',
                        'actions': proc_actions,
                        'stamina': proc_stamina,
                        'time_sec': proc_time,
                    },
                ],
                'total_stamina':       total_stamina,
                'total_time_seconds':  total_time,
                'total_casts':         total_fish_casts,
                'total_actions':       proc_actions,
                'items_per_full_node': items_per_node,
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
                         set(game_data.get('cargo_by_item', {}).keys()))
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
            quantity    = float(_p('quantity', '1'))
            rod_power   = float(_p('rod_power', '1'))
            pick_power  = float(_p('pick_power', '1'))
            axe_power   = float(_p('axe_power', '1'))
            gather_speed = float(_p('gather_speed', '1.0'))
            if gather_speed <= 0:
                gather_speed = 1.0
            if quantity <= 0:
                quantity = 1.0
        except ValueError as exc:
            self._send(400, {'error': f'Invalid numeric parameter: {exc}'})
            return

        tool_powers = {
            10: rod_power,
            4:  pick_power,
            1:  axe_power,
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
                item_id_raw, quantity, tool_powers, gather_speed, game_data, recipes
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
