/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_API_TARGET?: string;
  readonly VITE_MOCK?: string;
  /** Public Cloudflare Turnstile site key; set for the public build, empty locally. */
  readonly VITE_TURNSTILE_SITE_KEY?: string;
  readonly VITE_MOCK_PASSTHROUGH?: string;
}
