import { useState } from 'react';
import { retryLiveRun } from '../../api/client';
import type { RunSummaryResponse } from '../../api/types';

interface Props {
  summary: RunSummaryResponse | null;
  onRetry?: (newRunId: string | null) => void;
}

export default function AttributionSummary({ summary, onRetry }: Props) {
  const [retrying, setRetrying] = useState(false);
  const [retryError, setRetryError] = useState<string | null>(null);

  const handleRetry = async () => {
    if (!summary) return;
    setRetrying(true);
    setRetryError(null);
    try {
      const result = await retryLiveRun(summary.run_id, {});
      if (result.ok && result.run_id) {
        onRetry?.(result.run_id);
      } else {
        setRetryError(result.failure?.message || 'Retry failed — unknown error');
      }
    } catch (err: any) {
      setRetryError(err.message || 'Network error during retry');
    } finally {
      setRetrying(false);
    }
  };

  if (!summary) {
    return (
      <div className="attribution-summary">
        <div className="summary-empty">No run available</div>
      </div>
    );
  }

  const isFailed = summary.status === 'FAILED_SYSTEM' || summary.status === 'FAILED_REQUEST';
  const isSuccess = summary.status === 'SUCCEEDED';
  const isPartial = summary.status === 'PARTIAL' || summary.status === 'INSUFFICIENT';

  return (
    <div className="attribution-summary">
      <div className="summary-header">
        <h3>Run Summary</h3>
        <span className={`summary-status ${summary.status.toLowerCase()}`}>
          {summary.status.replace('_', ' ')}
        </span>
      </div>

      <div className="summary-details">
        {summary.model_id && (
          <div className="summary-row">
            <span className="row-label">Model</span>
            <span className="row-value">{summary.model_id}</span>
          </div>
        )}
        {summary.last_completed_node && (
          <div className="summary-row">
            <span className="row-label">Last Node</span>
            <span className="row-value">{summary.last_completed_node}</span>
          </div>
        )}
        {summary.retry && summary.retry.retry_count > 0 && (
          <div className="summary-row">
            <span className="row-label">Retries</span>
            <span className="row-value">{summary.retry.retry_count}</span>
          </div>
        )}
      </div>

      {/* Failure details */}
      {isFailed && summary.failure && (
        <div className="summary-failure">
          <div className="failure-title">
            {summary.failure.sub_reason === 'timeout' ? 'Timeout' : 'Error'}
          </div>
          {summary.failure.message && (
            <p className="failure-msg">{summary.failure.message}</p>
          )}
          {summary.failure.node && (
            <p className="failure-reason">
              Failed at node: <strong>{summary.failure.node}</strong>
            </p>
          )}
        </div>
      )}

      {/* Success indicator */}
      {isSuccess && (
        <div className="summary-success">
          Attribution analysis completed successfully.
        </div>
      )}

      {/* Partial warning */}
      {isPartial && (
        <div className="summary-partial">
          Analysis completed with limited evidence. Results may be less reliable.
        </div>
      )}

      {/* Retry error message */}
      {retryError && (
        <div className="retry-error">{retryError}</div>
      )}

      {/* Retry button — only for failed + retryable runs */}
      {isFailed && (
        <button
          className="summary-retry-button"
          onClick={handleRetry}
          disabled={retrying}
        >
          {retrying ? 'Retrying...' : 'Retry Attribution'}
        </button>
      )}
    </div>
  );
}
