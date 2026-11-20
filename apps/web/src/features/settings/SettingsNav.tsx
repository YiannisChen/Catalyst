import type React from 'react';
import { useEffect, useState } from 'react';

const sections = [
  { id: 'models', label: 'Models and keys' },
  { id: 'spending', label: 'Spending limits' },
  { id: 'admin', label: 'Admin' },
  { id: 'data-updates', label: 'Data updates' },
] as const;

type SectionId = (typeof sections)[number]['id'];

function scrollerOf(element: HTMLElement | null): HTMLElement {
  for (let node = element?.parentElement; node; node = node.parentElement) {
    if (/(auto|scroll)/.test(getComputedStyle(node).overflowY) && node.scrollHeight > node.clientHeight)
      return node;
  }
  return (document.scrollingElement as HTMLElement | null) ?? document.documentElement;
}

/** The section whose top has passed the reading line; the last one once the page is scrolled to the end. */
function currentSection(): SectionId {
  const scroller = scrollerOf(document.getElementById(sections[0].id));
  if (scroller.scrollTop + scroller.clientHeight >= scroller.scrollHeight - 4) return sections.at(-1)!.id;
  let current: SectionId = sections[0].id;
  for (const { id } of sections) {
    const element = document.getElementById(id);
    if (element && element.getBoundingClientRect().top <= window.innerHeight * 0.3) current = id;
  }
  return current;
}

export function SettingsNav(): React.JSX.Element {
  const [active, setActive] = useState<SectionId>('models');

  useEffect(() => {
    const update = () => setActive(currentSection());
    update();
    // The page may scroll in a parent element, so listen in the capture phase.
    window.addEventListener('scroll', update, { capture: true, passive: true });
    window.addEventListener('resize', update);
    return () => {
      window.removeEventListener('scroll', update, { capture: true });
      window.removeEventListener('resize', update);
    };
  }, []);

  return (
    <aside className="sticky top-6 flex h-fit flex-col gap-1">
      <h1 className="type-title mb-2 px-3">Settings</h1>
      {sections.map(({ id, label }) => (
        <a
          key={id}
          href={`#${id}`}
          aria-current={active === id ? 'location' : undefined}
          onClick={() => setActive(id)}
          className={`control-md flex items-center rounded-md px-3 text-body-sm no-underline ${active === id ? 'bg-bg-300 text-ink-100 font-medium' : 'text-ink-200 hover:bg-bg-200 hover:text-ink-100'}`}
        >
          {label}
        </a>
      ))}
    </aside>
  );
}
