"""Apply only the tested helper changes; preserve the original feature rollback."""
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import sys

from deploy import BASE, PROFILE, FILES, sha, replace

receipt=json.loads((BASE/'deployment.json').read_text())
current={name:sha((BASE/'work'/name).read_bytes()) for name in FILES}
short=json.loads((BASE/'line-gate-short.json').read_text())
assert short['status']=='PASS'
assert json.loads((BASE/('implementation-'+short['implementation_sha256']+'.json')).read_text())==current
long=json.loads((BASE/'line-gate-long.json').read_text())
assert long['status']=='PASS'
tested=json.loads((BASE/('implementation-'+long['implementation_sha256']+'.json')).read_text())
# The only later change is frame retention, exercised by the short follow-up test.
assert all(tested[name]==value for name,value in current.items() if name!='profile/hooks/enrich_cached_video.py')
assert json.loads((PROFILE/'gateway_state.json').read_text())['active_agents']==0
for row in receipt['files']:
    assert sha(Path(row['path']).read_bytes())==row['after_sha256'],row['path']
changes=[]
for name in ('enrich_cached_video.py','video_takeaway_cascade.py','video_review.py'):
    target=PROFILE/'hooks'/name
    row=next(row for row in receipt['files'] if row['path']==str(target))
    data=(BASE/'work/profile/hooks'/name).read_bytes()
    compile(data,str(target),'exec')
    if sha(data)!=row['after_sha256']:
        changes.append((target,row,data))
if '--check' in sys.argv:
    print(json.dumps({'status':'READY','files':[str(p) for p,_,_ in changes]}))
    raise SystemExit(0)
for target,row,data in changes:
    backup=Path(str(target)+'.bak-'+datetime.now().strftime('%Y%m%d-%H%M%S'))
    assert not backup.exists()
    shutil.copy2(target,backup)
    record={'time':datetime.now(timezone.utc).isoformat(),'path':str(target),'backup':str(backup),
            'before_sha256':row['after_sha256'],'after_sha256':sha(data)}
    replace(target,data,target.stat().st_mode & 0o777)
    assert sha(target.read_bytes())==sha(data)
    row['after_sha256']=sha(data)
    receipt.setdefault('updates',[]).append(record)
    replace(BASE/'deployment.json',json.dumps(receipt,indent=2).encode(),0o600)
print(json.dumps({'status':'HELPERS_UPDATED','count':len(changes),'gateway_restart_required':False}))
