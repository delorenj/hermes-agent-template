"""The fleet base is THE default voice; a desk delta pins a voice only on purpose.

Regression for the 2026-11-20 report: the default changed to kurzgesagt in
~/.hermes/config.yaml, yet desks kept speaking carlin because every provisioning
path stamped the template's `vox_voice = "carlin"` into each desk's delta.
"""
import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "template/.scripts/lib/voice-config.py"


@pytest.fixture(scope="module")
def vc():
    spec = importlib.util.spec_from_file_location("voice_config_under_test", SOURCE)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_no_voice_given_and_none_pinned_inherits(vc):
    delta = {}
    vc.reconcile_delta(delta, "vox")
    assert delta["tts"] == {"provider": "vox"}
    assert delta["x-pjangler-merge"]["list_patches"]["plugins.enabled"]["add"] == ["tts/vox"]


def test_existing_pin_is_kept_and_written_to_both_leaves(vc):
    # infra-pm's half pin: only tts.voice set.
    delta = {"tts": {"voice": "brule"}}
    vc.reconcile_delta(delta, "vox")
    assert delta["tts"]["voice"] == delta["tts"]["vox"]["voice"] == "brule"


def test_provider_only_pin_becomes_operative(vc):
    # james-brennan-pm's half pin: only tts.vox.voice, which vox never speaks over a base tts.voice.
    delta = {"tts": {"vox": {"voice": "carlin", "base_url": "https://vox.test"}}}
    vc.reconcile_delta(delta, "vox")
    assert delta["tts"]["voice"] == "carlin"
    assert delta["tts"]["vox"] == {"voice": "carlin", "base_url": "https://vox.test"}


def test_top_level_pin_wins_a_split(vc):
    delta = {"tts": {"voice": "brule", "vox": {"voice": "kurzgesagt"}}}
    vc.reconcile_delta(delta, "vox")
    assert delta["tts"]["voice"] == delta["tts"]["vox"]["voice"] == "brule"


def test_explicit_voice_pins(vc):
    delta = {"tts": {"voice": "brule"}}
    vc.reconcile_delta(delta, "vox", "walken")
    assert delta["tts"]["voice"] == delta["tts"]["vox"]["voice"] == "walken"


def test_inherit_leaves_other_provider_keys(vc):
    delta = {"tts": {"vox": {"base_url": "https://vox.test"}}}
    vc.reconcile_delta(delta, "vox")
    assert delta["tts"] == {"provider": "vox", "vox": {"base_url": "https://vox.test"}}


def test_invalid_pin_refused(vc):
    with pytest.raises(vc.ContractError):
        vc.reconcile_delta({"tts": {"voice": "../etc"}}, "vox")
