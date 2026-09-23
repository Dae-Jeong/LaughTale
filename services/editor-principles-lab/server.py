"""Local learning server: one process, one lock, atomic JSON replacement."""
import argparse
import copy
import json
import math
import os
from pathlib import Path
import re
import socket
import tempfile
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent


def validate(doc, doc_id=None):
    if not isinstance(doc, dict) or doc.get('schema_version') != 1:
        raise ValueError('schema_version must be 1')
    if not isinstance(doc.get('id'), str) or not re.fullmatch(r'[a-f0-9-]{36}', doc['id']) or (doc_id and doc['id'] != doc_id):
        raise ValueError('invalid document id')
    if not isinstance(doc.get('title'), str) or not 1 <= len(doc['title']) <= 120:
        raise ValueError('title must have 1–120 characters')
    for k in ('width', 'height'):
        if type(doc.get(k)) not in (int, float) or not math.isfinite(doc[k]) or not 100 <= doc[k] <= 2000:
            raise ValueError('invalid canvas dimensions')
    if not isinstance(doc.get('elements'), list) or len(doc['elements']) > 200:
        raise ValueError('maximum 200 elements')
    ids = set()
    for e in doc['elements']:
        if not isinstance(e, dict) or not isinstance(e.get('id'), str) or not 1 <= len(e['id']) <= 80 or e['id'] in ids:
            raise ValueError('invalid/duplicate element id')
        ids.add(e['id'])
        if e.get('type') not in ('text', 'rectangle'):
            raise ValueError('invalid element type')
        for k in ('x', 'y', 'width', 'height'):
            if type(e.get(k)) not in (int, float) or not math.isfinite(e[k]) or e[k] < 0:
                raise ValueError('invalid geometry')
        if e['width'] < 1 or e['height'] < 1 or e['x'] + e['width'] > doc['width'] or e['y'] + e['height'] > doc['height']:
            raise ValueError('element outside canvas')
        if e['type'] == 'text' and (not isinstance(e.get('text'), str) or len(e['text']) > 500):
            raise ValueError('invalid text')
        if e['type'] == 'rectangle' and (not isinstance(e.get('fill'), str) or not re.fullmatch(r'#[0-9a-fA-F]{6}', e['fill'])):
            raise ValueError('invalid fill')
    return copy.deepcopy(doc)


class Store:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()

    def read(self, doc_id):
        if not re.fullmatch(r'[a-f0-9-]{36}', doc_id):
            raise FileNotFoundError(doc_id)
        return json.loads((self.directory / f'{doc_id}.json').read_text())

    def write(self, record):
        fd, name = tempfile.mkstemp(dir=self.directory, suffix='.tmp')
        try:
            with os.fdopen(fd, 'w') as f:
                json.dump(record, f, ensure_ascii=False, allow_nan=False)
                f.flush()
                os.fsync(f.fileno())
            os.replace(name, self.directory / f"{record['document']['id']}.json")
        finally:
            if os.path.exists(name):
                os.unlink(name)

    def create(self, title='새 문서'):
        if not isinstance(title, str) or not 1 <= len(title) <= 120:
            raise ValueError('invalid title')
        record = {'revision': 1, 'document': {'schema_version': 1, 'id': str(uuid.uuid4()), 'title': title, 'width': 800, 'height': 500, 'elements': []}}
        with self.lock:
            self.write(record)
        return record

    def save(self, doc_id, body, fault='none'):
        doc = validate(body.get('document'), doc_id)
        expected = body.get('expected_revision')
        if type(expected) is not int or expected < 1:
            raise ValueError('expected_revision must be a positive integer')
        if fault not in ('none', 'before', 'lost'):
            raise ValueError('invalid fault')
        with self.lock:
            current = self.read(doc_id)
            if current['revision'] != expected:
                return 409, {'error': 'conflict', **current}
            if fault == 'before':
                return 503, {'error': 'injected_before_write'}
            record = {'document': doc, 'revision': expected + 1}
            self.write(record)
            return 200, record


def make_server(port=18120, directory=ROOT / 'data'):
    store = Store(directory)

    class Handler(BaseHTTPRequestHandler):
        def reply(self, status, body):
            raw = json.dumps(body, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(raw)))
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):
            path = urlsplit(self.path).path
            try:
                if path == '/api/documents':
                    with store.lock:
                        records = [json.loads(p.read_text()) for p in sorted(store.directory.glob('*.json'))]
                    self.reply(200, {'documents': [{'id': r['document']['id'], 'title': r['document']['title'], 'revision': r['revision']} for r in records]})
                elif path.startswith('/api/documents/'):
                    self.reply(200, store.read(path.rsplit('/', 1)[1]))
                else:
                    routes = {'/': ROOT / 'web/index.html', '/guide/': ROOT / 'web/guide.html', '/results/': ROOT / 'web/results.html'}
                    target = routes.get(path)
                    if target is None and path.startswith(('/web/', '/results/')):
                        target = (ROOT / path.lstrip('/')).resolve()
                        if not target.is_relative_to(ROOT / path.split('/')[1]):
                            target = None
                    if target is None or not target.is_file():
                        raise FileNotFoundError(path)
                    types = {'.html': 'text/html', '.css': 'text/css', '.mjs': 'text/javascript', '.json': 'application/json', '.md': 'text/plain', '.png': 'image/png', '.svg': 'image/svg+xml'}
                    raw = target.read_bytes()
                    self.send_response(200)
                    self.send_header('Content-Type', types.get(target.suffix, 'application/octet-stream') + '; charset=utf-8')
                    self.send_header('Content-Length', str(len(raw)))
                    self.send_header('Cache-Control', 'no-store')
                    self.end_headers()
                    self.wfile.write(raw)
            except FileNotFoundError:
                self.reply(404, {'error': 'not_found'})

        def mutate(self):
            # Reject cross-origin browser writes; no CORS, no remote interface.
            origin = self.headers.get('Origin')
            host = f'127.0.0.1:{self.server.server_port}'
            if self.headers.get('Host') != host or (origin and origin != f'http://{host}'):
                return self.reply(403, {'error': 'origin'})
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length <= 250_000:
                    raise ValueError('invalid body size')
                body = json.loads(self.rfile.read(length))
                if not isinstance(body, dict):
                    raise ValueError('body must be object')
                path = urlsplit(self.path).path
                if self.command == 'POST' and path == '/api/documents':
                    return self.reply(201, store.create(body.get('title', '새 문서')))
                if self.command != 'PUT' or not path.startswith('/api/documents/'):
                    return self.reply(404, {'error': 'not_found'})
                fault = self.headers.get('X-Lab-Fault', 'none')
                status, result = store.save(path.rsplit('/', 1)[1], body, fault)
                if fault == 'lost' and status == 200:
                    self.close_connection = True
                    self.connection.shutdown(socket.SHUT_RDWR)
                    self.connection.close()
                    return
                self.reply(status, result)
            except FileNotFoundError:
                self.reply(404, {'error': 'not_found'})
            except (ValueError, TypeError, KeyError) as e:
                self.reply(400, {'error': str(e)})
            except OSError:
                self.reply(503, {'error': 'storage_failure'})

        do_POST = mutate
        do_PUT = mutate

    server = ThreadingHTTPServer(('127.0.0.1', port), Handler)
    server.store = store
    return server


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=18120)
    parser.add_argument('--data-dir', type=Path, default=ROOT / 'data')
    args = parser.parse_args()
    server = make_server(args.port, args.data_dir)
    print(f'Editor lab http://127.0.0.1:{server.server_port}/ PID={os.getpid()} data={args.data_dir}', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
