import type { JSX } from 'react';
import { Badge, Chip, Panel } from '@/components/ui';
import { ExampleSection } from './ExampleSection';
import featuredFixture from '../../../../../contracts/fixtures/page-featured-list.json';
import type { components } from '@/api/schema';

type Item = components['schemas']['FeaturedItemDTO'];
const items = (featuredFixture.response as { items: Item[] }).items;
function label(outcome: string | null | undefined): { text: string; tone: 'up' | 'warn' | 'neutral' } {
  if (outcome === 'ANSWERED') return { text: 'Answered', tone: 'up' };
  if (outcome === 'ABSTAINED') return { text: 'No clear cause', tone: 'warn' };
  return { text: 'Tier only', tone: 'neutral' };
}
function Cards({ values }: { values: Item[] }): JSX.Element {
  return values.length ? (
    <div className="grid grid-cols-1 gap-3 md:grid-cols-2 xl:grid-cols-3">
      {values.map((item) => {
        const result = label(item.outcome);
        return (
          <article
            key={item.case_id}
            className="flex flex-col gap-3 rounded-md border border-line-100 bg-bg-100 p-4"
          >
            <div className="flex items-center justify-between gap-2">
              <span className="font-numeric text-body">
                {item.ticker} · {item.session_date}
              </span>
              <Badge tone={result.tone}>{result.text}</Badge>
            </div>
            <div>
              <h3 className="type-heading m-0">{item.title}</h3>
              <p className="type-body-sm m-0 text-ink-200">{item.summary}</p>
            </div>
            <div className="flex flex-wrap gap-2">
              {item.tags.map((tag) => (
                <Chip key={tag} label={tag} />
              ))}
            </div>
          </article>
        );
      })}
    </div>
  ) : (
    <div className="rounded-md border border-line-100 bg-bg-100 p-6">
      <p className="type-body m-0">No featured cases yet.</p>
      <p className="type-caption m-0 text-ink-300">Saved runs appear here once they are published.</p>
    </div>
  );
}
export function FeaturedSection(): JSX.Element {
  return (
    <ExampleSection label="Featured" name="Featured case grid">
      <Panel title="Cases">
        <Cards values={items} />
      </Panel>
      <Panel title="Empty">
        <Cards values={[]} />
      </Panel>
      <p className="type-caption m-0 text-ink-300">Synthetic · events-t3-now / page-a-answered-now-b5</p>
    </ExampleSection>
  );
}
