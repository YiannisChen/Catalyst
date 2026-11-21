# Catalyst diagrams

`catalyst-framework.svg` is the README's main figure. It tells one analysis as a
small newsroom:

- **Data**: market prices, peer and sector moves, news, and SEC filings.
- **AI research desk**: a news researcher names a reason and quotes the article;
  a fact checker gives a second opinion on whether the article explains the move.
- **Editor**: fixed rules in code, no AI. A reason is published only if the quote
  is in the article, the article came out before the move, it is about this
  company, and the stock beat its peers.
- **Answer**: the worked example is a real featured case (Oracle, 2025-12-19,
  Confirmed). The candles are ORCL daily bars from the served market store.

The SVG is generated. Edit the source and rebuild:

```sh
python docs/diagrams/source/build_framework.py
```

## Social preview

`social-preview.png` (1280×640) is the repository's social preview, uploaded in
GitHub under Settings → General → Social preview. It is rendered from
`social-preview.svg`, which `source/build_social_preview.py` generates with the
same palette and the same ORCL case.

## Logos

The source logos in `source/logos/` only identify where Catalyst's data comes
from. Each mark belongs to its owner.

| File | Source |
|---|---|
| `yahoo-finance.png` | Wikimedia Commons, "Yahoo! Finance logo.svg" |
| `cnbc.png` | Wikimedia Commons, "CNBC logo.svg" |
| `benzinga.png` | Wikimedia Commons, "Benzinga Wordmark.png" |
| `finnhub.png` | finnhub.io site logo |
| `sec-seal.png` | Wikimedia Commons, "Seal of the United States Securities and Exchange Commission.svg" (U.S. government work) |
