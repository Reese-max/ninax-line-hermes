"""Idempotent profile installation, drift-safe updates and reversible receipts.

Does not start a gateway, acquire a lease, alter a webhook or copy credentials.
"""
import argparse
from datetime import datetime,timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

BASE=Path(__file__).resolve().parent


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest() if Path(path).is_file() else None


def replace(path,data,mode=0o600):
    path=Path(path)
    path.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent,delete=False) as output:
        tmp=Path(output.name)
        os.chmod(tmp,mode)
        output.write(data);output.flush();os.fsync(output.fileno())
    tmp.replace(path)


def save(path,doc):
    replace(path,json.dumps(doc,ensure_ascii=False,indent=2).encode())


def build_plan(profile,hermes,settings,baseline=None):
    import yaml
    profile,hermes=Path(profile).resolve(),Path(hermes).resolve()
    lock=json.loads((BASE/'runtime-lock.json').read_text())
    if subprocess.check_output(['git','rev-parse','HEAD'],cwd=hermes,text=True).strip()!=lock['hermes']['commit']:
        raise ValueError('unsupported_hermes_commit')
    if settings.get('hermes_root')!=str(hermes):
        raise ValueError('settings_hermes_mismatch')
    expected={'hermes_root','pipeline_root','pipeline_command','stt_python','jobs_root','provider_env_files'}
    if set(settings)!=expected or any(not Path(settings[k]).is_absolute() for k in expected-{'provider_env_files'}):
        raise ValueError('invalid_path_settings')
    if not isinstance(settings['provider_env_files'],list) or any(not Path(p).is_absolute() for p in settings['provider_env_files']):
        raise ValueError('invalid_provider_env_paths')
    old={r['path']:r['after_sha256'] for r in (baseline or {}).get('files',[])}
    originals=json.loads((BASE/'baseline.json').read_text())['files']
    updates=[]
    for name in ('gateway/run_turn_runner.py','plugins/platforms/line/adapter.py'):
        core=hermes/name
        packaged=BASE/'work/hermes'/name
        original=originals['hermes/'+name]['sha256']
        allowed={original,sha(packaged)}
        if old.get(str(core)):
            allowed.add(old[str(core)])
        if not core.is_file() or sha(core) not in allowed:
            raise ValueError('unsupported_hermes_core')
        updates.append((core,packaged.read_bytes(),0o644))
    for folder in ('hooks','plugins/line-platform','skills/media/video-timeline-pipeline'):
        source=BASE/'work/profile'/folder
        # Only the published package belongs to the installation; never copy .env or bytecode.
        names=(['video_evidence.py','video_review.py','video_recovery.py','video_workflow.py',
                'video_takeaway_cascade.py','enrich_cached_video.py','brightdata_ig_fallback.py','apify_ig_fallback.py']
               if folder=='hooks' else ['plugin.yaml','__init__.py','line_input_lifecycle.py','README.md'] if folder.startswith('plugins') else ['SKILL.md'])
        for name in names:
            target=profile/folder/name
            data=(source/name).read_bytes()
            if target.exists() and sha(target) not in {hashlib.sha256(data).hexdigest(),old.get(str(target))}:
                raise ValueError('unrecorded_profile_drift: '+str(target))
            updates.append((target,data,0o600))
    config=profile/'config.yaml'
    doc=yaml.safe_load(config.read_text()) if config.exists() else {}
    enabled=doc.setdefault('plugins',{}).setdefault('enabled',[])
    if 'line-platform' not in enabled:
        enabled.append('line-platform')
    hooks=doc.get('hooks',{}).get('pre_llm_call',[])
    if hooks:
        doc['hooks']['pre_llm_call']=[h for h in hooks if h.get('command')!=str(profile/'hooks/video-url-prefetch.sh')]
    updates.append((config,yaml.safe_dump(doc,allow_unicode=True,sort_keys=False).encode(),0o600))
    updates.append((profile/'video-settings.json',json.dumps(settings,indent=2).encode(),0o600))
    wrapper=Path(settings['pipeline_command'])
    if wrapper.is_relative_to(profile):
        code=(BASE/'video-pipeline').read_text().split('\n',1)[1]
        updates.append((wrapper,(f'#!{hermes}/venv/bin/python\n'+code).encode(),0o700))
    elif not wrapper.is_file():
        raise ValueError('external_pipeline_wrapper_missing')
    plan=[]
    for target,data,mode in updates:
        if target.is_symlink() or not any(target.resolve().is_relative_to(root) for root in (profile,hermes)):
            raise ValueError('unsafe_install_target')
        if target.suffix=='.py':
            compile(data,str(target),'exec')
        after=hashlib.sha256(data).hexdigest()
        if sha(target)!=after:
            plan.append({'path':str(target),'data':data,'before_sha256':sha(target),'after_sha256':after,
                         'mode':(target.stat().st_mode & 0o777) if target.exists() else mode})
    return plan


def apply(plan,receipt):
    receipt=Path(receipt)
    if receipt.exists():
        raise ValueError('receipt_already_exists')
    for row in plan:
        if sha(row['path'])!=row['before_sha256']:
            raise ValueError('install_baseline_changed')
    rows=[]
    for row in plan:
        path=Path(row['path']);backup=None
        if path.exists():
            backup=Path(str(path)+'.bak-'+datetime.now().strftime('%Y%m%d'))
            if backup.exists():
                backup=Path(str(path)+'.bak-'+datetime.now().strftime('%Y%m%d-%H%M%S'))
            if backup.exists():
                raise ValueError('backup_already_exists')
            shutil.copy2(path,backup)
        rows.append({k:v for k,v in row.items() if k!='data'}|{'backup':str(backup) if backup else None})
    doc={'status':'prepared','time':datetime.now(timezone.utc).isoformat(),'files':rows}
    save(receipt,doc)  # All recovery material is durable before the first replacement.
    for row in plan:
        if sha(row['path'])!=row['before_sha256']:
            raise ValueError('install_baseline_changed')
        replace(row['path'],row['data'],row['mode'])
        if sha(row['path'])!=row['after_sha256']:
            raise ValueError('install_write_mismatch')
    doc['status']='applied';save(receipt,doc)
    return doc


def rollback(receipt):
    receipt=Path(receipt);doc=json.loads(receipt.read_text())
    if doc['status']=='rolled_back':
        return doc
    for row in doc['files']:
        if sha(row['path']) not in {row['before_sha256'],row['after_sha256']}:
            raise ValueError('rollback_refuses_drift')
        if row['backup'] and sha(row['backup'])!=row['before_sha256']:
            raise ValueError('rollback_backup_changed')
    for row in reversed(doc['files']):
        path=Path(row['path'])
        if sha(path)==row['before_sha256']:
            continue
        if row['backup']:
            replace(path,Path(row['backup']).read_bytes(),row['mode'])
        else:
            retained=Path(str(path)+'.rollback-'+datetime.now().strftime('%Y%m%d-%H%M%S'))
            if retained.exists():
                raise ValueError('rollback_retained_path_exists')
            path.rename(retained)
    doc['status']='rolled_back';save(receipt,doc)
    return doc


def doctor(settings):
    lock=json.loads((BASE/'runtime-lock.json').read_text())
    hermes=Path(settings['hermes_root']);pipeline=Path(settings['pipeline_root'])
    failures=[]
    if subprocess.check_output(['git','rev-parse','HEAD'],cwd=hermes,text=True).strip()!=lock['hermes']['commit']:
        failures.append('hermes_commit_mismatch')
    if sha(pipeline/'pipeline.py')!=lock['pipeline_sha256']:
        failures.append('pipeline_source_mismatch')
    for label,python in [('hermes',hermes/'venv/bin/python'),('pipeline',pipeline/'.venv/bin/python'),('stt',Path(settings['stt_python']))]:
        expected=lock['environments'][label]
        code="import importlib.metadata as m,json,sys;print(json.dumps({'python':sys.version.split()[0],'packages':{d.metadata['Name'].lower().replace('_','-'):d.version for d in m.distributions()}}))"
        current=json.loads(subprocess.check_output([str(python),'-c',code],text=True,timeout=20))
        if current['python']!=expected['python']:
            failures.append(label+'_python_mismatch')
        for name,version in expected['packages'].items():
            if current['packages'].get(name.lower().replace('_','-'))!=version:
                failures.append(label+'_package_mismatch:'+name)
    for name in ('ffmpeg','ffprobe'):
        if not shutil.which(name):
            failures.append(name+'_missing')
    model=lock['whisper_model']
    code=("from huggingface_hub import snapshot_download;from pathlib import Path;import hashlib,json;"
          "p=Path(snapshot_download("+repr(model['repository'])+",revision="+repr(model['revision'])+",local_files_only=True,allow_patterns="+repr(list(model['files']))+"));"
          "print(json.dumps({n:hashlib.sha256((p/n).read_bytes()).hexdigest() for n in "+repr(list(model['files']))+"}))")
    checked=subprocess.run([settings['stt_python'],'-c',code],capture_output=True,text=True,timeout=30)
    if checked.returncode or json.loads(checked.stdout)!=model['files']:
        failures.append('whisper_model_missing_or_changed')
    return {'status':'PASS' if not failures else 'FAIL','failures':failures}


def bootstrap(root,python311,python313):
    root=Path(root).resolve()
    if root.exists():
        raise ValueError('bootstrap_requires_new_directory')
    root.mkdir(parents=True)
    lock=json.loads((BASE/'runtime-lock.json').read_text())
    hermes=root/'hermes-agent'
    def run(args,cwd=None):
        subprocess.run([str(a) for a in args],cwd=cwd,check=True)
    run(['git','init',hermes])
    run(['git','remote','add','origin',lock['hermes']['repository']],hermes)
    run(['git','fetch','--depth','1','origin',lock['hermes']['commit']],hermes)
    run(['git','checkout','--detach','FETCH_HEAD'],hermes)
    shutil.copytree(BASE/'vendor/video-pipeline',root/'video-pipeline')
    for label,target,python in [('hermes',hermes/'venv',python311),('pipeline',root/'video-pipeline/.venv',python313),('stt',root/'stt',python311)]:
        run([python,'-m','venv',target])
        requirements=root/(label+'-requirements.txt')
        packages=lock['environments'][label]['packages']
        requirements.write_text('\n'.join(name+'=='+version for name,version in packages.items() if name!='hermes-agent')+'\n')
        run([target/'bin/python','-m','pip','install','-r',requirements])
    run([hermes/'venv/bin/python','-m','pip','install','--no-deps','-e',hermes])
    model=lock['whisper_model']
    run([root/'stt/bin/python','-c',"from huggingface_hub import snapshot_download; snapshot_download("+
         repr(model['repository'])+",revision="+repr(model['revision'])+",allow_patterns="+repr(list(model['files']))+",token=False)"])
    settings={'hermes_root':str(hermes),'pipeline_root':str(root/'video-pipeline'),
              'pipeline_command':str(root/'profile/bin/video-pipeline'),'stt_python':str(root/'stt/bin/python'),
              'jobs_root':str(root/'video-pipeline/jobs'),'provider_env_files':[str(root/'profile/.env')]}
    save(root/'settings.json',settings)
    return {'status':'ENVIRONMENT_BUILT','settings':str(root/'settings.json'),'credentials_copied':False}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--profile',type=Path)
    parser.add_argument('--settings',type=Path)
    parser.add_argument('--baseline',type=Path)
    parser.add_argument('--receipt',type=Path)
    parser.add_argument('--apply',action='store_true')
    parser.add_argument('--rollback',type=Path)
    parser.add_argument('--doctor',action='store_true')
    parser.add_argument('--bootstrap',type=Path)
    parser.add_argument('--python311',default='python3.11')
    parser.add_argument('--python313',default='python3.13')
    args=parser.parse_args()
    if args.bootstrap:
        result=bootstrap(args.bootstrap,args.python311,args.python313)
    elif args.rollback:
        result=rollback(args.rollback)
    else:
        if not args.settings:
            parser.error('--settings is required')
        settings=json.loads(args.settings.read_text())
        if args.doctor:
            result=doctor(settings)
        else:
            if not args.profile or (args.apply and not args.receipt):
                parser.error('--profile and, for --apply, --receipt are required')
            plan=build_plan(args.profile,settings['hermes_root'],settings,json.loads(args.baseline.read_text()) if args.baseline else None)
            result=apply(plan,args.receipt) if args.apply else {'status':'READY','files':[{k:v for k,v in row.items() if k!='data'} for row in plan]}
    print(json.dumps(result,ensure_ascii=False))
    return int(result.get('status')=='FAIL')


if __name__=='__main__':
    raise SystemExit(main())
