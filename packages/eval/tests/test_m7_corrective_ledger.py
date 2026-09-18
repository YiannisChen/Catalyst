from __future__ import annotations

from catalyst_eval.v1_1.execution_ledger import ExecutionLedger, LedgerRow


def test_execution_ledger_replaces_file_atomically_and_leaves_no_temp(tmp_path):
    path = tmp_path / "execution.jsonl"
    row = LedgerRow(
        eval_id="eval:1",
        case_id="case:1",
        run_manifest_id="manifest:1",
        run_manifest_hash="a" * 64,
        result_artifact_id="result:1",
        result_artifact_hash="b" * 64,
        terminal_status="CANCELLED",
        attempts=1,
        provider_calls=2,
        cost_usd=0.25,
        checksum="",
    )
    ExecutionLedger(rows=(row,)).write(path)
    assert path.is_file()
    assert not tuple(tmp_path.glob(".*.tmp"))
    assert ExecutionLedger.load(path).rows[0].terminal_status == "CANCELLED"
