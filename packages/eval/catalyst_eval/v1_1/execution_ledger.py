"""Execution ledger for Stage-1 (M7-8).

Rows bind eval/case/run/result identities, terminal status, attempts,
provider calls, cost, and a row checksum. A nonterminal or corrupt row is
never silently reused; completed identity-valid terminal rows are never
rerun or billed twice. The ledger is a separate untracked execution artifact
and is never rewritten as EvalManifest identity.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from catalyst_eval.v1_1.loader import canonical_bytes

LEDGER_SCHEMA_VERSION = "v1_1_stage1_execution_ledger_v1"


@dataclass(frozen=True)
class LedgerRow:
    eval_id: str
    case_id: str
    run_manifest_id: str
    run_manifest_hash: str
    result_artifact_id: str
    result_artifact_hash: str
    terminal_status: str
    attempts: int
    provider_calls: int
    cost_usd: float | None
    checksum: str
    identity_valid: bool = True

    def compute_checksum(self) -> str:
        payload = {
            "eval_id": self.eval_id,
            "case_id": self.case_id,
            "run_manifest_id": self.run_manifest_id,
            "run_manifest_hash": self.run_manifest_hash,
            "result_artifact_id": self.result_artifact_id,
            "result_artifact_hash": self.result_artifact_hash,
            "terminal_status": self.terminal_status,
            "attempts": self.attempts,
            "provider_calls": self.provider_calls,
            "cost_usd": self.cost_usd,
        }
        return hashlib.sha256(canonical_bytes(payload)).hexdigest()


_TERMINAL_LEDGER_STATUSES = frozenset({"COMPLETED", "FAILED", "CANCELLED"})


class ExecutionLedger:
    """Append-only JSONL ledger with identity-valid reuse rules."""

    def __init__(self, *, rows: Sequence[LedgerRow] = ()) -> None:
        self._rows = tuple(rows)

    @property
    def rows(self) -> tuple[LedgerRow, ...]:
        return self._rows

    def append(self, row: LedgerRow) -> "ExecutionLedger":
        return ExecutionLedger(rows=(*self._rows, row))

    def write(self, path: str | Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        lines = [
            json.dumps(
                {
                    "schema_version": LEDGER_SCHEMA_VERSION,
                    "row": {
                        "eval_id": row.eval_id,
                        "case_id": row.case_id,
                        "run_manifest_id": row.run_manifest_id,
                        "run_manifest_hash": row.run_manifest_hash,
                        "result_artifact_id": row.result_artifact_id,
                        "result_artifact_hash": row.result_artifact_hash,
                        "terminal_status": row.terminal_status,
                        "attempts": row.attempts,
                        "provider_calls": row.provider_calls,
                        "cost_usd": row.cost_usd,
                        "checksum": row.compute_checksum(),
                    },
                },
                sort_keys=True,
            )
            for row in self._rows
        ]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    @classmethod
    def load(
        cls, path: str | Path, *, expected_eval_id: str | None = None
    ) -> "ExecutionLedger":
        path = Path(path)
        if not path.is_file():
            return cls()
        rows: list[LedgerRow] = []
        with path.open("r", encoding="utf-8") as handle:
            for line_no, line in enumerate(handle, start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ValueError(f"{path}:{line_no}: corrupt ledger row: {exc}") from exc
                row_data = record.get("row")
                if not isinstance(row_data, dict):
                    raise ValueError(f"{path}:{line_no}: ledger row must be an object")
                declared = row_data.get("checksum")
                row = LedgerRow(
                    **{**row_data, "checksum": declared or ""}
                )
                valid = declared is not None and declared == row.compute_checksum()
                row = LedgerRow(
                    **{**row_data, "checksum": declared or "", "identity_valid": valid}
                )
                if expected_eval_id is not None and row.eval_id != expected_eval_id:
                    raise ValueError(
                        f"{path}:{line_no}: ledger eval_id {row.eval_id!r} is "
                        f"incompatible with expected {expected_eval_id!r}"
                    )
                rows.append(row)
        return cls(rows=rows)

    def reusable_terminal_rows(self, eval_id: str) -> tuple[LedgerRow, ...]:
        """Identity-valid terminal rows only; corrupt/nonterminal never reused."""
        return tuple(
            row
            for row in self._rows
            if row.eval_id == eval_id
            and row.identity_valid
            and row.terminal_status in _TERMINAL_LEDGER_STATUSES
        )


__all__ = ["ExecutionLedger", "LEDGER_SCHEMA_VERSION", "LedgerRow"]
