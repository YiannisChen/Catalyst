import type React from 'react';
import { useEffect, useRef, useState, type ReactNode } from 'react';
import { Panel, PanelGroup, PanelResizeHandle, type ImperativePanelHandle } from 'react-resizable-panels';
import { getSidePanelWidth, setSidePanelWidth } from '@/lib/storage';
import { tokens } from '@/styles/tokens';
export type WorkbenchLayoutProps = { chart: ReactNode; news: ReactNode; attribution: ReactNode };
export function WorkbenchLayout({ chart, news, attribution }: WorkbenchLayoutProps): React.JSX.Element {
  const container = useRef<HTMLDivElement>(null);
  const sidePanel = useRef<ImperativePanelHandle>(null);
  const [size, setSize] = useState({ width: 0, height: 0 });
  const [rightPx, setRightPx] = useState(
    () => getSidePanelWidth() ?? Number.parseFloat(tokens['side-panel']),
  );
  useEffect(() => {
    if (!container.current) return;
    const observer = new ResizeObserver(([entry]) => {
      if (entry) setSize({ width: entry.contentRect.width, height: entry.contentRect.height });
    });
    observer.observe(container.current);
    return () => observer.disconnect();
  }, []);
  useEffect(() => {
    if (size.width > 0) sidePanel.current?.resize((Math.max(360, Math.min(640, rightPx)) / size.width) * 100);
  }, [size.width, rightPx]);
  const onHorizontalLayout = (sizes: number[]) => {
    const value = sizes[1];
    if (value != null && size.width > 0) {
      const px = Math.round((size.width * value) / 100);
      setRightPx(px);
      setSidePanelWidth(px);
    }
  };
  return (
    <main className="min-h-0 h-full min-w-0 p-2">
      <div ref={container} className="h-full min-h-0 min-w-0">
        {size.width > 0 && size.height > 0 && (
          <PanelGroup direction="horizontal" onLayout={onHorizontalLayout} className="h-full min-h-0 min-w-0">
            <Panel
              minSize={size.width ? 100 - Math.min(100, (640 / size.width) * 100) : 40}
              className="min-h-0 min-w-0"
            >
              <div className="flex h-full min-h-0 flex-col gap-2 overflow-y-auto">
                <div className="workbench-chart min-h-0">{chart}</div>
                {news}
              </div>
            </Panel>
            <PanelResizeHandle
              aria-label="Resize attribution panel"
              className="group relative w-2 shrink-0 cursor-col-resize"
            >
              <span className="resize-grip-indicator absolute left-1/2 top-1/2 w-1 -translate-x-1/2 -translate-y-1/2 rounded-full bg-line-200 group-hover:bg-ink-300" />
            </PanelResizeHandle>
            <Panel
              ref={sidePanel}
              defaultSize={size.width ? (rightPx / size.width) * 100 : 40}
              minSize={size.width ? Math.min(100, (360 / size.width) * 100) : 30}
              maxSize={size.width ? Math.min(100, (640 / size.width) * 100) : 60}
              className="min-h-0 min-w-0"
            >
              {attribution}
            </Panel>
          </PanelGroup>
        )}
      </div>
    </main>
  );
}
