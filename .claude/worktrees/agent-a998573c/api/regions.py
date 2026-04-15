"""
GET /api/regions

Returns list of active game regions with player counts.
"""

import json
import sys
import os
from http.server import BaseHTTPRequestHandler

sys.path.insert(0, os.path.dirname(__file__))
from _lib import api_get, cors_headers


class handler(BaseHTTPRequestHandler):
    def do_OPTIONS(self):
        self._send(200, {})

    def do_GET(self):
        try:
            data = api_get('/api/regions/status')
            regions = [
                {
                    'id':      r.get('regionId') or r.get('id'),
                    'name':    r.get('regionName') or r.get('name', ''),
                    'players': r.get('playerCount') or r.get('players', 0),
                }
                for r in data.get('regions', data if isinstance(data, list) else [])
                if (r.get('regionId') or r.get('id')) is not None
            ]
            regions.sort(key=lambda r: r['id'])
            self._send(200, {'regions': regions})
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
