from __future__ import annotations

import importlib


def test_validator_uses_threshold_override(monkeypatch):
    monkeypatch.setenv("CATALYST_M_THRESHOLD", "0.40")
    mod = importlib.import_module("catalyst_agents.nodes.validator")
    importlib.reload(mod)
    assert mod._effective_m_threshold() == 0.40


def test_default_m_threshold_is_calibrated():
    from catalyst_agents.nodes.critic import M_THRESHOLD
    assert M_THRESHOLD == 0.45
