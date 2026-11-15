// MavBAC light/dark mode. Load in <head> (not deferred) so the right mode is set before the page paints.
// Remembers the choice per browser; otherwise follows the device setting.
(function () {
  var KEY = 'movbac-theme';
  function saved() { try { return localStorage.getItem(KEY); } catch (e) { return null; } }
  function system() { return window.matchMedia && matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark'; }
  function apply(t) {
    document.documentElement.setAttribute('data-theme', t);
    var b = document.getElementById('themeBtn');
    if (b) {
      var next = t === 'dark' ? 'light' : 'dark';
      b.innerHTML = '<span class="knob" aria-hidden="true"></span>' + (next === 'light' ? 'Light mode' : 'Dark mode');
      b.setAttribute('aria-label', 'Switch to ' + next + ' mode');
    }
  }
  apply(saved() || system());
  window.movbacTheme = function () { return document.documentElement.getAttribute('data-theme'); };
  document.addEventListener('DOMContentLoaded', function () {
    var b = document.getElementById('themeBtn');
    apply(window.movbacTheme());
    if (b) b.addEventListener('click', function () {
      var t = window.movbacTheme() === 'dark' ? 'light' : 'dark';
      try { localStorage.setItem(KEY, t); } catch (e) {}
      apply(t);
      window.dispatchEvent(new CustomEvent('movbac-theme', { detail: t }));
    });
  });
  if (window.matchMedia) matchMedia('(prefers-color-scheme: light)').addEventListener('change', function (e) {
    if (!saved()) { apply(e.matches ? 'light' : 'dark'); window.dispatchEvent(new CustomEvent('movbac-theme')); }
  });
})();
