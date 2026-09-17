#!/usr/bin/env python3
"""Managed heartbeat adapter; never dispatches a worker without Krebs authority."""
import json
import os
from pathlib import Path
import subprocess
import sys

root=Path(sys.argv[1]).resolve()
manifest=json.loads((root/'.project.json').read_text())
execution=manifest.get('execution',{})
actor=os.environ.get('PILOT_ACTOR_ID') or execution.get('pm_actor')
if not actor:
    raise SystemExit('managed execution: actor enrollment missing')
def px(*args):
    result=subprocess.run(['px',*args,'--actor',actor,'--json'],cwd=root,capture_output=True,text=True)
    if result.returncode:
        raise SystemExit('managed execution: controller unavailable or enrollment invalid')
    return json.loads(result.stdout)
status=px('task','status')
board=status['board']; attempt=board.get('active')
if execution.get('mode')=='managed' and attempt and attempt['actor_id']==actor and not attempt.get('revoked') and not board.get('pending'):
    px('run','heartbeat',attempt['ticket_id'],'--run-id',attempt['run_id'],
       '--generation',str(attempt['generation']),'--revision',str(board['revision']))
print(json.dumps({'managed':True,'mode':execution['mode'],'active_run':attempt.get('run_id') if attempt else None,
                  'dispatch':'claim and px run start required; legacy sentinel fenced'}))
