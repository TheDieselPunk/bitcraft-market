"""
GET /api/version

Returns the current deployment version from Vercel's auto-injected
git environment variables, plus the recipe cache build timestamp.
"""

import json
import os
import sys
from http.server import BaseHTTPRequestHandler

sys.path.insert(0, os.path.dirname(__file__))
from _lib import cors_headers, load_recipes_cache


class handler(BaseHTTPRequestHandler):
    def do_OPTIONS(self):
        self._send(200, {})

    def do_GET(self):
        sha    = os.environ.get('VERCEL_GIT_COMMIT_SHA', '')
        branch = os.environ.get('VERCEL_GIT_COMMIT_REF', 'local')
        env    = os.environ.get('VERCEL_ENV', 'development')

        meta = load_recipes_cache().get('__meta__', {})

        self._send(200, {
            'sha':            sha[:7] if sha else 'dev',
            'sha_full':       sha or 'dev',
            'branch':         branch,
            'env':            env,
            'recipes_built_at': meta.get('built_at'),
            'recipes_counts': {
                'market':       meta.get('market', 0),
                'intermediates': meta.get('intermediates', 0),
                'ingredients':  meta.get('ingredients', 0),
            },
        })

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
