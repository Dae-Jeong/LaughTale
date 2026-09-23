import concurrent.futures
import copy
import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from server import make_server, Store


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.server = make_server(0, self.temp.name)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.temp.cleanup()

    def call(self, method, path='/api/documents', body=None, headers=None):
        conn = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=3)
        conn.request(method, path, json.dumps(body) if body is not None else None, headers or {})
        response = conn.getresponse()
        raw = response.read()
        conn.close()
        return response.status, json.loads(raw) if raw else None

    def create(self):
        code, record = self.call('POST', body={})
        self.assertEqual(code, 201)
        return record

    def put(self, record, **kw):
        return self.call('PUT', '/api/documents/'+record['document']['id'], {'document':record['document'], 'expected_revision':record['revision']}, **kw)

    def test_create_list_save_reload_and_restart_storage(self):
        r = self.create()
        r['document']['title'] = '보존'
        status, saved = self.put(r)
        self.assertEqual((status, saved['revision']), (200, 2))
        self.assertEqual(self.call('GET')[1]['documents'][0]['title'], '보존')
        self.assertEqual(Store(self.temp.name).read(r['document']['id']), saved)

    def test_concurrent_revision_one_winner(self):
        r = self.create()
        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            results = list(pool.map(lambda _: self.put(r), range(2)))
        self.assertEqual(sorted(s for s, _ in results), [200, 409])
        self.assertEqual(self.server.store.read(r['document']['id'])['revision'], 2)

    def test_server_restart_restores_saved_document(self):
        r = self.create()
        r['document']['title'] = '재시작 후 복원'
        _, saved = self.put(r)
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.server = make_server(0, self.temp.name)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        code, restored = self.call('GET', '/api/documents/' + r['document']['id'])
        self.assertEqual(code, 200)
        self.assertEqual(restored, saved)

    def test_before_failure_keeps_last_file(self):
        r = self.create()
        r['document']['title'] = '미저장'
        self.assertEqual(self.put(r, headers={'X-Lab-Fault':'before'})[0], 503)
        self.assertEqual(self.server.store.read(r['document']['id'])['revision'], 1)

    def test_response_lost_writes_and_retry_conflicts(self):
        r = self.create()
        with self.assertRaises(http.client.RemoteDisconnected):
            self.put(r, headers={'X-Lab-Fault':'lost'})
        self.assertEqual(self.server.store.read(r['document']['id'])['revision'], 2)
        self.assertEqual(self.put(r)[0], 409)

    def test_atomic_replace_failure_retains_file(self):
        r = self.create()
        with patch('server.os.replace', side_effect=OSError('injected')):
            self.assertEqual(self.put(r)[0], 503)
        self.assertEqual(self.server.store.read(r['document']['id']), r)
        self.assertEqual(list(Path(self.temp.name).glob('*.tmp')), [])

    def test_invalid_inputs(self):
        r = self.create()
        bad = [dict(r['document'], width=float('nan')), dict(r['document'], height=float('inf')), dict(r['document'], title=''), dict(r['document'], elements=[{'id':'a'}])]
        e = {'id':'x','type':'rectangle','x':0,'y':0,'width':20,'height':20,'fill':'#000000'}
        bad.append(dict(r['document'],elements=[e,e]))
        for doc in bad:
            with self.subTest(doc=doc):
                self.assertEqual(self.put({'document':doc,'revision':1})[0],400)
        self.assertEqual(self.put({'document':r['document'],'revision':True})[0],400)

    def test_missing_and_static_boundaries(self):
        for path in ['/data/test.json','/.git/config','/server.py','/web/../server.py','/api/documents/nope']:
            self.assertEqual(self.call('GET',path)[0],404)

    def test_origin_host_and_size(self):
        self.assertEqual(self.call('POST',body={},headers={'Origin':'http://evil.invalid'})[0],403)
        self.assertEqual(self.call('POST',body={},headers={'Host':'evil.invalid'})[0],403)
        self.assertEqual(self.call('POST',body={'title':'x'*250001})[0],400)


if __name__ == '__main__':
    unittest.main()
