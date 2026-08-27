import { lazy, Suspense } from 'react';

// Disable browser scroll restoration — refresh always starts at top
if (typeof history !== 'undefined' && 'scrollRestoration' in history) {
  history.scrollRestoration = 'manual';
}
import LiveWorkbench from './components/workbench/LiveWorkbench';
import './App.css';

// Demo fixtures are bundled only when VITE_ENABLE_DEMO=1; the default live
// path is the SSE workbench and never falls back to demo data (M6-12).
const DemoWorkbench = lazy(() => import('./components/workbench/DemoWorkbench'));

function App() {
  const enableDemo = import.meta.env.VITE_ENABLE_DEMO === '1';

  if (enableDemo) {
    return (
      <Suspense fallback={null}>
        <DemoWorkbench />
      </Suspense>
    );
  }
  return <LiveWorkbench />;
}

export default App;
