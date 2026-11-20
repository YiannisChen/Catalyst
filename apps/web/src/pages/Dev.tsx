import type React from 'react';
import { PrimitivesSection } from './dev/PrimitivesSection';
import { CatalystSection } from './dev/CatalystSection';
import { ResearchSection } from './dev/ResearchSection';
import { WorkbenchSection } from './dev/WorkbenchSection';
import { AnswerSection } from './dev/AnswerSection';
import { SettingsSection } from './dev/SettingsSection';
import { HistorySection } from './dev/HistorySection';
import { FeaturedSection } from './dev/FeaturedSection';
import { DataSection } from './dev/DataSection';

export default function Dev(): React.JSX.Element {
  return (
    <main className="mx-auto flex h-full w-full max-w-7xl flex-col gap-6 overflow-auto p-6">
      <header className="flex flex-col gap-1">
        <div className="type-label text-accent">Development</div>
        <h1 className="type-display m-0">Component gallery</h1>
        <p className="type-body m-0 text-ink-200">
          Controls and research components with their available states.
        </p>
      </header>
      <PrimitivesSection />
      <CatalystSection />
      <ResearchSection />
      <WorkbenchSection />
      <AnswerSection />
      <SettingsSection />
      <HistorySection />
      <FeaturedSection />
      <DataSection />
    </main>
  );
}
