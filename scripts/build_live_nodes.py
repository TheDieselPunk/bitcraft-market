"""
Build data/live_nodes.json: how many of each gatherable node stand in the mainland
regions right now, from the bitcraftsync relay (BitJita's as fallback).

build_game_data.py and build_recipes.py drop nodes with fewer than MIN_LIVE_NODES in
every region, so the calculators don't offer routes from nodes the world doesn't have:
ones made by buildings, growth stages or events, or that only grow on the islands.
Activated fishing nodes (Frenzied School, Baited School) only exist once someone chums
or baits one, so they count the node they're made from. Tag-skipped and prospected
nodes aren't looked up: they're dropped anyway, or (searched fishing spots) kept
though they never stand in the world until someone finds one.

A node counts as live as soon as one region has enough. Counting every node of the
common types would mean millions of rows (4.7M in the last full count), so each region
is first sampled: every node in STRIPS map rows spread across it, one subscription per
row (~35k rows each). Common types pass there; only the types still short are counted
over the whole region. Counts are therefore lower bounds once they reach MIN_LIVE_NODES. Rebuilt at most every REFRESH_HOURS (pass --force to rebuild now); if the
relay is down the last snapshot is kept. Needs websocket-client (CI installs it).
"""

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from build_game_data import (
    LIVE_NODES_FILE, MIN_LIVE_NODES, SKIPPED_RESOURCE_TAGS, build_node_activation, fetch_json,
    prospected_resource_ids, to_list,
)
from _relay import RegionRelay
from build_sailing_cargo import REGIONS

REFRESH_HOURS = 20   # the 6-hourly CI job rebuilds this about once a day
BATCH_SIZE    = 25   # node types per relay subscription
HOME_REGION   = 12   # looked up first; most nodes stand here too
STRIPS        = 4    # sampled map rows per region
STRIP_MIN     = 50   # sample only while at least this many types are pending


def count_nodes(relay, resource_ids):
    """{resource_id: live node count} for several node types in the relay's region."""
    tables = relay.snapshot([f'SELECT * FROM resource_state WHERE resource_id = {int(r)}'
                             for r in resource_ids])
    counts = dict.fromkeys(resource_ids, 0)
    for row in tables.get('resource_state', []):
        rid = row[1]   # resource_state row: [entity_id, resource_id, direction]
        counts[rid] = counts.get(rid, 0) + 1
    return counts


def strip_counts(relay, wanted):
    """{resource_id: node count} over STRIPS map rows spread across the region."""
    _, min_z, _, height = relay.bounds()
    counts = {}
    for i in range(STRIPS):
        z = min_z + int(height * (i + 0.5) / STRIPS)
        tables = relay.snapshot([
            'SELECT resource_state.* FROM resource_state JOIN location_state '
            'ON resource_state.entity_id = location_state.entity_id '
            f'WHERE location_state.chunk_index >= {z * 1000} AND location_state.chunk_index < {z * 1000 + 1000}'])
        for row in tables.get('resource_state', []):
            if row[1] in wanted:
                counts[row[1]] = counts.get(row[1], 0) + 1
    return counts


def survey(resource_ids):
    """Best single-region count per node type, stopping once a region has MIN_LIVE_NODES."""
    best = dict.fromkeys(resource_ids, 0)
    pending = sorted(resource_ids)
    for region in [HOME_REGION] + [r for r in REGIONS if r != HOME_REGION]:
        with RegionRelay(region) as relay:   # one connection per region, a subscription per query
            if len(pending) >= STRIP_MIN:
                for rid, n in strip_counts(relay, set(pending)).items():
                    best[rid] = max(best[rid], n)
                pending = [rid for rid in pending if best[rid] < MIN_LIVE_NODES]
                print(f'  R{region} sampled: {len(pending)} node types left to count in full')
            for i in range(0, len(pending), BATCH_SIZE):
                for rid, n in count_nodes(relay, pending[i:i + BATCH_SIZE]).items():
                    best[rid] = max(best[rid], n)
        pending = [rid for rid in pending if best[rid] < MIN_LIVE_NODES]
        print(f'  R{region} {REGIONS[region]} (via {relay.via}): {len(pending)} node types still not found')
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

    print(f'Counting {len(set(lookup.values()))} node types on the relay ...')
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
            'counts':    'best single-region count; a lower bound once >= min_nodes (sampled map rows)',
        },
        'nodes': nodes,
    }, indent=1))
    absent = sum(n < MIN_LIVE_NODES for n in nodes.values())
    print(f'Written to {LIVE_NODES_FILE}: {len(nodes)} nodes, {absent} not standing in any mainland region')


if __name__ == '__main__':
    main()
