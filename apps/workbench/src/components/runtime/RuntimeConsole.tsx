import NodeTimeline from './NodeTimeline';
import type { RunSummaryResponse, RunEventResponse } from '../../api/types';

interface Props {
  summary: RunSummaryResponse | null;
  events: RunEventResponse[];
}

export default function RuntimeConsole({ summary, events }: Props) {
  return (
    <div className="runtime-console">
      <div className="runtime-header">
        <h3>Execution Timeline</h3>
        {summary && (
          <span className={`runtime-status ${summary.status.toLowerCase()}`}>
            {summary.status}
          </span>
        )}
      </div>

      <div className="runtime-content">
        <NodeTimeline events={events} summary={summary} />
      </div>
    </div>
  );
}
