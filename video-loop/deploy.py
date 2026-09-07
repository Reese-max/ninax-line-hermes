"""Apply the reviewed scope with drift checks, dated backups, and a rollback receipt."""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

import yaml

BASE=Path(__file__).parent
PROFILE=Path('/home/box/irisx-failover-restore/20260906/profile-irisx')
HERMES=Path('/home/box/.hermes/hermes-agent')
CONFIG_SHA='43cca379b51a6be065d4eadbb8ffa6c68781c801cb549ead7d16396f941ad1e6'
FILES=['hermes/gateway/run_turn_runner.py']+['profile/hooks/'+n+'.py' for n in
    ('video_evidence','enrich_cached_video','video_takeaway_cascade','video_recovery','video_review','video_workflow')]+[
    'profile/plugins/line-platform/'+n for n in ('plugin.yaml','__init__.py','README.md')]+[
    'profile/skills/media/video-timeline-pipeline/SKILL.md']


def sha(data):return hashlib.sha256(data).hexdigest()


def replace(path,data,mode):
    path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    with tempfile.NamedTemporaryFile(dir=path.parent,delete=False) as handle:
        temporary=Path(handle.name)
        os.chmod(temporary,mode)
        handle.write(data);handle.flush();os.fsync(handle.fileno())
    temporary.replace(path)


def main():
    if '--rollback' in sys.argv:
        receipt=json.loads((BASE/'deployment.json').read_text())
        for row in receipt['files']:
            assert sha(Path(row['path']).read_bytes())==row['after_sha256'],f"changed since deployment: {row['path']}"
            if row['backup']:
                assert sha(Path(row['backup']).read_bytes())==row['before_sha256'],'backup changed'
        for row in reversed(receipt['files']):
            path=Path(row['path'])
            assert sha(path.read_bytes())==row['after_sha256'],f'changed since deployment: {path}'
            if row['backup']:
                backup=Path(row['backup'])
                replace(path,backup.read_bytes(),backup.stat().st_mode & 0o777)
            else:
                retained=Path(str(path)+'.rollback-'+datetime.now().strftime('%Y%m%d-%H%M%S'))
                assert not retained.exists()
                path.rename(retained)  # Retain new files while removing the overriding plugin manifest.
        print('ROLLBACK_RESTORED; new files retained under dated rollback names')
        return
    baseline=json.loads((BASE/'baseline.json').read_text())
    assert subprocess.check_output(['git','rev-parse','HEAD'],cwd=HERMES,text=True).strip()==baseline['head']
    assert not subprocess.check_output(['git','status','--porcelain','--untracked-files=no'],cwd=HERMES,text=True).strip()
    updates=[]
    for name in FILES:
        data=(BASE/'work'/name).read_bytes()
        root=HERMES if name.startswith('hermes/') else PROFILE
        target=root/name.split('/',1)[1]
        assert not target.is_symlink()
        old=baseline['files'].get(name)
        if old:
            assert str(target)==old['remote'] and sha(target.read_bytes())==old['sha256'],f'baseline drift: {name}'
        else:
            assert not target.exists() or sha(target.read_bytes())==sha(data),f'different new file already exists: {name}'
        if name.endswith('.py'):compile(data,str(target),'exec')
        updates.append((target,data))
    config=PROFILE/'config.yaml'
    assert sha(config.read_bytes())==CONFIG_SHA,'config changed since review'
    doc=yaml.safe_load(config.read_text())
    enabled=doc.setdefault('plugins',{}).setdefault('enabled',[])
    if 'line-platform' not in enabled:enabled.append('line-platform')
    legacy=str(PROFILE/'hooks/video-url-prefetch.sh')
    hooks=doc.get('hooks',{}).get('pre_llm_call',[])
    assert sum(h.get('command')==legacy for h in hooks)==1,'unexpected legacy hook registrations'
    doc['hooks']['pre_llm_call']=[h for h in hooks if h.get('command')!=legacy]
    updates.append((config,yaml.safe_dump(doc,allow_unicode=True,sort_keys=False).encode()))
    if '--check' in sys.argv:
        print(json.dumps({'status':'READY','files':[str(p) for p,_ in updates],'head':baseline['head']}))
        return
    receipt={'time':datetime.now(timezone.utc).isoformat(),'files':[]}
    for target,data in updates:
        mode=(target.stat().st_mode & 0o777) if target.exists() else 0o600
        backup=None
        before=sha(target.read_bytes()) if target.exists() else None
        if target.exists():
            suffix=datetime.now().strftime('%Y%m%d')
            backup=Path(str(target)+'.bak-'+suffix)
            if backup.exists():backup=Path(str(target)+'.bak-'+datetime.now().strftime('%Y%m%d-%H%M%S'))
            assert not backup.exists()
            shutil.copy2(target,backup)
        replace(target,data,mode)
        assert sha(target.read_bytes())==sha(data)
        receipt['files'].append({'path':str(target),'backup':str(backup) if backup else None,
                                'before_sha256':before,'after_sha256':sha(data)})
        replace(BASE/'deployment.json',json.dumps(receipt,indent=2).encode(),0o600)
    print(json.dumps({'status':'FILES_APPLIED','count':len(receipt['files']),'receipt':str(BASE/'deployment.json')}))


if __name__=='__main__':main()
