"""Hidden-gold isolation and leakage scan (M7-4).

Stage-1 run artifacts must never carry hidden-gold oracle fields: expected
statuses, acceptable cause labels, expected research behavior, reviewer
notes, relevance/materiality/independence annotations, expected refusal, or
hidden answer text. Evidence ids that arrive naturally through the pinned
retrieval result / ContextPack inventory are allowed and scored later; an
evidence id is a leakage violation only when copied from hidden-gold input
without a matching served retrieval record. Findings carry field/path/reason
but never repeat secret or hidden-gold values.

Eval joins run results to cases only after execution by an opaque eval-run
binding; gold is never preloaded into the runtime (see
``verify_hidden_gold_boundary``).
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
from typing import Any, Iterable, Literal, Sequence

from catalyst_eval.v1_1.case import GoldenCase

ArtifactKind = Literal["prompt", "state", "trace", "output", "artifact"]


@dataclass(frozen=True)
class RunArtifactInput:
    """One production artifact under scan, classified by model visibility.

    ``post_writer_persisted`` is an explicit provenance assertion supplied by
    the runtime-artifact loader after it has confirmed that this is the
    ``run_diagnostics`` artifact published with the post-Writer terminal
    transaction.  It is deliberately not inferred from a path or a trace
    kind: generic/model-visible traces must remain subject to the gold scan.
    """

    path: str
    kind: ArtifactKind
    payload: Any
    artifact_type: str | None = None
    post_writer_persisted: bool = False


_FORBIDDEN_IMPORT_RE = re.compile(
    r"(?:from\s+catalyst_eval\s+import\s+.*(?:golden_set|v1_1\.case)"
    r"|from\s+catalyst_eval\.(?:golden_set|v1_1\.case)"
    r"|import\s+catalyst_eval\.(?:golden_set|v1_1\.case))"
)

_FORBIDDEN_FIELD_NAMES = frozenset(
    {
        "oracle_status",
        "expected_status",
        "acceptable_cause_labels",
        "expected_primary_evidence",
        "expected_refusal_reason",
        "expected_attribution_type",
        "expected_research_behavior",
        "evidence_judgments",
        "independence_group",
        "materiality",
        "relevance",
        "annotator",
        "reviewer_notes",
        "notes",
        "hidden_answer",
    }
)

# Statuses are hidden when they sit in a prompt/state/trace; the model's own
# output status is the runtime conclusion and is not oracle leakage.
_INPUT_KINDS = frozenset({"prompt", "state", "trace"})

# Post-Writer `run_diagnostics.terminal` fields persisted at run.completed.
# They are runtime conclusions, not model-visible gold (live c04 false positive).
_RUNTIME_TERMINAL_STATUS_LEAVES = frozenset(
    {"terminal.result_status", "terminal.status_ceiling"}
)


def _is_verified_post_writer_run_diagnostics(
    artifact: RunArtifactInput,
) -> bool:
    """Return true only for a verified terminal diagnostics artifact.

    The three independent bindings are intentional: artifact type/provenance
    comes from the persistence boundary, while schema and terminal event are
    checked from the persisted payload itself.  Field paths alone are never a
    reason to exempt a model-visible trace.
    """
    if artifact.artifact_type != "run_diagnostics":
        return False
    if artifact.post_writer_persisted is not True:
        return False
    if not isinstance(artifact.payload, dict):
        return False
    if artifact.payload.get("schema_version") != "v1.1_run_diagnostics_v1":
        return False
    terminal = artifact.payload.get("terminal")
    return isinstance(terminal, dict) and (
        terminal.get("terminal_event_type") == "run.completed"
    )


def _iter_nodes(value: Any) -> Iterable[tuple[str, Any]]:
    """Yield (path, node) for every dict key and string leaf of a payload."""
    stack: list[tuple[str, Any]] = [("", value)]
    while stack:
        prefix, node = stack.pop()
        if isinstance(node, dict):
            for key, child in node.items():
                path = f"{prefix}.{key}" if prefix else str(key)
                yield path, child
                stack.append((path, child))
        elif isinstance(node, list):
            for index, child in enumerate(node):
                yield f"{prefix}[{index}]", child
                stack.append((f"{prefix}[{index}]", child))


def _iter_strings(value: Any) -> Iterable[tuple[str, str]]:
    for path, node in _iter_nodes(value):
        if isinstance(node, str):
            yield path, node
        elif not isinstance(node, (dict, list)):
            yield path, str(node)


def scan_run_for_gold(
    run_artifacts: Sequence[RunArtifactInput],
    gold_case: GoldenCase,
    *,
    served_evidence_ids: Sequence[str] = (),
) -> list[str]:
    """Scan production artifacts for hidden-gold leakage.

    Returns a list of findings; each finding names the artifact path, the
    artifact kind, and a reason, and never repeats the hidden-gold value.
    """
    findings: list[str] = []
    served = set(served_evidence_ids)
    gold_labels = [
        label.label for label in gold_case.acceptable_cause_labels if label.label
    ]
    gold_evidence_ids = {
        judgment.evidence_id for judgment in gold_case.evidence_judgments
    }

    for artifact in run_artifacts:
        prefix = f"{artifact.kind}:{artifact.path}"

        def _report(reason: str, path: str | None = None) -> None:
            where = f"{prefix}.{path}" if path else prefix
            findings.append(f"{where}: {reason}")

        # 1. Forbidden oracle field names anywhere in the payload.
        for path, _node in _iter_nodes(artifact.payload):
            leaf = path.rsplit(".", 1)[-1].split("[", 1)[0]
            if leaf in _FORBIDDEN_FIELD_NAMES:
                _report(f"forbidden oracle field name {leaf!r}", path)

        # 2. Hidden oracle status/refusal values in model-visible inputs.
        if artifact.kind in _INPUT_KINDS:
            verified_post_writer_diagnostics = (
                _is_verified_post_writer_run_diagnostics(artifact)
            )
            for path, text in _iter_strings(artifact.payload):
                if (
                    verified_post_writer_diagnostics
                    and ".".join(path.split(".")[-2:])
                    in _RUNTIME_TERMINAL_STATUS_LEAVES
                ):
                    continue
                if gold_case.oracle_status and gold_case.oracle_status in text:
                    _report("hidden oracle status", path)
                if gold_case.expected_refusal_reason and (
                    gold_case.expected_refusal_reason in text
                ):
                    _report("hidden expected refusal reason", path)

        # 3. Cause labels and hidden answer text are never model-visible.
        if artifact.kind != "output":
            for path, text in _iter_strings(artifact.payload):
                if any(label and label in text for label in gold_labels):
                    _report("hidden cause label", path)
                    break
        if gold_case.notes:
            for path, text in _iter_strings(artifact.payload):
                if gold_case.notes in text:
                    _report("hidden reviewer note/answer text", path)

        # 4. Evidence ids: allowed only with a matching served record.
        for path, text in _iter_strings(artifact.payload):
            if text in gold_evidence_ids and text not in served:
                _report(
                    "gold evidence id without a matching served retrieval record",
                    path,
                )

    return findings


_PACKAGE_ROOTS: tuple[Path, ...] = (
    Path(__file__).resolve().parents[3] / "agents",
    Path(__file__).resolve().parents[3] / "app",
    Path(__file__).resolve().parents[3] / "data-core",
)


def verify_hidden_gold_boundary() -> list[str]:
    """Static check: agents/app/data-core production source never imports gold.

    ``catalyst_eval`` is eval-owned; production packages must not import
    ``catalyst_eval.golden_set`` or ``catalyst_eval.v1_1.case``. Test
    directories are excluded: eval-boundary ownership tests legitimately
    import the eval contract to assert package ownership.
    """
    violations: list[str] = []
    for root in _PACKAGE_ROOTS:
        if not root.is_dir():
            continue
        for source in root.rglob("*.py"):
            if "__pycache__" in source.parts or "tests" in source.parts:
                continue
            try:
                text = source.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            for line_no, line in enumerate(text.splitlines(), start=1):
                if _FORBIDDEN_IMPORT_RE.search(line):
                    violations.append(
                        f"{source.relative_to(root)}:{line_no}: forbidden gold import"
                    )
    return violations


__all__ = [
    "ArtifactKind",
    "RunArtifactInput",
    "scan_run_for_gold",
    "verify_hidden_gold_boundary",
]
