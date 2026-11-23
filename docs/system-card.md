# Catalyst System Card

This card describes the system as frozen and evaluated in October 2026. Plans and pending decisions below are historical; the prospective rounds ended on 2026-11-13, and current status is in [README.md](../README.md) and [docs/project-retrospective.md](project-retrospective.md).

- **Version**: v2.0.1, 2026-10-18 (failure mode 11 added; web retrieval rewritten to match the code); v1.0: 2026-10-16
- **Systems covered**:
  - Frozen backend `backend-freeze-v1` (default configuration, evidence-constrained publisher);
  - Second version `backend-freeze-v2` (four publishing rules);
  - Second-version extension `backend-freeze-v2.1` (adds the `filing` rule).
  - The latter two are not defaults; the second prospective evaluation will determine whether to use them.
- **Sources for metrics**: All figures in this document refer to the results registry, in the form 〔R: record ID〕.

## 1. What the system does

Given a large-cap U.S. stock and a trading day D, the system determines whether the price movement on day D can be attributed to a specific sourced event that occurred before the movement:
- **Yes**: The conclusion is assembled from verbatim quotations from source text, with the publisher, time, and link;
- **No**: The system abstains, explains why, and provides market facts.

The public interface names its five results **Confirmed**, **Reported**, **With its group**, **No clear cause**, and **Ordinary move**. The second model check is the fact checker; the editor applies fixed code rules to publish results. The current daily universe contains 102 large-cap U.S. stocks.

The system uses only DeepSeek and free data. Model cost is about 0.8 cents per case (first run on the test set) 〔R: test.cost.publisher〕.

## 2. Intended uses

- Research and review: Quickly check whether a sourced company-specific event can explain a large move in a large-cap stock on a given day.
- Scenarios that require auditable outputs: Every conclusion can be traced to source text and a timestamp, and abstentions include an explanation.
- Starting point for human analysis: Market facts, direction agreement, and decomposition data can help determine where to investigate first.

## 3. Out-of-scope uses

- **Investment advice or trading signals**: The system does not provide investment advice.
- **Moves driven by macroeconomic factors, sectors, or commodities**: The system may answer only when source text explicitly attributes the move to such a factor. Coverage is low and the error rate is high (§5).
- **Intraday attribution, fund flows, and position analysis**: The system does not have these data.
- **Small-cap stocks, non-U.S. stocks, and non-trading days**: These have not been evaluated.
- **Commercial use**: Data sources such as the Polygon free tier permit personal, non-commercial use only ([data notes](data/data-card.md)).

## 4. Capability claims

| Claim | Evidence | Limitations |
|---|---|---|
| High precision on published conclusions about company-specific events | One run on the development set plus 22/23 on the test set 〔R: pooled.publisher.issuer_right〕; 7/7 on the test set 〔R: test.publisher.issuer_right〕 | This is precision among answered cases, not coverage: on the test set, the system answered only 7/10 company-specific event cases 〔R: test.publisher.issuer_answered〕; development-set results are optimistic |
| On earnings days, the main event in answered cases is almost always a company earnings report | 99/100 〔R: t6.category_right〕 | There is no gold label; only the category was evaluated, not the mechanism. Coverage is about half 〔R: t6.answered〕 |
| Abstention calibration is better than that of web-enabled large language models | Correct abstention rate: 88% on the test set 〔R: test.publisher.correct_abstain_rate〕 versus 0% for web-enabled large language models 〔R: test.gpt.correct_abstain_rate〕 | The system still published 2/17 test-set cases where it should have abstained 〔R: test.publisher.abstain_published〕 |
| Cited evidence in the test set did not fall outside the time window | 0 out of 23 citations were outside the window 〔R: test.leak.outside〕 | The audit checked times stored in the database. Two source timestamps were later found to be inaccurate (§6, item 5) and were rechecked against the correction table: all cited test-set evidence remains within the window and was unaffected; two corrected Benzinga items in the context for EOG 2026-09-16 fall after market close and were not cited. Only cited evidence in the test set was audited. Timestamps with date only are unmarked, leaving residual risk (§6) |
| The main bottleneck was identified | Both preregistered experiments were judged FAIL, and all placebo publishing counts were 0 〔R: v3.verdict〕〔R: b.verdict〕〔R: b.placebo〕 | — |
| Results are stable across runs | Answer/abstention flip rate 〔R: t3.decision_flip〕 | 80 development-set cases, 3 runs |
| The second-version extension improves attribution coverage at the **event level** on earnings days (identifies the event) | On 100 new earnings-day cases, published answers increased from 55 to 82 〔R: v21.e100.published〕; all 27 added answers were judged correct 〔R: v21.e100.added_right〕; 0/58 placebo days 〔R: v21.placebo.published〕 | 26 of the added answers are PARTIAL: they explain what happened but not which figure exceeded expectations. All cases had gold labels requiring an answer. This is not the default configuration; the second evaluation will decide whether to use it |

## 5. Claims that cannot be made

| Claim | Reason |
|---|---|
| The system can attribute moves to macroeconomic or sector factors | It answered 6 systematic-factor cases on the test set and got only 2 right 〔R: test.publisher.systematic_right〕 |
| The system is better than web-enabled large language models at finding causes | On cases that required an answer, the system got 9/23 right 〔R: test.publisher.gold_answer_right〕, versus 17/23 for web-enabled large language models 〔R: test.gpt.gold_answer_right〕. The system's advantage is limited to abstention |
| The new publisher is more accurate than the early baseline | The test-set net scores are −9 versus −7, with overlapping confidence intervals 〔R: test.publisher.net〕〔R: test.baseline.net〕 |
| The new publisher is cheaper than the early baseline | Measured test-set costs are the same 〔R: test.cost.publisher〕〔R: test.cost.baseline〕 |
| Development-set results represent real-world performance | The error rate worsened substantially from the development set to the test set 〔R: derived.dev_test_wrong_gap〕 |
| The test set is completely clean | Test cases came from a set previously diagnosed using threshold rules |
| The test-set net score is positive | The test-set net score is negative, with a confidence interval that crosses 0 〔R: test.publisher.net〕 |
| The system never uses future information | The leakage audit covered only cited evidence in the test set |
| The second-version extension improves explanations at the **mechanism level** (identifies which figure exceeded expectations) | Nearly all added answers are PARTIAL 〔R: v21.e100.added_right〕; earnings figure parsing has not been implemented (ADR-019) |
| The second version or its extension is better overall than the first version | On the development set, the rules changed only a few cases. Changes on the test set are an in-sample description; the second prospective evaluation will determine the result |

## 6. Known failure modes

1. **Stale news**: On trading days without a clear cause, the system published old acquisition or earnings news. There were 2 cases on the test set, one in each of the first and second stages 〔R: test.publisher.abstain_published〕.
2. **Insufficient use of company filings**: When a company has an 8-K but no report saying that the stock price moved as a result, and there is another company event that day, the system does not publish an answer. On earnings-day cases, most abstentions were of this type 〔R: t6.no_eligible〕. One cause: earnings press releases published after market close on the previous trading day are extracted as belonging to another trading day and excluded from the normal path (run behavior RG-14).
3. **Unreliable systematic attribution**:
   - For sector moves, the system may mistake a secondary cause in the gold label or another driver for the main cause;
   - It makes more errors when it infers a group-level cause directly from market decomposition 〔R: m3.inferred_group_wrong〕.
4. **Source quality**: The publisher does not rank sources. Verbatim attribution sentences from commentary sites (Motley Fool, 24/7 Wall St., and others) can be used as grounds for publication.
5. **Timestamps**:
   - Web evidence with a date only is assigned a time of 23:59:59Z on that date. This can place D−1 evidence in the pre-market period on D 〔R: t1.preopen_dminus1〕;
   - Tavily midnight timestamps are treated as having minute-level precision 〔R: t1.tavily_midnight〕;
   - Timestamps differ between two ingestion paths (time-zone investigation on 2026-10-22): For Benzinga and CNBC company news ingested through Finnhub, Finnhub treated these sources' Eastern Time timestamps as UTC. Stored publication times for Benzinga items ingested from 2026-09-30 through 10-08 are 4–5 hours early, so after-close reports are treated as intraday. Published answers in development-set CAT 2026-04-30 and the first version of T6 A 2026-05-28 cited evidence that was actually published after market close. All cited evidence in the frozen test set remains within the window after correcting the timestamps. However, two Benzinga items in the context for EOG 2026-09-16 fall after market close after correction and were not cited. Existing figures have not been recalculated. The correction table is in the data layer (`data/corrections/time_corrections_v1.csv`, data/collectors `80edfe1`). Sensitivity analysis was added at scoring time for both prospective evaluations as preregistered (second evaluation §10; first evaluation addendum `5767b97`).
   - SEC 8-K receipt times are inaccurate (found 2026-10-22; investigation completed 10-23): For about 60% of issuers (consistent per company: each company is either always right or always wrong), the SEC submissions API's `acceptanceDateTime` is exactly 4 hours later during daylight saving time or 5 hours later during standard time than the receipt time on the EDGAR index page. Among the 11,200 8-Ks already in the database, 4,955 are 4 hours late, 2,088 are 5 hours late, and 4,122 match (correction table `data/corrections/sec_acceptance_v1.csv`). The direction is late, so this does not make after-the-fact news appear to be before the fact, but it can place 8-Ks in the wrong time period: filings received between 12:00 and 16:00 Eastern are pushed past market close and into the next trading day's window; filings received before market open are treated as after the open. A read-only check of all existing runs (excluding the two prospective evaluations) found no changes to the window classification of cited SEC evidence. The earnings anchor and earnings fallback classifications changed for only one session (T6 BRK-B 2025-11-04, first-version run, no answer published). There were 92 sessions where an 8-K received before market open that day was treated as received after the open; 8 matched the time-gate blocking pattern. Published answers in development-set and T6 cases and in v2 / v2.1 runs on the new earnings-day sample (such as KR 2026-03-05 and PODD 2026-05-06) may have been blocked by the time gate if the correct times had been used. This is only an upper bound because the gate also checks all main-cause sources. The test set lists only BLK 2026-07-15. Existing figures have not been recalculated. New ingestions have used index-page times since 10-22. The v2 SEC index (`m9/sec_filings.py`) was fixed after the evaluation ended. Sensitivity analyses were added to scoring for both prospective evaluations as described in the appendices.
6. **Trading-day mismatch**: When an event occurs intraday on D−1 but the stock price moves on D, the time rule excludes the event (TSLA x04; see inferred association-channel results).
7. **Review errors**:
   - Agreement between automated and human review is based on only 30 cases 〔R: t2.kappa4〕;
   - On re-review, the same reviewer changes the judgment about once every 13 cases 〔R: judge.self_agreement〕, mostly at the boundary between PARTIAL and incorrect;
   - The rubric has no label for “correctly saying that the catalyst cannot be confirmed.”
8. **The second version rarely publishes its earnings passthrough** 〔R: v2.t6.fallback_published〕: Flattened attachment excerpts do not look like complete sentences, so the quote rules reject them. The second-version extension instead cites attachment units.
9. **Heuristic for attachment units** (second-version extension): Multiline headings are not recognized; attachments with unusual layouts may produce incorrect units.
10. **Run-to-run uncertainty**: Common random numbers cover only recorded requests. Retrieval cutoff time is based on the wall clock, so rerunning some cases can switch between answering and abstaining.
11. **Run behavior**: Handling of missing data, external service failures, and run failures, including failure direction, is described in the run behavior handling specification. Relevant details: When the model service is entirely unavailable, the system displays an ordinary abstention instead of a run failure (RQ-07); an abstention's “possible explanation” includes one reason from the fact checker (RQ-14); the publisher's only actual conclusion status is PARTIAL (RQ-12); the production HTTP service does not refresh data before a run, and abstentions do not distinguish “no news” from “data not ingested” (RG-02).

## 7. Cost and latency

Data is from the test set (40 cases, one run). All figures are for the **first run**: before technical retries, and with the four configurations and the web-enabled large language model baseline running in parallel. They therefore do not represent the cost and elapsed time for the full evaluation after retries:

| Configuration | Cost per case (mean) | Latency (median / p90) | Model calls (median / max) | First-run timeouts |
|---|---|---|---|---|
| Evidence-constrained publisher | 〔R: test.cost.publisher〕 | 〔R: test.latency.publisher〕 | 〔R: test.calls.publisher〕 | 〔R: test.timeouts.publisher〕 |
| Early baseline publisher | 〔R: test.cost.baseline〕 | 〔R: test.latency.baseline〕 | — | — |
| Web-enabled large language model baseline | Subscription-based, not counted | 〔R: test.latency.gpt〕 | — | — |

- **Cause of timeouts**: The four configurations and the web-enabled large language model baseline ran in parallel.
- **Web retrieval**: No retrieval during a run; the web cache is built beforehand, with at most 4 Tavily credits per stock (prospective evaluation). The production HTTP service has no build step (run behavior RG-11).
- **Event discovery agent**: Cost per case was not measured separately (unverified); the code limit is $0.03.

## 8. Evaluation and evidence

- **Full protocol**: Evaluation protocol.
- **Design basis**: Backend design §11, design decision records.
- **Prospective evaluation** (two rounds starting 2026-10-19): Provides an uncontaminated estimate; this card will be updated when results are available.
  - Round one evaluates the first version (preregistered);
  - Round two evaluates non-inferiority of the second version and its extension relative to the first version (preregistered).
- **Reviewer model**: A model that passed calibration was used; cheaper candidates did not pass calibration (calibration report).
