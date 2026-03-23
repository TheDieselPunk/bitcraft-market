"""
_effort_calc.py — Pure effort calculation logic (no I/O, no HTTP).

Recursively resolves a production chain for a target item + quantity and
accumulates stamina, time, and ingredient costs split by profession.

Key mechanics (confirmed empirically):
  - Extraction probability is per health-point of the resource node, not per cast.
    prob_per_cast = prob_per_hp * tool_power
  - stamina_per_item = stamina_per_cast / (prob_per_hp * tool_power)
  - time_per_item    = time_per_cast    / (prob_per_hp * tool_power)
  - bait_per_item    = consumption_chance / (prob_per_hp * tool_power)
  - Crafting: total_stamina = (actionsRequired / tool_power) * stamina_per_action * crafts_needed
"""

import math


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

def _empty_profession():
    return {
        'gathering_stamina': 0.0,
        'gathering_time':    0.0,
        'crafting_stamina':  0.0,
        'crafting_time':     0.0,
    }


class EffortAccumulator:
    def __init__(self):
        self.by_profession  = {}   # skill_name -> {gathering_stamina, ...}
        self.ingredient_costs = {}  # item_id -> {name, quantity}
        self.chain          = []   # flat step list for display
        self.warnings       = []

    def add_gather(self, skill_name, stamina, time_sec):
        p = self.by_profession.setdefault(skill_name, _empty_profession())
        p['gathering_stamina'] += stamina
        p['gathering_time']    += time_sec

    def add_craft(self, skill_name, stamina, time_sec):
        p = self.by_profession.setdefault(skill_name, _empty_profession())
        p['crafting_stamina'] += stamina
        p['crafting_time']    += time_sec

    def add_ingredient(self, item_id, quantity, name=''):
        entry = self.ingredient_costs.setdefault(item_id, {'name': name, 'quantity': 0.0})
        entry['quantity'] += quantity
        if name and not entry['name']:
            entry['name'] = name

    def add_step(self, **kwargs):
        self.chain.append({'step': len(self.chain), **kwargs})

    def warn(self, msg):
        if msg not in self.warnings:
            self.warnings.append(msg)

    def to_dict(self):
        return {
            'by_profession':   self.by_profession,
            'ingredient_costs': self.ingredient_costs,
            'chain':           self.chain,
            'warnings':        self.warnings,
        }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _normalize_extraction(entry, item_id=None):
    """
    Normalize an extraction recipe entry to a consistent snake_case format.

    Handles two formats:
      - game_data format (snake_case):  prob_per_hp, stamina_per_cast, consumed, ...
      - recipes.json format (camelCase): extractedItemStacks[*].probability,
                                         staminaRequirement, consumedItemStacks, ...

    In both cases the probability value represents per-health-point probability;
    the actual per-cast probability = prob_per_hp * tool_power.
    """
    if 'prob_per_hp' in entry:
        return entry  # already normalized

    # Pick the matching item stack (highest probability if item_id given)
    stacks = entry.get('extractedItemStacks', [])
    if item_id:
        stacks = [s for s in stacks if str(s.get('item_id', '')) == str(item_id)] or stacks
    best_stack = max(stacks, key=lambda s: s.get('probability', 0)) if stacks else {}

    consumed = [
        {
            'item_id': str(c['item_id']),
            'consumption_chance': c.get('consumption_chance', 1.0),
        }
        for c in entry.get('consumedItemStacks', [])
        if c.get('item_type') == 'item'
    ]

    return {
        'prob_per_hp':     best_stack.get('probability', 0.0),
        'output_quantity': best_stack.get('quantity', 1),
        'stamina_per_cast': entry.get('staminaRequirement', 0.0),
        'time_per_cast':   entry.get('timeRequirement', 1.6),
        'tool_requirements': [
            {'tool_type': r.get('tool_type'), 'level': r.get('level', 1), 'power': r.get('power', 1)}
            for r in entry.get('toolRequirements', [])
        ],
        'level_requirements': [
            {'skill_id': r.get('skill_id'), 'level': r.get('level', 1)}
            for r in entry.get('levelRequirements', [])
        ],
        'consumed': consumed,
        'resource_id': str(entry.get('resourceId', entry.get('resource_id', ''))),
    }


def _get_tool_power(recipe, tools):
    """Return the player's tool power for the first matching tool requirement."""
    for req in recipe.get('toolRequirements', []):
        tt = req.get('tool_type')
        if tt is not None and tt in tools:
            return max(tools[tt]['power'], 1)
    # Also check snake_case variant (game_data.json uses snake_case)
    for req in recipe.get('tool_requirements', []):
        tt = req.get('tool_type')
        if tt is not None and tt in tools:
            return max(tools[tt]['power'], 1)
    return 1


def _skill_name(recipe, skill_names):
    """Extract skill name from a recipe's level requirements (or bare skill_id)."""
    # Cargo recipes store skill_id directly at top level
    if 'skill_id' in recipe and recipe['skill_id'] is not None:
        return skill_names.get(recipe['skill_id'], f'skill_{recipe["skill_id"]}')
    reqs = recipe.get('levelRequirements') or recipe.get('level_requirements', [])
    if reqs:
        sid = reqs[0].get('skill_id')
        if sid is not None:
            return skill_names.get(sid, f'skill_{sid}')
    return 'Unknown'


def _pick_best_extraction(ext_list, tools, item_id=None):
    """
    From a list of extraction entries, pick the best one the player can use.
    Normalizes entries to snake_case format first.
    Prefer entries whose tool_type the player has; break ties by highest
    effective prob_per_hp * tool_power.
    """
    if not ext_list:
        return None

    best = None
    best_score = -1

    for entry in ext_list:
        norm = _normalize_extraction(entry, item_id)
        if norm['prob_per_hp'] <= 0:
            continue
        tp = _get_tool_power(norm, tools)
        score = norm['prob_per_hp'] * tp
        if score > best_score:
            best_score = score
            best = norm

    return best


def _pick_best_crafting(recipe_list, tools, all_obtainable, item_id=None):
    """
    From a list of crafting recipes, pick the first one where all item
    ingredients are in all_obtainable (or there are no item ingredients).
    Unpack recipes and enrichment/upgrade recipes (where the output item is
    also an ingredient) are skipped.
    """
    def is_enrichment(recipe):
        """True if the recipe consumes the same item it produces (e.g. Enrich Bait)."""
        if item_id is None:
            return False
        return any(
            str(i.get('item_id')) == str(item_id)
            for i in recipe.get('consumedItemStacks', [])
        )

    for recipe in recipe_list:
        if is_enrichment(recipe):
            continue
        item_ings = [
            i for i in recipe.get('consumedItemStacks', [])
            if i.get('item_type') == 'item'
        ]
        if item_ings and not all(str(i['item_id']) in all_obtainable for i in item_ings):
            continue
        return recipe
    # Fallback: return first non-enrichment recipe regardless of ingredients
    for recipe in recipe_list:
        if not is_enrichment(recipe):
            return recipe
    return None


def _find_loot_source(item_id, all_recipes):
    """
    Search for a using-recipe on any item that directly or indirectly produces item_id.

    Two cases:
      1. Direct: a using-recipe's craftedItemStacks contains item_id directly.
         e.g. Azure Minni → (Craft Fine Bait) → Fine Bait
      2. Via intermediate: a using-recipe produces an intermediate whose
         itemListPossibilities contains item_id.
         e.g. Azure Sphyra → (Craft Azure Sphyra Products) → Azure Sphyra Products
              → itemListPossibilities → Fine Fish Oil

    Returns (mid_or_None, chance, qty, source_item_id, using_recipe) or None.
    """
    # Case 1: direct production via using-recipe
    for src_id, src_rec in all_recipes.items():
        if src_id == item_id:
            continue  # skip self-enrichment (e.g. Enrich Fine Bait on Fine Bait itself)
        for urec in src_rec.get('using', []):
            # Skip if the target item is also consumed (enrichment pattern)
            consumed_ids = {str(i.get('item_id', '')) for i in urec.get('consumedItemStacks', [])}
            if item_id in consumed_ids:
                continue
            for out in urec.get('craftedItemStacks', []):
                if str(out.get('item_id', '')) == item_id:
                    qty = out.get('quantity', 1)
                    return (None, 1.0, qty, src_id, urec)

    # Case 2: via intermediate + itemListPossibilities
    # Sum expected yield across ALL matching possibilities (e.g. 90%*2 + 10%*2 = 2.0)
    for mid, mrec in all_recipes.items():
        if not mrec.get('intermediate'):
            continue
        expected_yield = sum(
            entry.get('chance', 1.0) * entry.get('quantity', 1)
            for entry in mrec.get('itemListPossibilities', [])
            if str(entry.get('targetId', '')) == item_id
        )
        if expected_yield <= 0:
            continue
        for src_id, src_rec in all_recipes.items():
            for urec in src_rec.get('using', []):
                if 'unpack' in urec.get('name', '').lower():
                    continue
                for out in urec.get('craftedItemStacks', []):
                    if str(out['item_id']) == mid:
                        return (mid, 1.0, expected_yield, src_id, urec)
    return None


# ---------------------------------------------------------------------------
# Resolution paths
# ---------------------------------------------------------------------------

def _resolve_extraction_entry(item_id, quantity, entry, tools, acc, visited,
                               depth, all_recipes, game_data, skill_names):
    """Shared logic for Path A and Path B extraction."""
    tp = _get_tool_power(entry, tools)

    prob = entry['prob_per_hp']
    if prob <= 0:
        acc.warn(f'Zero probability for {item_id} extraction — treated as external cost.')
        acc.add_ingredient(item_id, quantity)
        return

    stamina_per_item = entry['stamina_per_cast'] / (prob * tp)
    time_per_item    = entry['time_per_cast']    / (prob * tp)
    skill = _skill_name(entry, skill_names)
    item_name = (all_recipes.get(item_id) or {}).get('name') or item_id

    acc.add_gather(skill, stamina_per_item * quantity, time_per_item * quantity)
    acc.add_step(
        item_id=item_id,
        item_name=item_name,
        method='extraction',
        skill=skill,
        tool_power=tp,
        quantity=quantity,
        stamina=round(stamina_per_item * quantity, 2),
        time_sec=round(time_per_item * quantity, 2),
    )

    # Recurse on consumed items (bait etc.)
    for consumed in entry.get('consumed', []):
        bait_per_item = consumed['consumption_chance'] / (prob * tp)
        resolve_effort(
            str(consumed['item_id']),
            bait_per_item * quantity,
            all_recipes, game_data, tools, acc,
            visited, depth + 1, skill_names,
        )


def _resolve_crafting_recipe(item_id, quantity, recipe, tools, acc, visited,
                              depth, all_recipes, game_data, skill_names):
    """Shared logic for crafting + loot-chain processing steps."""
    tp = _get_tool_power(recipe, tools)
    output_qty = 1
    for out in recipe.get('craftedItemStacks', []):
        if str(out.get('item_id', '')) == item_id or not item_id:
            output_qty = out.get('quantity', 1)
            break
    if output_qty <= 0:
        output_qty = 1

    actions = max(recipe.get('actionsRequired', 1), 1)
    crafts_needed = quantity / output_qty

    total_stamina = (actions / tp) * recipe.get('staminaRequirement', 0.0) * crafts_needed
    total_time    = (actions / tp) * recipe.get('timeRequirement',    0.0) * crafts_needed
    skill = _skill_name(recipe, skill_names)
    item_name = (all_recipes.get(item_id) or {}).get('name') or item_id

    acc.add_craft(skill, total_stamina, total_time)
    acc.add_step(
        item_id=item_id,
        item_name=item_name,
        method='crafting',
        recipe_name=recipe.get('name', ''),
        skill=skill,
        tool_power=tp,
        quantity=quantity,
        stamina=round(total_stamina, 2),
        time_sec=round(total_time, 2),
    )

    # Recurse on each item ingredient
    for ing in recipe.get('consumedItemStacks', []):
        if ing.get('item_type') != 'item':
            continue
        ing_qty = ing['quantity'] * crafts_needed
        resolve_effort(
            str(ing['item_id']),
            ing_qty,
            all_recipes, game_data, tools, acc,
            visited, depth + 1, skill_names,
        )


# ---------------------------------------------------------------------------
# Cargo resolution (PATH E)
# ---------------------------------------------------------------------------

def _resolve_cargo_gathering(cargo_id, quantity, game_data, tools, acc, skill_names, cargo_name=''):
    """Calculate gathering effort to obtain `quantity` units of a Cargo item."""
    entries = game_data.get('cargo_extraction', {}).get(cargo_id, [])
    if not entries:
        acc.add_ingredient(f'cargo:{cargo_id}', quantity, cargo_name or cargo_id)
        acc.warn(f'No extraction data for cargo {cargo_id} ({cargo_name}) — counted as external.')
        return

    best = _pick_best_extraction(entries, tools)
    if not best:
        acc.add_ingredient(f'cargo:{cargo_id}', quantity, cargo_name or cargo_id)
        return

    tp = _get_tool_power(best, tools)
    prob = best['prob_per_hp']
    stamina_per_cargo = best['stamina_per_cast'] / (prob * tp)
    time_per_cargo    = best['time_per_cast']    / (prob * tp)
    skill = _skill_name(best, skill_names)

    acc.add_gather(skill, stamina_per_cargo * quantity, time_per_cargo * quantity)
    acc.add_step(
        item_id=f'cargo:{cargo_id}',
        item_name=cargo_name or cargo_id,
        method='extraction',
        skill=skill,
        tool_power=tp,
        quantity=quantity,
        stamina=round(stamina_per_cargo * quantity, 2),
        time_sec=round(time_per_cargo * quantity, 2),
    )


def _resolve_cargo_recipe(item_id, quantity, recipe, tools, acc, all_recipes,
                           game_data, skill_names):
    """PATH E: Process a gatherable Cargo item into regular items."""
    tp = _get_tool_power(recipe, tools)
    output_qty = max(recipe.get('output_quantity', 1), 1)
    actions = max(recipe.get('actions_required', 1), 1)
    crafts_needed = quantity / output_qty

    total_stamina = (actions / tp) * recipe.get('stamina_per_action', 0.0) * crafts_needed
    total_time    = (actions / tp) * recipe.get('time_per_action',    0.0) * crafts_needed
    skill = _skill_name(recipe, skill_names)
    item_name = (all_recipes.get(item_id) or {}).get('name') or item_id

    acc.add_craft(skill, total_stamina, total_time)
    acc.add_step(
        item_id=item_id,
        item_name=item_name,
        method='crafting',
        recipe_name=recipe.get('recipe_name', ''),
        skill=skill,
        tool_power=tp,
        quantity=quantity,
        stamina=round(total_stamina, 2),
        time_sec=round(total_time, 2),
    )

    cargo_id   = recipe.get('cargo_input_id', '')
    cargo_qty  = recipe.get('cargo_input_qty', 1) * crafts_needed
    cargo_name = recipe.get('cargo_input_name', cargo_id)
    if cargo_id:
        _resolve_cargo_gathering(cargo_id, cargo_qty, game_data, tools, acc, skill_names, cargo_name)


# ---------------------------------------------------------------------------
# Main recursive resolver
# ---------------------------------------------------------------------------

MAX_DEPTH = 20


def resolve_effort(
    item_id,
    quantity,
    all_recipes,
    game_data,
    tools,
    acc,
    visited=None,
    depth=0,
    skill_names=None,
):
    """
    Recursively resolve the effort to obtain `quantity` units of `item_id`.

    Resolution priority:
      A. Extraction recipe in all_recipes (recipes.json)
      B. Extraction data in game_data.json (T4 fish etc.)
      C. Crafting recipe in all_recipes
      D. Loot from an intermediate (e.g. Fine Fish Oil via Azure Sphyra Products)
      Fallback: treat as external ingredient cost
    """
    if visited is None:
        visited = frozenset()
    if skill_names is None:
        from _lib import SKILL_NAMES
        skill_names = SKILL_NAMES

    if depth > MAX_DEPTH:
        acc.warn(f'Max depth exceeded resolving {item_id} — treated as external cost.')
        acc.add_ingredient(item_id, quantity)
        return

    if item_id in visited:
        # Circular dependency (e.g. bait crafted from itself)
        name = (all_recipes.get(item_id) or {}).get('name', item_id)
        acc.warn(f'Circular dependency: {name} ({item_id}) treated as external cost.')
        acc.add_ingredient(item_id, quantity, name)
        return

    visited = visited | {item_id}
    recipe = all_recipes.get(item_id, {})
    name = recipe.get('name', item_id)

    # --- PATH E: Cargo-based processing (FIRST — trunk/ore chunk chains take priority) ---
    # Items like Rough Wood Log or Ferralith Ore Piece are produced by processing
    # gatherable cargo (Rough Wood Trunk, Ferralith Ore Chunk). Check this before
    # extraction so we follow the trunk→log / chunk→piece chain rather than
    # any direct-extraction fallback.
    cargo_recipes = game_data.get('cargo_by_item', {}).get(item_id)
    if cargo_recipes:
        best_cargo = next(
            (r for r in cargo_recipes if _get_tool_power(r, tools) > 1),
            cargo_recipes[0],
        )
        _resolve_cargo_recipe(
            item_id, quantity, best_cargo, tools, acc, all_recipes,
            game_data, skill_names,
        )
        return

    # --- PATH A: Extraction recipe in recipes.json ---
    if recipe.get('extraction'):
        best = _pick_best_extraction(recipe['extraction'], tools, item_id)
        if best:
            _resolve_extraction_entry(
                item_id, quantity, best, tools, acc, visited,
                depth, all_recipes, game_data, skill_names,
            )
            return

    # --- PATH B: Extraction data in game_data.json (T4 fish, etc.) ---
    gd_entries = game_data.get('extraction_by_item', {}).get(item_id)
    if gd_entries:
        best = _pick_best_extraction(gd_entries, tools)
        if best:
            _resolve_extraction_entry(
                item_id, quantity, best, tools, acc, visited,
                depth, all_recipes, game_data, skill_names,
            )
            return

    # --- PATH C: Crafting recipe in recipes.json ---
    all_known = (
        set(all_recipes.keys())
        | set(game_data.get('extraction_by_item', {}).keys())
        | set(game_data.get('cargo_by_item', {}).keys())
    )
    if recipe.get('crafting'):
        best = _pick_best_crafting(recipe['crafting'], tools, all_known, item_id)
        if best:
            _resolve_crafting_recipe(
                item_id, quantity, best, tools, acc, visited,
                depth, all_recipes, game_data, skill_names,
            )
            return

    # --- PATH D: Loot from intermediate ---
    loot_source = _find_loot_source(item_id, all_recipes)
    if loot_source:
        mid, chance, loot_qty, src_id, using_recipe = loot_source
        if chance <= 0:
            acc.warn(f'Zero loot chance for {item_id} — treated as external cost.')
            acc.add_ingredient(item_id, quantity, name)
            return

        procs_needed = quantity / (chance * loot_qty)

        # Crafting effort for the processing step (output_qty=None means we pass src_id qty)
        _resolve_crafting_recipe(
            src_id, procs_needed, using_recipe, tools, acc, visited,
            depth, all_recipes, game_data, skill_names,
        )
        return  # _resolve_crafting_recipe recurses on source item automatically

    # --- Fallback: external ingredient ---
    acc.add_ingredient(item_id, quantity, name)
    if name:
        acc.warn(f'No production chain found for {name} ({item_id}) — counted as external input.')


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def calculate_effort(item_id, quantity, all_recipes, game_data, tools, skill_names=None):
    """
    Top-level call. Returns an EffortAccumulator with all results populated.

    Merges game_data['extra_recipes'] (bait/extraction-consumed items fetched
    from the bitjita API at build time) into all_recipes so the chain resolver
    can follow them without needing them in the main recipes.json cache.
    """
    if skill_names is None:
        from _lib import SKILL_NAMES
        skill_names = SKILL_NAMES

    # Merge extra_recipes from game_data (e.g. Azure Minni using/crafting chains)
    # without mutating the caller's dict.
    # Only include crafting + using fields — extraction is handled via
    # game_data['extraction_by_item'] (Path B) to avoid format conflicts.
    merged = dict(all_recipes)
    for iid, rec in game_data.get('extra_recipes', {}).items():
        if iid not in merged:
            merged[iid] = {
                'name':      rec.get('name', iid),
                'tier':      rec.get('tier'),
                'tag':       rec.get('tag', ''),
                'ingredient':    rec.get('ingredient', True),
                'intermediate':  rec.get('intermediate', False),
                'extraction': [],   # handled by game_data Path B
                'crafting':  rec.get('crafting', []),
                'using':     rec.get('using', []),
                'itemListPossibilities': rec.get('itemListPossibilities', []),
            }

    acc = EffortAccumulator()
    resolve_effort(
        str(item_id), float(quantity),
        merged, game_data, tools, acc,
        skill_names=skill_names,
    )

    # Round final profession numbers for cleaner output
    for prof in acc.by_profession.values():
        for k in prof:
            prof[k] = round(prof[k], 2)

    return acc
