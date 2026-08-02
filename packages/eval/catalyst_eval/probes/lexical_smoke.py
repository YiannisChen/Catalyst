"""Eval compatibility exports for production-owned Pre-B6 lexical smoke."""

from catalyst_data.pre_b6_probes import (
    SMOKE_STOPWORDS_V1,
    LexicalSmokeError,
    SmokeProbeResult,
    build_lexical_smoke_query,
    fts5_and_query,
    run_lexical_smoke_probe,
)

__all__ = [
    "SMOKE_STOPWORDS_V1",
    "LexicalSmokeError",
    "SmokeProbeResult",
    "build_lexical_smoke_query",
    "fts5_and_query",
    "run_lexical_smoke_probe",
]
