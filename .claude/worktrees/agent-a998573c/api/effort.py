"""
GET /api/effort?item_id=<id>&quantity=<n>&player_id=<id>
GET /api/effort?item_id=<id>&quantity=<n>&tool_powers=10:26,9:14

Recursively resolves a production chain and returns estimated stamina,
time, and ingredient costs split by profession and activity type.

Parameters:
  item_id     (required) — item ID from the recipe cache
  quantity    (optional, default 1) — target quantity
  player_id   (optional) — fetches live toolbelt to determine tool powers
  tool_powers (optional) — fallback if no player_id; format: "type:power,type:power"
"""

import json
import sys
import os
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

sys.path.insert(0, os.path.dirname(__file__))
from _lib import (
    get_toolbelt, load_recipes_cache, load_game_data,
    cors_headers, SKILL_NAMES,
)
from _effort_calc import calculate_effort


class handler(BaseHTTPRequestHandler):
    def do_OPTIONS(self):
        self._send(200, {})

    def do_GET(self):
        params = parse_qs(urlparse(self.path).query)

        item_id     = params.get('item_id',     [''])[0].strip()
        quantity_s  = params.get('quantity',    ['1'])[0].strip()
        player_id   = params.get('player_id',   [''])[0].strip()
        tool_pow_s  = params.get('tool_powers', [''])[0].strip()

        if not item_id:
            self._send(400, {'error': 'item_id is required'})
            return

        try:
            quantity = max(1.0, float(quantity_s))
        except ValueError:
            self._send(400, {'error': 'quantity must be a number'})
            return

        try:
            # Load recipe + game data from disk (no network)
            all_recipes = load_recipes_cache()
            game_data   = load_game_data()

            if item_id not in all_recipes and \
               item_id not in game_data.get('extraction_by_item', {}) and \
               item_id not in game_data.get('extra_recipes', {}):
                self._send(404, {'error': f'item {item_id} not found in recipe cache'})
                return

            # Resolve tool powers
            tools = {}
            if player_id:
                tools = get_toolbelt(player_id)
                if not tools:
                    # Non-fatal: proceed with default power 1 and warn
                    pass
            elif tool_pow_s:
                for pair in tool_pow_s.split(','):
                    try:
                        tt, pw = pair.strip().split(':')
                        tools[int(tt)] = {'level': 1, 'power': int(pw), 'name': '', 'tier': None}
                    except (ValueError, TypeError):
                        pass

            # Run the effort calculation
            acc = calculate_effort(item_id, quantity, all_recipes, game_data, tools, SKILL_NAMES)

            # Identify any tool types used by the item's recipe that aren't in the toolbelt
            item_rec = all_recipes.get(item_id, game_data.get('extra_recipes', {}).get(item_id, {}))
            missing_tools = []
            for recipe_list in (item_rec.get('extraction', []), item_rec.get('crafting', [])):
                for rec in recipe_list:
                    for req in rec.get('toolRequirements', []):
                        tt = req.get('tool_type')
                        if tt is not None and tt not in tools:
                            if tt not in missing_tools:
                                missing_tools.append(tt)

            item_name = item_rec.get('name', item_id)

            result = {
                'item_id':      item_id,
                'item_name':    item_name,
                'quantity':     quantity,
                'by_profession': acc.by_profession,
                'ingredient_costs': acc.ingredient_costs,
                'chain':        acc.chain,
                'warnings':     acc.warnings,
                'missing_tool_types': missing_tools,
            }

            self._send(200, result)

        except Exception as e:
            self._send(500, {'error': str(e)})

    def _send(self, status, body):
        payload = json.dumps(body).encode()
        self.send_response(status)
        for k, v in cors_headers().items():
            self.send_header(k, v)
        self.send_header('Content-Length', len(payload))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):
        pass  # suppress default access log noise
