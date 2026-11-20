import type React from 'react';
import type { JSX } from 'react';
import { Link } from 'react-router';
export default function NotFound(): React.JSX.Element {
  return (
    <main className="mx-auto flex w-full max-w-7xl flex-col gap-4 px-5 py-6">
      <h1 className="type-display m-0">Page not found</h1>
      <p className="type-body m-0 text-ink-200">This page does not exist.</p>
      <Link className="text-info underline" to="/">
        Return to the workbench
      </Link>
    </main>
  );
}
