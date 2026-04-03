import unittest

from catalyst_data.transmuter import clean_html_to_markdown


SAMPLE_HTML = """<html>
<head><title>Test Article</title></head>
<body>
<nav>Menu Item 1 | Menu Item 2</nav>
<article>
<h1>NVIDIA Q4 Earnings Beat</h1>
<p>NVIDIA reported record revenue. See <a href="https://example.com/report">the full report</a> for details.</p>
<p>Analysts at <a href="https://investor.nvidia.com">NVIDIA IR</a> provided guidance.</p>
<p>Internal link <a href="/local/page">ignored</a> should not appear in references.</p>
</article>
<footer>Copyright 2026 All Rights Reserved</footer>
<script>var x = 1;</script>
<style>.ad { display: block; }</style>
<aside>Related Ads Content</aside>
</body>
</html>"""


class TestCleanHtmlExcludedTags(unittest.TestCase):
    def test_nav_removed(self):
        result = clean_html_to_markdown(SAMPLE_HTML)
        self.assertNotIn("Menu Item 1", result)

    def test_footer_removed(self):
        result = clean_html_to_markdown(SAMPLE_HTML)
        self.assertNotIn("Copyright 2026", result)

    def test_script_removed(self):
        result = clean_html_to_markdown(SAMPLE_HTML)
        self.assertNotIn("var x = 1", result)

    def test_style_removed(self):
        result = clean_html_to_markdown(SAMPLE_HTML)
        self.assertNotIn(".ad { display", result)

    def test_aside_removed(self):
        result = clean_html_to_markdown(SAMPLE_HTML)
        self.assertNotIn("Related Ads", result)


class TestCleanHtmlContentPreserved(unittest.TestCase):
    def test_headline_preserved(self):
        result = clean_html_to_markdown(SAMPLE_HTML)
        self.assertIn("NVIDIA Q4 Earnings Beat", result)

    def test_body_text_preserved(self):
        result = clean_html_to_markdown(SAMPLE_HTML)
        self.assertIn("NVIDIA reported record revenue", result)


class TestCleanHtmlEvidenceChain(unittest.TestCase):
    def test_references_section_present(self):
        result = clean_html_to_markdown(SAMPLE_HTML)
        self.assertIn("## References", result)

    def test_links_numbered_by_first_appearance(self):
        result = clean_html_to_markdown(SAMPLE_HTML)
        self.assertIn("[1] https://example.com/report", result)
        self.assertIn("[2] https://investor.nvidia.com", result)

    def test_local_links_excluded(self):
        result = clean_html_to_markdown(SAMPLE_HTML)
        self.assertNotIn("/local/page", result)

    def test_duplicate_urls_single_number(self):
        html = '<p><a href="https://x.com">A</a> and <a href="https://x.com">B</a></p>'
        result = clean_html_to_markdown(html)
        lines = [l for l in result.splitlines() if l.startswith("[")]
        self.assertEqual(len(lines), 1)


class TestCleanHtmlEdgeCases(unittest.TestCase):
    def test_empty_html_returns_empty(self):
        self.assertEqual(clean_html_to_markdown(""), "")

    def test_whitespace_only_returns_empty(self):
        self.assertEqual(clean_html_to_markdown("   "), "")

    def test_no_links_no_references_section(self):
        result = clean_html_to_markdown("<p>Plain text only.</p>")
        self.assertNotIn("## References", result)
        self.assertIn("Plain text only", result)

    def test_determinism(self):
        r1 = clean_html_to_markdown(SAMPLE_HTML)
        r2 = clean_html_to_markdown(SAMPLE_HTML)
        self.assertEqual(r1, r2)


from catalyst_data.transmuter import financial_json_to_markdown_table


class TestFinancialJsonFlat(unittest.TestCase):
    def test_flat_dict_has_table_separator(self):
        data = {"revenue": 35082, "net_income": 12285, "eps": 4.93}
        result = financial_json_to_markdown_table(data)
        self.assertIn("|---|---|", result)

    def test_flat_dict_contains_all_keys(self):
        data = {"revenue": 35082, "net_income": 12285}
        result = financial_json_to_markdown_table(data)
        self.assertIn("revenue", result)
        self.assertIn("net_income", result)

    def test_flat_dict_contains_values(self):
        data = {"revenue": 35082}
        result = financial_json_to_markdown_table(data)
        self.assertIn("35082", result)

    def test_flat_dict_sorted_keys(self):
        data = {"z_field": 3, "a_field": 1}
        result = financial_json_to_markdown_table(data)
        a_pos = result.index("a_field")
        z_pos = result.index("z_field")
        self.assertLess(a_pos, z_pos)


class TestFinancialJsonNested(unittest.TestCase):
    def test_nested_dict_has_separator(self):
        data = {
            "2024-Q1": {"revenue": 26044, "net_income": 14881},
            "2024-Q2": {"revenue": 30040, "net_income": 16599},
        }
        result = financial_json_to_markdown_table(data)
        self.assertIn("|---|", result)

    def test_nested_dict_contains_row_keys(self):
        data = {
            "2024-Q1": {"revenue": 26044},
            "2024-Q2": {"revenue": 30040},
        }
        result = financial_json_to_markdown_table(data)
        self.assertIn("2024-Q1", result)
        self.assertIn("2024-Q2", result)

    def test_nested_dict_contains_values(self):
        data = {"2024-Q1": {"revenue": 26044}}
        result = financial_json_to_markdown_table(data)
        self.assertIn("26044", result)


class TestFinancialJsonEdgeCases(unittest.TestCase):
    def test_empty_dict_returns_empty(self):
        self.assertEqual(financial_json_to_markdown_table({}), "")

    def test_determinism(self):
        data = {"z": 3, "a": 1, "m": 2}
        r1 = financial_json_to_markdown_table(data)
        r2 = financial_json_to_markdown_table(data)
        self.assertEqual(r1, r2)


if __name__ == "__main__":
    unittest.main()
