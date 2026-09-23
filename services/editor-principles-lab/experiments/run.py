"""Isolated E2 negative control; never opens the app's data directory."""
import copy
import argparse
import json
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server import Store


def run():
    with tempfile.TemporaryDirectory(prefix='editor-e2-') as directory:
        store = Store(directory)
        first = store.create('baseline')
        a, b = copy.deepcopy(first), copy.deepcopy(first)
        a['document']['title'], b['document']['title'] = 'A edit', 'B edit'
        def body(r):
            return {'document': r['document'], 'expected_revision': r['revision']}
        protected = [store.save(first['document']['id'], body(r))[0] for r in (a, b)]
        protected_final = store.read(first['document']['id'])
        # Intentionally broken ONLY in this temporary fixture: no revision check.
        store.write(a)
        store.write(b)
        broken_final = store.read(first['document']['id'])
        before = store.save(first['document']['id'], body(b), 'before')[0]
        before_final = store.read(first['document']['id'])
        status, lost_commit = store.save(first['document']['id'], body(b), 'lost')
        # Store commits; HTTP layer deliberately drops the response (API test proves it).
        retry = store.save(first['document']['id'], body(b))[0]
        result = {'experiment':'E2','fixture':'temporary directory, removed after run','seed':'fixed A/B titles; random document ID','protected_statuses':protected,'protected_final':protected_final,'broken_final':broken_final,'before_status':before,'before_final':before_final,'lost_commit':lost_commit,'retry_after_lost':retry,'oracle':{'protected_one_winner':protected == [200,409] and protected_final['document']['title']=='A edit','broken_lost_update':broken_final['document']['title']=='B edit','before_unchanged':before_final==broken_final,'lost_persisted_and_retry_conflicts':status==200 and retry==409}}
        assert all(result['oracle'].values()), result
        return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, default=Path(__file__).resolve().parents[1] / 'results/conflict-failure.json')
    output = parser.parse_args().output
    output.parent.mkdir(exist_ok=True)
    result=run()
    output.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(result['oracle'], ensure_ascii=False))
    print(output)
