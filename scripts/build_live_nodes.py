"""
Build data/live_nodes.json: how many of each gatherable node stand in the mainland
regions right now, from the BitJita relay.

build_game_data.py and build_recipes.py drop nodes with fewer than MIN_LIVE_NODES in
every region, so the calculators don't offer routes from nodes the world doesn't have:
ones made by buildings, growth stages or events, or that only grow on the islands.
Activated fishing nodes (Frenzied School, Baited School) only exist once someone chums
or baits one, so they count the node they're made from. Tag-skipped and prospected
nodes aren't looked up: they're dropped anyway, or (searched fishing spots) kept
though they never stand in the world until someone finds one.

A node counts as live as soon as one region has enough, so common nodes cost one
lookup. Rebuilt at most every REFRESH_HOURS (pass --force to rebuild now); if the
relay is down the last snapshot is kept. Needs websocket-client (CI installs it).
"""

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import websocket

sys.path.insert(0, str(Path(__file__).parent))
from build_game_data import (
    LIVE_NODES_FILE, MIN_LIVE_NODES, SKIPPED_RESOURCE_TAGS, build_node_activation, fetch_json,
    prospected_resource_ids, to_list,
)
from build_sailing_cargo import REGIONS, RELAY_HEADERS, RELAY_WS_URL

REFRESH_HOURS = 20   # the 6-hourly CI job rebuilds this about once a day
BATCH_SIZE    = 25   # node types per relay subscription
HOME_REGION   = 12   # looked up first; most nodes stand here too


def count_nodes(region, resource_ids):
    """{resource_id: live node count} for several node types in one region."""
    ws = websocket.create_connection(RELAY_WS_URL.format(region=region),
                                     subprotocols=['v1.json.spacetimedb'],
                                     header=RELAY_HEADERS, timeout=60)
    try:
        ws.send(json.dumps({'Subscribe': {
            'query_strings': [f'SELECT * FROM resource_state WHERE resource_id = {int(r)}' for r in resource_ids],
            'request_id':    1,
        }}))
        while True:   # IdentityToken arrives first
            msg = json.loads(ws.recv())
            if 'SubscriptionError' in msg:
                raise RuntimeError(msg['SubscriptionError'].get('error', msg))
            if 'InitialSubscription' in msg:
                break
    finally:
        ws.close()
    counts = dict.fromkeys(resource_ids, 0)
    for table in msg['InitialSubscription']['database_update']['tables']:
        for update in table['updates']:
            for row in update['inserts']:
                rid = json.loads(row)[1]   # resource_state row: [entity_id, resource_id, direction]
                counts[rid] = counts.get(rid, 0) + 1
    return counts


def survey(resource_ids):
    """Best single-region count per node type, stopping once a region has MIN_LIVE_NODES."""
    best = dict.fromkeys(resource_ids, 0)
    pending = sorted(resource_ids)
    for region in [HOME_REGION] + [r for r in REGIONS if r != HOME_REGION]:
        for i in range(0, len(pending), BATCH_SIZE):
            for rid, n in count_nodes(region, pending[i:i + BATCH_SIZE]).items():
                best[rid] = max(best[rid], n)
        pending = [rid for rid in pending if best[rid] < MIN_LIVE_NODES]
        print(f'  R{region} {REGIONS[region]}: {len(pending)} node types still not found')
        if not pending:
            break
    return best


def main():
    try:
        built = json.loads(LIVE_NODES_FILE.read_text())['__meta__']['built']
        age = datetime.now(timezone.utc) - datetime.strptime(built, '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=timezone.utc)
        if age < timedelta(hours=REFRESH_HOURS) and '--force' not in sys.argv:
            print(f'{LIVE_NODES_FILE.name} is {age.total_seconds() / 3600:.1f} h old; keeping it.')
            return
    except (FileNotFoundError, KeyError, ValueError):
        pass

    resources  = to_list(fetch_json('resource_desc.json'))
    extraction = to_list(fetch_json('extraction_recipe_desc.json'))
    dropped    = {r['id'] for r in resources if r.get('tag') in SKIPPED_RESOURCE_TAGS} | prospected_resource_ids(
        to_list(fetch_json('prospecting_desc.json')), to_list(fetch_json('resource_clump_desc.json')), resources)
    sources    = {r['resource_id'] for r in extraction
                  if r.get('extracted_item_stacks') and r.get('resource_id') not in dropped}
    # Chummed / baited nodes are made from a node that does stand in the world
    made_from  = {int(rid): int(a['source_resource_id'])
                  for rid, a in build_node_activation(extraction, resources, {}).items()}
    lookup     = {rid: made_from.get(rid, rid) for rid in sources}

    print(f'Counting {len(set(lookup.values()))} node types on the BitJita relay ...')
    try:
        counts = survey(set(lookup.values()))
    except Exception as exc:
        # Keep the last good snapshot rather than drop every node
        print(f'  Relay unavailable ({exc}); leaving {LIVE_NODES_FILE.name} unchanged.')
        return
    nodes = {str(rid): counts[base] for rid, base in sorted(lookup.items())}
    LIVE_NODES_FILE.write_text(json.dumps({
        '__meta__': {
            'built':     datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
            'regions':   sorted(REGIONS),
            'min_nodes': MIN_LIVE_NODES,
            'counts':    'best single-region count; lookups stop once a region has min_nodes',
        },
        'nodes': nodes,
    }, indent=1))
    absent = sum(n < MIN_LIVE_NODES for n in nodes.values())
    print(f'Written to {LIVE_NODES_FILE}: {len(nodes)} nodes, {absent} not standing in any mainland region')


if __name__ == '__main__':
    main()
