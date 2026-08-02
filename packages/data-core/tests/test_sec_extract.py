"""SEC extract + PDF policy."""

from catalyst_data.sec.extract import extract_document_text


def test_html_success_meets_rag_min_char():
    body = ("earnings guidance " * 40).encode()
    html = b"<html><body>" + body + b"</body></html>"
    out = extract_document_text(html, content_type="text/html", is_primary=True)
    assert out.status == "success"
    assert len(out.text) >= 200


def test_whitespace_not_success():
    out = extract_document_text(b"   \n\t  ", content_type="text/plain")
    assert out.status == "empty_extract"


def test_pdf_primary_mandatory_failed():
    out = extract_document_text(
        b"%PDF-1.4 fake", content_type="application/pdf", is_primary=True, requiredness="mandatory"
    )
    assert out.status == "mandatory_failed"


def test_pdf_exhibit_optional_degraded():
    out = extract_document_text(
        b"%PDF-1.4 fake",
        content_type="application/pdf",
        is_primary=False,
        requiredness="optional_degraded",
    )
    assert out.status == "pdf_skipped"
    assert out.requiredness_effect == "optional_degraded"


def test_no_ocr_empty_string_success():
    out = extract_document_text(b"%PDF", is_primary=False, requiredness="optional_degraded")
    assert out.status != "success"
    assert out.text == ""
