import DemoWorkbench from './components/workbench/DemoWorkbench';

// Disable browser scroll restoration — refresh always starts at top
if (typeof history !== 'undefined' && 'scrollRestoration' in history) {
  history.scrollRestoration = 'manual';
}
import LiveWorkbench from './components/workbench/LiveWorkbench';
import './App.css';

function App() {
  const enableDemo = import.meta.env.VITE_ENABLE_DEMO === '1';

  if (enableDemo) {
    return <DemoWorkbench />;
  }
  return <LiveWorkbench />;
}

export default App;
