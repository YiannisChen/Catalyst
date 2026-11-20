from __future__ import annotations

from catalyst_agents.llm.client import ChatClient


def test_bound_clients_share_cached_sdk_by_credentials(monkeypatch):
    import catalyst_agents.llm.client as client_module
    import openai

    created = []
    monkeypatch.setattr(client_module, "_SDK_CLIENTS", {})
    for name in ("ALL_PROXY", "all_proxy", "HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
        monkeypatch.delenv(name, raising=False)

    class StubSDK:
        def __init__(self, **kwargs):
            created.append(self)
            self.kwargs = kwargs

    monkeypatch.setattr(openai, "OpenAI", StubSDK)
    first = ChatClient(model="m", api_key="key-one", base_url="https://provider.example")
    assert first.bind()._client() is first.bind()._client()
    second = ChatClient(model="m", api_key="key-two", base_url="https://provider.example")
    assert second._client() is not first._client()
    assert len(created) == 2


def test_full_cache_drops_least_recent_without_closing_it(monkeypatch):
    import catalyst_agents.llm.client as client_module
    import openai

    for name in ("ALL_PROXY", "all_proxy", "HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(client_module, "_SDK_CLIENTS", {})
    monkeypatch.setattr(client_module, "_SDK_CLIENT_CACHE_SIZE", 2)

    class StubSDK:
        def __init__(self, **kwargs):
            self.http = kwargs["http_client"]

    monkeypatch.setattr(openai, "OpenAI", StubSDK)
    first = ChatClient(model="m", api_key="key-1", base_url="https://p.example")._client()
    ChatClient(model="m", api_key="key-2", base_url="https://p.example")._client()
    ChatClient(model="m", api_key="key-1", base_url="https://p.example")._client()  # key-1 is now most recent
    ChatClient(model="m", api_key="key-3", base_url="https://p.example")._client()  # drops key-2
    hashes = {k[1] for k in client_module._SDK_CLIENTS}
    import hashlib
    assert hashlib.sha256(b"key-1").hexdigest() in hashes
    assert hashlib.sha256(b"key-2").hexdigest() not in hashes
    assert not first.http.is_closed


def test_provider_error_redaction_covers_common_key_shapes():
    from catalyst_agents.runtime.provider_capability import redact_provider_error

    text = redact_provider_error("bad key sk-abcdefgh xai-ABCDEFGH AIzaSyABCDEFG gsk_abcdefgh own-secret", "own-secret")
    assert "abcdefgh" not in text and "ABCDEFGH" not in text and "SyABCDEFG" not in text and "own-secret" not in text
