"""Tests for RuntimeCredentialStore."""

from catalyst_app.runtime_credential_store import RuntimeCredentialStore


def test_register_and_get():
    store = RuntimeCredentialStore()
    store.register("run-1", api_key="sk-test-123")
    cred = store.get("run-1")
    assert cred is not None
    assert cred.api_key == "sk-test-123"
    assert cred.run_id == "run-1"


def test_get_nonexistent_returns_none():
    store = RuntimeCredentialStore()
    assert store.get("nonexistent") is None


def test_remove_cleans_up():
    store = RuntimeCredentialStore()
    store.register("run-1", api_key="sk-test")
    store.remove("run-1")
    assert store.get("run-1") is None


def test_thread_safety():
    import concurrent.futures

    store = RuntimeCredentialStore()

    def register_and_get(i):
        store.register(f"run-{i}", api_key=f"key-{i}")
        return store.get(f"run-{i}").api_key

    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as ex:
        futures = [ex.submit(register_and_get, i) for i in range(100)]
        results = [f.result() for f in futures]
    assert len(results) == 100
    assert store.active_count == 100


def test_cleanup_on_terminal():
    """Simulate run lifecycle: register → run → remove."""
    store = RuntimeCredentialStore()
    store.register("run-1", api_key="sk-abc")
    assert store.get("run-1") is not None
    store.remove("run-1")
    assert store.get("run-1") is None


def test_double_register_overwrites():
    store = RuntimeCredentialStore()
    store.register("run-1", api_key="first-key")
    store.register("run-1", api_key="second-key")
    assert store.get("run-1").api_key == "second-key"


def test_remove_nonexistent_is_safe():
    store = RuntimeCredentialStore()
    store.remove("nonexistent")  # should not raise


def test_active_count():
    store = RuntimeCredentialStore()
    assert store.active_count == 0
    store.register("a", api_key="k1")
    store.register("b", api_key="k2")
    assert store.active_count == 2
    store.remove("a")
    assert store.active_count == 1
