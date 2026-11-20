import { useEffect } from 'react';
export function useWorkbenchKeys(
  onPalette: () => void,
  onClear: () => void,
  onStep: (direction: -1 | 1) => void,
  paletteOpen: boolean,
): void {
  useEffect(() => {
    const keydown = (event: KeyboardEvent) => {
      // Any open modal (palette, run dialog) owns the keyboard; Radix also handles Esc there.
      if (event.defaultPrevented || document.querySelector('[role="dialog"][data-state="open"]')) return;
      const target = event.target as HTMLElement | null;
      const typing = target?.matches('input, textarea, select, [contenteditable="true"]');
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'k') {
        event.preventDefault();
        onPalette();
        return;
      }
      if (event.key === 'Escape' && !paletteOpen) onClear();
      if (!typing && !paletteOpen && event.key === 'ArrowLeft') {
        event.preventDefault();
        onStep(-1);
      }
      if (!typing && !paletteOpen && event.key === 'ArrowRight') {
        event.preventDefault();
        onStep(1);
      }
    };
    window.addEventListener('keydown', keydown);
    return () => window.removeEventListener('keydown', keydown);
  }, [onPalette, onClear, onStep, paletteOpen]);
}
