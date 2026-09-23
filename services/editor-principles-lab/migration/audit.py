"""Read-only tree inventory; writes evidence only to an explicitly supplied output."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
from datetime import datetime, timezone


def inventory(root):
    entries = {}
    for parent, dirs, files in os.walk(root, followlinks=False):
        for name in sorted(dirs + files):
            path = Path(parent) / name
            rel = str(path.relative_to(root))
            stat = path.lstat()
            row = {'mode': oct(stat.st_mode & 0o777), 'size': stat.st_size}
            if path.is_symlink():
                row.update(kind='symlink', target=os.readlink(path))
            elif path.is_dir():
                row = {'kind': 'directory', 'mode': row['mode']}
            else:
                row.update(kind='file', sha256=hashlib.sha256(path.read_bytes()).hexdigest())
            entries[rel] = row
    return dict(sorted(entries.items()))


def git(root, *args):
    result = subprocess.run(['git', '-C', str(root), *args], capture_output=True, text=True)
    return {'code': result.returncode, 'stdout': result.stdout, 'stderr': result.stderr}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('root', type=Path)
    parser.add_argument('output', type=Path)
    parser.add_argument('--compare', type=Path)
    args = parser.parse_args()
    result = {'at': datetime.now(timezone.utc).isoformat(), 'root': str(args.root.resolve()),
              'entries': inventory(args.root)}
    result['git'] = {label: git(args.root, *command) for label, command in {
        'status': ['status', '--porcelain=v1', '--untracked-files=all'],
        'ignored': ['ls-files', '--others', '--ignored', '--exclude-standard'],
        'tracked': ['ls-files'], 'head': ['rev-parse', '--verify', 'HEAD'],
        'remotes': ['remote', '-v'], 'diff': ['diff', '--binary'],
        'staged': ['diff', '--cached', '--binary']}.items()}
    if args.compare:
        previous = json.loads(args.compare.read_text())
        result['changed_entries'] = [key for key in sorted(set(previous['entries']) | set(result['entries']))
                                     if previous['entries'].get(key) != result['entries'].get(key)]
        result['git_equal'] = previous['git'] == result['git']
    with args.output.open('x') as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2)
    print(json.dumps({'entries': len(result['entries']), 'changed': result.get('changed_entries'),
                      'git_equal': result.get('git_equal'), 'output': str(args.output)}))
