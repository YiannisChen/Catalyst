"""Audit or migrate immutable body snapshots; never writes the source database."""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

from catalyst_data.ingestion import input_manifest
from catalyst_data.ingestion.time import timefields
from catalyst_data.retrieval import evidence_fts_projection, web_bodies
from catalyst_data.storage import (
    body_migration as migration,
)
from catalyst_data.storage import (
    body_versions,
    legacy_cohorts,
    legacy_import,
    snapshots,
    temporal_evidence,
)

ENVIRONMENT = ('PYTHONPATH','CATALYST_ARCHIVE_DIR','CATALYST_DATA_DIR','CATALYST_RUNTIME_DATA_DIR',
               'CATALYST_RATELIMIT_DIR','CATALYST_RESILIENCE_DB')


def main(argv=None):
    arguments = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True)
    parser.add_argument('--kind', choices=('alpaca', 'legacy_chunks'), required=True)
    parser.add_argument('--snapshot-observed-at', required=True)
    parser.add_argument('--destination')
    parser.add_argument('--audit-only',action='store_true')
    parser.add_argument('--legacy-cohort')
    parser.add_argument('--legacy-catalog')
    args = parser.parse_args(arguments)
    try:
        source = Path(args.source).expanduser()
        if not source.is_absolute():
            raise ValueError('source must be an absolute snapshot path')
        legacy_import_mode = args.kind == 'legacy_chunks' and not args.audit_only
        catalog_path = None
        if args.kind == 'legacy_chunks':
            if args.audit_only:
                if not args.legacy_cohort:
                    raise ValueError('legacy chunk inventory requires an explicit cohort label')
                if args.legacy_catalog:
                    raise ValueError('audit-only legacy inventory does not take a catalog')
            else:
                if not args.legacy_catalog:
                    raise ValueError('legacy chunk import requires an explicit pinned catalog')
                catalog_path = Path(args.legacy_catalog).expanduser()
                if not catalog_path.is_absolute():
                    raise ValueError('legacy catalog must be an absolute path')
        elif args.legacy_cohort or args.legacy_catalog:
            raise ValueError('legacy options are only valid for legacy_chunks')

        if args.audit_only and args.destination:
            raise ValueError('audit-only does not take a destination')
        destination_path = None
        if not args.audit_only:
            if not args.destination:
                raise ValueError('migration requires an absolute new destination')
            destination_path = Path(args.destination).expanduser()
            if not destination_path.is_absolute():
                raise ValueError('migration requires an absolute new destination')
            if destination_path.is_symlink():
                raise ValueError('destination cannot be a symlink')
            destination_path = destination_path.resolve(strict=False)

        required = [source, Path(__file__).resolve(), Path(migration.__file__).resolve(),
                    Path(body_versions.__file__).resolve(), Path(input_manifest.__file__).resolve(),
                    Path(timefields.__file__).resolve()]
        if legacy_import_mode:
            required.extend((
                catalog_path,
                Path(legacy_import.__file__).resolve(),
                Path(legacy_cohorts.__file__).resolve(),
                Path(snapshots.__file__).resolve(),
                Path(temporal_evidence.__file__).resolve(),
                Path(evidence_fts_projection.__file__).resolve(),
                Path(web_bodies.__file__).resolve(),
            ))
        invocation = ['-m','catalyst_data.ingestion.storage.body_migration',*arguments]
        receipt = input_manifest.require_manifest(
            invocation=invocation, required_paths=required, required_env=ENVIRONMENT)
        captured = timefields.stored(args.snapshot_observed_at)
        if captured > timefields.now_utc():
            raise ValueError('snapshot observation is in the future')

        result = None
        if args.audit_only:
            with sqlite3.connect(source.as_uri() + '?mode=ro&immutable=1', uri=True) as src:
                result = (migration.audit_alpaca(src) if args.kind == 'alpaca'
                          else migration.inventory_legacy_chunks(src))
        elif legacy_import_mode:
            # The catalog must itself have passed require_manifest before its
            # source-store identity is read and used to construct the pin set.
            try:
                catalog_spec = json.loads(catalog_path.read_text(encoding='utf-8'))
                source_store_id = catalog_spec['source_snapshot']['store_id']
            except (OSError, UnicodeError, ValueError, KeyError, TypeError):
                raise ValueError('legacy catalog source declaration is invalid') from None
            if not isinstance(source_store_id, str) or not source_store_id.strip():
                raise ValueError('legacy catalog source declaration is invalid')
            stores = snapshots.load_verified_stores(
                os.environ['CATALYST_INPUT_MANIFEST'], invocation=invocation,
                required_stores={source_store_id: source}, required_env=ENVIRONMENT,
            )
            cohort = legacy_cohorts.load_cohort(catalog_path, stores)
            if args.legacy_cohort is not None and args.legacy_cohort != cohort.cohort_id:
                raise ValueError('legacy cohort label does not match the pinned catalog')
            if captured != timefields.stored(cohort.snapshot_captured_at):
                raise ValueError('snapshot observation time does not match the pinned catalog')
            source_sha_before = input_manifest.fingerprint(source)
            if source_sha_before != cohort.source_snapshot_sha256:
                raise ValueError('legacy source snapshot hash does not match the pinned catalog')
            snapshots.validate_verified_stores(stores)

            pinned_paths = {Path(item['path']) for item in receipt['files']}
            if any(destination_path == path or (path.is_dir() and destination_path.is_relative_to(path))
                   for path in pinned_paths):
                raise ValueError('destination cannot change a pinned input')
            fd = os.open(destination_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            os.close(fd)
            destination = sqlite3.connect(destination_path)
            try:
                from catalyst_data.storage.connect import connect_readonly
                with closing(connect_readonly(stores.stores[source_store_id].path, immutable=False,
                                              timeout_ms=10_000)) as src:
                    with destination:
                        migration_result = legacy_import.migrate_legacy_chunks(
                            src, destination, cohort=cohort)
                snapshots.validate_verified_stores(stores)
                source_sha_after = input_manifest.fingerprint(source)
                if source_sha_after != source_sha_before:
                    raise ValueError('legacy source changed during import')
                integrity = destination.execute('PRAGMA integrity_check').fetchone()
                if integrity != ('ok',):
                    raise ValueError('destination integrity check failed')
                counts = {
                    table: destination.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
                    for table in ('body_versions', 'legacy_version_metadata',
                                  'temporal_evidence_map', 'evidence_text_projection')
                }
                result = {
                    **migration_result,
                    'integrity_check': integrity[0],
                    'counts': counts,
                    'source_sha256_before': source_sha_before,
                    'source_sha256_after': source_sha_after,
                }
            finally:
                destination.close()
        else:
            destination = None
            if any(destination_path == Path(item['path']) or
                   (Path(item['path']).is_dir() and destination_path.is_relative_to(Path(item['path'])))
                   for item in receipt['files']):
                raise ValueError('destination cannot change a pinned input')
            fd = os.open(destination_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            os.close(fd)
            destination = sqlite3.connect(destination_path)
            try:
                with sqlite3.connect(source.as_uri() + '?mode=ro&immutable=1', uri=True) as src:
                    with destination:
                        result = migration.migrate_alpaca(src, destination, observed_at=captured)
            finally:
                destination.close()
        # Detect inputs being changed during the operation without emitting a
        # second receipt. Inputs still need operational isolation while running.
        input_manifest.verify(os.environ['CATALYST_INPUT_MANIFEST'],invocation=invocation,
                              required_paths=required,required_env=ENVIRONMENT)
        print(json.dumps({'status':'OK','kind':args.kind,'snapshot_observed_at':captured.isoformat(),
            'legacy_cohort':args.legacy_cohort,'input_manifest_sha256':receipt['manifest_sha256'],
            'result':result}))
        return 0
    except Exception as exc:
        print(json.dumps({'status': 'ERROR', 'error': str(exc) if isinstance(exc, input_manifest.PreflightError)
                          else type(exc).__name__}))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
