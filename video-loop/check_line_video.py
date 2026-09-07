"""Real gateway/plugin/LLM flow. Only LINE transport is redirected to localhost."""
import argparse
import asyncio
import base64
import hashlib
import hmac
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import time
from urllib.parse import urlsplit

import aiohttp
from aiohttp import web
import yaml

BASE=Path(__file__).parent
HERMES=Path('/home/box/.hermes/hermes-agent')
PRODUCTION=Path('/home/box/irisx-failover-restore/20260906/profile-irisx')
parser=argparse.ArgumentParser()
parser.add_argument('--case',choices=['short','long','normal'],default='short')
args=parser.parse_args()
from deploy import FILES
IMPLEMENTATION={name:hashlib.sha256((BASE/'work'/name).read_bytes()).hexdigest() for name in FILES}
IMPLEMENTATION_SHA=hashlib.sha256(json.dumps(IMPLEMENTATION,sort_keys=True).encode()).hexdigest()
(BASE/('implementation-'+IMPLEMENTATION_SHA+'.json')).write_text(json.dumps(IMPLEMENTATION,indent=2))
HOME=Path('/tmp')/('ninax-line-'+args.case+'-'+str(os.getpid()))
HOME.mkdir(mode=0o700)
for key in list(os.environ):
    if key.startswith(('LINE_','TELEGRAM_','DISCORD_','SLACK_')):
        del os.environ[key]
os.environ.update(HERMES_HOME=str(HOME),LINE_HOST='127.0.0.1',LINE_PORT='0',
                  LINE_CHANNEL_ACCESS_TOKEN='isolated-token',LINE_CHANNEL_SECRET='isolated-secret',
                  LINE_ALLOW_ALL_USERS='true',HERMES_DISABLE_BACKGROUND_REVIEW='1')
cfg=yaml.safe_load((PRODUCTION/'config.yaml').read_text())
cfg['plugins']={'enabled':['line-platform']}
cfg['terminal']['cwd']=str(HOME)
cfg['platform_toolsets']={'line':['terminal']}
cfg['agent']['max_turns']=4
cfg['agent']['gateway_timeout']=340
cfg['kanban']['enabled']=False
cfg['cron']['enabled']=False
cfg['hooks']={}
(HOME/'config.yaml').write_text(yaml.safe_dump(cfg,allow_unicode=True,sort_keys=False))
os.chmod(HOME/'config.yaml',0o600)
for name in ('auth.json','SOUL.md'):
    if (PRODUCTION/name).exists():
        shutil.copy2(PRODUCTION/name,HOME/name)
        os.chmod(HOME/name,0o600)
for folder in ('hooks','plugins'):
    shutil.copytree(BASE/'work/profile'/folder,HOME/folder)
sys.path.insert(0,str(HERMES))
# Load the exact edited module against real installed imports before the runner imports it.
import gateway
spec=importlib.util.spec_from_file_location('gateway.run_turn_runner',BASE/'work/hermes/gateway/run_turn_runner.py')
turn_module=importlib.util.module_from_spec(spec)
sys.modules[spec.name]=turn_module
spec.loader.exec_module(turn_module)
gateway.run_turn_runner=turn_module


async def main():
    captured=[]
    delivered=asyncio.Event()
    async def capture(request):
        if request.path=='/v2/bot/info':
            return web.json_response({'userId':'U'+'1'*32,'displayName':'NINAX isolated test'})
        body=await request.json()
        if request.path in ('/v2/bot/message/reply','/v2/bot/message/push'):
            captured.append({'path':request.path,'messages':body['messages']})
            if any(m.get('type')=='text' and (args.case!='normal' or 'NINAX_NORMAL_CHECK' in m.get('text',''))
                   for m in body['messages']):
                delivered.set()
        return web.json_response({})
    app=web.Application()
    app.router.add_route('*','/{path:.*}',capture)
    api=web.AppRunner(app)
    await api.setup()
    site=web.TCPSite(api,'127.0.0.1',0)
    await site.start()
    api_port=site._server.sockets[0].getsockname()[1]
    original=aiohttp.ClientSession._request
    async def guarded_request(self,method,url,**kwargs):
        parsed=urlsplit(str(url))
        if parsed.hostname in ('api.line.me','api-data.line.me'):
            url=f'http://127.0.0.1:{api_port}{parsed.path}'
        return await original(self,method,url,**kwargs)
    aiohttp.ClientSession._request=guarded_request
    from gateway.config import GatewayConfig,Platform,PlatformConfig
    from gateway.run import GatewayRunner
    config=GatewayConfig(platforms={Platform('line'):PlatformConfig(enabled=True,gateway_restart_notification=False,
        extra={'channel_access_token':'isolated-token','channel_secret':'isolated-secret','host':'127.0.0.1','port':0,
               'allow_all_users':True,'slow_response_threshold':0 if args.case=='normal' else 1,
               'video_jobs_root':str(BASE/'test-jobs')})},loop_watchdog=False)
    runner=GatewayRunner(config)
    started=time.monotonic()
    try:
        assert await runner.start()
        adapter=runner.adapters[Platform('line')]
        assert type(adapter).__name__=='VideoLineAdapter',type(adapter).__name__
        port=adapter._site._server.sockets[0].getsockname()[1]
        code={'short':'DZrNsyXivVc','long':'DY66ObpOlby'}.get(args.case)
        question=(f'請完整摘要這支影片的實際內容，包含重要情節與結尾：https://www.instagram.com/reel/{code}/'
                  if code else '請用 terminal 執行 printf NINAX_NORMAL_CHECK，然後只回覆它的輸出。')
        user='U'+'0'*32
        event={'type':'message','webhookEventId':'ninax-loop-'+str(os.getpid()),'replyToken':'isolated-reply',
               'timestamp':int(time.time()*1000),'source':{'type':'user','userId':user},
               'message':{'type':'text','id':str(os.getpid()),'text':question}}
        raw=json.dumps({'events':[event]},ensure_ascii=False).encode()
        signature=base64.b64encode(hmac.new(b'isolated-secret',raw,hashlib.sha256).digest()).decode()
        async with aiohttp.ClientSession() as client:
            async with client.post(f'http://127.0.0.1:{port}/line/webhook',data=raw,
                  headers={'X-Line-Signature':signature,'Content-Type':'application/json'}) as response:
                assert response.status==200
        await asyncio.wait_for(delivered.wait(),timeout=310)
        await asyncio.sleep(0.5)
        text='\n'.join(m.get('text','') for c in captured for m in c['messages'])
        if code:
            sessions=[json.loads(p.read_text()) for p in (HOME/'video-turns').glob('*.json') if not p.name.endswith('.delivery.json')]
            assert len(sessions)==1,sessions
            result=sessions[0]['result']
            assert result['audit']['status']=='pass',result['audit']
            assert len(captured)==1,'preview/postback/duplicate escaped'
            plugin=sys.modules[type(adapter).__module__]
            assert plugin.digest(captured[0]['messages'])==result['approval']['payload_sha256']
            binding=result['approval']['binding']
            evidence=json.loads((Path(result['job'])/'evidence.json').read_text())
            assert evidence['processed_ranges']['visual_samples'][-1]>=evidence['duration']-0.5,'ending not sampled'
            state={'chat_id':user,'message_id':event['message']['id'],'binding':binding,
                   'approval':result['approval'],'result':result,'delivery':'pending'}
            token=plugin._TURN.set(state)
            try:
                assert not (await adapter.send(user,'有標題又很長，但這是未檢核的草稿。')).success
                assert not (await adapter.send(user,result['text'])).success,'same turn must not deliver twice'
                state['approval']=None
                assert not (await adapter.send(user,result['text'])).success,'missing approval must fail closed'
            finally:
                plugin._TURN.reset(token)
            rid=adapter._cache.register_pending(user)
            adapter._cache.set_ready(rid,'未審核摘要，不能從 postback 直接送出。')
            await adapter._handle_postback_event({'source':{'type':'user','userId':user},'replyToken':'bad-cache-reply',
                'postback':{'data':json.dumps({'action':'show_response','request_id':rid})}})
            assert len(captured)==1,'postback bypassed approval'
            if args.case=='short':
                delivered.clear()
                event.update(webhookEventId=event['webhookEventId']+'-repeat',replyToken='isolated-repeat',
                             timestamp=int(time.time()*1000))
                event['message']={**event['message'],'id':event['message']['id']+'-repeat','text':'再說一次'}
                raw=json.dumps({'events':[event]},ensure_ascii=False).encode()
                signature=base64.b64encode(hmac.new(b'isolated-secret',raw,hashlib.sha256).digest()).decode()
                async with aiohttp.ClientSession() as client:
                    async with client.post(f'http://127.0.0.1:{port}/line/webhook',data=raw,
                        headers={'X-Line-Signature':signature,'Content-Type':'application/json'}) as response:
                        assert response.status==200
                await asyncio.wait_for(delivered.wait(),timeout=20)
                await asyncio.sleep(.5)
                assert len(captured)==2 and captured[0]['messages']==captured[1]['messages']
                saved=json.loads(adapter._session_path(binding['session_key']).read_text())['result']
                repeat_id=saved['approval']['binding']['turn_id']
                assert repeat_id!=binding['turn_id']
                updated=json.loads((Path(result['job'])/'evidence.json').read_text())
                assert updated['turns'][repeat_id]['review_calls']==[],'plain recall should reuse audited evidence'
                delivered.clear()
                event.update(webhookEventId=event['webhookEventId']+'-recover',replyToken='isolated-recover',
                             timestamp=int(time.time()*1000))
                event['message']={**event['message'],'id':event['message']['id']+'-recover','text':'完整一點'}
                raw=json.dumps({'events':[event]},ensure_ascii=False).encode()
                signature=base64.b64encode(hmac.new(b'isolated-secret',raw,hashlib.sha256).digest()).decode()
                async with aiohttp.ClientSession() as client:
                    async with client.post(f'http://127.0.0.1:{port}/line/webhook',data=raw,
                        headers={'X-Line-Signature':signature,'Content-Type':'application/json'}) as response:
                        assert response.status==200
                await asyncio.wait_for(delivered.wait(),timeout=310)
                await asyncio.sleep(.5)
                assert len(captured)==3,'one final response per explicit request'
                recovered=json.loads(adapter._session_path(binding['session_key']).read_text())['result']
                assert recovered['audit']['status']=='pass',recovered['audit']
                assert plugin.digest(captured[2]['messages'])==recovered['approval']['payload_sha256']
                updated=json.loads((Path(result['job'])/'evidence.json').read_text())
                latest=updated['turns'][recovered['approval']['binding']['turn_id']]
                assert latest['recovery']['new_evidence'] and latest['recovery']['requested_refresh']
                assert len(latest['evidence_snapshot']['processed_ranges']['visual_samples'])>len(evidence['processed_ranges']['visual_samples'])
                assert set(evidence['processed_ranges']['visual_samples'])<=set(latest['evidence_snapshot']['processed_ranges']['visual_samples']),\
                    'new sampling must preserve previously observed frames'
                assert latest['review_calls'],'new content requires a new audit'
        else:
            assert 'NINAX_NORMAL_CHECK' in text,text
        db=sqlite3.connect('file:'+str(HOME/'state.db')+'?mode=ro',uri=True)
        rows=db.execute('SELECT role,content FROM messages ORDER BY rowid').fetchall()
        db.close()
        assert any(role=='user' and question in content for role,content in rows)
        assert any(role=='assistant' and ('來源：' in content if code else 'NINAX_NORMAL_CHECK' in content) for role,content in rows)
        if not code:
            assert any(role=='tool' and 'NINAX_NORMAL_CHECK' in content for role,content in rows),'native tool result missing'
        receipt={'gate':'real-line-video-flow','status':'PASS','case':args.case,'home':str(HOME),
                 'implementation_sha256':IMPLEMENTATION_SHA,'test_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                 'elapsed_seconds':round(time.monotonic()-started,1),'webhook_http':200,
                 'adapter':type(adapter).__name__,'captured_delivery':captured,'real_line_message_sent':False,
                 'persistence_roles':[r[0] for r in rows]}
        (BASE/('line-gate-'+args.case+'.json')).write_text(json.dumps(receipt,ensure_ascii=False,indent=2))
        print(json.dumps(receipt,ensure_ascii=False),flush=True)
    finally:
        await runner.stop()
        await api.cleanup()
        aiohttp.ClientSession._request=original


receipt_path=BASE/('line-gate-'+args.case+'.json')
receipt_path.write_text(json.dumps({'status':'RUNNING','case':args.case,'home':str(HOME),'implementation_sha256':IMPLEMENTATION_SHA}))
try:
    asyncio.run(main())
except Exception as exc:
    receipt_path.write_text(json.dumps({'status':'FAIL','case':args.case,'home':str(HOME),
        'implementation_sha256':IMPLEMENTATION_SHA,'error':type(exc).__name__,'real_line_message_sent':False}))
    raise
