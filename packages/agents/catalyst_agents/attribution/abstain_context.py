"""What an abstention still shows: a labeled possible explanation and news leads.

Status stays ABSTAIN and no claim is added. Two optional, code-rendered parts:

- POSSIBLE_EXPLANATION (not established): only the PRIMARY the same-event
  verifier rejected as UNLINKED — news from this session about this stock or
  its group, quote-checked (R1-R6, subject and timing gates passed), where
  nothing shows it moved the stock. Its verbatim quotes and the verifier's
  reason are shown. A candidate rejected as a recap, preview, evergreen
  commentary, another company or a group the stock moved against is never
  shown as an explanation.
- RELATED_NEWS (leads, not causes): up to MAX_NEWS in-window articles whose
  stored title names the stock: title, publisher, time and link only.
"""
from __future__ import annotations

from typing import Any

MAX_NEWS = 5
EXPLANATION_CATEGORIES = frozenset({"UNLINKED"})


def possible_explanation(verifier: Any) -> str:
    last = getattr(verifier, "last", None)
    if not last or last.get("verdict") is not False:
        return ""
    if last.get("category") not in EXPLANATION_CATEGORIES:
        return ""
    return f"{last['statement']} Not established: {last['reason']}"


def related_news_items(context_pack: Any, ticker: str) -> list[dict[str, str]]:
    from catalyst_data.retrieval.web_bodies import subject_matcher

    from catalyst_agents.attribution.ceilings import _title_source

    titles_of = _title_source()
    if titles_of is None:
        return []
    temporal = context_pack.temporal_identity
    start, end = temporal.publication_window()
    items = [
        item for item in context_pack.evidence_inventory
        if ":web_body:" in item.evidence_id
        and (item.eligible_at >= start if temporal.evidence_window_start_at is not None else item.eligible_at > start)
        and item.eligible_at <= end
    ]
    titles = titles_of([item.evidence_id for item in items])
    about = subject_matcher(ticker)
    leads, seen = [], set()
    for item in sorted(items, key=lambda i: i.eligible_at):
        title = " ".join(titles.get(item.evidence_id, "").split())
        url = getattr(item, "canonical_url", None) or ""
        if not title or not about(title) or url in seen:
            continue
        seen.add(url)
        leads.append({
            "title": title,
            "publisher": getattr(item, "publisher", None) or "news",
            "published_at": item.eligible_at.strftime("%Y-%m-%d %H:%M UTC"),
            "url": url,
        })
        if len(leads) >= MAX_NEWS:
            break
    return leads


def related_news(context_pack: Any, ticker: str) -> list[str]:
    """Compatibility view of the leads rendered by ``render``."""
    return [
        f"{item['title']} — {item['publisher']}, {item['published_at']}, {item['url']}"
        for item in related_news_items(context_pack, ticker)
    ]


def structured(
    verifier: Any, context_pack: Any, ticker: str,
    possible_quotes: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Return structured optional abstention content used by both renderers."""
    last = getattr(verifier, "last", None)
    explanation = None
    if last and last.get("verdict") is False and last.get("category") in EXPLANATION_CATEGORIES:
        event_quotes = [
            quote for quote in (possible_quotes or ())
            if "EVENT" in quote.get("roles", ())
            and "EVENT_SUBJECT_MATCH" in quote.get("role_reasons", {}).get("EVENT", ())
        ]
        if event_quotes:
            explanation = {
                "label": "Event evidence is time-verified; its link to the move is not established.",
                "quotes": event_quotes,
                "verifier_reason": "No attribution quote met the event-time grade requirements.",
            }
        elif not any("EVENT" in quote.get("roles", ()) for quote in (possible_quotes or ())):
            explanation = {
                "quotes": list(possible_quotes or []),
                "verifier_reason": last["reason"],
            }
    return {"possible_explanation": explanation, "leads": related_news_items(context_pack, ticker)}


def render(verifier: Any, context_pack: Any, ticker: str) -> str:
    """The abstention's extra sections as text ("" when there is nothing to add)."""
    return render_structured(structured(verifier, context_pack, ticker))


def render_structured(content: dict[str, Any]) -> str:
    """Render text from the same structured leads/explanation snapshot."""
    parts = []
    explanation = content["possible_explanation"]
    if explanation:
        quotes = explanation["quotes"]
        statement = " ".join(
            f"“{quote['text']}” — {quote['publisher']}, {quote['published_at']}"
            + (f", {quote['url']}" if quote.get("url") else "")
            for quote in quotes
        )
        label = explanation.get("label", "Possible explanation — not established")
        rendered = f"{label} {statement} Not established: {explanation['verifier_reason']}"
        parts += ["## POSSIBLE_EXPLANATION — not established", rendered, ""]
    news = content["leads"]
    if news:
        lines = [f"{item['title']} — {item['publisher']}, {item['published_at']}, {item['url']}" for item in news]
        parts += ["## RELATED_NEWS — leads, not causes", *[f"- {line}" for line in lines], ""]
    return "\n".join(parts).strip()


__all__ = ["render", "render_structured", "structured", "possible_explanation", "related_news", "related_news_items"]
