"""Owned route migration is repairable; foreign endpoints remain handwriting."""
import copy
import json

import pytest
import yaml

from test_specialist_profile import fixture, run, snapshot

LEGACY = "https://api.automaticai.io/v1"
CURRENT = "https://api-direct.automaticai.io/v1"
KEYS = ("api", "base_url")


def legacy_receipt(profile, delta):
    """Model a saved legacy receipt, before the fleet migrated the delta."""
    receipt_path = profile / "specialist-projection.json"
    receipt = json.loads(receipt_path.read_text())
    for key in KEYS:
        receipt["config"][f"providers.automaticai.{key}"] = LEGACY
    receipt_path.write_text(json.dumps(receipt))
    for name in ("config.delta.yaml", "config.yaml"):
        data = yaml.safe_load((profile / name).read_text())
        data["providers"]["automaticai"].update({key: LEGACY for key in KEYS})
        (profile / name).write_text(yaml.safe_dump(data))
    # The old generated file remains receipt-owned. Delta is allowed to gain
    # unrelated operator overrides independently of its historical fingerprint.
    import hashlib
    config = yaml.safe_load((profile / "config.yaml").read_text())
    receipt["files"]["config.yaml"] = hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()
    receipt_path.write_text(json.dumps(receipt))
    (profile / "config.delta.yaml").write_text(yaml.safe_dump(delta))


@pytest.mark.parametrize("api,base_url", [(LEGACY, LEGACY), (CURRENT, CURRENT), (LEGACY, CURRENT), (CURRENT, LEGACY)])
@pytest.mark.parametrize("generated_current", [False, True])
def test_owned_provider_migration_previews_repairs_and_converges(tmp_path, api, base_url, generated_current):
    fleet, profile, request, env = fixture(tmp_path)
    assert run(request, env).returncode == 0
    delta = yaml.safe_load((profile / "config.delta.yaml").read_text())
    delta["providers"]["automaticai"].update(api=api, base_url=base_url)
    delta["operator"] = {"keep": "unrelated"}
    legacy_receipt(profile, delta)
    if generated_current:
        config = yaml.safe_load((profile / "config.yaml").read_text())
        config["providers"]["automaticai"].update(api=api, base_url=base_url)
        config["operator"]["keep"] = "unrelated"
        (profile / "config.yaml").write_text(yaml.safe_dump(config))
    before, base = snapshot(profile), (fleet / "config.yaml").read_bytes()
    preview = run(request, env, "--check")
    assert preview.returncode == 0, preview.stderr
    assert "specialist-projection.json" in json.loads(preview.stdout)["changed"]
    assert snapshot(profile) == before
    repaired = run(request, env)
    assert repaired.returncode == 0, repaired.stderr
    for name in ("config.delta.yaml", "config.yaml"):
        data = yaml.safe_load((profile / name).read_text())
        assert all(data["providers"]["automaticai"][key] == CURRENT for key in KEYS)
        assert data["operator"]["keep"] == "unrelated"
        assert data["skills"]["inherit_global"] is True
    receipt = json.loads((profile / "specialist-projection.json").read_text())
    assert all(receipt["config"][f"providers.automaticai.{key}"] == CURRENT for key in KEYS)
    complete = snapshot(profile)
    for args in [("--check",), ()]:
        again = run(request, env, *args)
        assert again.returncode == 0, again.stderr
        assert json.loads(again.stdout)["changed"] == []
        assert snapshot(profile) == complete
    assert (fleet / "config.yaml").read_bytes() == base


@pytest.mark.parametrize("key", KEYS)
@pytest.mark.parametrize("endpoint", ["https://foreign.test/v1", "https://api-direct.automaticai.io/v1/", "http://api-direct.automaticai.io/v1"])
def test_foreign_provider_routes_refuse_preview_and_apply_without_writes(tmp_path, key, endpoint):
    _, profile, request, env = fixture(tmp_path)
    assert run(request, env).returncode == 0
    delta = yaml.safe_load((profile / "config.delta.yaml").read_text())
    delta["providers"]["automaticai"][key] = endpoint
    legacy_receipt(profile, delta)
    before = snapshot(profile)
    for args in [("--check",), ()]:
        refused = run(request, env, *args)
        assert refused.returncode != 0
        assert f"ownership conflict: config providers.automaticai.{key}" in refused.stderr
        assert snapshot(profile) == before


def test_route_repair_does_not_mask_unrelated_owned_or_generated_conflicts(tmp_path):
    _, profile, request, env = fixture(tmp_path)
    assert run(request, env).returncode == 0
    delta = yaml.safe_load((profile / "config.delta.yaml").read_text())
    legacy_receipt(profile, delta)
    clean = copy.deepcopy(delta)
    for key in ("enabled", "key_env"):
        delta = copy.deepcopy(clean)
        delta["providers"]["automaticai"][key] = False if key == "enabled" else "FOREIGN_KEY"
        (profile / "config.delta.yaml").write_text(yaml.safe_dump(delta))
        before = snapshot(profile)
        refused = run(request, env)
        assert refused.returncode != 0 and f"providers.automaticai.{key}" in refused.stderr
        assert snapshot(profile) == before
    (profile / "config.delta.yaml").write_text(yaml.safe_dump(clean))
    config = yaml.safe_load((profile / "config.yaml").read_text())
    config["handwritten"] = "keep"
    (profile / "config.yaml").write_text(yaml.safe_dump(config))
    before = snapshot(profile)
    refused = run(request, env)
    assert refused.returncode != 0 and "handwritten config.yaml" in refused.stderr
    assert snapshot(profile) == before


def test_unknown_prior_route_is_not_adopted_as_the_known_migration(tmp_path):
    _, profile, request, env = fixture(tmp_path)
    assert run(request, env).returncode == 0
    receipt_path = profile / "specialist-projection.json"
    receipt = json.loads(receipt_path.read_text())
    receipt["config"]["providers.automaticai.api"] = "https://other-prior.test/v1"
    receipt_path.write_text(json.dumps(receipt))
    before = snapshot(profile)
    refused = run(request, env)
    assert refused.returncode != 0 and "providers.automaticai.api" in refused.stderr
    assert snapshot(profile) == before
