> Supporting reference; canonical source: `docs/superpowers/specs/2026-04-27-catalyst-fullversion-strategy.md` (strategy), `docs/full-version-execution-spec.md` (technical), `docs/current_situation.md` (status).

# GDELT API — Catalyst Data-Core

> **Scope:** Project reference for **DOC 2.0**, **GEO 2.0**, and **Context 2.0**, distilled from GDELT blog posts. Official project: [GDELT Project](https://www.gdeltproject.org/).

---

## Rate limits — does the official text give a fixed number?

**No.** The DOC / GEO / Context articles do **not** state a public **requests per second** or **requests per minute** quota.

- Other GDELT posts discuss quotas, **HTTP 429**, and protecting backend infrastructure — that is **not** the same as a published constant you can hard-code.
- **Operational approach:** throttle politely, back off on errors; on **429** or blocks, slow down and widen spacing.

**Catalyst implementation** (`connectors/gdelt.py`): enforces **≥ ~5.5 seconds** between **GDELT DOC API** calls (`GDELT_API_DELAY_SECONDS`). That aligns with the **community guidance** below; it is **not** an official formula from GDELT.

### Community “safe line” (unofficial)

When the vendor does not publish a number, practitioners often use the following. **Policies may change** — treat **429 / blocks** as ground truth.

| Guideline | Detail |
|-----------|--------|
| **Spacing** | **One request every 5–10 seconds** → about **6–12 RPM**. |
| **Implementation** | Common pattern: `time.sleep(5)` or `time.sleep(6)` between consecutive calls. |
| **Ceiling (empirical)** | **Sustained rates above ~1 request/second** are very likely to trigger throttling or temporary blocks — avoid. |
| **Concurrency** | **Avoid** multi-threaded or highly parallel hits to GDELT; use **sequential** requests with delays. |

**Attribution (community):** Ken Blake, Ph.D. — guidance summarized from the *GDELT Headline Scrape* line of work (not an SEC or GDELT statutory document). Quote the original project if you need a verbatim citation.

---

## Three JSON APIs (do not mix paths)

| API | Base path | Purpose |
|-----|-----------|---------|
| **DOC 2.0** | `GET https://api.gdeltproject.org/api/v2/doc/doc` | Full-text search: article lists, timelines, image collages, word clouds (**used by Catalyst**). |
| **GEO 2.0** | `GET https://api.gdeltproject.org/api/v2/geo/geo` | Geography: keyword / image → maps (HTML, GeoJSON, etc.). |
| **Context 2.0** | `GET https://api.gdeltproject.org/api/v2/context/context` | **Sentence-level** matches (all terms in one sentence) + snippets; initial window about **72 hours**. |

The sections below focus on **DOC 2.0** (matches `gdelt.py`). GEO and Context are summarized only; see GDELT blog posts for full parameter lists.

---

## DOC 2.0 — capabilities

- **Search window:** By default, searches roughly the **last 3 months** of monitored coverage; narrow with **`TIMESPAN`** or **`STARTDATETIME` / `ENDDATETIME`**. Exact bounds must fall within the API’s allowed lookback (blog: **within the last 3 months** when using precise endpoints).
- **Historical depth:** Blog describes index evolution from **2017-01-01** onward; **effective range is defined by live API behavior**.
- **Languages:** Search across **65 machine-translated** languages using English query terms (Translingual stack).
- **Imagery:** VGKG-processed news images (`imagetag`, `imagewebtag`, etc.).
- **Formats:** **JSON / JSONP**, CSV, HTML visualizations, RSS / JSONFeed in some modes; **CORS** allows `Access-Control-Allow-Origin: *`.
- **Volume control:** **`MAXRECORDS`** and related parameters cap response size.

---

## DOC 2.0 — parameters used by Catalyst

Aligned with `gdelt.py`:

| Parameter | Usage in code | Notes |
|-----------|----------------|-------|
| `query` | `{ticker} stock` | Search string; combine with operators below. |
| `mode` | `ArtList` | Article list (metadata; not full article body in JSON). |
| `maxrecords` | `25` | Max list size; GDELT docs cite default **75** and cap **250** for ArtList / image modes — if behavior differs, trust the live API. |
| `timespan` | `3d` | e.g. `1h`, `3d`, `1w`, `2m` (months); minimum granularity on the order of **15 minutes** (per blog). |
| `format` | `json` | Also: `jsonp`, `html`, `csv`, `rss`, … depending on `mode`. |
| `sort` | optional `DateDesc` | Also date ascending, tone sorts, hybrid relevance, etc. |

**Response shape (code expectation):** top-level **`articles`** array; elements often include `title`, `url`, `seendate`, `domain`. Catalyst then **GETs** the first N article URLs to retrieve HTML.

---

## DOC 2.0 — common `query` operators (subset)

Operators belong **inside** the `query` string, not as separate URL parameter names.

| Pattern | Meaning |
|---------|---------|
| `"exact phrase"` | Phrase match |
| `(a OR b OR c)` | OR (**no nesting** of OR blocks) |
| `-word` or `-sourcelang:spanish` | Exclusion |
| `domain:cnn.com` / `domainis:un.org` | Domain filter (latter: exact host) |
| `sourcecountry:france` | Outlet country |
| `sourcelang:spanish` | Original article language (65 supported) |
| `theme:TERROR` | GKG theme |
| `tone>5` / `tone<-5` | Tone threshold |
| `toneabs>10` | Emotional intensity (sign ignored) |
| `near20:"trump putin"` | Proximity (approximate; phrases not supported in `near`) |
| `repeat3:"trump"` | Minimum term frequency in document |
| `imagetag:"…"`, `imagewebtag:"…"`, `imageocrmeta:"…"` | Image search (image-related **modes** only) |

For the full operator list, see the GDELT **FULL DOCUMENTATION** section of the DOC 2.0 blog post.

---

## DOC 2.0 — common `mode` values

| `mode` | Role |
|--------|------|
| **ArtList** | Article list (**Catalyst**) |
| ArtGallery | Magazine-style article layout |
| ImageCollage / ImageCollageInfo / ImageGallery / … | Image collages / galleries |
| TimelineVol / TimelineVolRaw / TimelineVolInfo / TimelineTone / … | Timeline outputs |
| ToneChart | Tone histogram |
| WordCloud* | Word / image-tag clouds |

---

## DOC 2.0 — other parameters

| Parameter | Role |
|-----------|------|
| **TIMESPAN** | Relative lookback: `15min`, `2h`, `3d`, `1w`, `2m`, … |
| **STARTDATETIME / ENDDATETIME** | `YYYYMMDDHHMMSS`; must stay within API lookback (blog: **last 3 months** for DOC). |
| **MAXRECORDS** | ArtList and some ImageCollage modes; default **75**, max **250**. |
| **TIMELINESMOOTH** | Timeline modes only; moving window up to **30** steps. |
| **SORT** | `DateDesc`, `DateAsc`, `ToneDesc`, `ToneAsc`, `HybridRel`, etc. |

---

## GEO 2.0 — summary

- **URL:** `https://api.gdeltproject.org/api/v2/geo/geo?query=...`
- **Default lookback:** about **24 hours** in the blog; **TIMESPAN** can narrow to **15 minutes–7 days** (differs from DOC’s 3-month default — follow GEO docs).
- **Outputs:** interactive HTML maps, **GeoJSON**, RSS, CSV; modes include **Point / Country / ADM1 / SourceCountry** and **Image\*** variants.
- **QUERY:** overlaps heavily with DOC; adds **location**, **locationadm1**, **locationcc**, **near:lat,lon,radius**, etc.

---

## Context 2.0 — summary

- **URL:** `https://api.gdeltproject.org/api/v2/context/context`
- **Behavior:** all search terms must appear in the **same sentence**; returns contextual snippets; initial release limited to about **72 hours**; **MAXRECORDS** default **75**, max **200** (per blog).
- **Use case:** stricter relevance than DOC “whole document” matches. **Not** used by the current Catalyst pipeline.

---

## Catalyst integration

| Item | Location |
|------|----------|
| Logical source | `gdelt_news` |
| Connector | `packages/data-core/data_core/connectors/gdelt.py` |
| Guidance | Keep DOC API traffic **sparse**; when following article URLs, limit concurrency and send a **reasonable User-Agent** to avoid publisher blocks. |

---

## Further reading

- [GDELT DOC 2.0 API Debuts](https://blog.gdeltproject.org/gdelt-doc-2-0-api-debuts/)
- [GDELT GEO 2.0 API Debuts](https://blog.gdeltproject.org/gdelt-geo-2-0-api-debuts/)
- [Announcing the GDELT Context 2.0 API](https://blog.gdeltproject.org/announcing-the-gdelt-context-2-0-api/)

If GDELT changes windows, defaults, or field names, update **`gdelt.py`** and this file together.
