# Provider API Notes

These files are project-specific implementation notes, not substitutes for current provider documentation or plan terms. Provider capabilities, quotas, hosts, response fields, and licensing may change.

Rules for Catalyst:

- verify unstable details against official documentation before implementation;
- perform live canaries only with explicit operator authorization;
- never record API keys, authorization headers, cookies, signed URLs, or secret-bearing cursors;
- store one request-attempt record and one raw payload per actual response/page;
- keep connector capability differences explicit rather than silently falling back;
- treat the B2 technical contract as authoritative for provenance and status semantics.

Binding implementation contract: `../plans/2026-07-21-b2-b7-technical-contracts.md`.
