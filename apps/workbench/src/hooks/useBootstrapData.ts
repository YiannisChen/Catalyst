import { useEffect, useState } from 'react';

import { getRangeLocal, getRuntimeHealth, getTickers } from '../api/client';
import type { RangeLocalResponse, RuntimeHealthResponse, TickersResponse } from '../api/types';

export interface BootstrapData {
  tickers: TickersResponse;
  range: RangeLocalResponse;
  health: RuntimeHealthResponse;
}

export type BootstrapState =
  | { status: 'loading' }
  | { status: 'empty'; data: BootstrapData }
  | { status: 'ready'; data: BootstrapData }
  | { status: 'error'; message: string };

export function useBootstrapData(): BootstrapState {
  const [state, setState] = useState<BootstrapState>({ status: 'loading' });

  useEffect(() => {
    let isActive = true;

    async function loadBootstrapData() {
      setState({ status: 'loading' });

      try {
        const [tickers, range, health] = await Promise.all([
          getTickers(),
          getRangeLocal(),
          getRuntimeHealth(),
        ]);
        const data = { tickers, range, health };

        if (!isActive) return;

        setState(tickers.symbols.length === 0 ? { status: 'empty', data } : { status: 'ready', data });
      } catch (error) {
        if (!isActive) return;

        setState({
          status: 'error',
          message: error instanceof Error ? error.message : 'Unable to load workbench context.',
        });
      }
    }

    void loadBootstrapData();

    return () => {
      isActive = false;
    };
  }, []);

  return state;
}
