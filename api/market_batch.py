"""
GET /api/market_batch

Fetches raw sell orders for all tool ingredient item IDs from BitJita in
parallel, returning:
  { items: { "<id>": [{price, qty, regionId, regionName}] }, fetched_at }

The client computes the final price using whichever pricing mode is selected
(Median Sell / Raw VWAP / Trimmed VWAP / Cost to Fill).
"""

import json
import sys
import os
import time
import concurrent.futures
from http.server import BaseHTTPRequestHandler

sys.path.insert(0, os.path.dirname(__file__))
from _lib import api_get, cors_headers

# All ingredient item IDs used across all tool tiers (T1 – T10)
ITEM_IDS = [
    '1050001',    '1090004',    '1020003',    '1070004',    # T1 Ferralith
    '2050001',    '2090004',    '2020003',    '2070004',    # T2 Pyrelite
    '3050001',    '3090004',    '3020003',    '3070004',    # T3 Emarium
    '4050001',    '4090004',    '4020003',    '4070004',    # T4 Elenvar
    '5050001',    '5090004',    '5020003',    '5070004',    # T5 Luminite
    '6050001',    '6090004',    '6020003',    '6070004',    # T6 Rathium
    '1899017490', '625147590',  '1639308227', '806992520',  # T7 Aurumite
    '1464752960', '1224328894', '28056473',   '1743778001', # T8 Celestium
    '445742898',  '471802228',  '1227914325', '478917',     # T9 Umbracite
    '2069757207', '547017087',  '117329467',  '944952036',  # T10 Astralite
]


def fetch_item(item_id):
    """Fetch all sell orders for one item from BitJita."""
    try:
        data = api_get(f'/api/market/item/{item_id}')
        # API may return a top-level list or an object with a sellOrders key
        listing_list = (
            data if isinstance(data, list)
            else data.get('sellOrders', data.get('orders', []))
        )
        orders = []
        for listing in listing_list:
            price = listing.get('priceThreshold') or listing.get('price')
            qty   = listing.get('quantity') or listing.get('qty') or 1
            if price is None or float(price) <= 0:
                continue
            orders.append({
                'price':      float(price),
                'qty':        int(qty),
                'regionId':   listing.get('regionId'),
                'regionName': listing.get('regionName', ''),
            })
        return item_id, orders
    except Exception:
        return item_id, []


class handler(BaseHTTPRequestHandler):
    def do_OPTIONS(self):
        self._send(200, {})

    def do_GET(self):
        try:
            items = {}
            with concurrent.futures.ThreadPoolExecutor(max_workers=20) as executor:
                futures = {executor.submit(fetch_item, iid): iid for iid in ITEM_IDS}
                for future in concurrent.futures.as_completed(futures):
                    iid, orders = future.result()
                    items[iid] = orders

            self._send(200, {
                'items':      items,
                'fetched_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
            })
        except Exception as e:
            self._send(500, {'error': str(e)})

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
