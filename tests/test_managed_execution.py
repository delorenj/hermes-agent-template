import json
from pathlib import Path
import runpy
import subprocess
import sys
import pytest
SCRIPT=Path(__file__).resolve().parents[1]/'template/.scripts/managed-execution.py'

@pytest.mark.parametrize('case,expected',[
 ('paused',['status']),('idle',['status','planner']),('shadow',['status']),('pending',['status']),
 ('revoked',['status']),('foreign',['status']),('undispatched',['status']),
 ('dispatched',['status','heartbeat']),('success',['status','heartbeat','planner'])])
def test_managed_heartbeat_authority(tmp_path,monkeypatch,case,expected):
    (tmp_path/'.project.json').write_text(json.dumps({'execution':{'mode':'shadow' if case=='shadow' else 'managed','pm_actor':'pm'}}))
    active=None if case in {'idle','shadow','paused'} else {'actor_id':'other' if case=='foreign' else 'pm','revoked':case=='revoked','ticket_id':'ticket','run_id':'run','generation':1}
    board={'revision':2,'active':active,'pending':'intent' if case=='pending' else None,'tickets':{'ticket':{'dispatched':case=='dispatched','outcome':{'outcome':'success'} if case=='success' else None}}}
    monkeypatch.setenv('KREBS_PLANNER_ENABLED','false' if case=='paused' else 'true')
    calls=[]
    def run(argv,**kwargs):
        calls.append(argv[2]);return subprocess.CompletedProcess(argv,0,json.dumps({'board':board} if argv[2]=='status' else {'revision':3}),'')
    monkeypatch.setattr(subprocess,'run',run);monkeypatch.setattr(sys,'argv',[str(SCRIPT),str(tmp_path)]);monkeypatch.delenv('PILOT_ACTOR_ID',raising=False)
    runpy.run_path(str(SCRIPT),run_name='__main__')
    assert calls==expected

def test_invalid_manifest_never_calls_legacy(tmp_path,monkeypatch):
    (tmp_path/'.project.json').write_text('{broken')
    monkeypatch.setattr(sys,'argv',[str(SCRIPT),str(tmp_path)])
    monkeypatch.setattr(subprocess,'run',lambda *a,**kw:pytest.fail('must fail before dispatch'))
    with pytest.raises(json.JSONDecodeError):runpy.run_path(str(SCRIPT),run_name='__main__')
