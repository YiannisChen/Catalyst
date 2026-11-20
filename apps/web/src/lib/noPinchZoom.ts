/**
 * Stops trackpad pinches from zooming the whole page, which kept happening by accident.
 * A pinch reaches the page as a wheel event with ctrlKey set (Chrome, Firefox) or as a
 * gesture event (Safari). Keyboard zoom (⌘+ / ⌘−) and the browser menu still work.
 */
export function installNoPinchZoom(): void {
  window.addEventListener(
    'wheel',
    (event) => {
      if (event.ctrlKey) event.preventDefault();
    },
    { passive: false },
  );
  for (const type of ['gesturestart', 'gesturechange', 'gestureend']) {
    document.addEventListener(type, (event) => event.preventDefault(), { passive: false });
  }
}
