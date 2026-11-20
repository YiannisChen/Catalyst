"""Code-owned answer text (Q1c): no Writer LLM on the answer path.

Streams through the unchanged ``writer_node`` so delta persistence, the
code-owned structural suffix and post-stream assurance stay exactly as they
are. The only prose it emits is fixed template text around the observed move;
the causal lines come from ``assemble_structural_suffix``, whose statements are
the verified quotes (``attribution/quotes.py``).
"""
from __future__ import annotations

from typing import Any, Iterator

from catalyst_agents.attribution.analyst import AttributionStatus
from catalyst_agents.attribution.answer_snapshot import ABSTAIN_LIMITATION
from catalyst_agents.engine.earnings import BASIS as EARNINGS_BASIS
from catalyst_agents.runtime.provider_capability import (
    CAPABILITY_REVISION,
    ProviderCapability,
)

RENDERER_IDENTITY = "catalyst:quote_answer_renderer_v1"

_CAUSAL_NOTE = (
    "The cause lines below quote source documents verbatim; each quote was "
    "checked against its cited evidence and the session window. Which sentences "
    "were selected, and the cause category, are model judgments."
)
# The filing fallback's quote is picked by code (engine/earnings.py), so the
# model-judgment sentence of _CAUSAL_NOTE does not apply to it.
_FILING_NOTE = (
    "The cause lines below quote source documents verbatim; each quote was "
    "checked against its cited evidence and the session window. In this case "
    "the quoted line was chosen by a fixed rule, not by a model."
)
_ABSTAIN_NOTE = ABSTAIN_LIMITATION


class QuoteAnswerRenderer:
    """A provider-shaped, deterministic stream over one WriterInput."""

    capability_metadata = ProviderCapability(
        supports_structured_output=False,
        supports_true_streaming=True,
        declares_token_accounting=True,
        normalizes_timeout_errors=True,
        capability_revision=CAPABILITY_REVISION,
    )

    def __init__(self, writer_input: Any, basis: str | None = None) -> None:
        self.writer_input = writer_input
        self.basis = basis

    def render(self) -> str:
        observed = " ".join(str(self.writer_input.observed_move).split())
        if self.writer_input.final_status is AttributionStatus.ABSTAIN:
            lines = ["## OBSERVED_MOVE", observed, ""]
            extra = getattr(self.writer_input, "abstain_context", "")
            if extra:
                lines += [extra, ""]
            lines += ["## LIMITATIONS", _ABSTAIN_NOTE]
            lines.extend(f"- {item}" for item in self.writer_input.required_limitations)
            return "\n".join(lines) + "\n"
        note = _FILING_NOTE if self.basis == EARNINGS_BASIS else _CAUSAL_NOTE
        return f"{observed}\n{note}"

    def stream(self, messages: list[dict]) -> Iterator[dict]:
        del messages
        yield {"content": self.render(), "usage_metadata": {"input_tokens": 0, "output_tokens": 0}}


__all__ = ["QuoteAnswerRenderer", "RENDERER_IDENTITY"]
