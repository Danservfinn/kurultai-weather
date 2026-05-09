#!/usr/bin/env python3
from __future__ import annotations
import argparse
import functools
import http.server
import socketserver
from pathlib import Path

ROOT = Path('/Users/kublai/brain/projects')

class Handler(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):
        if self.path in ('/', ''):
            self.path = '/polymarket-weather-engine-performance.html'
        return super().do_GET()

    def end_headers(self):
        self.send_header('Cache-Control', 'no-store')
        super().end_headers()

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--host', default='127.0.0.1')
    ap.add_argument('--port', type=int, default=8766)
    args = ap.parse_args()
    h = functools.partial(Handler, directory=str(ROOT))
    with socketserver.TCPServer((args.host, args.port), h) as srv:
        print(f'url=http://{args.host}:{args.port}/polymarket-weather-engine-performance.html', flush=True)
        srv.serve_forever()

if __name__ == '__main__':
    main()
