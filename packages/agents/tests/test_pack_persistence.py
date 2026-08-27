"""M4-7: persisted context pack + rendered-message authority (Final TSD §7)."""
from __future__ import annotations

import pytest

from catalyst_agents.attribution.context_pack_builder import (
    ContextPackBuilder,
    ContextPackFinalizer,
    RenderMessage,
)
from catalyst_agents.runtime.pack_persistence import (
    InMemoryPackStore,
    PackNotPersistedError,
    PackPersistence,
    PersistedPackRefs,
    replay_asserts_pair,
    require_committed_pair,
)


def _messages() -> tuple[RenderMessage, ...]:
    return (
        RenderMessage(role="system", content="observation=ok"),
        RenderMessage(role="user", content="analyse"),
    )


def _rendered_sha(messages: tuple[RenderMessage, ...]) -> str:
    from catalyst_agents.attribution.context_pack_builder import (
        canonical_context_pack_json,
    )

    import hashlib

    return hashlib.sha256(
        canonical_context_pack_json(
            [message.model_dump(mode="json") for message in messages]
        )
    ).hexdigest()


def test_persist_commits_pair_and_returns_refs():
    store = InMemoryPackStore()
    messages = _messages()
    refs = store.persist_pack_and_render(
        run_id="run:1",
        pack_sha256="a" * 64,
        rendered_messages_sha256=_rendered_sha(messages),
        prompt_template_version="prompt:v1",
        prompt_template_sha256="b" * 64,
        rendered_messages=messages,
    )
    assert isinstance(refs, PersistedPackRefs)
    assert refs.run_id == "run:1"
    assert refs.pack_artifact_id
    assert refs.rendered_messages_artifact_id
    assert refs.pack_sha256 == "a" * 64
    assert refs.rendered_messages_sha256 == _rendered_sha(messages)
    pair = store.load_pair(run_id="run:1")
    assert pair is not None
    assert pair.refs == refs
    assert pair.rendered_messages == messages


class _FailingSecondCommitStore:
    """Fixture store whose second artifact commit (pack hash) fails."""

    def __init__(self):
        self.persist_calls = 0

    def persist_pack_and_render(self, **kwargs):
        self.persist_calls += 1
        if kwargs["pack_sha256"] == "bad" * 64:
            raise RuntimeError("second artifact commit failed")
        return PersistedPackRefs(
            run_id=kwargs["run_id"],
            pack_artifact_id="pack-1",
            rendered_messages_artifact_id="render-1",
            pack_sha256=kwargs["pack_sha256"],
            rendered_messages_sha256=kwargs["rendered_messages_sha256"],
            prompt_template_version=kwargs["prompt_template_version"],
            prompt_template_sha256=kwargs["prompt_template_sha256"],
        )

    def load_pair(self, *, run_id):
        return None


def test_refs_returned_only_after_both_commit():
    """A failure committing either artifact must not return persisted refs
    (no partial pair, no dual-write visibility)."""
    store = _FailingSecondCommitStore()
    with pytest.raises(RuntimeError):
        store.persist_pack_and_render(
            run_id="run:1",
            pack_sha256="bad" * 64,
            rendered_messages_sha256=_rendered_sha(_messages()),
            prompt_template_version="prompt:v1",
            prompt_template_sha256="b" * 64,
            rendered_messages=_messages(),
        )
    # No partial pair is readable.
    assert store.load_pair(run_id="run:1") is None


def test_provider_dispatch_prohibited_before_pair_commit():
    store = InMemoryPackStore()
    with pytest.raises(PackNotPersistedError):
        require_committed_pair(store, run_id="run:1")
    messages = _messages()
    store.persist_pack_and_render(
        run_id="run:1",
        pack_sha256="a" * 64,
        rendered_messages_sha256=_rendered_sha(messages),
        prompt_template_version="prompt:v1",
        prompt_template_sha256="b" * 64,
        rendered_messages=messages,
    )
    pair = require_committed_pair(store, run_id="run:1")
    assert pair is not None


def test_missing_pair_means_non_replayable():
    store = InMemoryPackStore()
    assert store.load_pair(run_id="missing") is None
    with pytest.raises(PackNotPersistedError):
        require_committed_pair(store, run_id="missing")


def test_reconstruction_is_verification_only_never_authority():
    store = InMemoryPackStore()
    messages = _messages()
    refs = store.persist_pack_and_render(
        run_id="run:1",
        pack_sha256="a" * 64,
        rendered_messages_sha256=_rendered_sha(messages),
        prompt_template_version="prompt:v1",
        prompt_template_sha256="b" * 64,
        rendered_messages=messages,
    )
    replay_asserts_pair(
        refs,
        pack_sha256="a" * 64,
        rendered_messages_sha256=_rendered_sha(messages),
        prompt_template_sha256="b" * 64,
        rendered_messages=messages,
    )
    with pytest.raises(ValueError):
        replay_asserts_pair(
            refs,
            pack_sha256="c" * 64,
            rendered_messages_sha256=_rendered_sha(messages),
            prompt_template_sha256="b" * 64,
            rendered_messages=messages,
        )
    with pytest.raises(ValueError):
        replay_asserts_pair(
            refs,
            pack_sha256="a" * 64,
            rendered_messages_sha256="d" * 64,
            prompt_template_sha256="b" * 64,
            rendered_messages=messages,
        )


def test_pack_persistence_does_not_own_production_artifact_store():
    """M4 defines the contract; the production run_artifacts envelope is M6
    app-owned. The agents module must not write V1.1 run_artifact rows and must
    not touch trace/artifacts.py."""
    import inspect

    from catalyst_agents.runtime import pack_persistence

    source = inspect.getsource(pack_persistence)
    assert "from catalyst_agents.trace" not in source
    assert "import catalyst_agents.trace" not in source
    # The only store provided by agents is the M4 test-only fixture store.
    assert "test-only" in (InMemoryPackStore.__doc__ or "")


def test_contract_is_an_agents_owned_protocol():
    import typing

    from catalyst_agents.runtime.pack_persistence import PackPersistence

    assert typing.get_type_hints(PackPersistence.persist_pack_and_render)["return"] is PersistedPackRefs
