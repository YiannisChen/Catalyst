"""Atomic, versioned GPU run report contract for build_corpus_embeddings_gpu.py.

The report is written atomically; ``status=completed`` only after artifact
checksums pass; interrupts/failures write a sanitized status/error_class with
no secrets; resume validates report identity; mock reports carry
``non_production=true``. Runtime info is injected so tests never load torch.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np
import pytest

CODE_REVISION = "04dabde9f44e9638d1b5ebad0eed002f5475c1a3"

CORPUS = "c" * 64
SNAPSHOT = "6" * 64
PROBE = "7" * 64
POSTBUILD = "8" * 64
MODEL_REVISION = "5617a9f61b028005a4858fdac845db406aefb181"


def _bundle(tmp_path: Path, count: int = 4) -> tuple[Path, dict]:
    from catalyst_data.retrieval.source_bundle import _chunk_record_hash, _stream_source_bundle_id

    records = []
    for index in range(count):
        text = f"fixture content {index} — 中文"
        records.append({
            "chunk_id": f"chunk:{index:04d}", "document_id": f"doc:{index}",
            "content_text": text, "content_hash": hashlib.sha256(text.encode()).hexdigest(),
            "metadata_hash": hashlib.sha256(f"meta:{index}".encode()).hexdigest(),
            "available_at": "2026-01-01T00:00:00Z",
            "ticker_associations": "[\"AAPL\"]", "corpus_manifest_id": CORPUS,
            "chunk_profile_version": "news_v2",
        })
    identity = _stream_source_bundle_id(
        corpus_manifest_id=CORPUS, snapshot_id=SNAPSHOT,
        probe_report_id=PROBE, postbuild_readiness_id=POSTBUILD,
        record_hashes=[_chunk_record_hash(r) for r in records],
    )
    root = tmp_path / f"source_{identity}"
    root.mkdir(parents=True)
    (root / "chunks.jsonl").write_text(
        "".join(json.dumps(r, sort_keys=True, separators=(",", ":")) + "\n" for r in records)
    )
    manifest = {
        "schema_version": "1.1.0", "source_bundle_id": identity,
        "corpus_manifest_id": CORPUS, "snapshot_id": SNAPSHOT,
        "probe_report_id": PROBE, "postbuild_readiness_id": POSTBUILD,
        "chunk_count": count, "universe_manifest_id": "u" * 64,
    }
    (root / "source_bundle_manifest.json").write_text(
        json.dumps(manifest, sort_keys=True, separators=(",", ":"))
    )
    checksums = {}
    for name in ("chunks.jsonl", "source_bundle_manifest.json"):
        checksums[name] = hashlib.sha256((root / name).read_bytes()).hexdigest()
    (root / "checksums.sha256").write_text(
        "\n".join(f"{value}  {name}" for name, value in sorted(checksums.items())) + "\n"
    )
    return root, manifest


def _cli():
    import scripts.build_corpus_embeddings_gpu as cli

    return cli


def _args(bundle: Path, output_dir: Path, manifest: dict, *, extra: list[str] | None = None) -> list[str]:
    values = [
        "--source-bundle", str(bundle),
        "--output-dir", str(output_dir),
        "--batch-size", "2",
        "--mock",
        "--expected-source-bundle-id", manifest["source_bundle_id"],
        "--expected-snapshot-id", SNAPSHOT,
        "--expected-corpus-manifest-id", CORPUS,
        "--expected-probe-report-id", PROBE,
        "--expected-postbuild-readiness-id", POSTBUILD,
        "--code-revision", CODE_REVISION,
    ]
    if extra:
        values.extend(extra)
    return values


def _fake_runtime() -> dict:
    return {
        "gpu_name": "NVIDIA Test GPU",
        "torch_version": "2.3.0+cu121",
        "cuda_version": "12.1",
        "peak_memory_allocated": 123456789,
        "peak_memory_reserved": 234567890,
    }


def _read_report(output_dir: Path) -> dict:
    return json.loads((output_dir / "gpu_run_report.json").read_text())


def test_completed_report_atomic_with_all_fields_and_mock_flag(monkeypatch, tmp_path):
    bundle, manifest = _bundle(tmp_path, 4)
    output = tmp_path / "out"
    cli = _cli()
    monkeypatch.setattr(cli, "_resolve_code_revision", lambda **kwargs: CODE_REVISION)
    rc = cli.main(_args(bundle, output, manifest), runtime_info_provider=_fake_runtime)
    assert rc == 0
    report = _read_report(output)
    assert report["schema_version"] == "gpu_run_report_v1"
    assert report["status"] == "completed"
    assert report["started_at"]
    assert report["completed_at"]
    assert report["code_revision"] == CODE_REVISION
    assert report["source_bundle_id"] == manifest["source_bundle_id"]
    assert report["snapshot_id"] == SNAPSHOT
    assert report["corpus_manifest_id"] == CORPUS
    assert report["probe_report_id"] == PROBE
    assert report["postbuild_readiness_id"] == POSTBUILD
    assert report["model_name"] == "BAAI/bge-m3"
    assert report["model_revision"] == MODEL_REVISION
    assert report["dtype"] == "float32"
    assert report["dimension"] == 1024
    assert report["normalization"] == "l2"
    assert report["requested_batch_size"] == 2
    assert report["vector_count"] == 4
    assert report["gpu_name"] == "NVIDIA Test GPU"
    assert report["torch_version"] == "2.3.0+cu121"
    assert report["cuda_version"] == "12.1"
    assert report["peak_memory_allocated"] == 123456789
    assert report["peak_memory_reserved"] == 234567890
    assert report["non_production"] is True
    # artifact hashes match the checksums.sha256 file exactly
    checksums = {}
    for line in (output / "checksums.sha256").read_text().splitlines():
        digest, name = line.split(None, 1)
        checksums[name.strip()] = digest
    assert report["artifact_hashes"] == checksums


def test_completed_only_written_after_checksums_pass(monkeypatch, tmp_path):
    bundle, manifest = _bundle(tmp_path, 4)
    output = tmp_path / "out"
    cli = _cli()
    monkeypatch.setattr(cli, "_resolve_code_revision", lambda **kwargs: CODE_REVISION)

    def tampered_checksums(output_dir):
        raise ValueError("artifact checksum mismatch: index_manifest.json")

    monkeypatch.setattr(cli, "_verify_artifact_checksums", tampered_checksums)
    with pytest.raises(ValueError, match="checksum mismatch"):
        cli.main(_args(bundle, output, manifest), runtime_info_provider=_fake_runtime)
    report = _read_report(output)
    assert report["status"] == "failed"
    assert report["error_class"] == "ValueError"
    assert report["artifact_hashes"] == {}


def test_interrupt_writes_sanitized_interrupted_status(monkeypatch, tmp_path):
    bundle, manifest = _bundle(tmp_path, 4)
    output = tmp_path / "out"
    cli = _cli()
    monkeypatch.setattr(cli, "_resolve_code_revision", lambda **kwargs: CODE_REVISION)
    monkeypatch.setattr(
        cli, "build_embedding_artifacts",
        lambda *args, **kwargs: (_ for _ in ()).throw(KeyboardInterrupt("operator stopped")),
    )
    with pytest.raises(KeyboardInterrupt):
        cli.main(_args(bundle, output, manifest), runtime_info_provider=_fake_runtime)
    report = _read_report(output)
    assert report["status"] == "interrupted"
    assert report["error_class"] == "KeyboardInterrupt"
    assert "operator stopped" not in json.dumps(report)
    assert "operator stopped" not in report.get("error_class", "")
    assert not any(key in report for key in ("api_key", "token", "password", "secret", "credential"))


def test_failure_report_does_not_contain_secrets(monkeypatch, tmp_path):
    bundle, manifest = _bundle(tmp_path, 4)
    output = tmp_path / "out"
    cli = _cli()
    monkeypatch.setattr(cli, "_resolve_code_revision", lambda **kwargs: CODE_REVISION)
    monkeypatch.setattr(
        cli, "build_embedding_artifacts",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            RuntimeError("boom api_key=supersecret value token=abc123")
        ),
    )
    with pytest.raises(RuntimeError, match="boom"):
        cli.main(_args(bundle, output, manifest), runtime_info_provider=_fake_runtime)
    report = _read_report(output)
    assert report["status"] == "failed"
    assert report["error_class"] == "RuntimeError"
    text = json.dumps(report)
    assert "supersecret" not in text
    assert "abc123" not in text


def test_resume_rejects_report_identity_mismatch(monkeypatch, tmp_path):
    bundle, manifest = _bundle(tmp_path, 4)
    output = tmp_path / "out"
    output.mkdir(parents=True)
    cli = _cli()
    monkeypatch.setattr(cli, "_resolve_code_revision", lambda **kwargs: CODE_REVISION)
    # a prior interrupted report with a different source bundle identity
    (output / "gpu_run_report.json").write_text(json.dumps({
        "schema_version": "gpu_run_report_v1",
        "status": "interrupted",
        "started_at": "2026-08-06T00:00:00+00:00",
        "completed_at": "2026-08-06T00:00:00+00:00",
        "code_revision": CODE_REVISION,
        "source_bundle_id": "0" * 64,
        "snapshot_id": SNAPSHOT,
        "corpus_manifest_id": CORPUS,
        "probe_report_id": PROBE,
        "postbuild_readiness_id": POSTBUILD,
        "model_name": "BAAI/bge-m3",
        "model_revision": MODEL_REVISION,
        "dtype": "float32",
        "dimension": 1024,
        "normalization": "l2",
        "requested_batch_size": 2,
        "vector_count": None,
        "gpu_name": None,
        "torch_version": None,
        "cuda_version": None,
        "peak_memory_allocated": None,
        "peak_memory_reserved": None,
        "artifact_hashes": {},
        "error_class": "KeyboardInterrupt",
        "non_production": True,
    }))
    with pytest.raises(ValueError, match="source_bundle_id"):
        cli.main(
            _args(bundle, output, manifest, extra=["--resume"]),
            runtime_info_provider=_fake_runtime,
        )


def test_resume_after_interrupt_writes_completed(monkeypatch, tmp_path):
    bundle, manifest = _bundle(tmp_path, 4)
    output = tmp_path / "out"
    cli = _cli()
    monkeypatch.setattr(cli, "_resolve_code_revision", lambda **kwargs: CODE_REVISION)
    interrupt_state = {"armed": True}
    real_build = cli.build_embedding_artifacts

    def interrupted_build(*args, **kwargs):
        if interrupt_state["armed"]:
            interrupt_state["armed"] = False
            raise KeyboardInterrupt("operator stopped")
        return real_build(*args, **kwargs)

    monkeypatch.setattr(cli, "build_embedding_artifacts", interrupted_build)
    with pytest.raises(KeyboardInterrupt):
        cli.main(_args(bundle, output, manifest), runtime_info_provider=_fake_runtime)
    interrupted = _read_report(output)
    assert interrupted["status"] == "interrupted"

    rc = cli.main(
        _args(bundle, output, manifest, extra=["--resume"]),
        runtime_info_provider=_fake_runtime,
    )
    assert rc == 0
    completed = _read_report(output)
    assert completed["status"] == "completed"
    assert completed["source_bundle_id"] == manifest["source_bundle_id"]
    assert completed["non_production"] is True
    assert completed["vector_count"] == 4


# ---------------------------------------------------------------------------
# Task 3 (Pre-GPU amendment): production fail-closed GPU report contract
# ---------------------------------------------------------------------------


def _production_args(bundle: Path, output_dir: Path, manifest: dict, *, extra: list[str] | None = None) -> list[str]:
    """Same as _args but without --mock: exercises the production path with an
    injected embedder (no real model, no GPU, no download)."""
    values = [
        "--source-bundle", str(bundle),
        "--output-dir", str(output_dir),
        "--batch-size", "2",
        "--expected-source-bundle-id", manifest["source_bundle_id"],
        "--expected-snapshot-id", SNAPSHOT,
        "--expected-corpus-manifest-id", CORPUS,
        "--expected-probe-report-id", PROBE,
        "--expected-postbuild-readiness-id", POSTBUILD,
        "--code-revision", CODE_REVISION,
    ]
    if extra:
        values.extend(extra)
    return values


def _fake_embed(texts):
    return np.ones((len(texts), 1024), dtype=np.float32)


def _production_main(monkeypatch, tmp_path, bundle, manifest, runtime_provider, extra=None):
    cli = _cli()
    monkeypatch.setattr(cli, "_resolve_code_revision", lambda **kwargs: CODE_REVISION)
    monkeypatch.setattr(cli, "load_real_cuda_embedder", lambda **kwargs: _fake_embed)
    output = tmp_path / "out"
    return cli.main(
        _production_args(bundle, output, manifest, extra=extra),
        runtime_info_provider=runtime_provider,
    ), output


def test_production_completed_requires_valid_runtime_info_fields(monkeypatch, tmp_path):
    bundle, manifest = _bundle(tmp_path, 4)
    rc, output = _production_main(
        monkeypatch, tmp_path, bundle, manifest, _fake_runtime,
    )
    assert rc == 0
    report = _read_report(output)
    assert report["status"] == "completed"
    assert "non_production" not in report or report["non_production"] is False
    assert report["gpu_name"] == "NVIDIA Test GPU"
    assert report["torch_version"] == "2.3.0+cu121"
    assert report["cuda_version"] == "12.1"
    assert report["peak_memory_allocated"] >= 0
    assert report["peak_memory_reserved"] >= report["peak_memory_allocated"]


def test_production_completed_blocked_by_incomplete_runtime_info(monkeypatch, tmp_path):
    bundle, manifest = _bundle(tmp_path, 4)

    def incomplete_runtime():
        return {
            "gpu_name": None,
            "torch_version": "2.3.0+cu121",
            "cuda_version": "12.1",
            "peak_memory_allocated": 123,
            "peak_memory_reserved": 456,
        }

    with pytest.raises(RuntimeError, match="runtime"):
        _production_main(monkeypatch, tmp_path, bundle, manifest, incomplete_runtime)
    report = _read_report(tmp_path / "out")
    assert report["status"] == "failed"
    assert report["error_class"] == "RuntimeError"
    assert report["gpu_name"] is None


def test_production_completed_blocked_by_peak_memory_invalid(monkeypatch, tmp_path):
    bundle, manifest = _bundle(tmp_path, 4)
    for index, runtime in enumerate((
        {
            "gpu_name": "GPU", "torch_version": "2.3.0", "cuda_version": "12.1",
            "peak_memory_allocated": -1, "peak_memory_reserved": 0,
        },
        {
            "gpu_name": "GPU", "torch_version": "2.3.0", "cuda_version": "12.1",
            "peak_memory_allocated": 500, "peak_memory_reserved": 100,
        },
    )):
        cli = _cli()
        monkeypatch.setattr(cli, "_resolve_code_revision", lambda **kwargs: CODE_REVISION)
        monkeypatch.setattr(cli, "load_real_cuda_embedder", lambda **kwargs: _fake_embed)
        output = tmp_path / f"out_{index}"
        with pytest.raises(RuntimeError, match="peak_memory"):
            cli.main(
                _production_args(bundle, output, manifest),
                runtime_info_provider=lambda r=runtime: r,
            )
        report = _read_report(output)
        assert report["status"] == "failed"


def test_runtime_info_collection_failure_blocks_completed(monkeypatch, tmp_path):
    bundle, manifest = _bundle(tmp_path, 4)

    def raising_runtime():
        raise RuntimeError("runtime collection boom")

    with pytest.raises(RuntimeError, match="runtime collection boom"):
        _production_main(monkeypatch, tmp_path, bundle, manifest, raising_runtime)
    report = _read_report(tmp_path / "out")
    assert report["status"] == "failed"
    assert report["error_class"] == "RuntimeError"
    assert "runtime collection boom" not in json.dumps(report)


def test_failure_path_runtime_is_best_effort_and_preserves_original_exception(monkeypatch, tmp_path):
    bundle, manifest = _bundle(tmp_path, 4)
    cli = _cli()
    monkeypatch.setattr(cli, "_resolve_code_revision", lambda **kwargs: CODE_REVISION)
    monkeypatch.setattr(
        cli, "build_embedding_artifacts",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            RuntimeError("original driver boom with /secret/path/key=topsecret")
        ),
    )

    def raising_runtime():
        raise RuntimeError("runtime boom that must not mask the original")

    output = tmp_path / "out"
    with pytest.raises(RuntimeError, match="original driver boom"):
        cli.main(
            _args(bundle, output, manifest),
            runtime_info_provider=raising_runtime,
        )
    report = _read_report(output)
    assert report["status"] == "failed"
    assert report["error_class"] == "RuntimeError"
    text = json.dumps(report)
    assert "runtime boom" not in text
    assert "topsecret" not in text
    assert "/secret/path" not in text


def test_preflight_failure_before_ownership_does_not_touch_output_dir(monkeypatch, tmp_path):
    bundle, manifest = _bundle(tmp_path, 4)
    output = tmp_path / "out"
    output.mkdir()
    sentinel = output / "vectors.npy"
    sentinel.write_bytes(b"existing-artifact")
    prior_report = output / "gpu_run_report.json"
    prior_report.write_text('{"status":"completed","source_bundle_id":"1111111111111111111111111111111111111111111111111111111111111111"}')
    report_before = prior_report.read_bytes()
    cli = _cli()
    monkeypatch.setattr(cli, "_resolve_code_revision", lambda **kwargs: CODE_REVISION)
    bad_args = _production_args(bundle, output, manifest)
    index = bad_args.index("--expected-source-bundle-id")
    bad_args[index + 1] = "0" * 64
    with pytest.raises(ValueError, match="source_bundle_id"):
        cli.main(bad_args, runtime_info_provider=_fake_runtime)
    # zero-write: no report overwrite, no artifact overwrite
    assert prior_report.read_bytes() == report_before
    assert sentinel.read_bytes() == b"existing-artifact"


def test_non_resume_non_empty_output_dir_fails_without_any_write(monkeypatch, tmp_path):
    bundle, manifest = _bundle(tmp_path, 4)
    output = tmp_path / "out"
    output.mkdir()
    sentinel = output / "checkpoint.json"
    sentinel.write_text("existing-checkpoint")
    cli = _cli()
    monkeypatch.setattr(cli, "_resolve_code_revision", lambda **kwargs: CODE_REVISION)
    with pytest.raises(ValueError, match="must be empty"):
        cli.main(
            _production_args(bundle, output, manifest),
            runtime_info_provider=_fake_runtime,
        )
    assert sentinel.read_text() == "existing-checkpoint"
    assert not (output / "gpu_run_report.json").exists()


def _interrupted_after_build(tmp_path, monkeypatch, bundle, manifest):
    """Run once interrupted after artifacts exist; returns (cli, output)."""
    cli = _cli()
    monkeypatch.setattr(cli, "_resolve_code_revision", lambda **kwargs: CODE_REVISION)
    interrupt_state = {"armed": True}
    real_verify = cli._verify_artifact_checksums

    def interrupt_verify(output_dir):
        if interrupt_state["armed"]:
            interrupt_state["armed"] = False
            raise KeyboardInterrupt("operator stop after build")
        return real_verify(output_dir)

    monkeypatch.setattr(cli, "_verify_artifact_checksums", interrupt_verify)
    output = tmp_path / "out"
    with pytest.raises(KeyboardInterrupt):
        cli.main(_args(bundle, output, manifest), runtime_info_provider=_fake_runtime)
    report = _read_report(output)
    assert report["status"] == "interrupted"
    assert (output / "index_manifest.json").is_file()
    return cli, output


def test_resume_cross_validates_index_manifest_identity(monkeypatch, tmp_path):
    bundle, manifest = _bundle(tmp_path, 4)
    _interrupted_after_build(tmp_path, monkeypatch, bundle, manifest)
    output = tmp_path / "out"

    manifest_payload = json.loads((output / "index_manifest.json").read_text())
    manifest_payload["source_bundle_id"] = "0" * 64
    (output / "index_manifest.json").write_text(json.dumps(manifest_payload))

    with pytest.raises(ValueError, match="index_manifest source_bundle_id"):
        _cli().main(
            _args(bundle, output, manifest, extra=["--resume"]),
            runtime_info_provider=_fake_runtime,
        )


def test_resume_cross_validates_code_revision(monkeypatch, tmp_path):
    bundle, manifest = _bundle(tmp_path, 4)
    _interrupted_after_build(tmp_path, monkeypatch, bundle, manifest)
    output = tmp_path / "out"

    report = json.loads((output / "gpu_run_report.json").read_text())
    report["code_revision"] = "0" * 40
    (output / "gpu_run_report.json").write_text(json.dumps(report))

    with pytest.raises(ValueError, match="code_revision"):
        _cli().main(
            _args(bundle, output, manifest, extra=["--resume"]),
            runtime_info_provider=_fake_runtime,
        )


def test_resume_cross_validates_model_revision(monkeypatch, tmp_path):
    bundle, manifest = _bundle(tmp_path, 4)
    _interrupted_after_build(tmp_path, monkeypatch, bundle, manifest)
    output = tmp_path / "out"

    report = json.loads((output / "gpu_run_report.json").read_text())
    report["model_revision"] = "0" * 40
    (output / "gpu_run_report.json").write_text(json.dumps(report))

    with pytest.raises(ValueError, match="model_revision"):
        _cli().main(
            _args(bundle, output, manifest, extra=["--resume"]),
            runtime_info_provider=_fake_runtime,
        )


def test_mock_completed_accepts_injected_runtime_without_gpu_fields(monkeypatch, tmp_path):
    bundle, manifest = _bundle(tmp_path, 4)
    output = tmp_path / "out"
    cli = _cli()
    monkeypatch.setattr(cli, "_resolve_code_revision", lambda **kwargs: CODE_REVISION)
    rc = cli.main(
        _args(bundle, output, manifest),
        runtime_info_provider=lambda: {
            "gpu_name": None, "torch_version": None, "cuda_version": None,
            "peak_memory_allocated": None, "peak_memory_reserved": None,
        },
    )
    assert rc == 0
    report = _read_report(output)
    assert report["status"] == "completed"
    assert report["non_production"] is True
    assert report["gpu_name"] is None


def test_failure_path_report_write_failure_does_not_mask_original(monkeypatch, tmp_path):
    """If the failure report write itself raises, the original build exception
    must still propagate (never masked by a secondary report error)."""
    bundle, manifest = _bundle(tmp_path, 4)
    output = tmp_path / "out"
    cli = _cli()
    monkeypatch.setattr(cli, "_resolve_code_revision", lambda **kwargs: CODE_REVISION)
    monkeypatch.setattr(
        cli, "build_embedding_artifacts",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("original build boom")),
    )

    def raising_report_write(*args, **kwargs):
        raise OSError("report write boom")

    monkeypatch.setattr(cli, "_write_gpu_run_report", raising_report_write)
    with pytest.raises(RuntimeError, match="original build boom"):
        cli.main(_args(bundle, output, manifest), runtime_info_provider=_fake_runtime)


def test_resume_rejects_mock_mode_mismatch(monkeypatch, tmp_path):
    """Resume must cross-bind --mock consistency: a production resume over a
    mock-built partial artifact (and vice versa) is rejected."""
    bundle, manifest = _bundle(tmp_path, 4)
    _interrupted_after_build(tmp_path, monkeypatch, bundle, manifest)
    output = tmp_path / "out"
    report = json.loads((output / "gpu_run_report.json").read_text())
    assert report["non_production"] is True

    # production resume over a mock partial artifact
    with pytest.raises(ValueError, match="non_production"):
        _cli().main(
            _production_args(bundle, output, manifest, extra=["--resume"]),
            runtime_info_provider=_fake_runtime,
        )

    # mock resume over a production partial artifact
    report["non_production"] = False
    (output / "gpu_run_report.json").write_text(json.dumps(report))
    with pytest.raises(ValueError, match="non_production"):
        _cli().main(
            _args(bundle, output, manifest, extra=["--resume"]),
            runtime_info_provider=_fake_runtime,
        )


def test_cli_resume_sigkill_keeps_checkpoint_only_and_rejects_code_revision(monkeypatch, tmp_path):
    """SIGKILL-equivalent: after the first shard, only checkpoint + shards
    remain (report/index_manifest never materialized). Resume must validate
    the checkpoint identity before embedding; a different code revision is
    rejected without any embed call."""
    # embed_with_oom_backoff caps batches at 32, and the driver default
    # shard_size is 1024: shard 0 completes after 32 successful embed calls,
    # then the 33rd call raises the SIGKILL-equivalent interrupt. Use 1056
    # chunks so exactly one shard is written before the interrupt.
    bundle, manifest = _bundle(tmp_path, 1056)
    output = tmp_path / "out"
    cli = _cli()
    monkeypatch.setattr(cli, "_resolve_code_revision", lambda expected=None, **kwargs: expected or CODE_REVISION)
    real_mock_embed = cli._mock_embed
    interrupt_state = {"calls": 0}

    def interrupted_embed(texts):
        interrupt_state["calls"] += 1
        if interrupt_state["calls"] <= 32:
            return real_mock_embed(texts)
        raise KeyboardInterrupt("sigkill-equivalent")

    monkeypatch.setattr(cli, "_mock_embed", interrupted_embed)
    with pytest.raises(KeyboardInterrupt):
        cli.main(
            _args(bundle, output, manifest, extra=["--batch-size", "32"]),
            runtime_info_provider=_fake_runtime,
        )
    # checkpoint + shards remain; simulate SIGKILL aftermath by dropping the
    # interrupted report and any uncommitted artifact files.
    assert (output / "checkpoint.json").is_file()
    assert list((output / "shards").glob("shard_*.npz"))
    for name in ("gpu_run_report.json", "index_manifest.json", "vectors.npy", "chunk_ids.json", "checksums.sha256"):
        target = output / name
        if target.exists():
            target.unlink()
    assert not (output / "index_manifest.json").exists()
    assert not (output / "gpu_run_report.json").exists()

    # different code revision: rejected before any embedding
    embed_called = {"count": 0}

    def counting_embed(texts):
        embed_called["count"] += 1
        return real_mock_embed(texts)

    monkeypatch.setattr(cli, "_mock_embed", counting_embed)
    with pytest.raises(ValueError, match="code_revision"):
        cli.main(
            _args(
                bundle, output, manifest,
                extra=["--resume", "--code-revision", "0" * 40, "--batch-size", "32"],
            ),
            runtime_info_provider=_fake_runtime,
        )
    assert embed_called["count"] == 0
