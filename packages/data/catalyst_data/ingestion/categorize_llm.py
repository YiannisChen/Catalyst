"""News categories by a small model, cached (B2).

The rule classifier in ``categorize`` scored 0.375 on 200 operator-labelled headlines; this prompt
(deepseek-flash, thinking off, batches of 25) scored 0.80 on the same set, which was labelled before
the prompt was used (docs/plans/2026-10-09-backend-features-for-frontend.md, B2). The category
table and rules below are a frozen copy of the labelling guide. Results are cached per article in
``news_categories`` so a headline is classified once.

  python -m catalyst_data.ingestion.categorize_llm session --ticker NOW --date 2026-04-10
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from catalyst_data.storage.connect import connect_readonly
from catalyst_data.storage.runtime_paths import runtime_store_path

PROMPT_VERSION = "news_cat_v1"
MODEL = "deepseek-flash"
BATCH = 25
CODES = ("EARNINGS", "ANALYST", "DEALS", "PRODUCT", "REGULATORY_LEGAL", "MANAGEMENT", "CAPITAL",
         "MARKET", "OPINION", "OTHER")
CATEGORY_TABLE = '| 代码 | 类别 | 包括 | 例子 |\n|---|---|---|---|\n| EARNINGS | 财报与指引 | 季度业绩、业绩指引、财报预告、对 EPS 或营收预期的讨论 | "Meta beats Q4 estimates, guides Q1 above" |\n| ANALYST | 分析师评级 | 评级上调或下调、目标价调整、首次覆盖 | "UBS downgrades ServiceNow to Neutral" |\n| DEALS | 交易与合同 | 并购、合作、合同或订单、入股投资 | "Pentagon taps Boeing to triple seeker output" |\n| PRODUCT | 产品、技术与临床 | 新产品、新技术发布，临床试验结果 | "Google unveils Googlebook" |\n| REGULATORY_LEGAL | 监管、法律与政策 | 监管机构调查、批准（包括 FDA）、诉讼、罚款、针对行业或公司的政策（如关税） | "Italy probes Booking.com" |\n| MANAGEMENT | 管理层与组织 | 高管变动、董事会、裁员、重组 | "Salesforce president steps down" |\n| CAPITAL | 资本动作 | 分红、回购、发债或增发、拆股 | "3M raises dividend 6.8%" |\n| MARKET | 市场与行情 | 大盘、板块、宏观、商品价格的走势；"异动股"榜单；对股价涨跌本身的描述 | "Stocks making the biggest moves premarket" |\n| OPINION | 观点与投资评论 | 没有新事件的投资评论："值得买吗""估值分析""长期持有理由" | "Is Visa stock a buy now?" |\n| OTHER | 其他 | 以上都不是 | |\n\n'
CATEGORY_RULES = '**判断规则：**\n- 一条讲了两件事时，选**标题的主语和动词**说的那件事。比如 "Stock falls after earnings miss" 选 EARNINGS，不选 MARKET。\n- "Why X stock is moving today" 这类文章，要看它说的原因是什么。原因是财报就选 EARNINGS，只说跟随大盘就选 MARKET。\n- 机构持仓变动（"XX fund buys shares of ..."）选 OTHER。\n\n'
SCHEMA = """
CREATE TABLE IF NOT EXISTS news_categories (
    item_key TEXT PRIMARY KEY,
    category TEXT NOT NULL,
    source TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    classified_at TEXT NOT NULL
);
"""


def default_db() -> Path:
    return runtime_store_path("news_categories_db")


def cached(db_path: Path | str, keys: Iterable[str]) -> dict[str, str]:
    """Read-only lookup; an absent database means nothing is classified yet."""
    keys = list(keys)
    path = Path(db_path)
    if not keys or not path.exists():
        return {}
    conn = connect_readonly(path, immutable=False, timeout_ms=10_000)
    try:
        out = {}
        for i in range(0, len(keys), 500):
            chunk = keys[i:i + 500]
            out.update(conn.execute(
                f"SELECT item_key, category FROM news_categories WHERE item_key IN ({','.join('?' * len(chunk))})",
                chunk).fetchall())
        return out
    finally:
        conn.close()


def _prompt(batch: list[dict[str, Any]]) -> str:
    listing = "\n".join(f"{i}: {it['title']} || {(it.get('summary') or '')[:200]}" for i, it in enumerate(batch))
    return ("Classify each financial news item into exactly one category code, by the main thing the "
            f"headline (and summary) reports.\n\nCATEGORIES:\n{CATEGORY_TABLE}\nRULES:\n{CATEGORY_RULES}\n"
            f"ITEMS:\n{listing}\n\nReturn JSON only: {{\"labels\": {{\"<item>\": \"<CODE>\", ...}}}}")


def classify(items: list[dict[str, Any]], api_key: str, db_path: Path | str | None = None,
             client: Any = None) -> dict[str, str]:
    """Classify items ({key, title, summary}) not yet cached; returns {key: category} for all."""
    db_path = Path(db_path or default_db())
    have = cached(db_path, [it["key"] for it in items])
    todo = [it for it in items if it["key"] not in have]
    if todo:
        if client is None:
            from openai import OpenAI
            client = OpenAI(api_key=api_key, base_url="https://api.deepseek.com/v1", timeout=120)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(db_path)
        conn.executescript(SCHEMA)
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        try:
            for i in range(0, len(todo), BATCH):
                batch = todo[i:i + BATCH]
                resp = client.chat.completions.create(
                    model=MODEL, messages=[{"role": "user", "content": _prompt(batch)}], temperature=0,
                    response_format={"type": "json_object"}, max_tokens=1200,
                    extra_body={"thinking": {"type": "disabled"}})
                labels = json.loads(resp.choices[0].message.content).get("labels", {})
                for j, it in enumerate(batch):
                    code = str(labels.get(str(j), "")).strip().upper()
                    if code in CODES:
                        conn.execute("INSERT OR REPLACE INTO news_categories VALUES (?,?,?,?,?)",
                                     (it["key"], code, "llm", PROMPT_VERSION, now))
                        have[it["key"]] = code
                conn.commit()
        finally:
            conn.close()
    return have


def main(argv: list[str] | None = None) -> int:
    from catalyst_data.ingestion.session_news import category_inputs

    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("session")
    s.add_argument("--ticker", required=True)
    s.add_argument("--date", required=True)
    args = ap.parse_args(argv)
    key = os.environ.get("DEEPSEEK_API_KEY")
    if not key:
        ap.error("DEEPSEEK_API_KEY is not set")
    items = category_inputs(args.ticker, args.date)
    result = classify(items, key)
    print(json.dumps({"items": len(items), "classified": len(result)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
