// Apply the saved theme before first paint (no flash); mirrors src/lib/theme.ts.
// A file, not an inline script, so the Content-Security-Policy can stay free of 'unsafe-inline'.
(function () {
  var p = 'system';
  try { p = localStorage.getItem('aiops.theme') || 'system'; } catch (e) {}
  var dark = p === 'dark' || (p !== 'light' && window.matchMedia('(prefers-color-scheme: dark)').matches);
  document.documentElement.dataset.theme = dark ? 'dark' : 'light';
})();
