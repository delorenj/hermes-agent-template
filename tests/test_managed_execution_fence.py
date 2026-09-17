import json
from pathlib import Path
import subprocess

ROOT=Path(__file__).resolve().parents[1]

def test_managed_plane_adapter_rejects_write_before_credentials(tmp_path):
    role=tmp_path/'agents/hermes/pm'
    provider=role/'.scripts/providers/plane.sh'
    provider.parent.mkdir(parents=True)
    provider.write_text((ROOT/'template/.scripts/providers/plane.sh').read_text())
    (tmp_path/'.project.json').write_text(json.dumps({'execution':{'mode':'managed'}}))
    result=subprocess.run(['sh',str(provider),'transition','issue','completed'],capture_output=True,text=True)
    assert result.returncode==78
    assert 'require px task through Krebs' in result.stderr

def test_shadow_has_same_write_fence(tmp_path):
    role=tmp_path/'agents/hermes/pm'
    provider=role/'.scripts/providers/plane.sh'
    provider.parent.mkdir(parents=True)
    provider.write_text((ROOT/'template/.scripts/providers/plane.sh').read_text())
    (tmp_path/'.project.json').write_text(json.dumps({'execution':{'mode':'shadow'}}))
    result=subprocess.run(['sh',str(provider),'comment','issue','hello'],capture_output=True,text=True)
    assert result.returncode==78
