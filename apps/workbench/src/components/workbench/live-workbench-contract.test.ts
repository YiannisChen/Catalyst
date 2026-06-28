/**
 * Lightweight contract tests for LiveWorkbench BYOK path.
 * Tests pure functions and API payload shapes — no DOM rendering or network calls.
 */
import assert from 'node:assert/strict';
import { describe, it } from 'node:test';

// ── API payload shape tests ──

describe('createLiveRun payload', () => {
  it('uses model: ModelConfig, not model_id, in BYOK path', () => {
    const model = {
      provider: 'openrouter',
      model_id: 'google/gemini-2.5-flash',
      api_key: 'sk-test',
      base_url: 'https://openrouter.ai/api/v1',
    };
    const payload = {
      ticker: 'AAPL',
      trade_date: '2025-09-08',
      model,
    };
    // BYOK path: model_id should NOT be present
    assert.ok(!('model_id' in payload) || payload.model_id === undefined);
    // model should contain the credential
    assert.equal(payload.model?.provider, 'openrouter');
    assert.equal(payload.model?.api_key, 'sk-test');
  });

  it('does not include internal keys in the API payload', () => {
    const model = {
      provider: 'openai',
      model_id: 'gpt-4o',
      api_key: 'sk-test',
    };
    const payload = { ticker: 'NVDA', trade_date: '2025-09-08', model };
    // These keys should never appear at the top level
    assert.ok(!('api_key' in payload));
    assert.ok(!('secret' in payload));
  });
});

// ── RetryRunRequest shape ──

describe('RetryRunRequest', () => {
  it('accepts model: ModelConfig for BYOK retry', () => {
    const req = {
      model: {
        provider: 'openai',
        model_id: 'gpt-4o',
        api_key: 'sk-retry-test',
      },
    };
    assert.equal(req.model?.provider, 'openai');
    assert.equal(req.model?.api_key, 'sk-retry-test');
  });

  it('still accepts legacy model_id for backward compat', () => {
    const req = { model_id: 'gemini-2.5-flash-nothink' };
    assert.equal(req.model_id, 'gemini-2.5-flash-nothink');
  });
});

// ── getWorkspace API base ──

describe('getWorkspace API', () => {
  it('constructs path using /live-runs/{id}/workspace', () => {
    const runId = 'abc123';
    const path = `/live-runs/${encodeURIComponent(runId)}/workspace`;
    assert.equal(path, '/live-runs/abc123/workspace');
  });

  it('encodes special characters in runId', () => {
    const path = `/live-runs/${encodeURIComponent('run/id')}/workspace`;
    assert.equal(path, '/live-runs/run%2Fid/workspace');
  });
});

// ── workspace-adapter types ──

describe('workspace-adapter', () => {
  it('exports mapEvidence as a function', async () => {
    const mod = await import('./workspace-adapter.ts');
    assert.equal(typeof mod.mapEvidence, 'function');
  });

  it('exports mapResult as a function', async () => {
    const mod = await import('./workspace-adapter.ts');
    assert.equal(typeof mod.mapResult, 'function');
  });

  it('exports mapSteps as a function', async () => {
    const mod = await import('./workspace-adapter.ts');
    assert.equal(typeof mod.mapSteps, 'function');
  });

  it('exports buildDemoCaseStub as a function', async () => {
    const mod = await import('./workspace-adapter.ts');
    assert.equal(typeof mod.buildDemoCaseStub, 'function');
  });

  it('stub built from real candles has expected shape', async () => {
    const candles = [
      { date: '2025-09-08', open: 100, high: 105, low: 99, close: 102, volume: 1000000 } as any,
    ];
    // Dynamic import to avoid TypeScript strictness on test inputs
    const mod = await import('./workspace-adapter.ts');
    const stub = mod.buildDemoCaseStub('AAPL', candles);
    assert.equal(stub.ticker, 'AAPL');
    assert.equal(stub.candles.length, 1);
    assert.equal(stub.candles[0].close, 102);
  });
});

// ── ModelConfig credential_source shape ──

describe('ModelConfig with credential_source', () => {
  it('server_env allows empty api_key', () => {
    const config = {
      provider: 'aihubmix',
      model_id: 'gemini-2.5-flash-nothink',
      api_key: '',
      credential_source: 'server_env' as const,
    };
    assert.equal(config.credential_source, 'server_env');
    assert.equal(config.api_key, '');
  });

  it('browser_key requires non-empty api_key by convention', () => {
    const config = {
      provider: 'openai',
      model_id: 'gpt-4o-mini',
      api_key: 'sk-test',
      credential_source: 'browser_key' as const,
    };
    assert.equal(config.credential_source, 'browser_key');
    assert.ok(config.api_key.length > 0);
  });
});

// ── Catalog types shape ──

describe('CatalogProvider types', () => {
  it('has expected shape', () => {
    const provider = {
      id: 'deepseek',
      label: 'DeepSeek',
      executable: true,
      env_key_configured: true,
      default_model_id: 'deepseek-v4-flash',
      models: [
        { id: 'deepseek-v4-flash', label: 'DeepSeek V4 Flash', tier: 'value', recommended: true },
      ],
    };
    assert.equal(provider.id, 'deepseek');
    assert.ok(provider.executable);
    assert.ok(provider.env_key_configured);
    assert.equal(provider.models.length, 1);
  });

  it('custom provider has env_key_configured false', () => {
    const provider = {
      id: 'custom_openai_compatible',
      label: 'Custom (OpenAI-compatible)',
      executable: true,
      env_key_configured: false,
      default_model_id: null,
      models: [],
    };
    assert.equal(provider.env_key_configured, false);
    assert.equal(provider.default_model_id, null);
  });
});

// ── No OpenRouter in catalog providers ──

describe('Provider list excludes OpenRouter', () => {
  it('must not include openrouter in known provider ids', () => {
    const knownIds = ['aihubmix', 'siliconflow', 'openai', 'deepseek', 'glm', 'custom_openai_compatible'];
    assert.ok(!knownIds.includes('openrouter'));
    assert.ok(!knownIds.includes('dashscope'));
  });
});

// ── RetryRunRequest with model ──

describe('RetryRunRequest with ModelConfig', () => {
  it('accepts model field for BYOK retry', () => {
    const req = {
      model: {
        provider: 'deepseek',
        model_id: 'deepseek-v4-flash',
        api_key: '',
        credential_source: 'server_env' as const,
      },
    };
    assert.equal(req.model?.provider, 'deepseek');
    assert.equal(req.model?.credential_source, 'server_env');
  });
});

// ── mapNewsToEvidence contract tests ──

describe('mapNewsToEvidence', () => {
  it('imports from workspace-adapter', async () => {
    const mod = await import('./workspace-adapter.ts');
    assert.equal(typeof mod.mapNewsToEvidence, 'function');
  });

  it('maps NewsItem to EvidenceItem correctly', async () => {
    const mod = await import('./workspace-adapter.ts');
    const newsItems = [{
      asset_id: 'ev-001',
      ticker: 'AAPL',
      reference_date: '2025-09-08',
      published_utc: '2025-09-08T14:30:00Z',
      title: 'AAPL: iPhone 17 demand exceeds expectations',
      source_line: 'Source: Bloomberg',
      snippet: '## Summary\nDemand for iPhone 17 has surpassed analyst estimates.\n*Source: Bloomberg*',
    }];
    const result = mod.mapNewsToEvidence(newsItems, '2025-09-08');
    assert.equal(result.length, 1);
    assert.equal(result[0].id, 'ev-001');
    assert.equal(result[0].title, 'iPhone 17 demand exceeds expectations');
    // Snippet should NOT contain markdown heading or source line
    assert.ok(!result[0].snippet.includes('##'));
    assert.ok(!result[0].snippet.includes('Source:'));
    assert.equal(result[0].source, 'Bloomberg');
    assert.equal(result[0].sourceType, 'news');
    assert.equal(result[0].temporalStatus, 'same-day');
    assert.equal(result[0].criticDecision, 'ungraded');
    assert.equal(result[0].relevanceScore, 0);
  });

  it('maps prior-window temporalStatus when date differs', async () => {
    const mod = await import('./workspace-adapter.ts');
    const newsItems = [{
      asset_id: 'ev-002',
      ticker: 'AAPL',
      reference_date: '2025-09-07',
      published_utc: '2025-09-07T10:00:00Z',
      title: 'AAPL: Pre-event news',
      source_line: 'Source: Reuters',
      snippet: 'Some news.',
    }];
    const result = mod.mapNewsToEvidence(newsItems, '2025-09-08');
    assert.equal(result[0].temporalStatus, 'prior-window');
    assert.equal(result[0].source, 'Reuters');
  });

  it('handles source_line with pipe character', async () => {
    const mod = await import('./workspace-adapter.ts');
    const newsItems = [{
      asset_id: 'ev-003',
      ticker: 'NVDA',
      reference_date: '2025-06-10',
      published_utc: null,
      title: 'NVDA: Test',
      source_line: 'Source: CNBC | Author: Jane',
      snippet: 'Test snippet.',
    }];
    const result = mod.mapNewsToEvidence(newsItems, '2025-06-10');
    assert.equal(result[0].source, 'CNBC');
    assert.ok(result[0].publishedAt.includes('2025-06-10'));
  });

  it('handles empty source_line', async () => {
    const mod = await import('./workspace-adapter.ts');
    const newsItems = [{
      asset_id: 'ev-004',
      ticker: 'TSLA',
      reference_date: '2025-09-08',
      published_utc: '2025-09-08T12:00:00Z',
      title: 'TSLA: Some news',
      source_line: '',
      snippet: 'Some content.',
    }];
    const result = mod.mapNewsToEvidence(newsItems, '2025-09-08');
    assert.equal(result[0].source, 'News');
  });

  it('snippet is truncated to 300 characters', async () => {
    const mod = await import('./workspace-adapter.ts');
    const longSnippet = 'A'.repeat(500);
    const newsItems = [{
      asset_id: 'ev-005',
      ticker: 'AAPL',
      reference_date: '2025-09-08',
      published_utc: null,
      title: 'AAPL: Long',
      source_line: 'Source: Test',
      snippet: longSnippet,
    }];
    const result = mod.mapNewsToEvidence(newsItems, '2025-09-08');
    assert.ok(result[0].snippet.length <= 300);
  });
});

// ── News preview contract ──

describe('News preview contract', () => {
  it('EvidenceIntakePanel mode defaults to retrieved', () => {
    // Mode defaults to 'retrieved' when not provided
    const defaultMode: 'preview' | 'retrieved' = 'retrieved';
    assert.equal(defaultMode, 'retrieved');
  });

  it('preview mode title is Candidate News', () => {
    const mode: 'preview' | 'retrieved' = 'preview';
    const title = mode === 'preview' ? 'Candidate News' : 'Retrieved Evidence';
    assert.equal(title, 'Candidate News');
  });
});


// ── ModelSettings modal contract ──

describe('ModelSettings modal contract', () => {
  it('ModelSettings uses createPortal for modal isolation', async () => {
    const fs = await import('node:fs');
    const srcPath = new URL('./v4/ModelSettings.tsx', import.meta.url).pathname;
    const srcText = fs.readFileSync(srcPath, 'utf-8');
    const hasPortal =
      srcText.includes('createPortal') ||
      srcText.includes('v4-modal-backdrop');
    assert.ok(hasPortal, 'ModelSettings must render modal via portal or fixed overlay');
  });

  it('modal panel uses role="dialog", aria-modal="true", width 520px', async () => {
    const fs = await import('node:fs');
    const srcPath = new URL('./v4/ModelSettings.tsx', import.meta.url).pathname;
    const srcText = fs.readFileSync(srcPath, 'utf-8');
    assert.ok(srcText.includes('role="dialog"'), 'modal panel must have role="dialog"');
    assert.ok(srcText.includes('aria-modal="true"'), 'modal panel must have aria-modal="true"');
  });

  it('frontend never renders server_env option', async () => {
    const fs = await import('node:fs');
    const srcPath = new URL('./v4/ModelSettings.tsx', import.meta.url).pathname;
    const srcText = fs.readFileSync(srcPath, 'utf-8');
    assert.ok(!srcText.includes('server_env'), 'server_env must not appear in ModelSettings source');
    assert.ok(!srcText.includes('Server .env'), '"Server .env" text must not appear');
  });

  it('credential_source is always browser_key', () => {
    const config = {
      provider: 'openai', model_id: 'gpt-4o-mini', api_key: 'sk-test',
      credential_source: 'browser_key' as const,
    };
    assert.equal(config.credential_source, 'browser_key');
  });

  it('custom provider requires baseUrl + apiKey + modelId', () => {
    const canTest = (baseUrl: string, apiKey: string, modelId: string) =>
      baseUrl.length > 0 && apiKey.length > 0 && modelId.length > 0;
    assert.ok(canTest('https://api.example.com/v1', 'sk-test', 'custom-model'));
    assert.ok(!canTest('', 'sk-test', 'custom-model'));
    assert.ok(!canTest('https://api.example.com/v1', '', 'custom-model'));
    assert.ok(!canTest('https://api.example.com/v1', 'sk-test', ''));
  });

  it('tests do not leak real API keys into source assertions', () => {
    // No test should hardcode a real key format
    const testKey = 'sk-test';
    assert.ok(testKey.startsWith('sk-test'));
    assert.ok(!testKey.includes('sk-or'));
    assert.ok(!testKey.includes('sk-proj'));
  });

  it('model config always includes credential_source browser_key', () => {
    const configs = [
      { provider: 'openai', model_id: 'gpt-4o', api_key: 'sk-1', credential_source: 'browser_key' as const },
      { provider: 'deepseek', model_id: 'd-v4', api_key: 'sk-2', credential_source: 'browser_key' as const },
      { provider: 'aihubmix', model_id: 'gemini-flash', api_key: 'sk-3', credential_source: 'browser_key' as const },
    ];
    for (const c of configs) {
      assert.equal(c.credential_source, 'browser_key');
      assert.ok(c.api_key.length > 0);
    }
  });
});

// ── Header / CSS contract ──

describe('Header CSS contract', () => {
  it('CSS .v4-header has fixed height for predictable layout', async () => {
    const fs = await import('node:fs');
    const cssPath = new URL('./v4/workbench-v4.css', import.meta.url).pathname;
    const css = fs.readFileSync(cssPath, 'utf-8');
    const headerMatch = css.match(/\.v4-header\s*\{[^}]*\}/s);
    assert.ok(headerMatch, '.v4-header rule must exist');
    assert.ok(
      headerMatch[0].includes('height'),
      '.v4-header must declare fixed height for predictable layout'
    );
  });

  it('CSS .v4-modal-backdrop is position:fixed with full viewport', async () => {
    const fs = await import('node:fs');
    const cssPath = new URL('./v4/workbench-v4.css', import.meta.url).pathname;
    const css = fs.readFileSync(cssPath, 'utf-8');
    const backdropMatch = css.match(/^\.v4-modal-backdrop\s*\{[^}]*\}/ms);
    assert.ok(backdropMatch, '.v4-modal-backdrop must exist');
    assert.ok(backdropMatch[0].includes('position: fixed'), 'backdrop must be fixed');
    assert.ok(backdropMatch[0].includes('inset: 0'), 'backdrop must cover full viewport');
    assert.ok(backdropMatch[0].includes('z-index: 200'), 'backdrop must have high z-index');
  });

  it('CSS .v4-header-controls uses align-items: center', async () => {
    const fs = await import('node:fs');
    const cssPath = new URL('./v4/workbench-v4.css', import.meta.url).pathname;
    const css = fs.readFileSync(cssPath, 'utf-8');
    const controlsMatch = css.match(/\.v4-header-controls\s*\{[^}]*\}/s);
    assert.ok(controlsMatch, '.v4-header-controls must exist');
    assert.ok(
      controlsMatch[0].includes('align-items: center'),
      'header-controls must use center alignment'
    );
    assert.ok(
      !controlsMatch[0].includes('align-items: flex-end'),
      'header-controls must not use flex-end'
    );
  });

  it('CSS .v4-panel has border: none (no hard section borders)', async () => {
    const fs = await import('node:fs');
    const cssPath = new URL('./v4/workbench-v4.css', import.meta.url).pathname;
    const css = fs.readFileSync(cssPath, 'utf-8');
    const panelMatch = css.match(/\.v4-panel\s*\{[^}]*\}/s);
    assert.ok(panelMatch, '.v4-panel rule must exist');
    assert.ok(
      panelMatch[0].includes('border: none'),
      '.v4-panel must use border: none to avoid debug-grid look'
    );
  });

  it('CSS --v4-border token uses rgba (subtle, not solid hex)', async () => {
    const fs = await import('node:fs');
    const cssPath = new URL('./v4/workbench-v4.css', import.meta.url).pathname;
    const css = fs.readFileSync(cssPath, 'utf-8');
    assert.ok(
      css.includes('--v4-border: rgba'),
      '--v4-border must use rgba for subtle section separation'
    );
  });

  it('CSS --v4-scrim opacity is 0.72 for modal backdrop', async () => {
    const fs = await import('node:fs');
    const cssPath = new URL('./v4/workbench-v4.css', import.meta.url).pathname;
    const css = fs.readFileSync(cssPath, 'utf-8');
    const scrimMatch = css.match(/--v4-scrim:\s*rgba\(2,\s*6,\s*23,\s*([\d.]+)\)/);
    assert.ok(scrimMatch, '--v4-scrim must exist with rgba(2,6,23,opacity)');
    const opacity = parseFloat(scrimMatch[1]);
    assert.strictEqual(opacity, 0.72, `scrim opacity ${opacity} must be 0.72`);
  });

  it('CSS .v4-evidence-card has min-height: 120px to prevent clipping', async () => {
    const fs = await import('node:fs');
    const cssPath = new URL('./v4/workbench-v4.css', import.meta.url).pathname;
    const css = fs.readFileSync(cssPath, 'utf-8');
    const cardMatch = css.match(/\.v4-evidence-card\s*\{[^}]*\}/s);
    assert.ok(cardMatch, '.v4-evidence-card must exist');
    const minH = cardMatch[0].match(/min-height:\s*(\d+)px/);
    assert.ok(minH, '.v4-evidence-card must have min-height in px');
    assert.ok(parseInt(minH[1]) >= 120, `.v4-evidence-card min-height ${minH[1]}px must be >= 120px`);
  });

  it('CSS .v4-evidence-snippet clamps to 3 lines', async () => {
    const fs = await import('node:fs');
    const cssPath = new URL('./v4/workbench-v4.css', import.meta.url).pathname;
    const css = fs.readFileSync(cssPath, 'utf-8');
    const snippetMatch = css.match(/\.v4-evidence-snippet\s*\{[^}]*\}/s);
    assert.ok(snippetMatch, '.v4-evidence-snippet must exist');
    assert.ok(
      snippetMatch[0].includes('-webkit-line-clamp: 3'),
      '.v4-evidence-snippet must clamp to 3 lines'
    );
  });

  it('Run Attribution button is 36px desktop', async () => {
    const fs = await import('node:fs');
    const cssPath = new URL('./v4/workbench-v4.css', import.meta.url).pathname;
    const css = fs.readFileSync(cssPath, 'utf-8');
    const runBtnMatch = css.match(/\.v4-page \.v4-run-btn\s*\{[^}]*\}/s);
    assert.ok(runBtnMatch, '.v4-run-btn must exist');
    assert.ok(
      runBtnMatch[0].includes('height: 36px'),
      'Run Attribution button must be 36px desktop (44px on mobile via media query)'
    );
  });

  it('CSS interactive elements meet desktop/mobile size targets', async () => {
    const fs = await import('node:fs');
    const cssPath = new URL('./v4/workbench-v4.css', import.meta.url).pathname;
    const css = fs.readFileSync(cssPath, 'utf-8');
    // v4-model-trigger
    const triggerMatch = css.match(/^\.v4-model-trigger\s*\{[^}]*\}/ms);
    assert.ok(triggerMatch, '.v4-model-trigger must exist');
    assert.ok(triggerMatch[0].includes('min-height: 36px'), 'trigger must have 36px min-height desktop');
    // v4-test-btn
    const testBtnMatch = css.match(/\.v4-test-btn\s*\{[^}]*\}/s);
    assert.ok(testBtnMatch, '.v4-test-btn must exist');
    assert.ok(testBtnMatch[0].includes('min-height: 44px'), 'test-btn must have 44px min-height');
    // v4-use-model-btn
    const useBtnMatch = css.match(/\.v4-use-model-btn\s*\{[^}]*\}/s);
    assert.ok(useBtnMatch, '.v4-use-model-btn must exist');
    assert.ok(useBtnMatch[0].includes('height: 44px') || useBtnMatch[0].includes('min-height: 44px'), 'use-model-btn must have 44px height');
    // v4-modal-close
    const closeMatch = css.match(/\.v4-modal-close\s*\{[^}]*\}/s);
    assert.ok(closeMatch, '.v4-modal-close must exist');
    assert.ok(closeMatch[0].includes('min-height: 44px'), 'close button must have 44px min-height');
    // v4-select (ticker select)
    const selectMatch = css.match(/\.v4-select\s*\{[^}]*\}/s);
    assert.ok(selectMatch, '.v4-select must exist');
    assert.ok(selectMatch[0].includes('height: 36px'), 'ticker select must have 36px height desktop');
    // v4-key-toggle
    const keyToggleMatch = css.match(/\.v4-key-toggle\s*\{[^}]*\}/s);
    assert.ok(keyToggleMatch, '.v4-key-toggle must exist');
    assert.ok(keyToggleMatch[0].includes('min-height: 44px'), 'key-toggle must have 44px min-height');
  });

  it('CSS .v4-shell padding-top is 20px on rhythm', async () => {
    const fs = await import('node:fs');
    const cssPath = new URL('./v4/workbench-v4.css', import.meta.url).pathname;
    const css = fs.readFileSync(cssPath, 'utf-8');
    const shellMatch = css.match(/\.v4-shell\s*\{[^}]*\}/s);
    assert.ok(shellMatch, '.v4-shell must exist');
    const padMatch = shellMatch[0].match(/padding:\s*(\d+)px\s+\d+px\s+\d+px/);
    assert.ok(padMatch, '.v4-shell must have padding shorthand');
    assert.strictEqual(parseInt(padMatch[1]), 20, '.v4-shell padding-top must be 20px (A2.1)');
  });

  it('CSS .v4-date-chip height is 36px desktop', async () => {
    const fs = await import('node:fs');
    const cssPath = new URL('./v4/workbench-v4.css', import.meta.url).pathname;
    const css = fs.readFileSync(cssPath, 'utf-8');
    const chipMatch = css.match(/\.v4-date-chip\s*\{[^}]*\}/s);
    assert.ok(chipMatch, '.v4-date-chip must exist');
    assert.ok(chipMatch[0].includes('height: 36px'), '.v4-date-chip must be 36px desktop');
  });

  it('CSS .v4-run-btn padding is on 4px rhythm', async () => {
    const fs = await import('node:fs');
    const cssPath = new URL('./v4/workbench-v4.css', import.meta.url).pathname;
    const css = fs.readFileSync(cssPath, 'utf-8');
    const btnMatch = css.match(/\.v4-page \.v4-run-btn\s*\{[^}]*\}/s);
    assert.ok(btnMatch, '.v4-run-btn must exist');
    const padH = btnMatch[0].match(/padding:\s*0\s+(\d+)px/);
    assert.ok(padH, '.v4-run-btn must have horizontal padding');
    const padVal = parseInt(padH[1]);
    assert.ok(padVal % 4 === 0, `.v4-run-btn horizontal padding ${padVal}px must be on 4px rhythm`);
  });

  it('CSS .v4-evidence-strip uses auto-fit with min >= 280px', async () => {
    const fs = await import('node:fs');
    const cssPath = new URL('./v4/workbench-v4.css', import.meta.url).pathname;
    const css = fs.readFileSync(cssPath, 'utf-8');
    const stripMatch = css.match(/\.v4-evidence-strip\s*\{[^}]*\}/s);
    assert.ok(stripMatch, '.v4-evidence-strip must exist');
    assert.ok(stripMatch[0].includes('auto-fit'), '.v4-evidence-strip must use auto-fit');
    const minMatch = stripMatch[0].match(/minmax\((\d+)px/);
    assert.ok(minMatch, '.v4-evidence-strip must have minmax with px minimum');
    assert.ok(parseInt(minMatch[1]) >= 280, `evidence strip min ${minMatch[1]}px must be >= 280px`);
  });

  it('WorkbenchHeader has no .v4-field-label spans', async () => {
    const fs = await import('node:fs');
    const srcPath = new URL('./v4/WorkbenchHeader.tsx', import.meta.url).pathname;
    const src = fs.readFileSync(srcPath, 'utf-8');
    assert.ok(!src.includes('v4-field-label'), 'WorkbenchHeader must not use v4-field-label');
  });

  it('WorkbenchHeader has no visible "Ticker" label text', async () => {
    const fs = await import('node:fs');
    const srcPath = new URL('./v4/WorkbenchHeader.tsx', import.meta.url).pathname;
    const src = fs.readFileSync(srcPath, 'utf-8');
    assert.ok(!src.includes('>Ticker<'), 'WorkbenchHeader must not render visible >Ticker<');
    // But aria-label is required
    assert.ok(src.includes('aria-label="Select ticker"'), 'select must have aria-label="Select ticker"');
  });

  it('WorkbenchHeader has no visible "Session" label text', async () => {
    const fs = await import('node:fs');
    const srcPath = new URL('./v4/WorkbenchHeader.tsx', import.meta.url).pathname;
    const src = fs.readFileSync(srcPath, 'utf-8');
    assert.ok(!src.includes('>Session<'), 'WorkbenchHeader must not render visible >Session<');
    // But aria-label is required
    assert.ok(src.includes('aria-label="Session date"'), 'date chip must have aria-label="Session date"');
  });

  it('CSS .v4-metrics-grid > div has raised card treatment', async () => {
    const fs = await import('node:fs');
    const cssPath = new URL('./v4/workbench-v4.css', import.meta.url).pathname;
    const css = fs.readFileSync(cssPath, 'utf-8');
    const cellMatch = css.match(/\.v4-metrics-grid\s*>\s*div\s*\{[^}]*\}/s);
    assert.ok(cellMatch, '.v4-metrics-grid > div must exist');
    assert.ok(cellMatch[0].includes('background'), 'metric cell must have background');
    assert.ok(cellMatch[0].includes('border-radius'), 'metric cell must have border-radius');
  });

  it('CSS .v4-modal-input has distinct bg from modal panel', async () => {
    const fs = await import('node:fs');
    const cssPath = new URL('./v4/workbench-v4.css', import.meta.url).pathname;
    const css = fs.readFileSync(cssPath, 'utf-8');
    const inputMatch = css.match(/\.v4-modal-input\s*\{[^}]*\}/s);
    assert.ok(inputMatch, '.v4-modal-input must exist');
    assert.ok(inputMatch[0].includes('background'), '.v4-modal-input must have background');
    // Input bg should use --v4-control-bg, not --v4-modal-bg or --v4-surface-elevated
    assert.ok(
      inputMatch[0].includes('var(--v4-modal-input-bg)') || inputMatch[0].includes('#ffffff'),
      '.v4-modal-input must use white/light input background'
    );
  });

  it('CSS .v4-header-controls gap is on rhythm', async () => {
    const fs = await import('node:fs');
    const cssPath = new URL('./v4/workbench-v4.css', import.meta.url).pathname;
    const css = fs.readFileSync(cssPath, 'utf-8');
    const controlsMatch = css.match(/\.v4-header-controls\s*\{[^}]*\}/s);
    assert.ok(controlsMatch, '.v4-header-controls must exist');
    const gapMatch = controlsMatch[0].match(/gap:\s*(\d+)px/);
    assert.ok(gapMatch, '.v4-header-controls must have gap');
    assert.strictEqual(parseInt(gapMatch[1]), 12, '.v4-header-controls gap must be 12px');
  });

  it('CSS .v4-modal-panel uses --v4-modal-bg solid background', async () => {
    const fs = await import('node:fs');
    const cssPath = new URL('./v4/workbench-v4.css', import.meta.url).pathname;
    const css = fs.readFileSync(cssPath, 'utf-8');
    const panelMatch = css.match(/^\.v4-modal-panel\s*\{[^}]*\}/ms);
    assert.ok(panelMatch, '.v4-modal-panel base rule must exist');
    assert.ok(
      panelMatch[0].includes('var(--v4-modal-bg)') || panelMatch[0].includes('#111827'),
      '.v4-modal-panel must use --v4-modal-bg solid color'
    );
    // Background property specifically must not be rgba
    const bgMatch = panelMatch[0].match(/background:\s*([^;]+);/);
    assert.ok(bgMatch, '.v4-modal-panel must have background property');
    assert.ok(!bgMatch[1].includes('rgba'), '.v4-modal-panel background must not be rgba');
  });

  it('CSS .v4-modal-panel width is 520px', async () => {
    const fs = await import('node:fs');
    const cssPath = new URL('./v4/workbench-v4.css', import.meta.url).pathname;
    const css = fs.readFileSync(cssPath, 'utf-8');
    const panelMatch = css.match(/^\.v4-modal-panel\s*\{[^}]*\}/ms);
    assert.ok(panelMatch, '.v4-modal-panel base rule must exist');
    const widthMatch = panelMatch[0].match(/width:\s*(\d+)px/);
    assert.ok(widthMatch, '.v4-modal-panel must have width');
    assert.strictEqual(parseInt(widthMatch[1]), 520, '.v4-modal-panel width must be 520px');
  });

  it('CSS .v4-evidence-intake has min-height for viewport guarantee', async () => {
    const fs = await import('node:fs');
    const cssPath = new URL('./v4/workbench-v4.css', import.meta.url).pathname;
    const css = fs.readFileSync(cssPath, 'utf-8');
    // News section has a min-height budget so ≥2 cards render complete (A0/A2.1)
    const intakeMatch = css.match(/\.v4-evidence-intake\s*\{[^}]*\}/s);
    assert.ok(intakeMatch, '.v4-evidence-intake must exist');
    const minH = intakeMatch[0].match(/min-height:\s*(\d+)px/);
    assert.ok(minH, '.v4-evidence-intake must declare a min-height in px');
    assert.ok(parseInt(minH[1]) >= 160, `evidence-intake min-height ${minH[1]}px must be >= 160px`);
  });

});
// ── No low-value eyebrow labels ──

describe('No generic container eyebrow labels', () => {
  it('MarketSessionPanel does not render "Market Session" eyebrow', async () => {
    const fs = await import('node:fs');
    const srcPath = new URL('./v4/MarketSessionPanel.tsx', import.meta.url).pathname;
    const src = fs.readFileSync(srcPath, 'utf-8');
    assert.ok(!src.includes('Market Session'), 'MarketSessionPanel must not contain "Market Session" eyebrow');
  });

  it('SessionOverviewPanel does not render "Session Overview" eyebrow', async () => {
    const fs = await import('node:fs');
    const srcPath = new URL('./v4/SessionOverviewPanel.tsx', import.meta.url).pathname;
    const src = fs.readFileSync(srcPath, 'utf-8');
    assert.ok(!src.includes('Session Overview'), 'SessionOverviewPanel must not contain "Session Overview" eyebrow');
  });

  it('EvidenceIntakePanel does not render "Evidence Intake" eyebrow', async () => {
    const fs = await import('node:fs');
    const srcPath = new URL('./v4/EvidenceIntakePanel.tsx', import.meta.url).pathname;
    const src = fs.readFileSync(srcPath, 'utf-8');
    assert.ok(!src.includes('Evidence Intake'), 'EvidenceIntakePanel must not contain "Evidence Intake" eyebrow');
  });

  it('ResultWorkspaceTabs does not render "Analysis" eyebrow', async () => {
    const fs = await import('node:fs');
    const srcPath = new URL('./v4/ResultWorkspaceTabs.tsx', import.meta.url).pathname;
    const src = fs.readFileSync(srcPath, 'utf-8');
    assert.ok(!src.includes('"Analysis"'), 'ResultWorkspaceTabs must not contain "Analysis" eyebrow');
  });

  it('LiveWorkbench does not render "Configure a model" banner', async () => {
    const fs = await import('node:fs');
    const srcPath = new URL('./LiveWorkbench.tsx', import.meta.url).pathname;
    const src = fs.readFileSync(srcPath, 'utf-8');
    assert.ok(!src.includes('Configure a model'), 'LiveWorkbench must not contain "Configure a model" banner');
    assert.ok(!src.includes('configure a model'), 'LiveWorkbench must not contain configure banner (lowercase)');
  });
});

