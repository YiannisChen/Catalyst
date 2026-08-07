#!/usr/bin/env python3
"""B6-G corpus source-bundle embedding driver.

The execution path is operator-controlled and requires a verified source
bundle; ``--mock`` is only
for fixture-scale local contract tests and produces non-production artifacts.

Production execution binds ``code_revision`` to the real Git HEAD and requires
a clean worktree. Mock/test mode can inject the revision resolver instead.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT / "packages" / "data-core") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "packages" / "data-core"))

from catalyst_data.config import BGE_M3_DIMENSION, BGE_M3_MODEL, BGE_M3_REVISION
from catalyst_data.retrieval.git_revision import resolve_git_revision
from catalyst_data.retrieval.gpu_contract import verify_source_bundle
from catalyst_data.retrieval.gpu_driver import build_embedding_artifacts, load_real_cuda_embedder
from catalyst_data.retrieval.embedder import fake_vector_for_tests
import numpy as np

GPU_RUN_REPORT_SCHEMA_VERSION = "gpu_run_report_v1"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _default_runtime_info() -> dict[str, Any]:
    """Best-effort GPU runtime info. Never raises; only imports torch when called."""
    info: dict[str, Any] = {
        "gpu_name": None,
        "torch_version": None,
        "cuda_version": None,
        "peak_memory_allocated": None,
        "peak_memory_reserved": None,
    }
    try:
        import torch

        info["torch_version"] = torch.__version__
        if getattr(torch, "version", None) is not None:
            info["cuda_version"] = torch.version.cuda
        if torch.cuda.is_available():
            info["gpu_name"] = torch.cuda.get_device_name(0)
            info["peak_memory_allocated"] = torch.cuda.max_memory_allocated()
            info["peak_memory_reserved"] = torch.cuda.max_memory_reserved()
    except Exception:
        pass
    return info


def _validate_runtime_info(info: dict[str, Any], *, mock: bool) -> dict[str, Any]:
    """Validate runtime info. Production completed reports must carry legal
    GPU/CUDA/VRAM fields; mock mode is permissive (fields may be null)."""
    if mock:
        return info
    missing = [
        key for key in ("gpu_name", "torch_version", "cuda_version")
        if not isinstance(info.get(key), str) or not info[key]
    ]
    if missing:
        raise RuntimeError("production runtime info incomplete: " + ",".join(missing))
    allocated = info.get("peak_memory_allocated")
    reserved = info.get("peak_memory_reserved")
    if not isinstance(allocated, (int, float)) or isinstance(allocated, bool) or allocated < 0:
        raise RuntimeError("production runtime info peak_memory_allocated invalid")
    if not isinstance(reserved, (int, float)) or isinstance(reserved, bool) or reserved < allocated:
        raise RuntimeError("production runtime info peak_memory_reserved invalid")
    return info


def _best_effort_runtime_info(provider: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    """Runtime info for failure/interrupted reports only; never raises and
    never masks the original exception."""
    try:
        return provider()
    except Exception:
        return {}


def _read_checksums(output_dir: Path) -> dict[str, str]:
    mapping: dict[str, str] = {}
    with (output_dir / "checksums.sha256").open(encoding="utf-8") as handle:
        for line in handle:
            digest, name = line.split(None, 1)
            mapping[name.strip()] = digest
    return mapping


def _verify_artifact_checksums(output_dir: Path) -> dict[str, str]:
    """Verify checksums.sha256 against actual artifact files; return the mapping."""
    checksums = _read_checksums(output_dir)
    for name, digest in checksums.items():
        if _sha256_file(output_dir / name) != digest:
            raise ValueError(f"artifact checksum mismatch: {name}")
    return checksums


def _write_gpu_run_report(
    output_dir: Path,
    *,
    status: str,
    started_at: str,
    code_revision: str,
    identities: dict[str, str],
    batch_size: int,
    vector_count: int | None,
    runtime_info: dict[str, Any],
    artifact_hashes: dict[str, str] | None = None,
    error_class: str | None = None,
    mock: bool = False,
) -> None:
    payload: dict[str, Any] = {
        "schema_version": GPU_RUN_REPORT_SCHEMA_VERSION,
        "status": status,
        "started_at": started_at,
        "completed_at": _utc_now(),
        "code_revision": code_revision,
        **identities,
        "model_name": BGE_M3_MODEL,
        "model_revision": BGE_M3_REVISION,
        "dtype": "float32",
        "dimension": BGE_M3_DIMENSION,
        "normalization": "l2",
        "requested_batch_size": batch_size,
        "vector_count": vector_count,
        "gpu_name": runtime_info.get("gpu_name"),
        "torch_version": runtime_info.get("torch_version"),
        "cuda_version": runtime_info.get("cuda_version"),
        "peak_memory_allocated": runtime_info.get("peak_memory_allocated"),
        "peak_memory_reserved": runtime_info.get("peak_memory_reserved"),
        "artifact_hashes": artifact_hashes or {},
        "error_class": error_class,
    }
    if mock:
        payload["non_production"] = True
    _atomic_json(output_dir / "gpu_run_report.json", payload)


def _validate_resume_report(
    output_dir: Path,
    *,
    code_revision: str,
    identities: dict[str, str],
    mock: bool,
) -> None:
    report_path = output_dir / "gpu_run_report.json"
    if not report_path.is_file():
        return
    report = json.loads(report_path.read_text(encoding="utf-8"))
    if report.get("schema_version") != GPU_RUN_REPORT_SCHEMA_VERSION:
        raise ValueError("gpu_run_report schema mismatch on resume")
    for key, expected in identities.items():
        if report.get(key) != expected:
            raise ValueError(f"gpu_run_report {key} mismatch on resume")
    if report.get("code_revision") != code_revision:
        raise ValueError("gpu_run_report code_revision mismatch on resume")
    if report.get("model_revision") != BGE_M3_REVISION:
        raise ValueError("gpu_run_report model_revision mismatch on resume")
    if report.get("status") == "completed":
        raise ValueError("gpu_run_report already completed; resume rejected")
    if report.get("non_production", False) != mock:
        raise ValueError("gpu_run_report non_production mode mismatch on resume")
    manifest_path = output_dir / "index_manifest.json"
    if manifest_path.is_file():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        for key, expected in identities.items():
            if manifest.get(key) != expected:
                raise ValueError(f"index_manifest {key} mismatch on resume")
        if manifest.get("code_revision") != code_revision:
            raise ValueError("index_manifest code_revision mismatch on resume")
        if manifest.get("model_revision") != BGE_M3_REVISION:
            raise ValueError("index_manifest model_revision mismatch on resume")
        if manifest.get("non_production", False) != mock:
            raise ValueError("index_manifest non_production mode mismatch on resume")


def _resolve_code_revision(*, expected: str | None, require_clean: bool) -> str:
    return resolve_git_revision(
        repo_root=REPO_ROOT, expected=expected, require_clean=require_clean,
    )


def _mock_embed(texts):
    return np.vstack([fake_vector_for_tests(text, dim=1024) for text in texts]).astype(np.float32)


def main(
    argv: list[str] | None = None,
    *,
    runtime_info_provider: Callable[[], dict[str, Any]] | None = None,
) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-bundle", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--batch-size", type=int, default=32)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--mock", action="store_true", help="fixture-only non-production vectors")
    parser.add_argument("--expected-source-bundle-id", required=True)
    parser.add_argument("--expected-snapshot-id", required=True)
    parser.add_argument("--expected-corpus-manifest-id", required=True)
    parser.add_argument("--expected-probe-report-id", required=True)
    parser.add_argument("--expected-postbuild-readiness-id", required=True)
    parser.add_argument(
        "--code-revision", default=None,
        help="must equal `git rev-parse HEAD` exactly in production",
    )
    args = parser.parse_args(argv)
    runtime_info = runtime_info_provider or _default_runtime_info
    started_at = _utc_now()
    identities = {
        "source_bundle_id": args.expected_source_bundle_id,
        "snapshot_id": args.expected_snapshot_id,
        "corpus_manifest_id": args.expected_corpus_manifest_id,
        "probe_report_id": args.expected_probe_report_id,
        "postbuild_readiness_id": args.expected_postbuild_readiness_id,
    }
    code_revision = args.code_revision or ""
    output_dir = Path(args.output_dir)
    # The run owns the output directory only after every zero-write preflight
    # gate passes. Failures before that must not create or overwrite reports,
    # artifacts, checkpoints, or prior reports.
    output_owned = False
    try:
        if args.mock:
            code_revision = _resolve_code_revision(
                expected=args.code_revision, require_clean=False,
            )
        else:
            code_revision = _resolve_code_revision(
                expected=args.code_revision, require_clean=True,
            )
        verify_source_bundle(
            args.source_bundle,
            expected_source_bundle_id=args.expected_source_bundle_id,
            expected_snapshot_id=args.expected_snapshot_id,
            expected_corpus_manifest_id=args.expected_corpus_manifest_id,
            expected_probe_report_id=args.expected_probe_report_id,
            expected_postbuild_readiness_id=args.expected_postbuild_readiness_id,
        )
        if args.resume:
            _validate_resume_report(
                output_dir, code_revision=code_revision, identities=identities,
                mock=args.mock,
            )
        if output_dir.exists() and not args.resume and any(output_dir.iterdir()):
            raise ValueError("non-resume embedding output directory must be empty")
        embed = _mock_embed if args.mock else load_real_cuda_embedder(batch_size=args.batch_size)
        output_owned = True
        build_embedding_artifacts(
            args.source_bundle, output_dir, embed_batch=embed,
            batch_size=args.batch_size, resume=args.resume, mock=args.mock,
            code_revision=code_revision,
        )
        artifact_hashes = _verify_artifact_checksums(output_dir)
        raw_manifest = json.loads((output_dir / "index_manifest.json").read_text(encoding="utf-8"))
        vector_count = raw_manifest.get("vector_count")
        # Production completed reports are only written after runtime info is
        # collected AND validated; a missing/invalid runtime blocks completion.
        runtime = _validate_runtime_info(runtime_info(), mock=args.mock)
        _write_gpu_run_report(
            output_dir,
            status="completed",
            started_at=started_at,
            code_revision=code_revision,
            identities=identities,
            batch_size=args.batch_size,
            vector_count=vector_count,
            runtime_info=runtime,
            artifact_hashes=artifact_hashes,
            mock=args.mock,
        )
        return 0
    except KeyboardInterrupt:
        if output_owned:
            try:
                _write_gpu_run_report(
                    output_dir,
                    status="interrupted",
                    started_at=started_at,
                    code_revision=code_revision,
                    identities=identities,
                    batch_size=args.batch_size,
                    vector_count=None,
                    runtime_info=_best_effort_runtime_info(runtime_info),
                    error_class="KeyboardInterrupt",
                    mock=args.mock,
                )
            except Exception:  # noqa: BLE001 - never mask the original interrupt
                pass
        raise
    except Exception as exc:
        if output_owned:
            try:
                _write_gpu_run_report(
                    output_dir,
                    status="failed",
                    started_at=started_at,
                    code_revision=code_revision,
                    identities=identities,
                    batch_size=args.batch_size,
                    vector_count=None,
                    runtime_info=_best_effort_runtime_info(runtime_info),
                    error_class=type(exc).__name__,
                    mock=args.mock,
                )
            except Exception:  # noqa: BLE001 - never mask the original failure
                pass
        raise


if __name__ == "__main__":
    raise SystemExit(main())
