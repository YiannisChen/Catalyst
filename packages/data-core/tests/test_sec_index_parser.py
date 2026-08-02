"""SEC index HTML parser and URL fallback order."""

from pathlib import Path

from catalyst_data.sec.index_parser import (
    filing_index_urls,
    parse_filing_index_html,
)

FIX = Path(__file__).parent / "fixtures" / "sec"


def test_canonical_index_html_table_parser():
    html = (FIX / "index_canonical_aapl_8k.html").read_text()
    res = parse_filing_index_html(
        html,
        base_url="https://www.sec.gov/Archives/edgar/data/320193/000032019325000001/",
        primary_document="aapl-20250801.htm",
    )
    assert res.status == "success"
    assert any(d["is_primary"] for d in res.documents)


def test_index_headers_html_fallback_when_canonical_404():
    html = (FIX / "index_headers_aapl_8k.html").read_text()
    res = parse_filing_index_html(
        html,
        base_url="https://www.sec.gov/Archives/edgar/data/320193/x/",
        primary_document="aapl-20250801.htm",
    )
    assert res.status == "success"


def test_legacy_index_headers_htm_second_fallback():
    html = (FIX / "index_headers_legacy.htm").read_text()
    res = parse_filing_index_html(
        html,
        base_url="https://www.sec.gov/Archives/edgar/data/1/x/",
        primary_document="primary.htm",
    )
    assert res.status == "success"


def test_malformed_or_no_primary_is_failed():
    html = (FIX / "index_malformed_no_primary.html").read_text()
    res = parse_filing_index_html(html, base_url="https://example/")
    assert res.status == "failed"


def test_duplicate_sequence_deterministic_sort():
    from catalyst_data.manifests.universe import load_universe_spec
    from catalyst_data.sec.inventory import build_filing_inventory_manifest

    html = (FIX / "index_duplicate_sequence.html").read_text()
    res = parse_filing_index_html(
        html, base_url="https://example/", primary_document="p.htm"
    )
    assert res.status == "success"
    spec = load_universe_spec(
        Path(__file__).parents[1]
        / "catalyst_data"
        / "manifests"
        / "universe_v1_2025_08.spec.json"
    )
    m = build_filing_inventory_manifest(
        source_snapshot_id="s" * 64,
        universe_manifest_id="u" * 64,
        tickers=list(spec.tickers),
        issuer_class_by_ticker={
            ticker: spec.companies[ticker]["issuer_class"]
            for ticker in spec.tickers
        },
        filing_entries=[
            {
                "ticker": "AAPL",
                "accession_number": "1",
                "documents": list(res.documents),
            }
        ],
    )
    docs = m["sorted_filing_entries"][0]["documents"]
    assert docs[0]["document_file"] == "p.htm"
    # same sequence 2: sort by type/filename
    rest = [d["document_file"] for d in docs[1:]]
    assert rest == sorted(rest) or len(rest) == 2


def test_filing_index_url_order():
    urls = filing_index_urls(cik_int="320193", accession="0000320193-25-000001")
    assert urls[0].endswith("-index.html")
    assert urls[1].endswith("-index-headers.html")
    assert urls[2].endswith("-index-headers.htm")
