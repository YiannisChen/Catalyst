import type { ArtifactResponse } from '../../api/types';

interface Props {
  artifacts: ArtifactResponse[];
}

export default function RawResponseViewer({ artifacts }: Props) {
  return (
    <div className="raw-response-viewer">
      {artifacts.length === 0 ? (
        <div className="viewer-empty">No raw responses available</div>
      ) : (
        <div className="viewer-list">
          {artifacts.map((artifact) => (
            <div key={`${artifact.node}-${artifact.artifact_type}`} className="viewer-item">
              <div className="viewer-item-header">
                <span className="artifact-node">{artifact.node}</span>
                <span className="artifact-type">{artifact.artifact_type}</span>
              </div>
              <pre className="viewer-content">
                {JSON.stringify(artifact.payload, null, 2)}
              </pre>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
