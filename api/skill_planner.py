"""
GET /api/skill_planner?player_id=<id>
  → Returns player's skill levels and XP, with XP gap to next multiple-of-10 threshold.

GET /api/skill_planner?player_id=<id>&skill_id=<n>&target_level=<n>
  → Returns all crafting/extraction options that grant XP for the given skill,
    sorted by fewest actions needed to reach target_level.
"""

import json
import math
import sys
import os
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

sys.path.insert(0, os.path.dirname(__file__))
from _lib import api_get, load_recipes_cache, cors_headers, SKILL_NAMES

# Module-level cache so warm Lambda instances reuse the table.
_LEVELS_CACHE = None


def load_levels():
    """Fetch and cache the XP level table from BitJita.
    Returns a sorted list of (level, xp_required) tuples.
    """
    global _LEVELS_CACHE
    if _LEVELS_CACHE is not None:
        return _LEVELS_CACHE
    data = api_get('/static/experience/levels.json')
    # data is a list of {level, xp}; sort by level ascending
    table = sorted(data, key=lambda e: e['level'])
    _LEVELS_CACHE = table
    return table


def xp_to_level(xp, table):
    """Return the current level for a given XP amount."""
    level = 1
    for entry in table:
        if entry['xp'] <= xp:
            level = entry['level']
        else:
            break
    return level


def level_to_xp(level, table):
    """Return the cumulative XP required to reach a given level."""
    for entry in table:
        if entry['level'] == level:
            return entry['xp']
    # If beyond table max, return last entry's xp
    return table[-1]['xp']


def next_threshold(current_level):
    """Return the next multiple-of-10 level above current_level (min 10)."""
    return max(10, ((current_level // 10) + 1) * 10)


def get_player_xp(player_id):
    """Fetch player profile and return {skill_id: xp} dict."""
    data = api_get(f'/api/players/{player_id}')
    player = data.get('player', {})
    xp_map = {}
    for entry in player.get('experience', []):
        skill_id = entry.get('skill_id')
        quantity = entry.get('quantity', 0)
        if skill_id is not None:
            xp_map[int(skill_id)] = quantity
    return xp_map


def build_skills_response(xp_map, table):
    """Build the skills list for Mode 1."""
    skills = []
    for skill_id, skill_name in sorted(SKILL_NAMES.items()):
        if skill_id == 1:  # Skip "ANY"
            continue
        current_xp = xp_map.get(skill_id, 0)
        current_level = xp_to_level(current_xp, table)
        threshold = next_threshold(current_level)
        # Cap threshold at table max
        max_level = table[-1]['level']
        threshold = min(threshold, max_level)
        threshold_xp = level_to_xp(threshold, table)
        xp_to_threshold = max(0, threshold_xp - current_xp)

        # Progress within the current 10-level band
        band_start_level = ((current_level // 10)) * 10
        band_start_xp = level_to_xp(max(1, band_start_level), table)
        band_end_xp = threshold_xp
        band_size = band_end_xp - band_start_xp
        if band_size > 0:
            progress_pct = min(100, round((current_xp - band_start_xp) / band_size * 100, 1))
        else:
            progress_pct = 100.0

        skills.append({
            'skill_id': skill_id,
            'skill_name': skill_name,
            'current_xp': current_xp,
            'current_level': current_level,
            'next_threshold': threshold,
            'xp_to_threshold': xp_to_threshold,
            'progress_pct': progress_pct,
        })
    return skills


def build_options_response(skill_id, current_xp, target_level, table, recipes):
    """Scan all recipes for XP-granting entries for the target skill."""
    target_xp = level_to_xp(target_level, table)
    xp_gap = max(0, target_xp - current_xp)
    options = []

    for item_id, item_data in recipes.items():
        item_name = item_data.get('name', item_id)

        # ── Extraction recipes ──
        for recipe in item_data.get('extraction', []):
            xp_per_cast = _get_exp_qty(recipe, skill_id)
            if xp_per_cast <= 0:
                continue
            casts_needed = math.ceil(xp_gap / xp_per_cast) if xp_gap > 0 else 0
            level_reqs = _format_level_reqs(recipe.get('levelRequirements', []))
            options.append({
                'type': 'Gather',
                'item_id': item_id,
                'item_name': item_name,
                'recipe_name': recipe.get('verbPhrase') or 'Extract',
                'xp_per_action': xp_per_cast,
                'actions_needed': casts_needed,
                'ingredients': [],
                'level_requirements': level_reqs,
            })

        # ── Crafting / using recipes ──
        for recipe in item_data.get('crafting', []) + item_data.get('using', []):
            exp_qty = _get_exp_qty(recipe, skill_id)
            if exp_qty <= 0:
                continue
            actions_required = recipe.get('actionsRequired', 1) or 1
            xp_per_craft = exp_qty * actions_required
            if xp_per_craft <= 0:
                continue
            crafts_needed = math.ceil(xp_gap / xp_per_craft) if xp_gap > 0 else 0
            output_item_id, output_item_name = _get_primary_output(recipe, recipes, item_id, item_name)

            # Gather item ingredients (skip cargo-type entries)
            ingredients = []
            for ing in recipe.get('consumedItemStacks', []):
                if ing.get('item_type', '').lower() != 'item':
                    continue
                ing_name = recipes.get(str(ing['item_id']), {}).get('name') or str(ing['item_id'])
                ingredients.append({
                    'item_id': str(ing['item_id']),
                    'item_name': ing_name,
                    'quantity': crafts_needed * ing.get('quantity', 1),
                    'per_craft': ing.get('quantity', 1),
                })

            level_reqs = _format_level_reqs(recipe.get('levelRequirements', []))
            options.append({
                'type': 'Craft',
                'item_id': output_item_id,
                'item_name': output_item_name,
                'recipe_name': recipe.get('name') or 'Craft',
                'xp_per_action': xp_per_craft,
                'actions_needed': crafts_needed,
                'ingredients': ingredients,
                'level_requirements': level_reqs,
            })

    # Sort by fewest actions needed (ascending), then xp_per_action descending
    options.sort(key=lambda o: (o['actions_needed'], -o['xp_per_action']))
    return {
        'xp_gap': xp_gap,
        'target_level': target_level,
        'options': options,
    }


def _get_exp_qty(recipe, skill_id):
    """Extract XP quantity for a given skill_id from a recipe's experiencePerProgress list."""
    for entry in recipe.get('experiencePerProgress', []):
        if entry.get('skill_id') == skill_id:
            return entry.get('quantity', 0)
    return 0


def _format_level_reqs(reqs):
    """Format level requirements as a list of {skill, level} dicts."""
    result = []
    for req in reqs:
        sid = req.get('skill_id') or (req.get('skill', {}) or {}).get('id')
        lv = req.get('level', 1)
        name = SKILL_NAMES.get(sid, str(sid)) if sid else '?'
        result.append({'skill': name, 'level': lv})
    return result


def _get_primary_output(recipe, recipes, fallback_item_id, fallback_item_name):
    """Return the recipe's main output item id/name, falling back to the parent item."""
    outputs = recipe.get('craftedItemStacks', [])
    if outputs:
        output_item_id = str(outputs[0].get('item_id', fallback_item_id))
        output_item_name = recipes.get(output_item_id, {}).get('name') or fallback_item_name
        return output_item_id, output_item_name
    return fallback_item_id, fallback_item_name


class handler(BaseHTTPRequestHandler):
    def do_OPTIONS(self):
        self._send(200, {})

    def do_GET(self):
        params = parse_qs(urlparse(self.path).query)
        player_id   = params.get('player_id',    [''])[0].strip()
        skill_id_s  = params.get('skill_id',     [''])[0].strip()
        target_lv_s = params.get('target_level', [''])[0].strip()

        if not player_id:
            self._send(400, {'error': 'player_id is required'})
            return

        try:
            table = load_levels()
            xp_map = get_player_xp(player_id)

            if skill_id_s:
                # Mode 2: options for a specific skill
                try:
                    skill_id = int(skill_id_s)
                except ValueError:
                    self._send(400, {'error': 'skill_id must be an integer'})
                    return

                if skill_id not in SKILL_NAMES:
                    self._send(400, {'error': f'unknown skill_id {skill_id}'})
                    return

                current_xp = xp_map.get(skill_id, 0)
                current_level = xp_to_level(current_xp, table)
                max_level = table[-1]['level']

                if target_lv_s:
                    try:
                        target_level = int(target_lv_s)
                    except ValueError:
                        self._send(400, {'error': 'target_level must be an integer'})
                        return
                else:
                    target_level = next_threshold(current_level)

                target_level = min(target_level, max_level)

                recipes = load_recipes_cache()
                result = build_options_response(skill_id, current_xp, target_level, table, recipes)
                result['skill_name'] = SKILL_NAMES[skill_id]
                result['current_xp'] = current_xp
                result['current_level'] = current_level
                self._send(200, result)

            else:
                # Mode 1: all skills overview
                skills = build_skills_response(xp_map, table)
                self._send(200, {'skills': skills})

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
        pass
