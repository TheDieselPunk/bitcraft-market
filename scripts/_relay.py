"""
Read-only snapshots of the live BitCraft region databases (SpacetimeDB JSON protocol),
for the CI build scripts. Adapted from BitcraftCV's bitjita_relay.py.

Relays, in order:
  1. relay.bitcraftsync.app — community mirror of every idc-* DB (BitJita's relay reads
     through it). Region N is on port 3000 + N. Rows arrive as JSON objects; _light()
     turns them into BitJita's light lists, so callers see one shape.
  2. relay.bitjita.com — used when bitcraftsync refuses or fails (and for the rest of
     the run after a 429).

The databases are idc-{region} since the launch world (2026-11-14); the old
bitcraft-live-{region} names are 404.

bitcraftsync's rules (relay.bitcraftsync.app/llms.txt): read-only, subscriptions only,
"cache what you fetch for at least a minute and do not poll in a loop". Its nginx answers
429 past its limit. So: one connection per region (each lookup re-subscribes on the same
connection, which replaces the previous subscription), a pause between connections, and
BitJita's relay for the rest of the run after a 429.
"""

import json
import time

import websocket   # pip install websocket-client

SYNC_WS_URL   = ('wss://relay.bitcraftsync.app:{port}/v1/database/idc-{region}/subscribe'
                 '?compression=None&light=true')
SYNC_HEADERS  = ['User-Agent: BitCraft Effort']
SYNC_TS_KEY   = '__timestamp_micros_since_unix_epoch__'

BITJITA_WS_URL  = 'wss://relay.bitjita.com/v1/database/idc-{region}/subscribe?compression=None&light=true'
BITJITA_HEADERS = ['User-Agent: BitJita (Billard)']

CONNECT_GAP = 3.0    # seconds between new connections to bitcraftsync

_sync_blocked = False   # a 429 this run: BitJita's relay only from here on
_last_sync_connect = 0.0


def _light(v):
    """A bitcraftsync value in BitJita's light-JSON shape: objects -> lists of their values
    (a timestamp object -> [micros], {} -> [])."""
    if isinstance(v, dict):
        if len(v) == 1 and SYNC_TS_KEY in v:
            return [v[SYNC_TS_KEY]]
        return [_light(x) for x in v.values()]
    if isinstance(v, list):
        return [_light(x) for x in v]
    return v


def short_error(e):
    """A websocket handshake error without the response headers it carries."""
    parts = str(e).split(' -+-+- ')
    return parts[0] + (f' ({parts[-1]})' if len(parts) > 1 else '')


class RegionRelay:
    """One connection to a region's live DB: bitcraftsync first, BitJita's relay if that
    fails. snapshot() can be called many times; each call is one subscription.

        with RegionRelay(12) as relay:
            rows = relay.snapshot(['SELECT * FROM resource_state WHERE resource_id = 158'])
    """

    def __init__(self, region, timeout=60):
        self.region = int(region)
        self.timeout = timeout
        self.via = None
        self._ws = None
        self._request_id = 0

    def __enter__(self):
        self._connect()
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self):
        if self._ws is not None:
            try:
                self._ws.close()
            except Exception:
                pass
            self._ws = None

    def _open(self, url, headers):
        return websocket.create_connection(url, subprotocols=['v1.json.spacetimedb'],
                                           header=headers, timeout=min(self.timeout, 20))

    def _connect(self, skip_sync=False):
        global _sync_blocked, _last_sync_connect
        self.close()
        self._request_id = 0
        if not (_sync_blocked or skip_sync):
            wait = CONNECT_GAP - (time.time() - _last_sync_connect)
            if wait > 0:
                time.sleep(wait)
            _last_sync_connect = time.time()
            try:
                self._ws = self._open(SYNC_WS_URL.format(port=3000 + self.region, region=self.region),
                                      SYNC_HEADERS)
                self.via = 'bitcraftsync'
                return
            except Exception as e:
                if '429' in str(e):
                    _sync_blocked = True
                print(f'  [relay] R{self.region} bitcraftsync: {short_error(e)} — trying BitJita')
        self._ws = self._open(BITJITA_WS_URL.format(region=self.region), BITJITA_HEADERS)
        self.via = 'bitjita'

    def _query(self, queries):
        self._request_id += 1
        self._ws.send(json.dumps({'Subscribe': {'query_strings': list(queries),
                                                'request_id': self._request_id}}))
        # recv() alone never times out while the server keeps pinging, so read frame by
        # frame against a deadline. IdentityToken comes first; live updates for the
        # previous subscription are skipped. A bad query comes back as SubscriptionError
        # (BitJita) or as a TransactionUpdate with status Failed (bitcraftsync).
        deadline = time.time() + self.timeout
        while True:
            if time.time() > deadline:
                raise TimeoutError(f'no snapshot in {self.timeout}s')
            try:
                opcode, data = self._ws.recv_data(control_frame=True)
            except websocket.WebSocketTimeoutException:
                continue
            if opcode != websocket.ABNF.OPCODE_TEXT:
                continue
            msg = json.loads(data)
            if 'SubscriptionError' in msg:
                raise RuntimeError(msg['SubscriptionError'].get('error', msg))
            failed = msg.get('TransactionUpdate', {}).get('status', {}).get('Failed')
            if failed:
                raise RuntimeError(failed)
            if 'InitialSubscription' in msg:
                break
        tables = {}
        for table in msg['InitialSubscription']['database_update']['tables']:
            rows = tables.setdefault(table['table_name'], [])
            for update in table['updates']:
                rows.extend(json.loads(row) for row in update['inserts'])
        if self.via == 'bitcraftsync':
            tables = {t: [_light(r) for r in rows] for t, rows in tables.items()}
        return tables

    def snapshot(self, queries):
        """Run subscription queries and return the initial rows as {table_name: [row, ...]},
        each row a list of column values in schema order. A failure on bitcraftsync is
        retried once on BitJita's relay."""
        try:
            return self._query(queries)
        except Exception as e:
            if self.via != 'bitcraftsync':
                raise
            print(f'  [relay] R{self.region} bitcraftsync: {short_error(e)} — retrying on BitJita')
            self._connect(skip_sync=True)
            return self._query(queries)

    def bounds(self):
        """The region's chunk rectangle: (min_x, min_z, width, height) in chunks.
        A location_state chunk_index is z * 1000 + x + 1, so map row z is the index
        range [z * 1000, z * 1000 + 1000)."""
        row = self.snapshot(['SELECT * FROM world_region_state'])['world_region_state'][0]
        # [id, min_chunk_x, min_chunk_z, width_chunks, height_chunks, region_index, ...]
        return row[1], row[2], row[3], row[4]
