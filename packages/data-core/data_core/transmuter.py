from __future__ import annotations

from bs4 import BeautifulSoup, Comment

EXCLUDED_TAGS = frozenset({
    "nav", "footer", "script", "style", "aside",
    "form", "iframe", "noscript",
})


def clean_html_to_markdown(raw_html: str, base_url: str = "") -> str:
    """Remove noise tags from HTML, extract text, build numbered evidence chain."""
    if not raw_html or not raw_html.strip():
        return ""

    soup = BeautifulSoup(raw_html, "html.parser")

    for comment in soup.find_all(string=lambda t: isinstance(t, Comment)):
        comment.extract()

    for tag_name in EXCLUDED_TAGS:
        for tag in soup.find_all(tag_name):
            tag.decompose()

    links: list[tuple[str, str]] = []
    for a_tag in soup.find_all("a", href=True):
        href = a_tag["href"]
        text = a_tag.get_text(strip=True)
        if href.startswith(("http://", "https://")):
            links.append((text, href))

    body_text = soup.get_text(separator="\n")
    lines = [line.strip() for line in body_text.splitlines()]
    lines = [line for line in lines if line]
    clean_text = "\n".join(lines)

    if not links:
        return clean_text

    seen: dict[str, int] = {}
    counter = 1
    ref_lines: list[str] = []
    for display_text, url in links:
        if url not in seen:
            seen[url] = counter
            desc = f": {display_text}" if display_text else ""
            ref_lines.append(f"[{counter}] {url}{desc}")
            counter += 1

    return clean_text + "\n\n## References\n\n" + "\n".join(ref_lines)
