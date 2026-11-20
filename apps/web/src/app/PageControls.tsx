import { createContext, useContext, useState, type ReactNode } from 'react';
const PageControlsContext = createContext<{
  controls: ReactNode;
  setControls: (value: ReactNode) => void;
} | null>(null);
export function PageControlsProvider({ children }: { children: ReactNode }) {
  const [controls, setControls] = useState<ReactNode>(null);
  return (
    <PageControlsContext.Provider value={{ controls, setControls }}>{children}</PageControlsContext.Provider>
  );
}
export function usePageControls(): { setControls: (value: ReactNode) => void } {
  const context = useContext(PageControlsContext);
  if (!context) throw new Error('Page controls are unavailable');
  return { setControls: context.setControls };
}
export function PageControlsSlot() {
  return useContext(PageControlsContext)?.controls ?? null;
}
