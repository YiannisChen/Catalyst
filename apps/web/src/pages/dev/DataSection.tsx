import type { JSX } from 'react';
import type { components } from '@/api/schema';
import { CoverageGrid } from '@/features/data/CoverageGrid';
import { DataSummary } from '@/features/data/DataSummary';
import { FailedItems } from '@/features/data/FailedItems';
import { buildCoverage } from '@/features/data/coverage';
import { StageList } from '@/features/data/StageList';
import { StoresTable } from '@/features/data/StoresTable';
import { ExampleSection } from './ExampleSection';
import overviewFixture from '../../../../../contracts/fixtures/page-data-overview.json';
import jobFixture from '../../../../../contracts/fixtures/page-data-job-latest.json';

type Overview = components['schemas']['DataOverviewResponse'];
type Job = components['schemas']['LatestJobResponse'];
const overview = overviewFixture.response as Overview;
const job = jobFixture.response as Job;

export function DataSection(): JSX.Element {
  const tickers = [...new Set(overview.coverage.flatMap((session) => session.tickers_with_news))];
  return (
    <ExampleSection label="Data" name="Data center">
      <p className="type-caption m-0 text-ink-300">Synthetic overview fixture · real latest job fixture</p>
      <DataSummary overview={overview} job={job} tickerCount={tickers.length} />
      <div className="grid gap-2 xl:grid-cols-2">
        <StageList job={job} />
        <FailedItems items={job.failures} />
      </div>
      <CoverageGrid rows={buildCoverage(overview.coverage, tickers)} />
      <StoresTable stores={overview.stores} searchIndex={overview.search_index} />
    </ExampleSection>
  );
}
