# Catalyst Post-V1.1 Market-Causality Ontology Direction

**Status:** Deferred design direction; not part of the Frozen V1.1 architecture or the M7 exit gate.

**Decision date:** 2026-08-29

## Purpose

Record the intended evolution from coarse evidence-bounded cause labels to a
market-causality representation that separates where a cause occurs, how it
acts, what role it plays in a causal sequence, and how strongly the available
evidence supports it.

This document does not amend the current M1-M8 implementation plans, change
Q-011, authorize production-schema changes, add a market-structure backend, or
claim that currently unavailable data exists. V1.1 remains fail-closed when a
mechanism cannot be supported from point-in-time evidence.

## Design principle

> Identify the event that initiated repricing, distinguish it from mechanisms
> that amplified the move, preserve competing explanations, and calibrate the
> conclusion to the strength of available evidence.

Do not encode domain, mechanism, causal role, and epistemic status in one enum.
They are orthogonal dimensions.

## Orthogonal dimensions

### Causal domain

- `COMPANY`
- `INDUSTRY`
- `MACRO_POLICY`
- `MARKET_STRUCTURE`
- `FLOW_POSITIONING`
- `INFORMATION_SENTIMENT`

### Mechanism type

- `EARNINGS_GUIDANCE`
- `ORDER_PRODUCT_CUSTOMER`
- `REGULATION_TARIFF_SANCTION`
- `RATE_REPRICING`
- `PEER_READTHROUGH`
- `SHORT_COVERING`
- `DEALER_GAMMA`
- `LEVERAGE_UNWIND`
- `PASSIVE_REBALANCE`
- `RUMOR_ANALYST_ACTION`
- `UNIDENTIFIED`

The list is an initial bounded vocabulary, not a commitment that every
mechanism has a V1.1 data backend.

### Causal role

- `TRIGGER`
- `AMPLIFIER`
- `ENABLING_CONDITION`
- `COUNTERVAILING_FACTOR`

### Evidence status

- `SUFFICIENT`
- `PARTIAL`
- `ABSTAIN`

Evidence status remains an epistemic result and must never be substituted for
a causal domain, mechanism, or role.

## Candidate component shape

```json
{
  "causal_domain": "COMPANY",
  "mechanism_type": "EARNINGS_GUIDANCE",
  "causal_role": "TRIGGER",
  "statement": "Raised guidance initiated the repricing.",
  "direction_contribution": "positive",
  "causal_scope": "direction_and_partial_magnitude",
  "support_evidence_ids": ["..."],
  "counter_evidence_ids": ["..."],
  "magnitude_fit": "PLAUSIBLE",
  "support_grade": "ESTABLISHED"
}
```

A market-structure component may be recorded as plausible only when its
missing evidence and capability boundary are explicit:

```json
{
  "causal_domain": "MARKET_STRUCTURE",
  "mechanism_type": "SHORT_COVERING",
  "causal_role": "AMPLIFIER",
  "statement": "Short covering may have amplified the move.",
  "support_grade": "PLAUSIBLE",
  "missing_evidence": [
    "point-in-time short-interest change",
    "intraday covering data"
  ]
}
```

`PRIMARY`, `SECONDARY`, and `CONTEXT` may remain publication roles in a future
ClaimPlan, but must not substitute for causal roles.

## Safety and evidence constraints

- Separate observed price path from causal interpretation.
- Require point-in-time temporal ordering between information and the price
  segment it is claimed to explain.
- Distinguish explaining direction from explaining magnitude.
- Do not quantify percentage causal contribution without a separately
  validated methodology.
- Treat technical patterns as observations or enabling context, not standalone
  causal proof.
- Do not promote a plausible amplifier to a proven primary trigger.
- Missing options, positioning, flow, liquidation, or intraday data remains a
  typed capability gap; it never authorizes model invention.

## Deferred implementation boundary

Any future implementation requires a formal post-V1.1 design amendment that
reconciles AnalystDecision, deterministic normalization, ClaimPlan,
ClaimValidator, WriterInput, human gold, metrics, and data capabilities. It
must preserve the current evidence-ID binding, temporal eligibility,
materiality, independence, contradiction, status-ceiling, and fail-closed
contracts. It must not add another reasoning agent or silently increase the
model-call budget.

Market-structure and multi-stage intraday attribution additionally require
point-in-time source coverage before they can become scorable production
capabilities.

## Current-series decision

Complete the existing M1-M8 V1.1 series under its frozen architecture. During
M7, unsupported mechanisms may be evaluated only as coverage/calibration
boundaries; this deferred ontology must not be treated as an already shipped
production capability.
