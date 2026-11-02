"""
Build data/sailing_cargo.json: what a sailing-cargo run looks like in each region.

Sailing cargo (Lost Wreckage / Shipment / Treasure) floats at sea. Opening a node
gives one box to unpack later, then you sail on to the next, so the Chain
Calculator turns a run into boxes per hour from:
  - node_types: each node's Sailing level, open time and box (static data)
  - ships:      every boat's Ocean speed (static data), matched to the player's vault
  - regions:    per Sailing level bracket, the average hop of a nearest-node route
                over the live nodes (BitJita relay) and each node type's share of it

Positions and speeds are both raw world units (game E/N = raw / 3), so hop / speed
is seconds. Needs websocket-client and numpy (CI installs them; the API doesn't).
"""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import websocket

sys.path.insert(0, str(Path(__file__).parent))
from build_game_data import fetch_json, possibility_chances, to_list

OUT_FILE = Path(__file__).parent.parent / 'data' / 'sailing_cargo.json'

RELAY_WS_URL  = 'wss://relay.bitjita.com/v1/database/bitcraft-live-{region}/subscribe?compression=None&light=true'
RELAY_HEADERS = ['User-Agent: BitJita (Billard)']

# Unopened nodes, as tracked by BitcraftCV's navigate.py
NODE_TYPES = {
    'wreckage': 1477126404,   # Lost Wreckage
    'shipment': 1043012047,   # Lost Shipment
    'treasure': 1293969473,   # Lost Treasure
}

# Mainland regions only: the seasonal island regions (3, 11, 15, 23) can't be reached.
REGIONS = {
    7: 'Virexal', 8: 'Solmere', 9: 'Marowik', 12: 'Elyndor', 13: 'Hexalis',
    14: 'Lumethis', 17: 'Draxen', 18: 'Oryxen', 19: 'Zephra',
}

ROUTE_STARTS = 40   # nearest-node routes simulated per region and bracket
ROUTE_HOPS   = 60   # hops per route; nodes respawn, so short routes match steady play


def node_positions(region, resource_id, timeout=15):
    """Raw (x, z) of every node of one resource type in a region's live DB."""
    ws = websocket.create_connection(RELAY_WS_URL.format(region=region),
                                     subprotocols=['v1.json.spacetimedb'],
                                     header=RELAY_HEADERS, timeout=timeout)
    try:
        query = ('SELECT location_state.* FROM location_state JOIN resource_state '
                 'ON location_state.entity_id = resource_state.entity_id '
                 f'WHERE resource_state.resource_id = {int(resource_id)}')
        ws.send(json.dumps({'Subscribe': {'query_strings': [query], 'request_id': 1}}))
        while True:   # IdentityToken arrives first
            msg = json.loads(ws.recv())
            if 'SubscriptionError' in msg:
                raise RuntimeError(msg['SubscriptionError'].get('error', msg))
            if 'InitialSubscription' in msg:
                break
    finally:
        ws.close()
    rows = []
    for table in msg['InitialSubscription']['database_update']['tables']:
        for update in table['updates']:
            rows.extend(json.loads(row) for row in update['inserts'])
    # location_state row: [entity_id, chunk_index, x, z, dimension]
    return [(float(r[2]), float(r[3])) for r in rows]


def average_hop(points, rng):
    """Mean hop of greedy nearest-unvisited routes from random starting nodes."""
    pts = np.asarray(points, float)
    hops = []
    for start in rng.choice(len(pts), min(ROUTE_STARTS, len(pts)), replace=False):
        visited = np.zeros(len(pts), bool)
        cur = start
        visited[cur] = True
        for _ in range(min(ROUTE_HOPS, len(pts) - 1)):
            dist = np.hypot(*(pts - pts[cur]).T)
            dist[visited] = np.inf
            cur = int(dist.argmin())
            hops.append(float(dist[cur]))
            visited[cur] = True
    return sum(hops) / len(hops)


def box_unpack(box_id, crafting, items, item_lists):
    """The box's standard Unpack recipe and its expected contents per box. A second
    variant (adds sheet music) replaces it only for players with that knowledge."""
    recipe = next(r for r in crafting
                  if not r.get('required_knowledges')
                  and [c['item_id'] for c in r['consumed_item_stacks']] == [box_id])
    outputs = {}
    for stack in recipe['crafted_item_stacks']:
        item_list = item_lists.get(items[stack['item_id']].get('item_list_id'))
        if not item_list:
            outputs[str(stack['item_id'])] = outputs.get(str(stack['item_id']), 0) + stack['quantity']
            continue
        for chance, possibility in possibility_chances(item_list):
            for actual in possibility['items']:
                key = str(actual['item_id'])
                outputs[key] = outputs.get(key, 0) + chance * actual['quantity'] * stack['quantity']
    return {
        'recipe_id':          recipe['id'],
        'actions':            recipe['actions_required'],
        'time_per_action':    recipe['time_requirement'],
        'stamina_per_action': recipe['stamina_requirement'],
        'outputs':            {k: round(v, 6) for k, v in outputs.items()},
    }


def build_node_types(resources, extraction, crafting, items, item_lists):
    """Sailing level, open time/stamina, box and box contents for each node type."""
    by_resource = {}
    for e in extraction:
        by_resource.setdefault(e['resource_id'], []).append(e)
    out = []
    for key, rid in NODE_TYPES.items():
        node = resources[rid]
        open_recipe = by_resource[rid][0]
        # Opening leaves an "Opened ..." node whose collect drop names the box
        opened = by_resource[node['on_destroy_yield_resource_id']][0]
        box_id = opened['extracted_item_stacks'][0]['item_stack']['item_id']
        out.append({
            'key':           key,
            'resource_id':   rid,
            'name':          node['name'],
            'sailing_level': max(l['level'] for l in open_recipe['level_requirements']
                                 if l['skill_id'] == 21),
            'open_time':     open_recipe['time_requirement'],
            'open_stamina':  open_recipe['stamina_requirement'],
            'box_item_id':   str(box_id),
            'unpack':        box_unpack(box_id, crafting, items, item_lists),
        })
    return out


def build_ships(deployables):
    """Every boat players can own, with its Ocean speed (raw units/s)."""
    ships = []
    for d in deployables:
        if d.get('deployable_type') != 'Boat' or 'Cheater' in d['name']:   # dev-only boat
            continue
        speeds = {s['surface_type']: s['speed'] for s in d.get('speed', [])}
        ships.append({
            'collectible_id': str(d['deploy_from_collectible_id']),
            'name':           d['name'],
            'ocean_speed':    speeds.get('Ocean', 0.0),
        })
    return sorted(ships, key=lambda s: (-s['ocean_speed'], s['name']))


def build_regions(node_types):
    rng = np.random.default_rng(0)
    levels = sorted({t['sailing_level'] for t in node_types})
    regions = {}
    for region, name in REGIONS.items():
        positions = {t['key']: node_positions(region, t['resource_id']) for t in node_types}
        counts = {k: len(v) for k, v in positions.items()}
        print(f'  R{region} {name}: {counts}')
        brackets = []
        for level in levels:
            keys = [t['key'] for t in node_types if t['sailing_level'] <= level]
            points = [p for k in keys for p in positions[k]]
            if len(points) < 2:
                continue
            total = len(points)
            brackets.append({
                'min_level': level,
                'nodes':     total,
                'avg_hop':   round(average_hop(points, rng), 1),
                'share':     {k: round(counts[k] / total, 4) for k in keys},
            })
        regions[str(region)] = {'name': name, 'counts': counts, 'brackets': brackets}
    return regions


def main():
    resources  = {r['id']: r for r in to_list(fetch_json('resource_desc.json'))}
    extraction = to_list(fetch_json('extraction_recipe_desc.json'))
    crafting   = to_list(fetch_json('crafting_recipe_desc.json'))
    items      = {i['id']: i for i in to_list(fetch_json('item_desc.json'))}
    item_lists = {l['id']: l for l in to_list(fetch_json('item_list_desc.json'))}
    node_types = build_node_types(resources, extraction, crafting, items, item_lists)
    ships      = build_ships(to_list(fetch_json('deployable_desc.json')))
    print('Reading sailing cargo nodes from the BitJita relay ...')
    try:
        regions = build_regions(node_types)
    except Exception as exc:
        # Keep the last good snapshot rather than publish an empty one
        print(f'  Relay unavailable ({exc}); leaving {OUT_FILE.name} unchanged.')
        return
    OUT_FILE.write_text(json.dumps({
        '__meta__': {
            'built': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
            'units': 'raw world units; game E/N = raw / 3; speeds in raw units/s',
        },
        'node_types': node_types,
        'ships':      ships,
        'regions':    regions,
    }, indent=1))
    print(f'Written to {OUT_FILE}')


if __name__ == '__main__':
    main()
