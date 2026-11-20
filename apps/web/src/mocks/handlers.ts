import { delay, http, HttpResponse, passthrough, type JsonBodyType } from 'msw';
import { fixtureRoutes, getMockScenario, selectFixture, sseFixtures } from './fixtures';

const apiPaths = [...fixtureRoutes.keys()].map((key) => {
  const split = key.indexOf(' ');
  return { method: key.slice(0, split), route: key.slice(split + 1) };
});
const routeHandlers = apiPaths.map(({ method, route }) => {
  const path = route.replace(/\{([^}]+)\}/g, ':$1');
  const handler = async ({
    request,
    params,
  }: {
    request: Request;
    params: Record<string, string | readonly string[] | undefined>;
  }) => {
    const query = Object.fromEntries(new URL(request.url).searchParams.entries());
    const body =
      request.method === 'GET' || request.method === 'HEAD'
        ? {}
        : await request
            .clone()
            .json()
            .catch(() => ({}));
    const requestParams = { ...query, ...params, ...(body && typeof body === 'object' ? body : {}) };
    const fixture = selectFixture(method, route, requestParams);
    return fixture
      ? HttpResponse.json(fixture.response as JsonBodyType, { status: 200 })
      : import.meta.env.VITE_MOCK_PASSTHROUGH === '1'
        ? passthrough()
        : HttpResponse.json({ detail: 'no_fixture' }, { status: 501 });
  };
  if (method === 'GET') return http.get(path, handler);
  if (method === 'POST') return http.post(path, handler);
  if (method === 'PUT') return http.put(path, handler);
  if (method === 'PATCH') return http.patch(path, handler);
  return http.delete(path, handler);
});
const sseRoute = '/api/live-runs/{run_id}/stream';
const eventHandlers = sseFixtures.length
  ? [
      http.get(sseRoute.replace(/\{([^}]+)\}/g, ':$1'), async ({ params }) => {
        const runId = String(params.run_id ?? '');
        const candidates = sseFixtures.filter(({ fixture }) => fixture.request.params?.run_id === runId);
        const chosen =
          candidates.find(
            ({ path }) =>
              path
                .split('/')
                .pop()
                ?.replace(/\.json$/, '') === getMockScenario(),
          ) ?? candidates[0];
        if (!chosen) return HttpResponse.json({ detail: 'no_fixture' }, { status: 501 });
        const encoder = new TextEncoder();
        const lines = chosen.events.split(/\r?\n/).filter(Boolean);
        const stream = new ReadableStream<Uint8Array>({
          async start(controller) {
            for (const line of lines) {
              const event = JSON.parse(line) as { run_id: string; sequence: number; event_type: string };
              controller.enqueue(
                encoder.encode(
                  `id: ${event.run_id}:${event.sequence}\nevent: ${event.event_type}\ndata: ${line}\n\n`,
                ),
              );
              await delay(250);
            }
            controller.close();
          },
        });
        return new HttpResponse(stream, {
          headers: { 'Content-Type': 'text/event-stream', 'Cache-Control': 'no-cache' },
        });
      }),
    ]
  : [];
const unknownApi = http.all(/\/api\/.*/, () =>
  import.meta.env.VITE_MOCK_PASSTHROUGH === '1'
    ? passthrough()
    : HttpResponse.json({ detail: 'no_fixture' }, { status: 501 }),
);
// The recorded event stream must win over the JSON manifest registered for the same route.
export const handlers = [...eventHandlers, ...routeHandlers, unknownApi];
