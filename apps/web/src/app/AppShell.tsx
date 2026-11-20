import type React from 'react';
import { useEffect, useState } from 'react';
import { Settings } from 'lucide-react';
import { NavLink, Outlet, useLocation, useNavigate } from 'react-router';
import { LogoMark } from '@/components/catalyst/Logo';
import { useOptions, useRecentRuns } from '@/api/queries';
import { PageControlsProvider, PageControlsSlot } from './PageControls';
import { useServerReachability } from './useServerReachability';
import { StateNotice } from '@/components/catalyst';
import { TopBarControls } from '@/features/workbench/TopBarControls';
import { TickerPalette } from '@/features/workbench/TickerPalette';
import { useRangeLocal } from '@/api/queries';
import { ADMIN_TOKEN_REJECTED_NOTICE } from '@/lib/storage';

const navItems = [
  { to: '/', label: 'Workbench', end: true },
  { to: '/featured', label: 'Featured' },
  { to: '/history', label: 'History' },
  { to: '/data', label: 'Data' },
] as const;
function TopBar(): React.JSX.Element {
  const options = useOptions();
  const recentRuns = useRecentRuns(5);
  const range = useRangeLocal();
  const navigate = useNavigate();
  const location = useLocation();
  const [paletteOpen, setPaletteOpen] = useState(false);
  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (location.pathname !== '/' && (event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'k') {
        event.preventDefault();
        setPaletteOpen(true);
      }
    };
    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
  }, [location.pathname]);
  const selectTicker = (ticker: string) => {
    navigate(`/?ticker=${encodeURIComponent(ticker)}`);
    setPaletteOpen(false);
  };
  const selectRun = (run: { ticker?: string | null; session_date?: string | null; run_id: string }) => {
    if (!run.ticker || !run.session_date) return;
    navigate(`/?ticker=${encodeURIComponent(run.ticker)}&date=${run.session_date}&run=${run.run_id}`);
    setPaletteOpen(false);
  };
  return (
    <header className="flex min-w-0 items-center gap-4 border-b border-line-100 bg-bg-000 px-4">
      <NavLink to="/" className="flex shrink-0 items-center gap-2 text-ink-100 no-underline">
        <LogoMark size={22} />
        <span className="type-heading">Catalyst</span>
      </NavLink>
      <nav aria-label="Pages" className="flex h-full shrink-0 items-stretch gap-1">
        {navItems.map((item) => (
          <NavLink
            key={item.to}
            to={item.to}
            end={'end' in item ? item.end : false}
            className={({ isActive }) =>
              `nav-tab inline-flex items-center px-3 text-body-sm font-medium no-underline ${isActive ? 'text-ink-100' : 'text-ink-300 hover:text-ink-100'}`
            }
          >
            {item.label}
            {item.label === 'Data' && options.data?.status === 'stale' && (
              <span
                aria-hidden="true"
                className="status-dot rounded-full bg-warn"
                title={`Data through ${options.data.last_trading_day ?? '—'} · the update for newer sessions has not finished. Open Data to see its progress.`}
              />
            )}
            {item.label === 'Data' && options.data?.status === 'unavailable' && (
              <span aria-hidden="true" className="status-dot rounded-full bg-warn" title="Data unavailable" />
            )}
          </NavLink>
        ))}
      </nav>
      <span aria-hidden="true" className="topbar-divider w-px bg-line-100" />
      <div aria-label="Page controls" className="min-w-fit flex-1">
        <PageControlsSlot />
        {location.pathname !== '/' && <TopBarControls onOpenPalette={() => setPaletteOpen(true)} />}
      </div>
      <NavLink
        to="/settings"
        aria-label="Settings"
        className="settings-button inline-flex shrink-0 items-center justify-center rounded-md border border-transparent bg-transparent text-ink-200 hover:bg-bg-200"
      >
        <Settings size={16} strokeWidth={1.5} />
      </NavLink>
      <TickerPalette
        open={paletteOpen}
        onOpenChange={setPaletteOpen}
        tickers={options.data?.tickers ?? []}
        pricedTickers={range.data?.tickers}
        recentRuns={recentRuns.data?.items ?? []}
        onSelectTicker={selectTicker}
        onSelectRun={selectRun}
      />
    </header>
  );
}
export function AppShell(): React.JSX.Element {
  const reachability = useServerReachability();
  const [adminNotice, setAdminNotice] = useState<string | null>(null);
  useEffect(() => {
    const rejected = () => setAdminNotice(ADMIN_TOKEN_REJECTED_NOTICE);
    window.addEventListener('catalyst:admin-token-rejected', rejected);
    return () => window.removeEventListener('catalyst:admin-token-rejected', rejected);
  }, []);
  return (
    <PageControlsProvider>
      <div className="app-shell relative grid h-dvh overflow-hidden bg-bg-000">
        <TopBar />
        {adminNotice && (
          <div className="border-b border-line-100 px-4 py-2">
            <StateNotice tone="warn" title="Admin token removed" body={adminNotice} />
          </div>
        )}
        {reachability.unreachable ? (
          <div className="border-b border-line-100 px-4 py-2">
            <StateNotice
              tone="down"
              title="Can't reach the server"
              body="The last request failed. Catalyst retries every 10 s; your inputs are kept."
              action={{ label: 'Retry now', onClick: reachability.retry }}
            />
          </div>
        ) : (
          // An empty (not display:none) slot keeps the page in the last grid row.
          <div />
        )}
        <Outlet />
      </div>
    </PageControlsProvider>
  );
}
