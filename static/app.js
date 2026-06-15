// Disable the submit button and show a "working" hint while a plan runs.
// Kept in a static file (not inline) so the page can use a strict
// Content-Security-Policy with script-src 'self' (no 'unsafe-inline').
(function () {
  var form = document.getElementById('planform');
  if (!form) return;
  form.addEventListener('submit', function () {
    var go = document.getElementById('go');
    var working = document.getElementById('working');
    if (go) { go.disabled = true; go.textContent = 'Planning…'; }
    if (working) { working.style.display = 'inline'; }
  });
})();
