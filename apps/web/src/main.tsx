import React from 'react';
import { createRoot } from 'react-dom/client';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { createBrowserRouter, RouterProvider } from 'react-router';
import '@fontsource-variable/geist';
import '@fontsource-variable/geist-mono';
import '@/styles/app.css';
import { AppShell } from '@/app/AppShell';
import Workbench from '@/pages/Workbench';
import NotFound from '@/pages/NotFound';
import RouteError from '@/app/RouteError';
import { installNoPinchZoom } from '@/lib/noPinchZoom';

installNoPinchZoom();

// The workbench is the home page and ships in the main bundle; other pages load when first opened.
// /dev pulls in every fixture for the gallery, so it must stay out of the main bundle.
const page = (load: () => Promise<{ default: React.ComponentType }>) => async () => ({
  Component: (await load()).default,
});

const queryClient = new QueryClient({
  defaultOptions: { queries: { retry: 1, refetchOnWindowFocus: false } },
});
const router = createBrowserRouter([
  {
    path: '/',
    element: <AppShell />,
    errorElement: <RouteError />,
    children: [
      { index: true, element: <Workbench /> },
      { path: 'featured', lazy: page(() => import('@/pages/Featured')) },
      { path: 'history', lazy: page(() => import('@/pages/History')) },
      { path: 'data', lazy: page(() => import('@/pages/Data')) },
      { path: 'settings', lazy: page(() => import('@/pages/Settings')) },
      ...(import.meta.env.DEV ? [{ path: 'dev', lazy: page(() => import('@/pages/Dev')) }] : []),
      { path: '*', element: <NotFound /> },
    ],
  },
]);
const root = createRoot(document.getElementById('root')!);
async function start(): Promise<void> {
  if (import.meta.env.VITE_MOCK === '1') {
    const badge = document.createElement('div');
    badge.className =
      'fixed bottom-2 right-2 z-50 rounded-sm border border-warn bg-bg-200 px-2 py-1 font-mono text-caption text-warn';
    try {
      const { worker } = await import('@/mocks/browser');
      await worker.start({ onUnhandledRequest: 'bypass' });
      const { getMockScenario } = await import('@/mocks/fixtures');
      badge.textContent = `MOCK · ${getMockScenario()}`;
    } catch (error) {
      // Some embedded browsers block service workers; render against the real API instead of a blank page.
      console.error('Mock service worker failed to start', error);
      badge.textContent = 'MOCK FAILED · using the real API';
    }
    document.body.append(badge);
  }
  root.render(
    <React.StrictMode>
      <QueryClientProvider client={queryClient}>
        <RouterProvider router={router} />
      </QueryClientProvider>
    </React.StrictMode>,
  );
}
void start();
