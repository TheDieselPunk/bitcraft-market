"""
GET /api/inventories?player_id=<id>

Returns raw inventories (all bags) and item lookup for a player.
Used by Equipment Simulator to detect worn gear.
"""

import json
import sys
import os
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

sys.path.insert(0, os.path.dirname(__file__))
from _lib import api_get, cors_headers


class handler(BaseHTTPRequestHandler):
    def do_OPTIONS(self):
        self._send(200, {})

    def do_GET(self):
        params = parse_qs(urlparse(self.path).query)
        player_id = params.get('player_id', [''])[0].strip()

        if not player_id:
            self._send(400, {'error': 'player_id parameter is required'})
            return

        try:
            data = api_get(f'/api/players/{player_id}/inventories')
            eq_data = api_get(f'/api/players/{player_id}/equipment')
            self._send(200, {
                'items':       data.get('items', {}),
                'inventories': data.get('inventories', []),
                'equipment':   eq_data.get('equipment', []),
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
