"""M5-11: no legacy MCJ production semantic path remains.

M5 plan §6.4: graph.py, runtime/service.py, runtime/runner.py, and state.py
must not import nodes.critic/judge/miner/validator, prompts.critic/judge, or
expand_macro, and the V1.1 new-write state must not carry the legacy semantic
fields critic_reasoning/causes/summary_md. Legacy behavior remains readable
only through historical readers.
"""
from __future__ import annotations

from pathlib import Path

AGENTS_ROOT = Path(__file__).resolve().parents[1] / "catalyst_agents"

_FORBIDDEN = (
    "nodes.critic",
    "nodes.judge",
    "nodes.miner",
    "nodes.validator",
    "prompts.critic",
    "prompts.judge",
    "expand_macro",
)

_FILES = (
    "graph.py",
    "runtime/service.py",
    "runtime/runner.py",
    "state.py",
)


def test_no_legacy_production_imports() -> None:
    for relative in _FILES:
        source = (AGENTS_ROOT / relative).read_text(encoding="utf-8")
        for forbidden in _FORBIDDEN:
            assert forbidden not in source, f"{relative} still references {forbidden!r}"


def test_v1_state_has_no_legacy_semantic_fields() -> None:
    state_source = (AGENTS_ROOT / "state.py").read_text(encoding="utf-8")
    for field in ("critic_reasoning", "causes", "summary_md"):
        assert field not in state_source, f"V1.1 state still carries {field!r}"


def test_no_legacy_node_modules_remain_in_production_package() -> None:
    nodes_dir = AGENTS_ROOT / "nodes"
    prompts_dir = AGENTS_ROOT / "prompts"
    for name in ("critic.py", "judge.py", "miner.py", "validator.py"):
        assert not (nodes_dir / name).exists(), f"{name} still exists in production nodes"
    for name in ("critic.md", "judge.md"):
        assert not (prompts_dir / name).exists(), f"{name} still exists in production prompts"
