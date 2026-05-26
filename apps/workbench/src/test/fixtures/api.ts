import type { ArtifactResponse } from '../../api/types';

export const retrievedChunksArtifactFixture: ArtifactResponse = {
  run_id: 'run_fixture',
  event_seq: 1,
  node: 'miner',
  artifact_type: 'retrieved_chunks',
  created_at: '2026-05-26T00:00:00Z',
  payload: {
    title: 'Company posts stronger-than-expected quarterly revenue',
    source: 'example-wire',
    date: '2026-05-25',
    score: 0.91,
    excerpt: 'Revenue and margin commentary were cited as attribution evidence.',
  },
};

export const errorSnapshotArtifactFixture: ArtifactResponse = {
  run_id: 'run_fixture',
  event_seq: 2,
  node: 'system_error_handler',
  artifact_type: 'error_snapshot',
  created_at: '2026-05-26T00:01:00Z',
  payload: {
    error_type: 'RuntimeDependencyError',
    message: 'Reranker service was unavailable during attribution.',
  },
};
