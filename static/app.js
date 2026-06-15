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

// Start-time field: default to the current local hour (matching what "now" means)
// and let the "Set to now" link reset it.
(function () {
  var start = document.getElementById('start');
  if (!start) return;
  function localNowHour() {
    var d = new Date();
    d.setMinutes(0, 0, 0);                              // round to the hour
    var local = new Date(d.getTime() - d.getTimezoneOffset() * 60000);
    return local.toISOString().slice(0, 16);           // 'YYYY-MM-DDTHH:MM' (local)
  }
  if (!start.value) start.value = localNowHour();
  var btn = document.getElementById('nowbtn');
  if (btn) btn.addEventListener('click', function () { start.value = localNowHour(); });
})();

// Location field: debounced type-ahead suggestions from the same-origin /suggest
// proxy, with mouse + keyboard selection.
(function () {
  var input = document.getElementById('location');
  if (!input) return;
  var holder = input.closest('.ac') || input.parentNode;
  var list = document.createElement('ul');
  list.className = 'ac-list';
  list.hidden = true;
  holder.appendChild(list);

  var pLat = document.getElementById('picked_lat');
  var pLng = document.getElementById('picked_lng');
  var pLabel = document.getElementById('picked_label');
  function setPicked(it) {            // remember the exact point behind a chosen label
    if (pLat) pLat.value = it ? it.lat : '';
    if (pLng) pLng.value = it ? it.lng : '';
    if (pLabel) pLabel.value = it ? it.label : '';
  }

  var items = [], sel = -1, timer = null, lastQ = '';

  function close() { list.hidden = true; list.innerHTML = ''; items = []; sel = -1; }

  function choose(i) {
    if (i < 0 || i >= items.length) return;
    input.value = items[i].label;
    setPicked(items[i]);
    close();
  }

  function render() {
    list.innerHTML = '';
    if (!items.length) { close(); return; }
    items.forEach(function (it, i) {
      var li = document.createElement('li');
      li.textContent = it.label;
      if (i === sel) li.className = 'sel';
      li.addEventListener('mousedown', function (e) { e.preventDefault(); choose(i); });
      list.appendChild(li);
    });
    list.hidden = false;
  }

  input.addEventListener('input', function () {
    var q = input.value.trim();
    lastQ = q;
    setPicked(null);                  // typing invalidates any previously picked point
    clearTimeout(timer);
    // Skip short queries and anything that looks like a lat,lng pair (two numbers
    // split by a comma). House-number addresses ("123 Main St") are NOT skipped.
    if (q.length < 2 || /^[-+]?\d{1,3}(\.\d+)?\s*,\s*[-+]?\d{1,3}(\.\d+)?/.test(q)) {
      close(); return;
    }
    timer = setTimeout(function () {
      fetch('/suggest?q=' + encodeURIComponent(q))
        .then(function (r) { return r.ok ? r.json() : []; })
        .then(function (data) {
          if (q !== lastQ) return;                       // ignore stale responses
          items = Array.isArray(data) ? data : [];
          sel = -1;
          render();
        })
        .catch(function () { close(); });
    }, 220);
  });

  input.addEventListener('keydown', function (e) {
    if (list.hidden || !items.length) return;
    if (e.key === 'ArrowDown') { e.preventDefault(); sel = Math.min(sel + 1, items.length - 1); render(); }
    else if (e.key === 'ArrowUp') { e.preventDefault(); sel = Math.max(sel - 1, 0); render(); }
    else if (e.key === 'Enter' && sel >= 0) { e.preventDefault(); choose(sel); }
    else if (e.key === 'Escape') { close(); }
  });

  input.addEventListener('blur', function () { setTimeout(close, 120); });
})();
