> Supporting reference; canonical source: `docs/superpowers/specs/2026-04-27-catalyst-fullversion-strategy.md` (strategy), `docs/full-version-execution-spec.md` (technical), `docs/current_situation.md` (status).

# Philosophy of Teacher Mac

> Core mandate: elevate software projects from "toys" to "enterprise-grade engineering." Never build things just to build them — engineer systems that solve concrete business problems with rigorous, quantifiable proof.

---

## 1. Technical Decision Chain (技术决策链)

Never use technology for the sake of checking off buzzwords. Every architectural choice — async patterns, caching, queues, databases — must answer one question: *"What specific business pain point does this solve?"*

If you cannot articulate the business justification for a technology choice, remove it. Interviewers and technical reviewers will immediately detect hollow buzzword stacking.

## 2. Anti-Marketing Metrics (反营销指标)

Hollow claims like "accuracy improved by 10%" with no context are a red flag. Every metric must be grounded in:

- **When** it was measured (time, dataset version)
- **What business outcome** it affects downstream
- **How** it was computed (reproducible methodology)

A number without a business context, a time frame, and a computation method is marketing, not engineering.

## 3. Evaluation is Non-Negotiable

**An AI project without an Evaluation Module is a toy, not an engineering project.**

You must have a formal framework to measure hallucination rates, response quality, and structural integrity. Evaluation is what separates a demo from a system that an enterprise would trust with real decisions and real money.

## 4. Systematic Guardrails and Observability (护栏与监控)

Enterprise systems require strict limitations to prevent catastrophic errors. Your architecture must answer:

- **Monitoring:** What exactly is being monitored? What data do the logs produce? What actionable decisions come from analyzing them?
- **Guardrails:** What hard limits prevent the system from producing dangerous or incorrect outputs? Rate limiting, timezone alignment, fallback mechanisms, input validation — each must be tied to a specific failure mode it prevents.

If you add monitoring or guardrails but cannot explain what failure they prevent, they are decoration.

## 5. Asset Compound Interest (资产复利)

The most important career principle: **never start from scratch.** Extract highly decoupled, reusable modules from every project and package them as independent open-source libraries.

- Each reusable module is a **personal industrial-grade IP asset**
- Assets compound — your next project builds on top of the last, not beside it
- A portfolio of published, production-quality libraries is a stronger career endorsement than a mediocre internship
- Proving you can independently architect enterprise-grade open-source systems is the "correct but difficult path"

## 6. High Completion Over Quick Demos (完成度 > 速度)

The goal is not a working prototype. The goal is a system that is **highly complete, usable, and reusable**. A polished, finished project speaks for itself. Interviewers can tell the difference between "I hacked this together" and "I engineered this to be used."

## 7. The Four Pillars of AI Applications

Every AI product in the industry is a combination of four modules:

1. **Application Orchestration** — how components are composed and routed
2. **Context Management** — knowledge bases, RAG, retrieval infrastructure
3. **Agents** — autonomous or semi-autonomous task execution
4. **Infrastructure** — evaluation, guardrails, monitoring, deployment

Building one strong, complete project in each pillar — with real business context — covers nearly any AI engineering role.

---

## Implications for Our Project

These principles translate into concrete requirements:

| Principle | What it demands |
|---|---|
| Technical Decision Chain | Every choice (SQLite over Postgres, Semaphore over sleep, Markdown over raw JSON) must have a documented business justification in the codebase — not just in conversation. |
| Anti-Marketing Metrics | Metrics like `noise_reduction_ratio`, `grounding_rate`, and `attribution_f1` must include target ranges, computation methods, and explanations of what downstream outcome they protect. |
| Evaluation is Non-Negotiable | The eval suite (`packages/eval`) must be scaffolded *before* the agent engine. Golden set first, scoring second, agents third. |
| Guardrails and Observability | Rate limiting, timezone alignment, fallback nodes, and the "never fabricate causes" policy are not nice-to-haves — they are the guardrails that make this an enterprise project. Each must be tested. |
| Asset Compound Interest | `packages/data-core` must be publishable as a standalone open-source package. If it cannot be installed and used independently of the rest of Catalyst, it is not yet a reusable asset. |
| High Completion | A partial system with all three pillars half-built is worse than one pillar fully complete, tested, documented, and published. Finish data-core completely before expanding. |
| Four Pillars | Catalyst covers orchestration (LangGraph workflow), context management (LanceDB RAG), and agents (attribution nodes). The fourth pillar — infrastructure (eval, CI gates, monitoring) — must not be an afterthought. |
