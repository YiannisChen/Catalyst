> Supporting reference; canonical source: `docs/superpowers/specs/2026-04-27-catalyst-fullversion-strategy.md` (strategy), `docs/full-version-execution-spec.md` (technical), `docs/current_situation.md` (status).

# Is Your Agent Project a Toy? (Rubric + Catalyst Self-Check)

**Source:** Paraphrased engineering hiring heuristics (original notes: informal discussion summarized as a self-audit checklist).  
**Purpose:** Use this as a **structured interview with yourself** before you claim “production-grade agent” on a resume, thesis, or demo deck. Then map the same questions to **Catalyst** honestly.

---

## 1. Toy-project self-audit (answer with evidence, not vibes)

If you **cannot point to a concrete example** for a question below, treat that line as a strike.

| # | Question | If you cannot answer… |
|---|------------|------------------------|
| 1 | When did your agent **last decide not to retrieve** (or to stop retrieving)? | Retrieval is likely **hard-coded** (“user asked → always search”). |
| 2 | Did the agent **remember a user preference from last week** today? | You likely have **no real memory** (only prompt context). |
| 3 | Can you state **how many points** your system beats a named baseline on a **defined metric**? | You likely have **no evaluation harness** (or no baseline). |
| 4 | If you “fold away” mega system prompts, **how many distinct agent roles** remain? | “Multi-agent” may collapse to **one do-everything LLM**. |
| 5 | If the user drops a **screenshot** (or other non-text), does the workflow **break**? | You may have a **text-only** pipeline with no ingestion contract. |
| 6 | When did the plan **change because an intermediate result was wrong**? | The graph may be **linear / scripted**, not conditionally replanned. |
| 7 | Have tool calls **failed**? After failure, did the system **recover without a human rerun**? | Tooling may be **happy-path only** (“harness is empty”). |

**Rule of thumb:** **3+ strikes** → you probably have a **demo**, not an engineering system—until you close the gaps with design + tests + metrics.

---

## 2. Minimum bar before you “pitch engineering” (three non-negotiables)

1. **Retrieval is a decision, not a reflex**  
   The system should decide **whether** to retrieve, **what** to retrieve, and **when evidence is sufficient**—not only “user spoke → search”.

2. **Retrieval strategy is defensible**  
   You can explain **why** your approach (e.g., hybrid retrieval, reranking, routing, GraphRAG, etc.) matches your failure modes. “We used X” without “we chose X because Y fails under Z” is a liability in review.

3. **Tooling has governance**  
   For MCP-like surfaces: **least privilege**, **high-risk action blocks**, **auditable calls**. “We integrated tools” without threat modeling reads as API wiring, not engineering.

---

## 3. Six upgrades (aim to **substantiate at least two** in depth)

These are common “depth” topics in serious agent reviews—pick a few and be ready to explain **mechanics** (data flow, state, failure modes), not labels.

1. **Multimodal inputs** (speech, images, multimodal RAG): how each modality is **normalized**, **stored**, and **grounded** in evaluation.
2. **Tiered memory**: long-term preferences + short-term task state + summarization policy (**what** to store, **when**, **how** recalled).
3. **Planner / executor split** (plus validation): why separation reduces drift and how you verify executor outputs.
4. **Modular skills / plugins**: dynamic loading, contracts, versioning—vs stuffing everything into prompts.
5. **Human-in-the-loop** for risky actions: graded risk policy, pause/resume, audit trail.
6. **Context safety**: prompt injection paths, policy gates, schema validation, redaction—**attack story + mitigation story**.

---

## 4. Four “sharp edge” topics (one deep narrative beats ten shallow features)

1. **Evaluation system**: tool success, retrieval quality, task completion—**with baselines and ablations**.  
2. **Harness hardening**: failures become **tests, schemas, guardrails**, not “edit prompt and rerun”.  
3. **Model routing**: cheap models for routing/checks, expensive models for reasoning—**explicit policy + cost/quality tradeoffs**.  
4. **Operational truth**: retries, idempotency, backoff, observability—what happens **Tuesday morning**, not only in the demo video.

---

## 5. Applying the rubric to **Catalyst** (honest mapping)

Use this table as a **gap tracker** for full-version work. Replace checkmarks with links to commits, tests, or ADRs as evidence.

| Rubric line | Catalyst today (midterm / research baseline) | What “non-toy” evidence would look like |
|-------------|-----------------------------------------------|----------------------------------------|
| Conditional retrieval / sufficiency | Miner-Critic-Judge includes **Critic-driven follow-up** in principle; strict path breadth is still limited per freeze notes | Documented **stop rules** + metrics showing unnecessary retrieval reduction without hurting grounding |
| Memory | **No end-user long-term memory** in the midterm product sense | Explicit **memory tier** design + eval for recall/privacy |
| Metrics vs baseline | **Eval package + harness** exists (e.g., attribution F1, grounding rate, harness tests) | Frozen **golden sets**, published **delta vs baseline** tables tied to CI |
| Multi-agent vs one prompt | **Distinct nodes** (Miner / Critic / Judge) with graph state, not a single chat prompt | Spec + tests proving **state transitions** and **failure handling** per node |
| Multimodal | **Text-centric** pipeline | Ingestion contract + eval for screenshots/PDFs/audio if claimed |
| Replanning | Graph branches / critic loops exist in design; maturity depends on implementation | LangSmith traces showing **plan changes caused by verifier output** |
| Tool failure + recovery | Partially addressed in design deltas (e.g., error classification); full recovery is a **full-version** concern | Retry policies, idempotent tools, user-visible recovery, **tests for failure injections** |

**Canonical docs to anchor claims (do not hand-wave):**

- System spec: `docs/superpowers/specs/2026-04-02-catalyst-system-design.md`  
- Midterm boundary: `docs/superpowers/specs/2026-04-07-catalyst-midterm-freeze-boundary.md`  
- Full-version deltas: `docs/full-version-design-delta.md`  
- Design rationale / bug linkage: `docs/design-review-rationale.md`, `docs/midterm-bug-report.md`  
- Current positioning: `docs/current_situation.md`

**Bottom line:** Catalyst is best described today as a **research-grade, spec-driven agentic RAG system with real evaluation plumbing**—not a toy **if** you defend it with artifacts (tests, metrics, ADRs, traces). It **will** read as a toy **if** you claim product completeness (memory, multimodal, governance, full recovery) without implementing and measuring them.

---

## 6. How to use this file in development

1. Once per milestone, score **Section 1** with links (PR, test name, trace).  
2. For each “strike”, open an ADR or a `docs/full-version-design-delta.md` item—**design first**, then code.  
3. Never delete failing evidence; **convert failures into constraints** (tests + schemas + policies).
