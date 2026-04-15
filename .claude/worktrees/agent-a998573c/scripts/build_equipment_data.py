#!/usr/bin/env python3
"""
build_equipment_data.py — Equipment catalog builder for Equipment Simulator.

Sources everything from static BitCraftToolBox/BitCraft_GameData files —
no per-item API calls, fast and reliable.

Files fetched:
  item_desc.json        — base item metadata (name, tier, rarity, tag)
  equipment_desc.json   — equipment slot + stats per item_id
  food_desc.json        — consumable buffs (item_id -> buffs list)
  buff_desc.json        — buff definitions (id -> description + stats)

Outputs web/data/equipment_data.json

Run from the web/ directory:
    python scripts/build_equipment_data.py
"""

import json
import time
import urllib.request
from pathlib import Path

GAMEDATA_BASE = (
    'https://raw.githubusercontent.com/BitCraftToolBox/'
    'BitCraft_GameData/cereal/cs/static'
)
OUT_FILE = Path(__file__).parent.parent / 'data' / 'equipment_data.json'

EQUIPMENT_TAGS = {'Leather Clothing', 'Cloth Clothing', 'Automata Heart', 'Jewelry'}
BUFF_TAGS      = {'Meal', 'Elixir'}

# Map BitCraftToolBox slot names to our UI slot names
SLOT_MAP = {
    'HeadClothing':  'head',
    'TorsoClothing': 'torso',
    'BeltClothing':  'belt',
    'LegClothing':   'legs',
    'FeetClothing':  'feet',
    'HandClothing':  'hands',
    'HeadArtifact':  'heart',    # Automata Hearts
    'HandArtifact':  'jewelry',  # Rings
}

# Sort order for rarity strings
RARITY_ORDER = {'Common': 0, 'Uncommon': 1, 'Rare': 2, 'Epic': 3, 'Legendary': 4}

# Stats relevant to the Equipment Simulator (others are ignored for now)
RELEVANT_STATS = {
    'MaxHealth', 'MaxStamina',
    'PassiveHealthRegenRate', 'PassiveStaminaRegenRate',
    'MovementMultiplier',
    'GatheringSpeed', 'CraftingSpeed', 'BuildingSpeed',
    'Evasion',
    # per-profession
    'CarpentryCritChance', 'CarpentrySpeed',
    'FarmingCritChance',   'FarmingSpeed',
    'FishingCritChance',   'FishingSpeed',
    'ForagingCritChance',  'ForagingSpeed',
    'ForestryCritChance',  'ForestrySpeed',
    'HuntingCritChance',   'HuntingSpeed',
    'LeatherworkingCritChance', 'LeatherworkingSpeed',
    'MasonryCritChance',   'MasonrySpeed',
    'MiningCritChance',    'MiningSpeed',
    'ScholarCritChance',   'ScholarSpeed',
    'SmithingCritChance',  'SmithingSpeed',
    'TailoringCritChance', 'TailoringSpeed',
}


def fetch_json(filename):
    url = f'{GAMEDATA_BASE}/{filename}'
    print(f'  Fetching {url} ...')
    req = urllib.request.Request(url, headers={'Accept': 'application/json'})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def as_list(d):
    return list(d.values()) if isinstance(d, dict) else list(d)


def build_equipment_catalog(items_list, equip_list):
    """
    Join item_desc with equipment_desc to build per-slot equipment catalog.
    Only includes items whose tag is in EQUIPMENT_TAGS.

    Returns {slot: [entry, ...]} sorted by (tier, rarity).
    """
    item_by_id  = {i['id']: i for i in items_list}
    equip_by_id = {e['item_id']: e for e in equip_list}

    by_slot = {}
    skipped = 0
    for item in items_list:
        if item.get('tag') not in EQUIPMENT_TAGS:
            continue
        iid  = item['id']
        eqd  = equip_by_id.get(iid)
        if not eqd:
            skipped += 1
            continue

        # Determine slot from first matching equipment slot
        slot = None
        for game_slot in eqd.get('slots', []):
            if game_slot in SLOT_MAP:
                slot = SLOT_MAP[game_slot]
                break
        if slot is None:
            skipped += 1
            continue

        rarity_str = item.get('rarity', 'Common')
        if rarity_str not in RARITY_ORDER:
            rarity_str = 'Common'

        # Filter stats to relevant ones
        stats = [
            {'id': s['id'], 'value': s['value'], 'is_pct': s['is_pct']}
            for s in eqd.get('stats', [])
            if s['id'] in RELEVANT_STATS
        ]

        entry = {
            'id':         str(iid),
            'name':       item['name'],
            'tier':       item.get('tier', 0),
            'rarity':     RARITY_ORDER.get(rarity_str, 0),
            'rarity_str': rarity_str,
            'slot':       slot,
            'stats':      stats,
        }
        by_slot.setdefault(slot, []).append(entry)

    if skipped:
        print(f'  Skipped {skipped} items (no equipment_desc entry or unknown slot).')

    # Deduplicate by (name, tier, rarity) — keep entry with most stats
    for slot in by_slot:
        seen = {}
        for entry in by_slot[slot]:
            key = (entry['name'], entry['tier'], entry['rarity_str'])
            if key not in seen or len(entry['stats']) > len(seen[key]['stats']):
                seen[key] = entry
        by_slot[slot] = list(seen.values())

    # Sort: tier asc, then rarity asc
    for slot in by_slot:
        by_slot[slot].sort(key=lambda e: (e['tier'], e['rarity']))

    return by_slot


def build_buff_catalog(items_list, food_list, buff_list):
    """
    Build meal and elixir buff catalog using food_desc + buff_desc.

    Returns (meals_list, elixirs_list), each entry:
        id, name, tier, tag, buffs (list of {description, duration, stats})
    """
    item_by_id = {i['id']: i for i in items_list}
    buff_by_id = {b['id']: b for b in buff_list}

    meals   = []
    elixirs = []

    for food in food_list:
        iid  = food['item_id']
        item = item_by_id.get(iid)
        if not item:
            continue
        tag = item.get('tag', '')
        if tag not in BUFF_TAGS:
            continue

        resolved_buffs = []
        for b_ref in food.get('buffs', []):
            buff = buff_by_id.get(b_ref['buff_id'])
            if not buff:
                continue
            # Only include relevant stats
            stats = [
                {'id': s['id'], 'value': s['value'], 'is_pct': s['is_pct']}
                for s in buff.get('stats', [])
                if s['id'] in RELEVANT_STATS or s['id'] in (
                    'PassiveHealthRegenRate', 'PassiveStaminaRegenRate',
                    'ActiveStaminaRegenRate', 'ActiveHealthRegenRate',
                )
            ]
            if not stats:
                continue
            resolved_buffs.append({
                'description': buff.get('description', ''),
                'duration':    b_ref.get('duration', buff.get('duration', 0)),
                'stats':       stats,
            })

        if not resolved_buffs:
            continue

        entry = {
            'id':     str(iid),
            'name':   item['name'],
            'tier':   item.get('tier', 0),
            'rarity': RARITY_ORDER.get(item.get('rarity', 'Common'), 0),
            'tag':    tag,
            'buffs':  resolved_buffs,
            'hunger': food.get('hunger', 0),
        }

        if tag == 'Meal':
            meals.append(entry)
        else:
            elixirs.append(entry)

    meals.sort(key=lambda e: (e['tier'], e['name']))
    elixirs.sort(key=lambda e: (e['tier'], e['name']))
    return meals, elixirs


def main():
    OUT_FILE.parent.mkdir(parents=True, exist_ok=True)

    print('Fetching static game data files...')
    items_raw = fetch_json('item_desc.json')
    equip_raw = fetch_json('equipment_desc.json')
    food_raw  = fetch_json('food_desc.json')
    buff_raw  = fetch_json('buff_desc.json')

    items_list = as_list(items_raw)
    equip_list = as_list(equip_raw)
    food_list  = as_list(food_raw)
    buff_list  = as_list(buff_raw)

    print(f'  items: {len(items_list)}, equipment: {len(equip_list)}, '
          f'food: {len(food_list)}, buffs: {len(buff_list)}')

    print('\nBuilding equipment catalog...')
    by_slot = build_equipment_catalog(items_list, equip_list)
    for slot, entries in sorted(by_slot.items()):
        print(f'  {slot:12s}: {len(entries)} entries')
    total_equip = sum(len(v) for v in by_slot.values())

    print('\nBuilding buff catalog...')
    meals, elixirs = build_buff_catalog(items_list, food_list, buff_list)
    print(f'  meals: {len(meals)}, elixirs: {len(elixirs)}')

    output = {
        'equipment': by_slot,
        'meals':     meals,
        'elixirs':   elixirs,
        '__meta__': {
            'built_at':    time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
            'total_equip': total_equip,
            'meals':       len(meals),
            'elixirs':     len(elixirs),
        },
    }

    OUT_FILE.write_text(json.dumps(output))
    size_kb = OUT_FILE.stat().st_size // 1024
    print(f'\nWritten to {OUT_FILE} ({size_kb} KB)')
    print(f'Total entries: {total_equip} equipment, {len(meals)} meals, {len(elixirs)} elixirs')


if __name__ == '__main__':
    main()
