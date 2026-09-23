"""One-shot read-only runtime/copy check; preserves the old evidence directory."""
import hashlib
import json
from pathlib import Path
import subprocess
from urllib.request import urlopen
from datetime import datetime, timezone

root = Path(__file__).resolve().parents[1]
before = json.loads((root / 'migration/source-before.json').read_text())
after = json.loads((root / 'migration/source-after-cutover.json').read_text())
files = {k: v for k, v in before['entries'].items() if v['kind'] == 'file'
         and not k.startswith(('.git/', 'node_modules/')) and '__pycache__' not in k and not k.endswith('.pyc')}
changed = [k for k, v in files.items() if hashlib.sha256((root / k).read_bytes()).hexdigest() != v['sha256']]
assert set(changed) == {'AGENTS.md', 'README.md', 'experiments/run.py', 'tests/browser.mjs'}, changed
assert not after['changed_entries'] and after['git_equal']
assert not (root / '.git').exists() and not (root / 'node_modules').exists()
http = {}
for path in ['/', '/guide/', '/results/', '/api/documents']:
    with urlopen('http://127.0.0.1:18120' + path, timeout=3) as response:
        http[path] = {'status': response.status, 'bytes': len(response.read())}
repos = json.loads(subprocess.check_output(['orca', 'repo', 'list', '--json']))['result']['repos']
setups = json.loads(subprocess.check_output(['orca', 'project', 'setups', '--json']))['result']['setups']
old = 'e1f1e63b-522a-455c-8377-0bb351e2a81b'
assert not any(x['id'] == old for x in repos + setups)
data = {str(p.relative_to(root / 'data')): hashlib.sha256(p.read_bytes()).hexdigest() for p in (root / 'data').rglob('*') if p.is_file()}
source_data = {k[5:]: v['sha256'] for k, v in before['entries'].items() if k.startswith('data/') and v['kind'] == 'file'}
assert data == source_data
result = {'at': datetime.now(timezone.utc).isoformat(), 'source_snapshot_at': before['at'],
          'source_entries_preserved': len(before['entries']), 'source_git_equal': after['git_equal'],
          'copied_files': len(files), 'intentional_changed_files': changed,
          'original_results_all_identical': all(k not in changed for k in files if k.startswith('results/')),
          'http': http, 'data_files': len(data), 'old_repo_and_setup_absent': True,
          'old_pid_exists': subprocess.run(['kill', '-0', '36889'], capture_output=True).returncode == 0,
          'new_process': subprocess.check_output(['ps', '-p', '28137', '-o', 'pid=,command='], text=True),
          'new_cwd': subprocess.check_output(['lsof', '-a', '-p', '28137', '-d', 'cwd', '-Fn'], text=True),
          'sha256': {k: hashlib.sha256((root / k).read_bytes()).hexdigest() for k in ['server.py','web/core.mjs','results/report.md','migration/source-before.json']}}
with (root / 'migration/final-check.json').open('x') as stream:
    json.dump(result, stream, ensure_ascii=False, indent=2)
print(json.dumps(result, ensure_ascii=False, indent=2))
