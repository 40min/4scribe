#!/usr/bin/env bash
# Final residual scratch cleanup, root/owner only. Exact approved paths only.
# No wildcards, no model/image/secret/repo deletion, no service/container restart.
set -euo pipefail
[[ $EUID == 0 ]] || { echo 'Run as root.' >&2; exit 1; }
cd /opt/40zen/services/4scribe
python3 - <<'PY'
import json, os, pathlib, subprocess, urllib.request
root = pathlib.Path('/opt/40zen/services/4scribe')
image = 'sha256:7708a83bff51186ec273612b63fe6e6575fb46971e49e74a5c226610791e6517'
targets = [
    pathlib.Path('/tmp/4scribe-backend-eval'),
    pathlib.Path('/tmp/4scribe-container-trial.FxWZDK'),
    pathlib.Path('/tmp/4scribe-container-trial.fs7H8M'),
    pathlib.Path('/tmp/4scribe-container-trial.h9inb2'),
    pathlib.Path('/tmp/4scribe-container-trial.wwn0IC'),
    pathlib.Path('/tmp/4scribe-completion-20261016T164940Z'),
    pathlib.Path('/tmp/4scribe-after-handoff-20261016T144331Z'),
    root / '.codex-worker',
]
empty_dirs = [pathlib.Path('/var/lib/fourscribe-trace')]

def require(ok, msg):
    if not ok:
        raise SystemExit('REFUSED: ' + msg)
def out(*a):
    return subprocess.check_output(a, text=True).strip()
def check_container():
    state = out('docker', 'inspect', '--format',
                '{{.Image}} {{.State.Status}} {{.State.Health.Status}}', 'fourscribe-container')
    require(state == image + ' running healthy', 'unexpected container image/state')
    mounts = json.loads(out('docker', 'inspect', '--format', '{{json .Mounts}}', 'fourscribe-container'))
    require(len(mounts) == 1
            and mounts[0]['Source'] == str(root / 'model-cache/hf')
            and mounts[0]['Destination'] == '/models/hf'
            and not mounts[0]['RW'], 'unexpected container mounts')
    with urllib.request.urlopen('http://127.0.0.1:8093/healthz', timeout=5) as response:
        health = json.load(response)
    require(health.get('ok') is True
            and health.get('verifier') == 'silero-vad-6.2.3'
            and health.get('aligner') == 'qwen3-forced-aligner-0.6b'
            and health.get('word_timestamps') is True, 'unexpected health/verifier')

def mount_targets():
    mounts = json.loads(out('findmnt', '--json', '--output', 'TARGET'))
    def walk(nodes):
        for n in nodes:
            yield n['target']
            yield from walk(n.get('children', []))
    return list(walk(mounts['filesystems']))

def check_target(p):
    if not p.exists():
        print('already_absent=' + str(p), flush=True)
        return None
    require(p.is_dir(), 'not a directory: ' + str(p))
    require(p.resolve() == p, 'symlink path: ' + str(p))
    require(not any(x.is_symlink() for x in (p, *p.parents)), 'symlink ancestor: ' + str(p))
    require(not any(m == str(p) or m.startswith(str(p) + '/') for m in mount_targets()),
            'mount under target: ' + str(p))
    for proc in pathlib.Path('/proc').iterdir():
        if not proc.name.isdigit():
            continue
        try:
            for name in ('exe', 'cwd'):
                try:
                    path = os.readlink(proc / name)
                except FileNotFoundError:
                    continue
                require(not (path == str(p) or path.startswith(str(p) + '/')),
                        'live process reference ' + proc.name + ' -> ' + str(p))
            try:
                with (proc / 'maps').open() as f:
                    require(not any(str(p) + '/' in line for line in f),
                            'live mapping ' + proc.name + ' -> ' + str(p))
            except FileNotFoundError:
                pass
        except (FileNotFoundError, ProcessLookupError):
            pass
        # Permission errors deliberately propagate: fail closed.
    return p

check_container()
existing = []
total = 0
for target in targets:
    p = check_target(target)
    if p is None:
        continue
    size = int(out('du', '-sB1', str(p)).split()[0])
    total += size
    print('will_delete=' + str(p) + ' bytes=' + str(size), flush=True)
    existing.append(p)

for p in empty_dirs:
    if not p.exists():
        print('already_absent=' + str(p), flush=True)
        continue
    require(p.is_dir() and p.resolve() == p, 'unexpected empty-dir target: ' + str(p))
    require(not any(p.iterdir()), 'directory not empty: ' + str(p))
    require(not any(m == str(p) or m.startswith(str(p) + '/') for m in mount_targets()),
            'mount at empty-dir target: ' + str(p))

check_container()
for p in existing:
    subprocess.run(['rm', '-rf', '--one-file-system', '--', str(p)], check=True)
    require(not p.exists(), 'removal incomplete: ' + str(p))
for p in empty_dirs:
    if p.exists():
        subprocess.run(['rmdir', '--', str(p)], check=True)
        require(not p.exists(), 'empty-dir removal incomplete: ' + str(p))
check_container()
print('cleanup_complete bytes_reclaimed=' + str(total) + ' container=healthy', flush=True)
PY
