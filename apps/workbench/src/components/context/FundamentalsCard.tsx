import { useState, useEffect } from 'react';
import { getFundamentals } from '../../api/client';
import type { FundamentalsResponse } from '../../api/client';

interface Props {
  ticker: string;
  tradeDate: string;
}

/** Format large numbers: 416161000000 -> "$416.2B" */
function fmtUsd(raw: string): string {
  const n = Number(raw);
  if (isNaN(n)) return raw;
  if (n === 0) return '$0';
  const abs = Math.abs(n);
  const sign = n < 0 ? '-' : '';
  if (abs >= 1e12) return `${sign}$${(abs / 1e12).toFixed(1)}T`;
  if (abs >= 1e9) return `${sign}$${(abs / 1e9).toFixed(1)}B`;
  if (abs >= 1e6) return `${sign}$${(abs / 1e6).toFixed(1)}M`;
  if (abs >= 1e3) return `${sign}$${(abs / 1e3).toFixed(1)}K`;
  return `${sign}$${abs.toFixed(2)}`;
}

function pctOf(numerator: string, denominator: string): string {
  const n = Number(numerator);
  const d = Number(denominator);
  if (isNaN(n) || isNaN(d) || d === 0) return '--';
  return `${((n / d) * 100).toFixed(1)}%`;
}

const INCOME_ROWS: Array<{ key: string; label: string; fmt: 'usd' | 'raw' }> = [
  { key: 'revenue', label: 'Revenue', fmt: 'usd' },
  { key: 'grossProfit', label: 'Gross Profit', fmt: 'usd' },
  { key: 'operatingIncome', label: 'Operating Income', fmt: 'usd' },
  { key: 'netIncome', label: 'Net Income', fmt: 'usd' },
  { key: 'ebitda', label: 'EBITDA', fmt: 'usd' },
];

const PER_SHARE_ROWS: Array<{ key: string; label: string }> = [
  { key: 'eps', label: 'EPS' },
  { key: 'epsDiluted', label: 'EPS (Diluted)' },
];

const EXTRA_ROWS: Array<{ key: string; label: string; fmt: 'usd' | 'raw' }> = [
  { key: 'researchAndDevelopmentExpenses', label: 'R&D Spend', fmt: 'usd' },
  { key: 'totalAssets', label: 'Total Assets', fmt: 'usd' },
  { key: 'totalLiabilities', label: 'Total Liabilities', fmt: 'usd' },
  { key: 'totalStockholdersEquity', label: 'Equity', fmt: 'usd' },
  { key: 'freeCashFlow', label: 'Free Cash Flow', fmt: 'usd' },
  { key: 'operatingCashFlow', label: 'Operating CF', fmt: 'usd' },
];

export default function FundamentalsCard({ ticker, tradeDate }: Props) {
  const [data, setData] = useState<FundamentalsResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [expanded, setExpanded] = useState(false);

  useEffect(() => {
    if (!ticker || !tradeDate) return;
    setLoading(true);
    setError(null);

    getFundamentals(ticker, tradeDate)
      .then((res) => {
        setData(res);
        setExpanded(false);
      })
      .catch((err) => setError(err.message))
      .finally(() => setLoading(false));
  }, [ticker, tradeDate]);

  if (loading) {
    return (
      <div className="fc">
        <div className="fc-state">
          <span className="loading-pulse" />
          Loading fundamentals...
        </div>
      </div>
    );
  }

  if (error) {
    return (
      <div className="fc">
        <div className="fc-state fc-error">Failed to load: {error}</div>
      </div>
    );
  }

  if (!data || Object.keys(data.metrics).length === 0) {
    return (
      <div className="fc">
        <div className="fc-state">No fundamentals data for {ticker}</div>
      </div>
    );
  }

  const m = data.metrics;
  const grossMargin = m.grossProfit && m.revenue ? pctOf(m.grossProfit, m.revenue) : null;
  const opMargin = m.operatingIncome && m.revenue ? pctOf(m.operatingIncome, m.revenue) : null;
  const netMargin = m.netIncome && m.revenue ? pctOf(m.netIncome, m.revenue) : null;

  const renderRow = (label: string, value: string) => (
    <div className="fc-row" key={label}>
      <span className="fc-row-label">{label}</span>
      <span className="fc-row-value">{value}</span>
    </div>
  );

  return (
    <div className="fc">
      {/* Header */}
      <div className="fc-header">
        <span className="fc-ticker">{ticker} Financials</span>
        <span className="fc-period">
          {m.period || 'FY'} {m.fiscalYear || ''} · {m.filingDate || '--'}
        </span>
      </div>

      {/* Margin badges */}
      {(grossMargin || opMargin || netMargin) && (
        <div className="fc-margins">
          {grossMargin && (
            <div className="fc-margin-badge">
              <span className="fc-margin-label">Gross</span>
              <span className="fc-margin-val">{grossMargin}</span>
            </div>
          )}
          {opMargin && (
            <div className="fc-margin-badge">
              <span className="fc-margin-label">Op.</span>
              <span className="fc-margin-val">{opMargin}</span>
            </div>
          )}
          {netMargin && (
            <div className="fc-margin-badge">
              <span className="fc-margin-label">Net</span>
              <span className="fc-margin-val">{netMargin}</span>
            </div>
          )}
        </div>
      )}

      {/* Income statement rows */}
      <div className="fc-section">
        <div className="fc-section-title">Income Statement</div>
        {INCOME_ROWS.map(({ key, label, fmt }) => {
          const raw = m[key];
          if (!raw && raw !== '0') return null;
          return renderRow(label, fmt === 'usd' ? fmtUsd(raw) : `$${raw}`);
        })}
      </div>

      {/* Per-share */}
      <div className="fc-section">
        <div className="fc-section-title">Per Share</div>
        {PER_SHARE_ROWS.map(({ key, label }) => {
          const raw = m[key];
          if (!raw && raw !== '0') return null;
          return renderRow(label, `$${raw}`);
        })}
      </div>

      {/* Extra rows (expandable) */}
      <button className="fc-expand" onClick={() => setExpanded(!expanded)}>
        {expanded ? 'Less' : `More (${Object.keys(m).length} fields)`}
      </button>

      {expanded && (
        <div className="fc-section">
          {EXTRA_ROWS.map(({ key, label, fmt }) => {
            const raw = m[key];
            if (!raw && raw !== '0') return null;
            return renderRow(label, fmt === 'usd' ? fmtUsd(raw) : raw);
          })}
        </div>
      )}
    </div>
  );
}
