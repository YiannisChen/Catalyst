"""Wave 3 production attribution user-smoke runner (the ONLY permitted entry point).

The production path is:

    loopback HTTP -> Catalyst app -> live run -> runtime dependency loader
    -> attribution graph -> retrieval/tool execution -> DeepSeek answer
    -> exported trace / RunAssuranceRecord / immutable evidence

The CLI never accepts a caller-supplied success token, fake validation
boolean, raw identity string, or API key argument.  Every gate (clean Git
runtime identity, frozen DB identity/counts, LanceDB/index manifest identity,
T4 evidence, full Wave 2 evidence with a code-produced ``FOUR_ARM_E2E_OK``,
CUDA + production pinned boundary, app dependency wiring, provider/model
validation) runs before any provider call or artifact write.  ``USER_SMOKE_OK``
is written only after all three approved cases match their expected classes,
trace/assurance/evidence identity checks pass, cost is known and bounded,
failure paths pass with zero provider calls, and the evidence secret scan
passes.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import socket
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from catalyst_data.config import BGE_M3_MODEL

from catalyst_eval.post_import.case_pack import load_case_pack
from catalyst_eval.post_import.four_arm import EmbeddingBoundary
from catalyst_eval.post_import.index_identity import (
    ApprovedFrozenIdentities,
    resolve_runtime_identity,
)
from catalyst_eval.post_import.user_smoke import (
    DEFAULT_MODEL_ID,
    DEFAULT_PROVIDER,
    HttpResponse,
    run_user_smoke,
)

REPO_ROOT = Path(__file__).resolve().parents[3]

APPROVED = ApprovedFrozenIdentities(
    snapshot_id="7a004accb187a17dd5661821913f5b84785af7fe448d6a92331f0bd8aea92f49",
    corpus_manifest_id="3274069269bbabd8099137933310ead43e3f378577f0124e5770f89eeec4cffc",
    source_bundle_id="8ffae891b4e1c86ed5968d48181a0f9d1bcb28f33fc0fc440be7340d5d4118d2",
    probe_report_id="25a596e98e4ff5e38f69f64f55404ddb05ab109c2c47d41f615c8744ec19ec23",
    postbuild_readiness_id="9f6c62fb0b077b1e946b4c6ee6779a6866b3c835b18acf213a045b75d91c472b",
    index_manifest_id="c7f4248b2b70009a1d8c57d21075342dfe82e3e8417388a62667f9ba87bda083",
    code_revision="bb43ebe20f29a13ef426e0a1a7c3aefc6d15ffd8",
    model_name="BAAI/bge-m3",
    model_revision="5617a9f61b028005a4858fdac845db406aefb181",
    tokenizer_revision="5617a9f61b028005a4858fdac845db406aefb181",
    dimension=1024,
    dtype="float32",
    normalization_mode="l2",
    table_name="chunks__staging__b3761f4b943542a8",
    vector_count=295506,
    db_sha256="bb37b213091e256033fa00272cb7a85617dbcddf69d6fe9b515840cd9f1ebe40",
    db_user_version=13,
    db_fk_violations=0,
)
EXPECTED_SERVED_ROWS = 295506
EXPECTED_ACTIVE_ELIGIBLE = 253984
EXPECTED_METADATA_ONLY = 41522
DEFAULT_INDEX_MANIFEST = REPO_ROOT / "data" / "embeddings" / "b6g_import_bb43ebe" / "index_manifest.json"
DEFAULT_LANCEDB_DIR = REPO_ROOT / "data" / "lancedb_gold" / "b6g_8ffae891b4e1"

SERVER_READY_TIMEOUT_SECONDS = 60.0


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _open_db_readonly(db_path: Path) -> sqlite3.Connection:
    uri = f"{db_path.resolve().as_uri()}?mode=ro&immutable=1"
    return sqlite3.connect(uri, uri=True)


def _verify_frozen_db_counts(conn: sqlite3.Connection) -> None:
    served = conn.execute("SELECT COUNT(*) FROM corpus_served_chunks").fetchone()[0]
    if served != EXPECTED_SERVED_ROWS:
        raise RuntimeError(f"corpus_served_chunks count {served} != {EXPECTED_SERVED_ROWS}")
    active = conn.execute(
        "SELECT COUNT(*) FROM corpus_served_chunks WHERE status='active' AND eligibility='eligible'"
    ).fetchone()[0]
    meta_only = conn.execute(
        "SELECT COUNT(*) FROM corpus_served_chunks WHERE status='metadata_only'"
    ).fetchone()[0]
    if active != EXPECTED_ACTIVE_ELIGIBLE or meta_only != EXPECTED_METADATA_ONLY:
        raise RuntimeError(
            f"served split mismatch active={active} metadata_only={meta_only}"
        )


class LoopbackHttpSession:
    """Loopback-only HTTP session for the Catalyst app (never remote)."""

    def __init__(self, base_url: str, *, timeout_seconds: float = 60.0) -> None:
        import httpx

        self._client = httpx.Client(base_url=base_url, timeout=timeout_seconds)

    def get(self, path: str) -> HttpResponse:
        resp = self._client.get(path)
        return HttpResponse(status_code=resp.status_code, json=_json_or_none(resp), headers=dict(resp.headers))

    def post(self, path: str, json=None) -> HttpResponse:
        resp = self._client.post(path, json=json)
        return HttpResponse(status_code=resp.status_code, json=_json_or_none(resp), headers=dict(resp.headers))

    def close(self) -> None:
        self._client.close()


def _json_or_none(resp: Any) -> Any:
    try:
        return resp.json()
    except Exception:
        return None


def _start_loopback_server(host: str, port: int) -> tuple[Any, Any, int]:
    """Bind a loopback socket and run the real app in a background thread."""
    try:
        import uvicorn
    except ImportError as exc:
        raise RuntimeError(
            "uvicorn is required to bind the Catalyst app on loopback; "
            "install it into the runtime venv before running the user smoke"
        ) from exc

    from catalyst_app.main import create_app

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((host, port))
    bound_port = int(sock.getsockname()[1])
    sock.listen(2048)

    config = uvicorn.Config(
        create_app(),
        host=host,
        port=bound_port,
        log_level="warning",
        lifespan="off",
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(
        target=server.run,
        kwargs={"sockets": [sock]},
        daemon=True,
    )
    thread.start()
    return server, thread, bound_port


def _wait_for_server_ready(http: LoopbackHttpSession) -> None:
    deadline = time.monotonic() + SERVER_READY_TIMEOUT_SECONDS
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            resp = http.get("/api/health/runtime")
            payload = resp.json
            if isinstance(payload, dict) and payload.get("status") in {"ready", "degraded"}:
                return
            last_error = RuntimeError(f"health status {payload!r}")
        except Exception as exc:  # server not accepting yet
            last_error = exc
        time.sleep(0.1)
    raise RuntimeError(f"loopback server did not become ready: {last_error}")


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, type=Path)
    parser.add_argument("--lancedb-dir", default=DEFAULT_LANCEDB_DIR, type=Path)
    parser.add_argument("--index-manifest", default=DEFAULT_INDEX_MANIFEST, type=Path)
    parser.add_argument("--t4-evidence-dir", required=True, type=Path)
    parser.add_argument("--wave2-evidence-dir", required=True, type=Path)
    parser.add_argument("--case-pack", default=None, type=Path,
                        help="full T4 case pack; defaults to --t4-evidence-dir/case_pack.jsonl")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--output-root", default="data/run_reports/post_import", type=Path)
    parser.add_argument("--embedding-mode", required=True, choices=["production_pinned"])
    parser.add_argument("--host", default="127.0.0.1",
                        help="loopback bind host (must remain loopback)")
    parser.add_argument("--port", default=0, type=int)
    parser.add_argument("--provider", default=DEFAULT_PROVIDER)
    parser.add_argument("--model-id", default=DEFAULT_MODEL_ID)
    parser.add_argument("--cost-ceiling-usd", default=5.0, type=float)
    parser.add_argument("--poll-timeout-seconds", default=300.0, type=float)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    db_path = Path(args.db)
    try:
        db_sha = _sha256_file(db_path)
    except OSError as exc:
        print(json.dumps({"ok": False, "error": f"cannot read frozen DB: {exc}"}, sort_keys=True))
        return 2
    if db_sha != APPROVED.db_sha256:
        print(json.dumps({"ok": False, "error": "frozen DB sha mismatch"}, sort_keys=True))
        return 2

    runtime_db_path = os.environ.get("CATALYST_DB_PATH")
    if not runtime_db_path:
        print(json.dumps({"ok": False, "error": "CATALYST_DB_PATH (writable derivative runtime DB) is required"},
                         sort_keys=True))
        return 2
    if not Path(runtime_db_path).is_file():
        print(json.dumps({"ok": False, "error": f"CATALYST_DB_PATH file missing: {runtime_db_path}"},
                         sort_keys=True))
        return 2

    conn = _open_db_readonly(db_path)
    try:
        _verify_frozen_db_counts(conn)
    except Exception as exc:
        conn.close()
        print(json.dumps({"ok": False, "error": str(exc)}, sort_keys=True))
        return 2

    try:
        resolved = resolve_runtime_identity(
            lancedb_dir=args.lancedb_dir,
            index_manifest_path=args.index_manifest,
            db_path=db_path,
            repo_root=REPO_ROOT,
            expected=APPROVED,
            require_clean=True,
        )
    except Exception as exc:
        conn.close()
        print(json.dumps({"ok": False, "error": f"runtime identity: {exc}"}, sort_keys=True))
        return 2

    # Gate: Wave 2 evidence must validate before provider use (module re-checks).
    try:
        case_pack_path = args.case_pack or (Path(args.t4_evidence_dir) / "case_pack.jsonl")
        cases = load_case_pack(case_pack_path)
    except Exception as exc:
        conn.close()
        print(json.dumps({"ok": False, "error": f"case pack: {exc}"}, sort_keys=True))
        return 2

    # App wiring: explicit authoritative index manifest + identity env contract.
    os.environ["CATALYST_DB_PATH"] = str(Path(runtime_db_path).resolve())
    os.environ["CATALYST_LANCEDB_DIR"] = str(Path(args.lancedb_dir).resolve())
    os.environ["CATALYST_INDEX_MANIFEST_PATH"] = str(Path(args.index_manifest).resolve())
    os.environ["CATALYST_CORPUS_MANIFEST_ID"] = resolved.corpus_manifest_id
    os.environ["CATALYST_INDEX_MANIFEST_ID"] = resolved.index_manifest_id
    os.environ["CATALYST_SOURCE_BUNDLE_ID"] = resolved.source_bundle_id
    os.environ["CATALYST_SNAPSHOT_ID"] = resolved.snapshot_id
    os.environ["CATALYST_PROBE_REPORT_ID"] = resolved.probe_report_id
    os.environ["CATALYST_POSTBUILD_READINESS_ID"] = resolved.postbuild_readiness_id
    os.environ["CATALYST_DEFAULT_MODEL"] = args.model_id

    try:
        import torch

        cuda_available = bool(torch.cuda.is_available())
        if not cuda_available:
            raise RuntimeError("CUDA is required for production_pinned; CPU fallback disabled")
        boundary = EmbeddingBoundary(
            embedding_mode="production_pinned",
            dimension=APPROVED.dimension,
            model_revision=APPROVED.model_revision,
            tokenizer_revision=APPROVED.tokenizer_revision,
            is_mock=False,
            cuda_available=True,
        )
    except Exception as exc:
        conn.close()
        print(json.dumps({"ok": False, "error": f"CUDA boundary: {exc}"}, sort_keys=True))
        return 2

    # Dependency loader preflight with the explicit authoritative manifest path.
    try:
        from catalyst_agents.runtime.dependencies import RuntimeDependencyLoader
        from catalyst_agents.runtime.query_embedding import ProductionBgeM3QueryEmbeddingFactory

        loader = RuntimeDependencyLoader(
            sqlite_db_path=Path(runtime_db_path),
            lancedb_dir=Path(args.lancedb_dir),
            default_model=args.model_id,
            require_identity_bound_runtime=True,
            query_embedding_factory=ProductionBgeM3QueryEmbeddingFactory(),
            index_manifest_path=Path(args.index_manifest),
        )
        health = loader.health()
        if health.get("status") != "ready":
            raise RuntimeError(f"app dependency health: {health.get('status')} {health.get('errors') or []}")
    except Exception as exc:
        conn.close()
        print(json.dumps({"ok": False, "error": f"dependency wiring: {exc}"}, sort_keys=True))
        return 2

    server = None
    thread = None
    http = None
    try:
        server, thread, bound_port = _start_loopback_server(args.host, args.port)
        http = LoopbackHttpSession(f"http://{args.host}:{bound_port}")
        _wait_for_server_ready(http)

        summary = run_user_smoke(
            cases=cases,
            run_id=args.run_id,
            output_root=args.output_root,
            resolved=resolved,
            wave2_dir=args.wave2_evidence_dir,
            t4_evidence_dir=args.t4_evidence_dir,
            boundary=boundary,
            runtime_db_path=Path(runtime_db_path),
            frozen_db_path=db_path,
            http=http,
            provider=args.provider,
            model_id=args.model_id,
            credential_source="server_env",
            poll_timeout_seconds=args.poll_timeout_seconds,
            cost_ceiling_usd=args.cost_ceiling_usd,
        )
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, sort_keys=True))
        return 2
    finally:
        if http is not None:
            try:
                http.close()
            except Exception:
                pass
        if server is not None:
            try:
                server.should_exit = True
            except Exception:
                pass
        if thread is not None:
            thread.join(timeout=5.0)

    print(json.dumps({
        "ok": True,
        "run_id": summary.run_id,
        "case_count": summary.case_count,
        "token_written": summary.token_written,
        "cost_known": summary.cost_known,
        "total_cost_usd": summary.total_cost_usd,
        "meta_path": str(summary.meta_path),
        "failure_paths": [{"name": r.name, "ok": r.ok, "provider_calls": r.provider_calls} for r in summary.failure_path_results],
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
