import type React from 'react';
import { useState } from 'react';
import { ArrowRight, Plus } from 'lucide-react';
import { Button, Input, Badge, Chip, Tabs, Panel, Skeleton } from '@/components/ui';
import { ExampleSection } from './ExampleSection';

export function PrimitivesSection(): React.JSX.Element {
  const [tab, setTab] = useState('answer');
  return (
    <>
      <ExampleSection label="Controls" name="Button">
        <div className="flex flex-wrap gap-2">
          {(['primary', 'secondary', 'ghost', 'danger'] as const).flatMap((variant) =>
            (['sm', 'md'] as const).flatMap((size) => [
              <Button key={`${variant}-${size}`} variant={variant} size={size}>
                {variant} {size}
              </Button>,
              <Button
                key={`${variant}-${size}-icon`}
                variant={variant}
                size={size}
                icon={<Plus size={16} strokeWidth={1.5} />}
              >
                With icon
              </Button>,
              <Button key={`${variant}-${size}-disabled`} variant={variant} size={size} disabled>
                Disabled
              </Button>,
            ]),
          )}
        </div>
      </ExampleSection>
      <ExampleSection label="Controls" name="Input">
        <div className="grid max-w-4xl grid-cols-2 gap-3">
          <Input placeholder="Search tickers" />
          <Input mono placeholder="AAPL" />
          <Input prefix={<span className="font-numeric">$</span>} placeholder="Price" />
          <Input suffix={<span className="type-caption">ET</span>} placeholder="Time" />
        </div>
      </ExampleSection>
      <ExampleSection label="Status" name="Badge">
        <div className="flex flex-wrap gap-2">
          {(['neutral', 'accent', 'up', 'down', 'info', 'warn'] as const).flatMap((tone) => [
            <Badge key={`${tone}-plain`} tone={tone}>
              {tone}
            </Badge>,
            <Badge key={`${tone}-dot`} tone={tone} dot>
              {tone}
            </Badge>,
          ])}
        </div>
      </ExampleSection>
      <ExampleSection label="Filters" name="Chip">
        <div className="flex flex-wrap gap-2">
          <Chip label="Earnings" />
          <Chip label="Analyst" selected />
          <Chip label="Deals" count={12} />
          <Chip label="Product" count={3} selected />
        </div>
      </ExampleSection>
      <ExampleSection label="Navigation" name="Tabs">
        <div className="max-w-xl">
          <Tabs
            value={tab}
            onChange={setTab}
            items={[
              { id: 'answer', label: 'Answer' },
              { id: 'relative', label: 'Relative', count: 4 },
              { id: 'process', label: 'Process' },
              { id: 'run', label: 'Run' },
            ]}
          />
          <div
            id={`tabpanel-${tab}`}
            role="tabpanel"
            aria-labelledby={`tab-${tab}`}
            className="type-body-sm mt-3 rounded-md border border-line-100 bg-bg-100 p-3"
          >
            Selected panel: {tab}
          </div>
        </div>
      </ExampleSection>
      <ExampleSection label="Containers" name="Panel">
        <div className="gallery-panel-grid grid grid-cols-2 gap-3">
          <Panel
            title="Panel with actions"
            actions={[
              <Button
                key="refresh"
                variant="ghost"
                size="sm"
                icon={<ArrowRight size={16} strokeWidth={1.5} />}
              >
                Open
              </Button>,
            ]}
          >
            <p className="type-body-sm m-0 text-ink-200">Scrollable panel content.</p>
          </Panel>
          <Panel title="Flush panel" flush>
            <div className="h-full bg-bg-200 p-3 text-body-sm">Flush body content</div>
          </Panel>
        </div>
      </ExampleSection>
      <ExampleSection label="Loading" name="Skeleton">
        <div className="flex max-w-xl flex-col gap-3 rounded-md border border-line-100 bg-bg-100 p-3">
          <Skeleton width="45%" height={16} />
          <Skeleton height={12} />
          <Skeleton width="72%" height={12} />
          <Skeleton width={96} height={28} />
        </div>
      </ExampleSection>
    </>
  );
}
