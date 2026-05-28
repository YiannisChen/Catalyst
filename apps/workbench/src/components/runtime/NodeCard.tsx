interface Props {
  node: string;
  latencyMs: number;
  inputTokens: number;
  outputTokens: number;
  costUsd: number;
}

export default function NodeCard({
  node,
  latencyMs,
  inputTokens,
  outputTokens,
  costUsd,
}: Props) {
  return (
    <div className="node-card">
      <div className="node-card-header">{node}</div>
      <div className="node-card-metrics">
        <span className="node-metric">
          Latency <strong>{latencyMs}ms</strong>
        </span>
        <span className="node-metric">
          In <strong>{inputTokens}</strong>
        </span>
        <span className="node-metric">
          Out <strong>{outputTokens}</strong>
        </span>
        <span className="node-metric">
          Cost <strong>${costUsd.toFixed(4)}</strong>
        </span>
      </div>
    </div>
  );
}
