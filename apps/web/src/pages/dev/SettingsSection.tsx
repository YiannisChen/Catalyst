import type React from 'react';
import { useState } from 'react';
import type { RunDefaults } from '@/lib/storage';
import type { components } from '@/api/schema';
import { AccessSection } from '@/features/settings/AccessSection';
import { DataUpdatesSection } from '@/features/settings/DataUpdatesSection';
import { ModelsSection } from '@/features/settings/ModelsSection';
import { RunDefaultsSection } from '@/features/settings/RunDefaultsSection';
import { ExampleSection } from './ExampleSection';
import quotaFixture from '../../../../../contracts/fixtures/page-quota-demo.json';
import dataJobFixture from '../../../../../contracts/fixtures/page-data-job-latest.json';

const quota = quotaFixture.response as unknown as components['schemas']['QuotaResponse'];
const job = dataJobFixture.response as unknown as components['schemas']['LatestJobResponse'];

export function SettingsSection(): React.JSX.Element {
  const [defaults, setDefaults] = useState<RunDefaults>({
    preferred: { provider: 'deepseek', modelId: 'deepseek-v4-flash' },
    costCapUsd: 0.2,
    askBeforeEachRun: true,
  });

  return (
    <ExampleSection label="Settings" name="Settings sections">
      <div className="flex flex-col gap-4">
        <AccessSection
          adminTokenSet={false}
          adminTokenEnabled
          onSaveAdminToken={() => undefined}
          onRemoveAdminToken={() => undefined}
        />
        <ModelsSection />
        <RunDefaultsSection value={defaults} onChange={setDefaults} />
        <DataUpdatesSection job={job} />
      </div>
    </ExampleSection>
  );
}
