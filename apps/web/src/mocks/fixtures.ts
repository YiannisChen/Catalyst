export interface FixtureRequest {
  kind?: string;
  method: string;
  route: string;
  params?: Record<string, unknown>;
  body?: unknown;
}
export interface Fixture {
  request: FixtureRequest;
  response?: unknown;
  response_file?: string;
  source: unknown;
  state: string;
}
const jsonModules = import.meta.glob<Fixture>('../../../../contracts/fixtures/*.json', {
  eager: true,
  import: 'default',
});
const eventModules = import.meta.glob<string>('../../../../contracts/fixtures/*.jsonl', {
  eager: true,
  import: 'default',
  query: '?raw',
});
const eventMetadata = Object.entries(jsonModules).filter(
  ([, fixture]) => fixture.request.kind === 'sse-events',
);
export const fixtures = Object.entries(jsonModules)
  .filter(([, fixture]) => fixture.request.kind !== 'sse-events')
  .map(([path, fixture]) => ({ path, fixture }));
export const fixtureRoutes = new Map<string, Array<{ path: string; fixture: Fixture }>>();
for (const entry of fixtures) {
  const key = `${entry.fixture.request.method.toUpperCase()} ${entry.fixture.request.route}`;
  fixtureRoutes.set(key, [...(fixtureRoutes.get(key) ?? []), entry]);
}
export const sseFixtures = eventMetadata.map(([path, fixture]) => ({
  path,
  fixture,
  events: eventModules[`../../../../contracts/fixtures/${fixture.response_file ?? ''}`] ?? '',
}));

function scenarioFromUrl(): string | null {
  try {
    return new URL(window.location.href).searchParams.get('scenario');
  } catch {
    return null;
  }
}
export function getMockScenario(): string {
  try {
    const fromUrl = scenarioFromUrl();
    if (fromUrl) {
      sessionStorage.setItem('catalyst.mockScenario', fromUrl);
      return fromUrl;
    }
    const chosen = sessionStorage.getItem('catalyst.mockScenario');
    if (chosen) return chosen;
  } catch {
    /* Session storage can be disabled. */
  }
  return 'default';
}
export function selectFixture(
  method: string,
  route: string,
  params: Record<string, unknown> = {},
): Fixture | undefined {
  const candidates = fixtureRoutes.get(`${method.toUpperCase()} ${route}`) ?? [];
  const matching = candidates.filter(({ fixture }) => {
    const expected = Object.entries(fixture.request.params ?? {});
    return expected.length > 0 && expected.every(([key, value]) => params[key] === value);
  });
  const available = matching.length ? matching : candidates;
  const scenario = getMockScenario();
  const scenarioMatch = available.find(
    ({ path }) =>
      path
        .split('/')
        .pop()
        ?.replace(/\.json$/, '') === scenario,
  );
  return (scenarioMatch ?? available[0])?.fixture;
}
