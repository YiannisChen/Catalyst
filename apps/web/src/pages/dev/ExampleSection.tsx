import type React from 'react';

export function ExampleSection({
  label,
  name,
  children,
}: {
  label: string;
  name: string;
  children: React.ReactNode;
}): React.JSX.Element {
  return (
    <section className="flex flex-col gap-3 border-t border-line-100 pt-4">
      <div>
        <div className="type-label text-ink-300">{label}</div>
        <h2 className="type-title m-0">{name}</h2>
      </div>
      {children}
    </section>
  );
}
