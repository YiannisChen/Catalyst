"""Deterministic kernel of the v2 publisher: pure functions only (no LangGraph, no model client).

The model calls these functions need (event dating) are made by the graph layer; the functions here build the
request and check the answer. Modules:

- rules       the v2 switches (all off reproduces A′)
- event_date  event_date_v1: request, and the code check of the model's date
- recency     the window after the prior close up to D's close; stale clusters; the 8-K earnings anchor
- gate        the frozen desk gate with the recency veto
- earnings    the earnings-filing fallback (Item 2.02 in the window, |research z| >= 2, no other material 8-K)
"""
