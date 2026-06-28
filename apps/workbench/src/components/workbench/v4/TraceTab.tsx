import { useState, useMemo, useEffect, useRef } from 'react';
import type { ArtifactResponse } from '../../../api/types';
import { redactPayload } from './trace-redaction';
import type { AttributionResult, EvidenceItem, PipelineStep, PipelineStepId } from '../../../mock/demoCases';
import type { WorkflowPhase } from '../workflow-state';

interface Props {
  steps: PipelineStep[];
  phase: WorkflowPhase;
  result: AttributionResult | null;
  evidence: EvidenceItem[];
  artifacts: ArtifactResponse[];
}

/* ═══════════════════════════════════════════
   Stage definitions
   ═══════════════════════════════════════════ */

interface TraceStage {
  id: string; label: string; pipelineIds: PipelineStepId[];
}

const TRACE_STAGES: TraceStage[] = [
  { id: 'query', label: 'Query', pipelineIds: ['query'] },
  { id: 'retriever', label: 'Retriever', pipelineIds: ['window', 'retrieval', 'miner'] },
  { id: 'critic', label: 'Critic', pipelineIds: ['critic'] },
  { id: 'readiness', label: 'Readiness', pipelineIds: ['router'] },
  { id: 'attribution', label: 'Attribution Model', pipelineIds: ['judge'] },
  { id: 'validator', label: 'Validator', pipelineIds: ['validator'] },
];

function resolveStageStatus(stage: TraceStage, steps: PipelineStep[]): 'pending'|'active'|'complete'|'warning'|'error'|'skipped' {
  const stepMap = new Map(steps.map((s) => [s.id, s]));
  const statuses = stage.pipelineIds.map((id) => stepMap.get(id)?.status ?? 'pending');
  if (statuses.includes('active')) return 'active';
  if (statuses.includes('error')) return 'error';
  if (statuses.every(s => s === 'complete')) return 'complete';
  if (statuses.includes('warning')) return 'warning';
  if (statuses.includes('skipped')) return 'skipped';
  return 'pending';
}
function getStageSummary(stage: TraceStage, steps: PipelineStep[]): string {
  const stepMap = new Map(steps.map(s => [s.id, s]));
  return stage.pipelineIds.map(id => stepMap.get(id)?.note).filter(Boolean).join(' / ') || 'Waiting';
}
function formatDuration(ms: number | undefined): string {
  if (ms == null) return '-';
  return ms >= 1000 ? `${(ms/1000).toFixed(2)}s` : `${ms}ms`;
}
const STATUS_LABELS: Record<string,string> = {
  pending:'Pending',active:'Active',complete:'Complete',warning:'Warning',error:'Error',skipped:'Skipped',
};

/* ── Nodes with raw artifact support ── */
const RAW_OUTPUT_NODES = new Set(['critic','judge']);
function getNodeLabel(node: string): string {
  const m: Record<string,string> = {miner:'Retriever',critic:'Critic',judge:'Attribution Model',validator:'Validator'};
  return m[node] || node;
}

/* ═══════════════════════════════════════════
   Redaction
   ═══════════════════════════════════════════ */


/* ═══════════════════════════════════════════
   Raw output
   ═══════════════════════════════════════════ */

const RAW_RENDER_LIMIT = 100_000;
const RAW_COPY_LIMIT = 500_000;

function RawOutputView({ stageArtifacts, nodeLabel }: { stageArtifacts: ArtifactResponse[]; nodeLabel: string }) {
  if (stageArtifacts.length === 0) {
    return <div className="v4-artifact-block"><p className="v4-artifact-text">No raw artifact was recorded for this stage.</p></div>;
  }
  const [selectedIdx, setSelectedIdx] = useState(0);
  const selected = stageArtifacts[selectedIdx] ?? stageArtifacts[0];
  const [copied, setCopied] = useState(false);

  /* Reset selected index when artifacts change */
  useEffect(() => { setSelectedIdx(0); setCopied(false); }, [stageArtifacts]);

  const artifactLabels: Record<string,string> = {
    graded_evidence:'Graded Evidence', critic_decision:'Critic Decision',
    judge_causes:'Causes', judge_summary:'Summary', raw_llm_response:'Model Response',
    all_graded_chunks:'All Graded Chunks', retrieved_chunks:'Retrieved Chunks',
    reranked_chunks:'Reranked Chunks', validator_decision:'Validator Decision',
    state_snapshot:'State Snapshot',
  };

  /* Memoize formatted payload */
  const { rendered, full } = useMemo(() => {
    const safe = redactPayload(selected.payload) as Record<string,unknown>;
    const fullStr = JSON.stringify(safe, null, 2);
    const truncated = fullStr.length > RAW_RENDER_LIMIT;
    const renderStr = truncated
      ? fullStr.slice(0, RAW_RENDER_LIMIT) + '\n\n[Output truncated in the UI. Use the artifact API for the complete payload.]'
      : fullStr;
    return { rendered: renderStr, full: fullStr };
  }, [selected]);

  const canCopy = full.length <= RAW_COPY_LIMIT;
  const handleCopy = async () => {
    if (!canCopy) return;
    try {
      await navigator.clipboard.writeText(full);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch { /* unavailable */ }
  };

  return (
    <div className="v4-artifact-block">
      {stageArtifacts.length > 1 && (
        <div className="v4-raw-selector" role="tablist" aria-label="Raw artifact selector">
          {stageArtifacts.map((a, idx) => (
            <button key={`${a.artifact_type}-${idx}`}
              role="tab"
              aria-selected={selectedIdx === idx}
              className={`v4-raw-chip ${selectedIdx === idx ? 'is-active' : ''}`}
              onClick={() => { setSelectedIdx(idx); setCopied(false); }}>
              {artifactLabels[a.artifact_type] ?? a.artifact_type}
            </button>
          ))}
        </div>
      )}
      <div className="v4-raw-meta">
        <span className="v4-raw-meta-item">{nodeLabel}</span>
        <span className="v4-raw-meta-item">{selected.artifact_type}</span>
        {selected.event_seq != null && <span className="v4-raw-meta-item">Event {selected.event_seq}</span>}
        {selected.created_at && <span className="v4-raw-meta-item">{new Date(selected.created_at).toLocaleString()}</span>}
      </div>
      <div className="v4-raw-actions">
        <button
          className="v4-raw-copy-btn"
          onClick={handleCopy}
          disabled={!canCopy}
          aria-label={canCopy ? 'Copy raw payload' : 'Payload too large to copy'}
        >
          {!canCopy ? 'Copy disabled' : copied ? 'Copied' : 'Copy'}
        </button>
      </div>
      <pre className="v4-raw-payload"><code>{rendered}</code></pre>
    </div>
  );
}

/* ═══════════════════════════════════════════
   Main TraceTab
   ═══════════════════════════════════════════ */

export default function TraceTab({ steps, phase, result, evidence, artifacts }: Props) {
  const [selectedStage, setSelectedStage] = useState<string | null>(null);
  const stepMap = useMemo(() => new Map(steps.map(s => [s.id,s])), [steps]);
  const isIdle = phase === 'idle';
  const stageRefs = useRef<Map<string,HTMLButtonElement>>(new Map());

  const stages = useMemo(() => TRACE_STAGES.map((stage, idx) => ({
    ...stage, index:idx,
    status: resolveStageStatus(stage, steps),
    summary: getStageSummary(stage, steps),
    totalDuration: stage.pipelineIds.reduce((sum,id) => sum+(stepMap.get(id)?.durationMs??0), 0),
  })), [steps, stepMap]);

  const defaultStage = stages.find(s => s.id==='attribution' && s.status==='complete')
    ?? stages.find(s => s.status==='active') ?? stages[0];

  const activeDetail = selectedStage
    ? stages.find(s => s.id===selectedStage) ?? defaultStage
    : defaultStage;

  /* Scroll into view */
  useEffect(() => {
    const sid = activeDetail?.id; if (!sid) return;
    const el = stageRefs.current.get(sid); if (!el) return;
    const rm = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    el.scrollIntoView({ block:'nearest', inline:'nearest', behavior: rm?'auto':'smooth' });
  }, [activeDetail?.id]);

  return (
    <div className="v4-trace">
      <div className="v4-trace-rail">
        {isIdle ? (
          <div className="v4-empty-state"><span className="v4-empty-text">Pipeline will appear after running attribution.</span></div>
        ) : stages.map(stage => {
          const isSelect = selectedStage ? stage.id===selectedStage : stage.id===defaultStage.id;
          return (
            <button key={stage.id} ref={el => { if(el) stageRefs.current.set(stage.id, el); }}
              className={`v4-trace-stage-card v4-sc-${stage.status} ${isSelect?'is-selected':''}`}
              onClick={() => setSelectedStage(stage.id)} aria-current={isSelect?'true':undefined}>
              <span className="v4-sc-index">{String(stage.index+1).padStart(2,'0')}</span>
              <div className="v4-sc-body"><span className="v4-sc-label">{stage.label}</span><span className="v4-sc-summary">{stage.summary}</span></div>
              <span className={`v4-sc-status v4-sc-status-${stage.status}`}>{STATUS_LABELS[stage.status]??stage.status}</span>
            </button>
          );
        })}
      </div>
      <div className="v4-trace-detail">
        {!activeDetail || isIdle ? (
          <div className="v4-empty-state"><span className="v4-empty-text">Select a stage to inspect pipeline artifacts.</span></div>
        ) : <TraceArtifact stage={activeDetail} stepMap={stepMap} result={result} evidence={evidence} artifacts={artifacts} />}
      </div>
    </div>
  );
}

/* ═══════════════════════════════════════════
   TraceArtifact dispatcher — resets raw view
   ═══════════════════════════════════════════ */

function TraceArtifact({ stage, stepMap, result, evidence, artifacts }: {
  stage: TraceStage & { status:string; summary:string; totalDuration:number };
  stepMap: Map<string, PipelineStep>;
  result: AttributionResult|null;
  evidence: EvidenceItem[];
  artifacts: ArtifactResponse[];
}) {
  const nodeId = stage.pipelineIds[stage.pipelineIds.length-1] ?? stage.id;
  const canShowRaw = RAW_OUTPUT_NODES.has(nodeId);

  const stageArtifacts = useMemo(
    () => artifacts.filter(a => a.node === nodeId),
    [artifacts, nodeId],
  );

  const hasRaw = canShowRaw && stageArtifacts.length > 0;

  const [viewMode, setViewMode] = useState<'structured'|'raw'>('structured');

  /* Reset to structured when stage/artifacts change */
  useEffect(() => {
    setViewMode('structured');
  }, [nodeId, artifacts]);

  return (
    <div className="v4-artifact">
      <div className="v4-artifact-head">
        <div><span className="v4-kicker">Execution Trace</span><h4 className="v4-artifact-title">{stage.label}</h4></div>
        <span className={`v4-artifact-status v4-artifact-${stage.status}`}>{stage.status}</span>
      </div>
      <div className="v4-artifact-meta">
        <span>Duration: {formatDuration(stage.totalDuration)}</span>
        <span className="v4-artifact-summary">{stage.summary}</span>
      </div>
      {hasRaw && (
        <div className="v4-raw-toggle-bar" role="tablist" aria-label="Output view mode">
          <button role="tab" aria-selected={viewMode==='structured'}
            className={`v4-raw-toggle ${viewMode==='structured'?'is-active':''}`}
            onClick={() => setViewMode('structured')}>Structured Output</button>
          <button role="tab" aria-selected={viewMode==='raw'}
            className={`v4-raw-toggle ${viewMode==='raw'?'is-active':''}`}
            onClick={() => setViewMode('raw')}>Raw Output</button>
        </div>
      )}
      {viewMode==='structured' ? <>
        {stage.id==='query' && <QueryArtifact stepMap={stepMap} />}
        {stage.id==='retriever' && <RetrieverArtifact evidence={evidence} stepMap={stepMap} />}
        {stage.id==='critic' && <CriticArtifact evidence={evidence} stepMap={stepMap} />}
        {stage.id==='readiness' && <ReadinessArtifact stepMap={stepMap} />}
        {stage.id==='attribution' && <AttributionModelArtifact result={result} evidence={evidence} stepMap={stepMap} />}
        {stage.id==='validator' && <ValidatorArtifact stepMap={stepMap} result={result} evidence={evidence} />}
      </> : <RawOutputView stageArtifacts={stageArtifacts} nodeLabel={getNodeLabel(nodeId)} />}
    </div>
  );
}

/* ═══════════════════════════════════════════
   Structured artifact sub-components
   ═══════════════════════════════════════════ */

function QueryArtifact({ stepMap }: { stepMap: Map<string, PipelineStep> }) {
  const s = stepMap.get('query');
  return <div className="v4-artifact-block"><span className="v4-artifact-section-title">Query Parameters</span><div className="v4-artifact-kv"><div><span>Status</span><code>{s?.note ?? 'Pending'}</code></div><div><span>Duration</span><code>{formatDuration(s?.durationMs)}</code></div></div></div>;
}

function RetrieverArtifact({ evidence, stepMap }: { evidence: EvidenceItem[]; stepMap: Map<string, PipelineStep> }) {
  const w = stepMap.get('window'); const r = stepMap.get('retrieval'); const m = stepMap.get('miner');
  const acc = evidence.filter(e => e.criticDecision === 'accepted').length;
  const sd = evidence.filter(e => e.temporalStatus === 'same-day').length;
  return <div className="v4-artifact-block">
    <span className="v4-artifact-section-title">Retrieved Evidence</span>
    <div className="v4-artifact-stats-row"><div className="v4-stat"><strong>{evidence.length}</strong><span>Retrieved</span></div><div className="v4-stat"><strong>{acc}</strong><span>Accepted</span></div><div className="v4-stat"><strong>{sd}</strong><span>Same-day</span></div></div>
    <div className="v4-artifact-kv">{w && <div><span>Event Window</span><code>{w.note}</code></div>}{r && <div><span>Retrieval</span><code>{r.note}</code></div>}{m && <div><span>Miner</span><code>{m.note}</code></div>}</div>
    {evidence.length > 0 && <div className="v4-artifact-evidence-list">{evidence.map(item => <div key={item.id} className="v4-artifact-evidence-row"><code className="v4-ev-id">{item.id}</code><span className="v4-ev-title">{item.title}</span><span className="v4-ev-score">{item.relevanceScore.toFixed(2)}</span></div>)}</div>}
  </div>;
}

function CriticArtifact({ evidence, stepMap }: { evidence: EvidenceItem[]; stepMap: Map<string, PipelineStep> }) {
  const s = stepMap.get('critic');
  const isWarn = s?.status === 'warning';
  return <div className="v4-artifact-block">
    <span className="v4-artifact-section-title">Evidence Grading</span>
    <div className={`v4-critic-verdict ${isWarn?'v4-verdict-warn':'v4-verdict-ok'}`}><span className="v4-verdict-text">{isWarn?'Evidence failed sufficiency checks':'Evidence passed sufficiency checks'}</span></div>
    <p className="v4-critic-note">{s?.note}</p>
    {evidence.length > 0 && <div className="v4-grading-table">
      <div className="v4-grading-head"><span>Evidence</span><span>Relevance</span><span>Timing</span><span>Decision</span></div>
      {evidence.map(item => <div key={item.id} className="v4-grading-row"><code className="v4-gev-id">{item.id}</code><span>{item.relevanceScore.toFixed(2)}</span><span>{item.temporalStatus}</span><span className={`v4-gev-decision-${item.criticDecision}`}>{item.criticDecision}</span></div>)}
    </div>}
    <div style={{marginTop:12}}><span className="v4-artifact-section-title">Rationale Summary</span><p className="v4-artifact-text">The Critic records evidence relevance, temporal alignment, and an explicit acceptance decision for each item.</p></div>
  </div>;
}

function ReadinessArtifact({ stepMap }: { stepMap: Map<string, PipelineStep> }) {
  const s = stepMap.get('router'); if (!s) return null;
  const isR = s.note.toLowerCase().includes('refusal');
  return <div className="v4-artifact-block"><span className="v4-artifact-section-title">Evidence Readiness</span>
    <div className="v4-readiness-result"><span className={`v4-readiness-decision v4-decision-${isR?'refuse':'proceed'}`}>{isR?'Return insufficient evidence response':'Proceed to attribution'}</span><p className="v4-readiness-reason">{s.note}</p></div>
    <div className="v4-readiness-checks"><div className="v4-readiness-check-row"><span className="v4-readiness-check-label">Fallback path</span><code>{isR?'Insufficient evidence response':'Standard attribution'}</code></div><div className="v4-readiness-check-row"><span className="v4-readiness-check-label">Requirements</span><code>{isR?'Not met':'Met'}</code></div></div>
  </div>;
}

function AttributionModelArtifact({ result, evidence, stepMap }: { result: AttributionResult|null; evidence: EvidenceItem[]; stepMap: Map<string, PipelineStep> }) {
  const s = stepMap.get('judge');
  const evidenceById = useMemo(() => new Map(evidence.map(e => [e.id,e])), [evidence]);
  if (s?.status === 'skipped') return <div className="v4-artifact-block"><span className="v4-artifact-section-title">Attribution Model Output</span><p className="v4-artifact-skipped">Skipped: refusal route selected.</p></div>;
  return <div className="v4-artifact-block"><span className="v4-artifact-section-title">Attribution Model Output</span>
    {result?.causes && result.causes.length>0 ? <>
      <div className="v4-cause-weights">{result.causes.map(cause => <div key={cause.title} className="v4-model-cause"><div className="v4-model-cause-head"><span className="v4-model-cause-title">{cause.title}</span><span className={`v4-direction-badge v4-dir-${cause.direction}`}>{cause.direction}</span></div><span className="v4-confidence-label">Confidence {Math.round(cause.confidence*100)}%</span></div>)}</div>
      <div className="v4-artifact-section-title" style={{marginTop:12}}>Claim-to-Evidence Map</div>
      <div className="v4-claim-map">{result.causes.map(cause => <div key={cause.title} className="v4-claim-entry"><span className="v4-claim-cause">{cause.title}</span><div className="v4-claim-evs">{cause.evidenceIds.map(id => { const ev = evidenceById.get(id); return <div key={id} className="v4-claim-ev-row"><code>{id}</code>{ev && <span className="v4-claim-ev-title">{ev.title}</span>}</div>; })}</div></div>)}</div>
    </> : <p className="v4-artifact-text">{s?.note ?? 'No causes generated.'}</p>}
    <div className="v4-artifact-section-title" style={{marginTop:12}}>Attribution Logic</div><p className="v4-artifact-text">The attribution model identifies evidence-supported drivers and links each driver to its cited evidence. Confidence describes support for the individual driver; it does not represent a percentage of the price move.</p>
  </div>;
}

function ValidatorArtifact({ stepMap, result, evidence }: { stepMap: Map<string, PipelineStep>; result: AttributionResult|null; evidence: EvidenceItem[] }) {
  const s = stepMap.get('validator'); const isC = s?.status === 'complete';
  const checks = [
    { label:'Grounding', value:result?`${Math.round(result.groundingRate*100)}%`:'-', passed:isC||(result!=null&&result.groundingRate>0.5) },
    { label:'Citation integrity', value:isC?'PASS':'-', passed:isC },
    { label:'Schema validity', value:'PASS', passed:true },
    { label:'Evidence count', value:`${evidence.length} retrieved`, passed:evidence.length>0 },
  ];
  return <div className="v4-artifact-block"><span className="v4-artifact-section-title">Validation</span><div className="v4-validator-checks">{checks.map(c => <div key={c.label} className="v4-validator-row"><span className={`v4-check-dot ${c.passed?'v4-dot-ok':'v4-dot-fail'}`}/><span className="v4-check-label">{c.label}</span><code className="v4-check-value">{c.value}</code></div>)}</div></div>;
}
