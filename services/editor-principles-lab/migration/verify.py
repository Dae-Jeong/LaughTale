"""Migration-only verification: isolated data and append-only named evidence runs."""
import json
import os
from pathlib import Path
import subprocess
from datetime import datetime, timezone

root = Path(__file__).resolve().parents[1]
out = root / 'migration' / 'verification-1'
out.mkdir(exist_ok=False)
env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1',
           LAB_PYTHON='/Users/marin/.pyenv/versions/3.13.2/bin/python3',
           LAB_RESULTS_DIR=str(out / 'browser'),
           PLAYWRIGHT_MODULE='/tmp/editor-migration-deps.yuPGdu/node_modules/playwright/index.mjs')
commands = {
    'python313': [env['LAB_PYTHON'], '-m', 'unittest', 'discover', '-s', 'tests', '-v'],
    'python39': ['/usr/bin/python3', '-m', 'unittest', 'discover', '-s', 'tests', '-v'],
    'core': ['node', '--test', 'tests/core.test.mjs'],
    'index-offline': [env['LAB_PYTHON'], 'experiments/index_compare.py'],
    'conflict': [env['LAB_PYTHON'], 'experiments/run.py', '--output', str(out / 'conflict.json')],
    'browser': ['node', 'tests/browser.mjs'],
}
results = {}
for name, argv in commands.items():
    start = datetime.now(timezone.utc).isoformat()
    run = subprocess.run(argv, cwd=root, env=env, text=True, stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT, timeout=120)
    (out / (name + '.log')).write_text(run.stdout)
    results[name] = {'argv': argv, 'start': start, 'end': datetime.now(timezone.utc).isoformat(),
                     'returncode': run.returncode}
    print(name, run.returncode, flush=True)
(out / 'summary.json').write_text(json.dumps(results, indent=2))
