"""Fixed provider and publisher evidence for confirmed publisher origins.

Only combinations seen in our own stores are listed, each with one stored row as
evidence (store:table:row id). Provider and publisher are the stored strings, so
no name is normalized into a family. The wire RSS feeds store no publisher
column; their feed is the publisher, so publisher repeats the stored provider.
Accesswire is not listed: no stored row from any of our sources names it
(checked 2026-11-01 across the Alpaca, Finnhub, Polygon, press RSS and body stores).
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

_REGISTRATION_TIME = "2026-11-01T00:00:00Z"


@dataclass(frozen=True)
class PublisherEntry:
    provider: str
    publisher: str
    content_state: str
    family: str
    evidence_ref: str
    effective_from: str
    effective_until: str | None = None


PUBLISHER_REGISTRY = (
    # effective_from is the registration time, not the time the combination first appeared in data.
    PublisherEntry("alpaca", "benzinga", "full_text", "benzinga",
                   "archive/alpaca_news.sqlite3:body_versions:alpaca_news/62679903",
                   effective_from=_REGISTRATION_TIME, effective_until=None),
    PublisherEntry("daily:finnhub+polygon", "Benzinga", "full_text", "benzinga",
                   "m9/news_bodies_v2.sqlite3:web_body_chunks:"
                   "v1:asset:5e872910f7550f0adb45f6b1937dbe27bab0750bb031398485a220f63376f288",
                   effective_from=_REGISTRATION_TIME, effective_until=None),
    PublisherEntry("finnhub", "Benzinga", "headline", "benzinga",
                   "m9/headline_archive.sqlite3:finnhub_news:138640196",
                   effective_from=_REGISTRATION_TIME, effective_until=None),
    PublisherEntry("polygon", "Benzinga", "headline", "benzinga",
                   "m9/headline_archive.sqlite3:polygon_feed:"
                   "cf1a9a821eb3a1f8124f6c218e07353dcd9af20b3b682767d3c0c816a3f8284f",
                   effective_from=_REGISTRATION_TIME, effective_until=None),
    PublisherEntry("daily:finnhub+polygon", "GlobeNewswire", "full_text", "globenewswire",
                   "m9/news_bodies_v2.sqlite3:web_body_chunks:"
                   "v1:asset:a5215a93c364ee9d4d2383d158dd7b9928ccb2f7320a8221411f4fb92f166cb8",
                   effective_from=_REGISTRATION_TIME, effective_until=None),
    PublisherEntry("polygon", "GlobeNewswire Inc.", "headline", "globenewswire",
                   "m9/headline_archive.sqlite3:polygon_feed:"
                   "20514140694dcb4521288fa8bcda4da4899583945ca636b4f250a16118426fb4",
                   effective_from=_REGISTRATION_TIME, effective_until=None),
    PublisherEntry("businesswire", "businesswire", "headline", "businesswire",
                   "archive/press_releases.sqlite3:press_rss:businesswire/20261020461310en",
                   effective_from=_REGISTRATION_TIME, effective_until=None),
    PublisherEntry("prnewswire", "prnewswire", "headline", "prnewswire",
                   "archive/press_releases.sqlite3:press_rss:prnewswire/"
                   "https://www.prnewswire.com/news-releases/prosecco-doc-chef-residency-concludes-"
                   "first-edition-with-us-culinary-exchange-planned-for-2027-302914492.html",
                   effective_from=_REGISTRATION_TIME, effective_until=None),
)


def registry_sha256(entries: tuple[PublisherEntry, ...] | None = None) -> str:
    """Hash registry identity.

    A read passes its applicable entries explicitly; that hash includes the entry
    identity and effective_from, but omits effective_until so closing an interval
    later does not rewrite a hash recorded while it was open. The no-argument
    form hashes the shipped registry including both interval bounds to identify
    the shipped registry/code version.
    """
    include_effective_until = entries is None
    if entries is None:
        entries = PUBLISHER_REGISTRY
    entries = sorted(entries, key=lambda entry: (
        entry.provider, entry.publisher, entry.content_state, entry.family, entry.evidence_ref,
        entry.effective_from,
        (entry.effective_until or "") if include_effective_until else "",
    ))
    rows = []
    for entry in entries:
        row = {"provider": entry.provider, "publisher": entry.publisher,
               "content_state": entry.content_state, "family": entry.family,
               "evidence_ref": entry.evidence_ref, "effective_from": entry.effective_from}
        if include_effective_until:
            row["effective_until"] = entry.effective_until
        rows.append(row)
    payload = json.dumps(rows, ensure_ascii=False, separators=(",", ":"),
                         sort_keys=True).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()
