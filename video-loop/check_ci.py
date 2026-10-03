"""Run the same bounded checks locally or on GitHub, with a commit-bound receipt."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import time


ROOT=Path(__file__).resolve().parents[1]
BASE=ROOT/'video-loop'


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('group',choices=['offline','pipeline-windows','hermes-contract'])
    parser.add_argument('--hermes',type=Path)
    parser.add_argument('--out',type=Path,required=True)
    args=parser.parse_args()
    if args.out.exists():parser.error('Use a new receipt path')
    expected=('win32',(3,13)) if args.group=='pipeline-windows' else ('linux',(3,11))
    assert (sys.platform,sys.version_info[:2])==expected,'Use the same OS and Python minor as the workflow'
    subprocess.run(['git','diff','--exit-code','HEAD'],cwd=ROOT,check=True,capture_output=True)
    revision=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip()
    if args.group=='offline':
        commands=[(ROOT,[sys.executable,'-m','compileall','-q','video-loop/work/profile','video-loop/install.py']),
                  (ROOT,[sys.executable,'-B','video-loop/check_goal.py']),
                  (ROOT,[sys.executable,'-B','video-loop/check_install.py']),
                  (ROOT,['git','diff','--check'])]
    elif args.group=='pipeline-windows':
        commands=[(ROOT,[sys.executable,'-B','video-loop/vendor/video-pipeline/pipeline.py','--self-test'])]
    else:
        if not args.hermes:parser.error('--hermes is required for native checks')
        hermes=args.hermes.resolve()
        lock=json.loads((BASE/'runtime-lock.json').read_text())
        assert subprocess.check_output(['git','rev-parse','HEAD'],cwd=hermes,text=True).strip()==lock['hermes']['commit']
        assert (hermes/'gateway/run_turn_runner.py').read_bytes()==(BASE/'work/hermes/gateway/run_turn_runner.py').read_bytes()
        commands=[(ROOT,[sys.executable,'-B','video-loop/check_video.py',str(hermes)]),
                  (ROOT,[sys.executable,'-B','video-loop/check_delivery.py',str(hermes)]),
                  (hermes,['bash','scripts/run_tests.sh','-j','2','tests/gateway/test_line_plugin.py',
                    'tests/gateway/test_stream_final_contract.py','tests/gateway/test_stream_final_adoption_gate.py',
                    str(BASE/'test_custom_turn.py'),str(ROOT/'tests/test_line_input_lifecycle.py'),str(ROOT/'tests/test_line_push_retry.py'),'-q'])]
    receipt={'group':args.group,'status':'RUNNING','commit':revision,'platform':platform.platform(),
             'python':platform.python_version(),'runner_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
             'started_at':datetime.now(timezone.utc).isoformat(),'metered_fetch_disabled':True,'checks':[]}
    args.out.parent.mkdir(parents=True,exist_ok=True)
    def save():args.out.write_text(json.dumps(receipt,ensure_ascii=False,indent=2),encoding='utf-8')
    save()
    env={**os.environ,'PYTHONUTF8':'1','HERMES_PYTHON':sys.executable,'NINAX_DISABLE_METERED_FETCH':'1'}
    started=time.monotonic()
    try:
        for cwd,command in commands:
            result=subprocess.run(command,cwd=cwd,env=env,capture_output=True,text=True,encoding='utf-8',timeout=600)
            receipt['checks'].append({'command':command,'exit_code':result.returncode,
                                      'stdout':result.stdout[-12000:],'stderr':result.stderr[-4000:]})
            save()
            if result.returncode:
                failed_check=next((Path(part).name for part in command[1:] if part.endswith(('.py','.sh'))),Path(command[0]).name)
                frames=[line.strip() for line in result.stderr.splitlines() if line.lstrip().startswith('File "')]
                caller=next((line for line in reversed(frames) if 'subprocess.py' not in line),
                            frames[-1] if frames else '')
                failures=[line.strip() for line in result.stderr.splitlines()
                          if line.lstrip().startswith(('AssertionError','ModuleNotFoundError','ImportError',
                                                       'ValueError','RuntimeError','TimeoutError','FileNotFoundError',
                                                       'PermissionError','OSError','CalledProcessError'))]
                details=' '.join(part for part in (caller,
                                                   failures[-1].split(':',1)[0] if failures else '') if part)
                raise RuntimeError(f'check_failed: {failed_check} exit={result.returncode} {details}'.rstrip())
        subprocess.run(['git','diff','--exit-code','HEAD'],cwd=ROOT,check=True,capture_output=True)
        receipt['status']='PASS'
    except Exception as exc:
        receipt.update(status='FAIL',reason=type(exc).__name__+': '+str(exc))
    finally:
        receipt['seconds']=round(time.monotonic()-started,2)
        save()
        print(json.dumps({k:v for k,v in receipt.items() if k!='checks'}))
    return int(receipt['status']!='PASS')


if __name__=='__main__':
    raise SystemExit(main())
