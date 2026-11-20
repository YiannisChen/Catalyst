# Security policy

## Reporting a vulnerability

Please report suspected vulnerabilities privately to the repository owner through the GitHub profile contact or a direct GitHub security advisory. Include affected paths, reproduction steps, impact, and any suggested mitigation. Do not include live credentials, customer data, or private deployment details in a public issue. There is no guaranteed response or fix timeline.

## Scope

In scope:

- Provider-key handling, including BYOK validation, transmission, persistence, logs, and browser storage.
- Public deployment boundaries: admission, invite signing, quotas, Turnstile verification, trusted-proxy handling, CORS/host validation, nginx, and deployment configuration.
- Run isolation, event and artifact access control, SQLite boundaries, and secret redaction.

Out of scope:

- Findings requiring a compromised provider, GitHub, Cloudflare, operating system, or user device.
- Availability issues that require sustained traffic beyond configured public quotas.
- Research-only code and local experiment data, which are not part of the public product export.

Use only accounts and systems you own or are authorized to test. The demo and public deployment are not financial advice or a trading service.
