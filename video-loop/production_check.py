"""Verify the sole standby writer and optionally restart its exact recorded process."""
import base64
import hashlib
import hmac
import json
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
import time
import urllib.request

from dotenv import dotenv_values

BASE=Path(__file__).parent
RESTORE=Path('/home/box/irisx-failover-restore/20260906')
PROFILE=RESTORE/'profile-irisx'
HERMES=Path('/home/box/.hermes/hermes-agent')
PUBLIC='https://irisx-lease.irisx-tracker.workers.dev'


def request(url,headers=None,data=None):
    headers={'User-Agent':'irisx-lease-guard/1',**(headers or {})}
    with urllib.request.urlopen(urllib.request.Request(url,data=data,headers=headers),timeout=12) as response:
        body=response.read()
        if not body:return response.status,{}
        if 'json' in response.headers.get('Content-Type',''):
            return response.status,json.loads(body)
        return response.status,{'body':body.decode(errors='replace')}


def lease():
    token=(RESTORE/'gate-e/data/node.token').read_text().strip()
    status,doc=request(PUBLIC+'/v1/lease',{'X-Irisx-Node-Token':token})
    assert status==200 and doc.get('active') and doc.get('holder')=='standby-cursor-grokbot'
    return {k:doc.get(k) for k in ('active','holder','generation','expires_at')}


def recorded_process():
    record=json.loads((PROFILE/'gateway.pid').read_text())
    proc=Path('/proc')/str(record['pid'])
    birth=int((proc/'stat').read_text().rsplit(')',1)[1].split()[19])
    env=dict(x.split('=',1) for x in (proc/'environ').read_bytes().decode().split('\0') if '=' in x)
    argv=(proc/'cmdline').read_bytes().decode().split('\0')[:-1]
    expected=[str(HERMES/'venv/bin/python'),str(HERMES/'venv/bin/hermes'),'gateway','run','--accept-hooks','--external-supervisor']
    assert record['kind']=='hermes-gateway' and birth==record['start_time']
    assert record['hermes_home']==env['HERMES_HOME']==str(PROFILE)
    assert argv==expected and (proc/'cwd').resolve()==PROFILE
    return record,argv,env


def main():
    deployment=json.loads((BASE/'deployment.json').read_text())
    for row in deployment['files']:
        expected=row['before_sha256'] if '--restored' in sys.argv else row['after_sha256']
        path=Path(row['path'])
        assert (not path.exists() if expected is None else hashlib.sha256(path.read_bytes()).hexdigest()==expected),row['path']
    before=lease()
    guard_pid=int((RESTORE/'gate-e/standby/watch.pid').read_text())
    guard=Path('/proc')/str(guard_pid)
    assert (guard/'cmdline').read_bytes().decode().split('\0')[:-1]==[
        '/usr/bin/python3',str(RESTORE/'gate-e/bin/irisx_guard.py'),'watch']
    guard_birth=(guard/'stat').read_text().rsplit(')',1)[1].split()[19]
    record,argv,env=recorded_process()
    old_pid=record['pid']
    if '--restart' in sys.argv:
        state=json.loads((PROFILE/'gateway_state.json').read_text())
        assert state.get('active_agents')==0,'gateway has active work; retry after that turn ends'
        fd=os.pidfd_open(old_pid)
        try:
            assert recorded_process()[0]==record
            signal.pidfd_send_signal(fd,signal.SIGTERM)
            assert select.select([fd],[],[],25)[0],'graceful shutdown did not complete'
        finally:os.close(fd)
        assert lease()['generation']==before['generation']
        with (RESTORE/'runtime/hermes-gateway.log').open('ab',buffering=0) as log:
            child=subprocess.Popen(argv,cwd=PROFILE,env=env,stdin=subprocess.DEVNULL,
                stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
        deadline=time.monotonic()+35
        while time.monotonic()<deadline:
            assert child.poll() is None,'new gateway exited'
            try:
                record,_,_=recorded_process()
                state=json.loads((PROFILE/'gateway_state.json').read_text())
                if record['pid']==child.pid and state.get('pid')==child.pid and state.get('platforms',{}).get('line',{}).get('state')=='connected':
                    break
            except (OSError,ValueError,AssertionError):pass
            time.sleep(.5)
        else:raise TimeoutError('gateway did not connect')
        compatibility=RESTORE/'gate-e/standby/hermes-irisx.pid'
        compatibility.write_text(str(child.pid)+'\n')
    else:
        state=json.loads((PROFILE/'gateway_state.json').read_text())
    after=lease()
    assert after['generation']==before['generation']
    assert (guard/'stat').read_text().rsplit(')',1)[1].split()[19]==guard_birth
    checks={}
    for path in ('/health','/line/webhook/health'):
        status,_=request(PUBLIC+path);assert status==200
        checks[path]=status
    values={**dotenv_values(PROFILE/'.env'),**env}
    raw=b'{"events":[]}'
    signature=base64.b64encode(hmac.new(values['LINE_CHANNEL_SECRET'].encode(),raw,hashlib.sha256).digest()).decode()
    status,_=request(PUBLIC+'/line/webhook',{'X-Line-Signature':signature,'Content-Type':'application/json','User-Agent':'LineBotWebhook/2.0'},raw)
    assert status==200;checks['signed_empty_webhook']=status
    auth={'Authorization':'Bearer '+values['LINE_CHANNEL_ACCESS_TOKEN']}
    status,bot=request('https://api.line.me/v2/bot/info',auth)
    assert status==200;checks['bot_info']=status
    status,webhook=request('https://api.line.me/v2/bot/channel/webhook/endpoint',auth)
    assert status==200 and webhook.get('active') and webhook.get('endpoint')==PUBLIC+'/line/webhook'
    checks['official_webhook']=status
    receipt={'status':'PASS','checks':checks,'old_pid':old_pid,'pid':record['pid'],'start_time':record['start_time'],
        'bot_name':bot.get('displayName'),'guard_pid':guard_pid,'lease':after,
        'gateway_state':state.get('gateway_state'),'line_state':state.get('platforms',{}).get('line',{}).get('state'),
        'real_line_message_sent':False,'phone_acceptance':'pending','time':time.time()}
    (BASE/'production-check.json').write_text(json.dumps(receipt,ensure_ascii=False,indent=2))
    print(json.dumps(receipt,ensure_ascii=False))


if __name__=='__main__':main()
