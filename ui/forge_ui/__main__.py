"""Run the forge-ui server: python3 -m forge_ui."""
from __future__ import annotations

import argparse
import errno
import os
import socket
import sys
import webbrowser

from forge_ui import __version__, reader
from forge_ui.server import make_server


HOST = '127.0.0.1'  # deliberately not an option: the UI shows prompts and reviews from private repos
DEFAULT_PORT = 8765


def _parser():
    parser = argparse.ArgumentParser(
        prog='python3 -m forge_ui',
        description='A small, read-only local UI for forge runs. Listens on 127.0.0.1 only.')
    parser.add_argument('--plan', action='append', default=[], metavar='DIR',
                        help='a plan dir to show (repeatable)')
    parser.add_argument('--root', action='append', default=[], metavar='DIR',
                        help='show every plan dir at depth 0-2 under DIR (repeatable)')
    parser.add_argument('--no-registry', action='store_true',
                        help="do not read forge's run registry")
    parser.add_argument('--registry-dir', metavar='DIR',
                        help='read the run registry from DIR instead of ~/.config/forge/runs')
    parser.add_argument('--port', type=int, default=DEFAULT_PORT,
                        help=f'port to listen on (default {DEFAULT_PORT}; 0 picks a free one)')
    parser.add_argument('--quiet', action='store_true', help='do not log requests')
    parser.add_argument('--open', action='store_true', help='open the page in the browser once started')
    parser.add_argument('--version', action='version', version=f'forge-ui {__version__}')
    return parser


def _message(level, text):
    print(f'forge-ui: {level}: {text}', file=sys.stderr)


def _port_taken(port):
    # Binding is not enough: SO_REUSEADDR lets a 127.0.0.1 bind succeed on macOS while another process
    # listens on 0.0.0.0 or [::] (dual-stack) at the same port, and the requests then go to whichever wins.
    # A probe connect reaches any of those listeners, so it is the check that matters.
    if port == 0:
        return False
    try:
        socket.create_connection((HOST, port), timeout=1).close()
    except socket.timeout:
        return True  # something accepted the SYN but does not answer: treat as taken
    except OSError:
        return False  # refused or unreachable: nothing is listening there
    return True


def main(argv=None):
    parser = _parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:  # --version, --help and usage errors leave through argparse
        return exc.code or 0
    if not 0 <= args.port <= 65535:
        _message('error', f'port {args.port} is out of range (0-65535)')
        return 2

    for flag, paths in (('--plan', args.plan), ('--root', args.root)):
        for path in paths:
            if not os.path.exists(path):
                _message('warning', f'{flag} {path}: no such file or directory')

    in_use = f'port {args.port} is already in use; try --port with another number'
    if _port_taken(args.port):
        _message('error', in_use)
        return 2

    catalog = reader.Catalog(plans=args.plan, roots=args.root, registry=not args.no_registry,
                             registry_dir=args.registry_dir)
    try:
        server = make_server(catalog, HOST, args.port, quiet=args.quiet)
    except OSError as exc:
        if exc.errno == errno.EADDRINUSE:
            _message('error', in_use)
        else:
            _message('error', f'cannot listen on {HOST}:{args.port}: {exc.strerror or exc}')
        return 2

    url = f'http://{HOST}:{server.server_address[1]}/'
    try:
        print(f'forge-ui on {url} ({len(catalog.runs())} runs)', flush=True)
        if args.open:
            webbrowser.open(url)
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
