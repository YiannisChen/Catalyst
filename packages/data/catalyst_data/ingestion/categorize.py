"""Deterministic headline categories (B2).

Rules are evaluated in this order against the title, then in the same order
against the summary. Event rules precede opinion and market-move rules so a
reported catalyst wins over its accompanying share-price reaction.

| rule id | category | title/summary signal |
| earnings.guidance | EARNINGS | earnings, quarterly results, EPS, revenue, sales, guidance, beats/misses estimates |
| analyst.rating | ANALYST | rating actions, coverage starts, price-target changes; not ordinary product upgrades |
| deals.contract | DEALS | acquisitions, mergers, partnerships, contracts, orders, investments |
| product.release | PRODUCT | product/technology releases and clinical results |
| legal.regulatory | REGULATORY_LEGAL | regulators, approvals, investigations, litigation, fines, policy |
| management.change | MANAGEMENT | executive/board changes, layoffs, restructuring |
| capital.action | CAPITAL | dividends, repurchases, offerings, debt issuance, stock splits |
| opinion.investment | OPINION | buy/sell questions, reasons to invest, valuation, long-term holding |
| market.move | MARKET | mover lists, index/sector/commodity moves, stock-price-only moves |
| other.default | OTHER | no preceding signal |
"""
from __future__ import annotations

import re

_RULES: tuple[tuple[str, str, re.Pattern[str]], ...] = (
    ("EARNINGS", "earnings.guidance", re.compile(
        r"\b(?:earnings|quarterly results|financial results|reports? (?:q[1-4]|first|second|third|fourth) "
        r"(?:quarter )?(?:earnings|results|revenue|sales|eps)|"
        r"eps|revenue|sales (?:rise|fall|grow|drop|beat|miss)|guidance|outlook|"
        r"beats? (?:wall street|quarterly|q[1-4]|revenue|earnings|estimates?)(?!\s+(?:delivery|deliveries|vehicles?))|"
        r"misses? (?:wall street|quarterly|q[1-4]|revenue|earnings|estimates?)(?!\s+(?:delivery|deliveries|vehicles?))|"
        r"tops? estimates?|falls? short of estimates?)\b", re.I)),
    ("ANALYST", "analyst.rating", re.compile(
        r"\b(?:initiates? coverage|starts? coverage|price target|target price|"
        r"rating(?:s)? (?:to|at)|reiterates? (?:a )?(?:buy|sell|hold)|"
        r"maintains? (?:a )?(?:buy|sell|hold|neutral|outperform|underperform)|"
        r"(?:raises?|cuts?|lowers?|lifts?|hikes?) (?:its )?(?:price )?target|"
        r"(?:upgrade|downgrade)(?:s|d)? (?:to )?(?:buy|sell|hold|neutral|overweight|underweight|"
        r"outperform|underperform|equal weight|market perform)|"
        r"analysts?\b.{0,45}\b(?:upgrade|downgrade|raise|cut|lower|lift|hike))\b", re.I)),
    ("DEALS", "deals.contract", re.compile(
        r"\b(?:acquires?|acquisition|to acquire|merger|merges? with|takeover|"
        r"partnership|partners? with|collaborates? with|contract|orders? (?:from|for)|"
        r"wins? (?:a )?(?:\$[\d.,]+\s*(?:billion|million)?\s*)?(?:contract|order)|"
        r"agreement|invests? (?:in|\$))\b", re.I)),
    ("PRODUCT", "product.release", re.compile(
        r"\b(?:unveils?|launches?|introduces?|releases?|rolls out|debut(?:s|ed)?|"
        r"new product|new chip|new platform|clinical (?:trial|study|results?)|"
        r"phase [123] (?:trial|results?)|trial results?)\b", re.I)),
    ("REGULATORY_LEGAL", "legal.regulatory", re.compile(
        r"\b(?:lawsuit|sues?|sued|settles? (?:a )?(?:lawsuit|case)|litigation|"
        r"fined|penalty|probes?|investigates?|antitrust|regulator|regulatory|"
        r"(?:fda|sec|ftc|doj) (?:approves?|clears?|rejects?|sues?|investigates?)|"
        r"(?:fda|sec) approval|approved by (?:the )?(?:fda|sec)|"
        r"approves? (?:the company's |its )?(?:drug|treatment|device|application)|"
        r"clears? (?:the company's |its )?(?:drug|treatment|device)|"
        r"policy|tariffs?|mandate|ban on|(?:new )?rules? (?:require|force|limit|ban|target|from))\b", re.I)),
    ("MANAGEMENT", "management.change", re.compile(
        r"\b(?:ceo|chief executive|president|chairman|chairwoman|director|board member)\b.{0,55}"
        r"\b(?:steps? down|resigns?|leaves?|named|appointed|replaces?|retires?)\b|"
        r"\b(?:steps? down|resigns?|named|appointed|replaces?|retires?)\b.{0,55}"
        r"\b(?:ceo|chief executive|president|chairman|chairwoman|director)\b|"
        r"\b(?:layoffs?|cuts? jobs|job cuts|restructur(?:e|es|ing)|reorganization)\b", re.I)),
    ("CAPITAL", "capital.action", re.compile(
        r"\b(?:dividend|buyback|share repurchase|stock repurchase|repurchases? shares|"
        r"stock split|share split|secondary offering|follow-on offering|public offering|"
        r"share offering|issues? (?:new )?(?:shares|stock|bonds|debt)|debt offering|"
        r"raises? (?:\$[\d.,]+\s*)?(?:in )?(?:debt|capital)|special dividend)\b", re.I)),
    ("OPINION", "opinion.investment", re.compile(
        r"\b(?:is .{0,45} stock a (?:buy|sell)|should you (?:buy|sell|hold)|"
        r"reasons? to (?:buy|own|hold|sell)|worth (?:buying|owning)|"
        r"long[- ]term (?:hold|investment|reasons?|outlook)|"
        r"underpricing|overvalued|undervalued|fair value|better buy|"
        r"which .{0,45} stock is a better|is .{0,45} stock ready for|"
        r"what investors need to know|good investment)\b", re.I)),
    ("MARKET", "market.move", re.compile(
        r"\b(?:stocks? making the biggest moves|stocks? that (?:are )?moving|"
        r"top (?:stock )?gainers? and losers?|market (?:today|wrap|update)|"
        r"dow|nasdaq|s&p 500|index|sector|crude|oil|gold|silver|copper|"
        r"shares? (?:rise|rises|rose|fall|falls|fell|gain|gains|jump|jumps|drop|drops|"
        r"climb|climbs|slide|slides|surge|surges)|stock (?:rises?|falls?|jumps?|drops?|"
        r"climbs?|slides?|surges?)|trades? (?:higher|lower|up|down)|"
        r"(?>up|down)\s+\d+(?:\.\d+)?%)", re.I)),
)
CATEGORY_CODES = tuple(rule[0] for rule in _RULES) + ("OTHER",)


def classify(title: str, summary: str | None) -> tuple[str, str]:
    """Return ``(category_code, rule_id)`` using fixed title-first ordering."""
    for text in (title or "", summary or ""):
        for category, rule_id, pattern in _RULES:
            if pattern.search(text):
                return category, rule_id
    return "OTHER", "other.default"
