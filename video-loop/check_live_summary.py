"""Paired semantic checks against the same native reviewer used in production."""
import json
import os
from pathlib import Path
import sys
import time

BASE=Path(__file__).parent
os.environ['HERMES_HOME']='/home/box/irisx-failover-restore/20260906/profile-irisx'
sys.path.insert(0,str(BASE/'work/profile/hooks'))
sys.path.insert(0,'/home/box/.hermes/hermes-agent')
from dotenv import load_dotenv
load_dotenv(Path(os.environ['HERMES_HOME'])/'.env')
from video_review import audit_candidate,audit_ok

evidence={'duration':60,'source':{'url':'https://www.youtube.com/watch?v=fixture123'},
          'evidence_status':'ready','gaps':[], 'limitations':['畫面為取樣觀察，未逐幀檢視。'],
          'items':[{'id':'E1','kind':'speech','start':0,'end':12,'text':'先拔掉電源，等待 30 秒；綠燈亮起前不要重新接上電源。'},
                   {'id':'E2','kind':'visible_text','start':24,'end':24,'text':'充電上限 20%'},
                   {'id':'E3','kind':'speech','start':48,'end':57,'text':'若綠燈沒有亮，就停止，無法完成；成功時畫面會顯示「設定完成」。'},
                   {'id':'E4','kind':'visual_observation','start':30,'end':30,'text':'示範者穿著白色上衣，旁邊放著透明的塑膠收納盒。'}]}
requirements={'must_cover':[{'id':'M1','description':'等待 30 秒且綠燈亮後才重新接電','evidence_ids':['E1']},
                           {'id':'M2','description':'畫面顯示充電上限 20%','evidence_ids':['E2']},
                           {'id':'M3','description':'綠燈未亮時停止，成功時顯示設定完成','evidence_ids':['E3']}]}
valid=('以下依語音及取樣畫面整理，未逐幀檢視。\n'
       '先拔掉電源，等待 30 秒，並在綠燈亮起後才重新接上電源。［00:00 語音］\n'
       '畫面顯示充電上限 20%。［00:24 畫面文字］\n'
       '如果綠燈未亮就停止，此時無法完成；成功時畫面顯示「設定完成」。［00:48 語音］')
cases={'complete':valid,
       'omitted_ending':valid.split('如果綠燈')[0]+'最後檢查畫面，便能了解影片主題與操作流程。',
       'changed_numbers':valid.replace('30 秒','3 秒').replace('20%','200%'),
       'reversed_condition':valid.replace('綠燈未亮就停止','綠燈未亮仍繼續'),
       'checklist_misses_ending':valid.split('如果綠燈')[0],
       'wrong_time':valid.replace('00:24','01:24')}
results=[]
for name,text in cases.items():
    ledger=[]
    messages=[{'type':'text','text':text}]
    required={'must_cover':requirements['must_cover'][:2]} if name=='checklist_misses_ending' else requirements
    audit=audit_candidate(evidence,required,messages,'請完整摘要操作與限制。',time.monotonic()+65,60,ledger)
    passed=audit_ok(audit,required,messages,evidence)
    result={'case':name,'expected_pass':name=='complete','actual_pass':passed,'audit':audit,'calls':ledger}
    results.append(result)
    (BASE/'live-summary-result.json').write_text(json.dumps(results,ensure_ascii=False,indent=2))
    print(json.dumps({k:result[k] for k in ('case','expected_pass','actual_pass')},ensure_ascii=False),flush=True)
    assert passed==(name=='complete'),result
print('SEMANTIC_AUDIT_GATE_PASS',flush=True)
