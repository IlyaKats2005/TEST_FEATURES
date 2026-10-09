#!/usr/bin/env python3
"""Share PocketMatch using a temporary HTTPS URL; keep the existing SQLite file."""
import argparse
import os
from pathlib import Path
import re
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import time
from urllib.request import Request, urlopen


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', type=Path, default=Path(__file__).resolve().parent)
    args = parser.parse_args()
    project = args.project.resolve()
    cloudflared = shutil.which('cloudflared')
    if not cloudflared:
        raise SystemExit('Install cloudflared first: brew install cloudflared')
    if not (project / 'mobile_app/server.py').is_file():
        raise SystemExit('Choose the PocketMatch folder with --project.')
    with socket.socket() as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(('127.0.0.1', 8001))
        except OSError:
            raise SystemExit('Port 8001 is occupied. Stop the existing PocketMatch server first.')
    data = project / 'mobile_app/data'
    data.mkdir(parents=True, exist_ok=True)
    database = data / 'history.sqlite3'
    if database.exists():
        backup = data / ('before-public-' + time.strftime('%Y%m%d-%H%M%S') + '.sqlite3')
        with sqlite3.connect(database) as source, sqlite3.connect(backup) as destination:
            source.backup(destination)
        backup.chmod(0o600)
    children = []
    def stop(signum, frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    address = data / 'public-url.txt'
    address.unlink(missing_ok=True)
    try:
        with (data / 'tunnel.log').open('w') as log:
            tunnel = subprocess.Popen([cloudflared, 'tunnel', '--config', '/dev/null',
                '--url', 'http://127.0.0.1:8001', '--no-autoupdate'], stdout=log, stderr=log)
            children.append(tunnel)
            origin = None
            for _ in range(90):
                if tunnel.poll() is not None:
                    raise RuntimeError('Tunnel stopped. See mobile_app/data/tunnel.log.')
                match = re.search(r'https://[a-z0-9-]+\.trycloudflare\.com', (data / 'tunnel.log').read_text())
                if match:
                    origin = match.group(0)
                    break
                time.sleep(1)
            if not origin:
                raise RuntimeError('No tunnel address received after 90 seconds.')
            env = os.environ.copy()
            env.update(PUBLIC_ORIGIN=origin, HISTORY_DB=str(database),
                HF_TOKEN_FILE=str(project / 'TOKEN.txt'),
                CATALOGUE_FILE=str(project / 'given_industrial_products.json'),
                TRUST_CADDY_PROXY='0')
            server = subprocess.Popen([sys.executable, '-m', 'gunicorn', '--chdir',
                str(project / 'mobile_app'), '--bind', '127.0.0.1:8001',
                '--workers', '1', '--threads', '8', '--timeout', '300',
                'server:create_app()'], env=env)
            children.append(server)
            for _ in range(30):
                if server.poll() is not None:
                    raise RuntimeError('The app could not start.')
                try:
                    req = Request('http://127.0.0.1:8001/healthz', headers={'Host': origin.removeprefix('https://')})
                    with urlopen(req, timeout=2) as response:
                        if response.status == 200:
                            break
                except OSError:
                    time.sleep(1)
            else:
                raise RuntimeError('The app did not become ready.')
            address.write_text(origin + '\n')
            print('\nPublic address: ' + origin, flush=True)
            print('Use your existing PocketMatch account. Keep this Mac awake. Ctrl+C stops sharing.', flush=True)
            while all(child.poll() is None for child in children):
                time.sleep(1)
            raise RuntimeError('The app or tunnel stopped; sharing has ended.')
    except KeyboardInterrupt:
        print('\nStopping public sharing.', flush=True)
    finally:
        for child in reversed(children):
            if child.poll() is None:
                child.terminate()
        for child in reversed(children):
            try:
                child.wait(timeout=30)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
        address.unlink(missing_ok=True)


if __name__ == '__main__':
    main()
