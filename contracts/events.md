# Public run events

`GET /api/live-runs/{run_id}/stream` returns `text/event-stream`. Each persisted
frame uses `id: {run_id}:{sequence}`, `event: {event_type}`, and a JSON
`PublicRunEvent` envelope in `data`. Sequence numbers increase monotonically;
`Last-Event-ID: {run_id}:{sequence}` resumes after that event. Heartbeats are
SSE comments and are not persisted events.

`events.schema.json` contains the JSON Schema for each event's payload, keyed
by event name. The public names and payload schemas are:

| Event | Payload model |
| --- | --- |
| `run.accepted` | `RunAcceptedPayload` |
| `stage.started` | `StageStartedPayload` |
| `evidence.retrieved` | `EvidenceRetrievedPayload` |
| `evidence.assessed` | `EvidenceAssessedPayload` |
| `followup.started` | `FollowUpStartedPayload` |
| `answer.started` | `AnswerStartedPayload` |
| `answer.delta` | `AnswerDeltaPayload` (compatibility stream only) |
| `answer.completed` | `AnswerCompletedPayload` |
| `assurance.completed` | `AssuranceCompletedPayload` |
| `run.completed` | `RunCompletedPayload` |
| `run.failed` | `RunFailedPayload` |
| `run.cancelled` | `RunCancelledPayload` |
| `facts.ready` | `FactsReadyPayload` |
| `tier.decided` | `TierDecidedPayload` |
| `retrieval.done` | `RetrievalDonePayload` |
| `analyst.decision` | `AnalystDecisionPayload` |
| `analyst.done` | `AnalystDonePayload` |
| `checks.done` | `ChecksDonePayload` |
| `gates.done` | `GatesDonePayload` |
| `verifier.done` | `VerifierDonePayload` |
| `answer.ready` | `AnswerReadyPayload` |

Every payload is validated against the event-specific closed Pydantic model
before persistence and again when replayed. The generated schema file is
checked against these models by `packages/api/tests/test_frontend_contract_artifacts.py`.
