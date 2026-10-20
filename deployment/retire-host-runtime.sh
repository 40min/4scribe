#!/usr/bin/env bash
# Permanent retirement only; no host rollback. Owner-reviewed root execution.
set -euo pipefail
[[ $EUID == 0 ]] || { echo 'Run as root.' >&2; exit 1; }
cd /opt/40zen/services/4scribe
python3 - <<'PY'
import json, os, pathlib, subprocess, urllib.request
root=pathlib.Path('/opt/40zen/services/4scribe')
venv=root/'.venv'
unit=pathlib.Path('/etc/systemd/system/fourscribe.service')
drop=pathlib.Path('/etc/systemd/system/fourscribe.service.d')
image='sha256:7708a83bff51186ec273612b63fe6e6575fb46971e49e74a5c226610791e6517'
def require(ok,msg):
 if not ok: raise SystemExit('REFUSED: '+msg)
def out(*a): return subprocess.check_output(a,text=True).strip()
def check_container():
 state=out('docker','inspect','--format','{{.Image}} {{.State.Status}} {{.State.Health.Status}}','fourscribe-container')
 require(state==image+' running healthy','unexpected container image/state')
 mounts=json.loads(out('docker','inspect','--format','{{json .Mounts}}','fourscribe-container'))
 require(len(mounts)==1 and mounts[0]['Source']==str(root/'model-cache/hf') and mounts[0]['Destination']=='/models/hf' and not mounts[0]['RW'],'unexpected container mounts')
 with urllib.request.urlopen('http://127.0.0.1:8093/healthz',timeout=5) as response: health=json.load(response)
 require(health.get('ok') is True and health.get('verifier')=='silero-vad-6.2.3' and health.get('aligner')=='qwen3-forced-aligner-0.6b' and health.get('word_timestamps') is True,'unexpected health/verifier')
check_container()
require(out('systemctl','show','fourscribe.service','--value','-p','ActiveState')=='inactive','host unit active')
require(out('systemctl','show','fourscribe.service','--value','-p','UnitFileState')=='disabled','host unit not disabled')
require(out('systemctl','show','fourscribe.service','--value','-p','FragmentPath')==str(unit),'unexpected unit path')
require(not out('systemctl','show','fourscribe.service','--value','-p','DropInPaths'),'unexpected effective drop-ins')
require(str(venv/'bin/python') in out('systemctl','show','fourscribe.service','--value','-p','ExecStart'),'unexpected legacy executable')
for target in (venv,unit,drop):
 require(target.resolve()==target,'symlink path: '+str(target))
 require(not any(p.is_symlink() for p in (target,*target.parents)),'symlink ancestor: '+str(target))
require(venv.is_dir() and unit.is_file() and drop.is_dir(),'missing or wrong-type target')
require(not list(drop.iterdir()),'drop-in directory not empty')
# Reject mountpoints anywhere beneath the directory before recursive deletion.
mounts=json.loads(out('findmnt','--json','--output','TARGET'))
def targets(nodes):
 for n in nodes:
  yield n['target']
  yield from targets(n.get('children',[]))
require(not any(p==str(venv) or p.startswith(str(venv)+'/') for p in targets(mounts['filesystems'])),'virtualenv contains a mount')
for proc in pathlib.Path('/proc').iterdir():
 if not proc.name.isdigit(): continue
 try:
  for name in ('exe','cwd'):
   try: path=os.readlink(proc/name)
   except FileNotFoundError: continue
   require(not (path==str(venv) or path.startswith(str(venv)+'/')),'live virtualenv process '+proc.name)
  try:
   with (proc/'maps').open() as f: require(not any(str(venv)+'/' in line for line in f),'live virtualenv mapping '+proc.name)
  except FileNotFoundError: pass
 except (FileNotFoundError,ProcessLookupError): pass
 # Permission errors fail closed; root must prove no live dependency.
# Recheck immediately before deletion. No service or container stop/restart.
check_container()
print('Preflight passed; removing exact obsolete host targets.',flush=True)
subprocess.run(['du','-sB1',str(venv)],check=True)
unit.unlink()
drop.rmdir()
subprocess.run(['systemctl','daemon-reload'],check=True)
subprocess.run(['rm','-rf','--one-file-system','--',str(venv)],check=True)
require(not venv.exists() and not unit.exists() and not drop.exists(),'removal incomplete')
check_container()
print('host_retirement=complete; container accepted and healthy; models/secrets retained')
PY
