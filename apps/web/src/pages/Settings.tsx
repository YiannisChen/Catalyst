import type React from 'react';
import { useEffect, useState } from 'react';
import { useSearchParams } from 'react-router';
import { useLatestDataJob } from '@/api/queries';
import { AccessSection } from '@/features/settings/AccessSection';
import { DataUpdatesSection } from '@/features/settings/DataUpdatesSection';
import { ModelsSection } from '@/features/settings/ModelsSection';
import { RunDefaultsSection } from '@/features/settings/RunDefaultsSection';
import { SettingsNav } from '@/features/settings/SettingsNav';
import { getAdminToken, removeAdminToken, setAdminToken } from '@/lib/storage';
import { useAnalysisModel } from '@/lib/useAnalysisModel';

export default function Settings(): React.JSX.Element {
  const dataJob = useLatestDataJob();
  const { defaults, updateDefaults } = useAnalysisModel();
  const [adminTokenSet, setAdminTokenSet] = useState(() => Boolean(getAdminToken()));
  const [params] = useSearchParams();
  useEffect(() => {
    const rejected = () => setAdminTokenSet(false);
    window.addEventListener('catalyst:admin-token-rejected', rejected);
    return () => window.removeEventListener('catalyst:admin-token-rejected', rejected);
  }, []);

  return (
    <main className="settings-layout mx-auto grid h-full w-full max-w-6xl content-start gap-6 overflow-auto px-5 py-6 lg:gap-7">
      <SettingsNav />
      <div className="flex min-w-0 flex-col gap-4">
        <ModelsSection />
        <RunDefaultsSection value={defaults} onChange={updateDefaults} />
        <AccessSection
          adminTokenSet={adminTokenSet}
          adminTokenEnabled={adminTokenSet || params.get('admin') === '1'}
          onSaveAdminToken={(value) => {
            setAdminToken(value);
            setAdminTokenSet(Boolean(value));
          }}
          onRemoveAdminToken={() => {
            removeAdminToken();
            setAdminTokenSet(false);
          }}
        />
        <DataUpdatesSection job={dataJob.data} loading={dataJob.isLoading} error={dataJob.isError} />
      </div>
    </main>
  );
}
