(() => {
  const root = document.documentElement;
  const preference = matchMedia('(prefers-color-scheme: dark)');
  let saved;
  try { saved = JSON.parse(localStorage.getItem('ap-theme')); } catch { /* Use system preference. */ }
  function apply(theme) {
    root.dataset.theme = theme;
    root.classList.toggle('wa-dark', theme === 'dark');
    root.classList.toggle('wa-light', theme === 'light');
  }
  apply(saved === 'light' || saved === 'dark' ? saved : preference.matches ? 'dark' : 'light');
  function labels() {
    const dark = root.dataset.theme === 'dark';
    document.querySelectorAll('[data-theme-toggle]').forEach(button => {
      button.setAttribute('aria-label', dark ? 'Switch to light theme' : 'Switch to dark theme');
      button.setAttribute('title', dark ? 'Switch to light theme' : 'Switch to dark theme');
      const label = button.querySelector('[data-theme-label]');
      if (label) label.textContent = dark ? 'Light' : 'Dark';
    });
    document.querySelector('meta[name="theme-color"]')?.setAttribute('content', getComputedStyle(document.body).backgroundColor);
  }
  document.addEventListener('click', event => {
    if (!event.target.closest('[data-theme-toggle]')) return;
    saved = root.dataset.theme === 'dark' ? 'light' : 'dark';
    apply(saved);
    try { localStorage.setItem('ap-theme', JSON.stringify(saved)); } catch { /* Theme still works for this visit. */ }
    labels();
    document.dispatchEvent(new CustomEvent('ap:theme'));
  });
  preference.addEventListener('change', event => {
    if (saved === 'light' || saved === 'dark') return;
    apply(event.matches ? 'dark' : 'light');
    labels();
    document.dispatchEvent(new CustomEvent('ap:theme'));
  });
  document.addEventListener('DOMContentLoaded', labels);
  document.addEventListener('htmx:afterSwap', labels);
})();
