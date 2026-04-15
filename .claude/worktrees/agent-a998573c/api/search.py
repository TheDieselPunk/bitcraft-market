"""
GET /api/search?username=<name>

Returns player ID, username, and toolbelt tools.
"""

import json
import sys
import os
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

sys.path.insert(0, os.path.dirname(__file__))
from _lib import api_get, get_player_id, get_toolbelt, cors_headers


class handler(BaseHTTPRequestHandler):
    def do_OPTIONS(self):
        self._send(200, {})

    def do_GET(self):
        params = parse_qs(urlparse(self.path).query)
        username = params.get('username', [''])[0].strip()

        if not username:
            self._send(400, {'error': 'username parameter is required'})
            return

        try:
            player_id = get_player_id(username)
            if not player_id:
                self._send(404, {'error': f'Player "{username}" not found'})
                return

            tools = get_toolbelt(player_id)
            if not tools:
                self._send(200, {
                    'player_id': player_id,
                    'username': username,
                    'tools': {},
                    'warning': 'No tools found in toolbelt',
                })
                return

            # Serialize tools with string keys for JSON
            self._send(200, {
                'player_id': player_id,
                'username': username,
                'tools': {str(k): v for k, v in tools.items()},
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
