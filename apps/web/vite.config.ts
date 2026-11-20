import { fileURLToPath, URL } from 'node:url';
import react from '@vitejs/plugin-react';
import tailwindcss from '@tailwindcss/vite';
import { defineConfig, loadEnv, searchForWorkspaceRoot } from 'vite';

export default defineConfig(({ mode, command }) => {
  const env = loadEnv(mode, fileURLToPath(new URL('.', import.meta.url)), '');
  if (
    command === 'build' &&
    mode === 'production' &&
    (process.env.CATALYST_PUBLIC_BUILD ?? env.CATALYST_PUBLIC_BUILD) === '1' &&
    !(process.env.VITE_TURNSTILE_SITE_KEY ?? env.VITE_TURNSTILE_SITE_KEY)
  ) {
    throw new Error('CATALYST_PUBLIC_BUILD=1 needs VITE_TURNSTILE_SITE_KEY');
  }
  return {
    plugins: [react(), tailwindcss()],
    resolve: {
      alias: { '@': fileURLToPath(new URL('./src', import.meta.url)) },
      dedupe: ['react', 'react-dom'],
    },
    build: {
      rollupOptions: {
        output: {
          // Libraries change less often than app code; separate chunks keep them cached across deploys.
          manualChunks: {
            react: ['react', 'react-dom', 'react-router'],
            charts: ['lightweight-charts'],
            query: ['@tanstack/react-query', 'openapi-fetch', '@microsoft/fetch-event-source'],
            ui: ['cmdk', '@radix-ui/react-dialog', 'react-resizable-panels'],
          },
        },
      },
    },
    optimizeDeps: {
      include: ['cmdk', 'lightweight-charts', 'react-resizable-panels'],
    },
    server: {
      fs: {
        allow: [
          searchForWorkspaceRoot(process.cwd()),
          fileURLToPath(new URL('../../contracts', import.meta.url)),
        ],
      },
      proxy: {
        '/api': {
          target: process.env.VITE_API_TARGET ?? 'http://127.0.0.1:8000',
          changeOrigin: true,
          // Answer 502 when the API is down, as nginx does in production, so the app's
          // "Can't reach the server" banner behaves the same in development.
          configure: (proxy) => {
            // The API's loopback guard accepts only port-less localhost origins; in production the
            // page and API share one origin. Drop the dev server's origin so POSTs are not refused.
            proxy.on('proxyReq', (proxyRequest) => proxyRequest.removeHeader('origin'));
            proxy.on('error', (_error, _request, response) => {
              if ('writeHead' in response && !response.headersSent) {
                response.writeHead(502, { 'content-type': 'application/json' });
                response.end(JSON.stringify({ detail: 'api_unreachable' }));
              }
            });
          },
        },
      },
    },
  };
});
