#!/usr/bin/env python3
"""Serve brain project artifacts with stdlib-only HTTP.

This is a local viewer for generated paper-research artifacts. It serves files
from /Users/kublai/brain/projects so the performance HTML can poll its sidecar
JSON snapshot. It does not trade, sign, place orders, or load credentials.
"""

from __future__ import annotations

import argparse
import functools
import http.server
import os


DEFAULT_DIRECTORY = "/Users/kublai/brain/projects"
DEFAULT_HOST = "127.0.0.1"
# 8765 is reserved by the authenticated Brain gateway on this host. Keep this
# unauthenticated local artifact viewer on 8766 so the documented dashboard URL
# works without HMAC headers while staying bound to localhost.
DEFAULT_PORT = 8766


class NoCacheHandler(http.server.SimpleHTTPRequestHandler):
    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-store")
        super().end_headers()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Serve the local brain projects directory for live JSON dashboard refresh."
    )
    parser.add_argument("--directory", default=DEFAULT_DIRECTORY, help="Directory to serve.")
    parser.add_argument("--host", default=DEFAULT_HOST, help="Bind host. Defaults to localhost.")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="Bind port.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    directory = os.path.abspath(args.directory)
    if not os.path.isdir(directory):
        raise FileNotFoundError(f"directory not found: {directory}")

    handler = functools.partial(NoCacheHandler, directory=directory)
    server = http.server.ThreadingHTTPServer((args.host, args.port), handler)
    url = f"http://{args.host}:{args.port}/polymarket-weather-engine-performance.html"
    print(f"serving={directory}")
    print(f"url={url}")
    print("mode=paper_only live_trading=false wallet=false order_placement=false")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
