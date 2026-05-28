import { useEffect, useRef, useState } from 'react';
import { getModels } from '../../api/client';
import type { ModelOption } from '../../api/types';

interface Props {
  selectedModel: string | null;
  onSelect: (modelId: string) => void;
}

export default function ModelSelector({ selectedModel, onSelect }: Props) {
  const [showPanel, setShowPanel] = useState(false);
  const [models, setModels] = useState<ModelOption[]>([]);
  const [loading, setLoading] = useState(false);
  const panelRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    setLoading(true);
    getModels()
      .then((res) => setModels(res.models ?? []))
      .catch(() => setModels([]))
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => {
    function handleClickOutside(e: MouseEvent) {
      if (panelRef.current && !panelRef.current.contains(e.target as Node)) {
        setShowPanel(false);
      }
    }
    document.addEventListener('mousedown', handleClickOutside);
    return () => document.removeEventListener('mousedown', handleClickOutside);
  }, []);

  function handleSelectModel(modelId: string) {
    setShowPanel(false);
    onSelect(modelId);
  }

  const selectedModelObj = models.find((m) => m.model_id === selectedModel);
  const displayLabel = selectedModelObj?.model_id || '---';

  return (
    <div className="model-selector">
      <div className="model-dropdown-wrapper" ref={panelRef}>
        <button
          className="model-current"
          onClick={() => setShowPanel((v) => !v)}
          disabled={loading || models.length === 0}
        >
          <span className="model-current-label">{displayLabel}</span>
          <span className={`model-arrow ${showPanel ? 'open' : ''}`}>&#9662;</span>
        </button>

        {showPanel && !loading && (
          <div className="model-panel">
            {models.length === 0 ? (
              <div className="model-panel-empty">No models available</div>
            ) : (
              <div className="model-panel-items">
                {models.map((model) => (
                  <button
                    key={model.model_id}
                    className={`model-panel-item ${
                      model.model_id === selectedModel ? 'active' : ''
                    }`}
                    onClick={() => handleSelectModel(model.model_id)}
                    title={model.label}
                  >
                    <span className="model-name">{model.label}</span>
                  </button>
                ))}
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
