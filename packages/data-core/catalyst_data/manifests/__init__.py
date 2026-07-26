"""B2-O manifest contracts."""

from .universe import (
    SourceCell,
    SourceScope,
    SourceWindows,
    UniverseManifest,
    UniverseSpec,
    build_b2o_source_scopes,
    build_universe_manifest,
    load_universe_spec,
    terminal_complete,
)
from .snapshot import DataSnapshotManifest, build_data_snapshot_manifest
from .operations import (
    BootstrapResult,
    PromotionResult,
    ResourceLimitError,
    bootstrap_candidate,
    promote_candidate,
    publish_corpus_with_resource_gate,
    sha256_file,
)

__all__ = [
    "BootstrapResult",
    "DataSnapshotManifest",
    "PromotionResult",
    "ResourceLimitError",
    "SourceCell",
    "SourceScope",
    "SourceWindows",
    "UniverseManifest",
    "UniverseSpec",
    "bootstrap_candidate",
    "build_b2o_source_scopes",
    "build_data_snapshot_manifest",
    "build_universe_manifest",
    "load_universe_spec",
    "promote_candidate",
    "publish_corpus_with_resource_gate",
    "sha256_file",
    "terminal_complete",
]
