// Plan-form behaviour. Kept in a static file (not inline) so the page can use a
// strict Content-Security-Policy with script-src 'self' (no 'unsafe-inline').

// While a plan runs: disable the button and show a progress overlay on the map
// that walks through what the planner is doing.
(function () {
  var form = document.getElementById('planform');
  if (!form) return;
  var STEPS = ['Checking the wind forecast…', 'Sketching loops around your start…',
               'Routing candidates on real roads…', 'Reading surfaces & traffic…',
               'Scoring each route on the wind you’ll meet…', 'Picking the best options…'];
  form.addEventListener('submit', function () {
    var go = document.getElementById('go');
    var overlay = document.getElementById('loading');
    var step = document.getElementById('loading-step');
    if (go) { go.disabled = true; go.lastChild.textContent = ' Planning…'; }
    if (overlay) overlay.hidden = false;
    var i = 0;
    if (step) setInterval(function () { i = Math.min(i + 1, STEPS.length - 1); step.textContent = STEPS[i]; }, 5500);
  });
  // coming back via the browser's back button: un-stick the busy state
  window.addEventListener('pageshow', function (e) {
    if (!e.persisted) return;
    var go = document.getElementById('go');
    var overlay = document.getElementById('loading');
    if (go) { go.disabled = false; go.lastChild.textContent = ' Plan my rides'; }
    if (overlay) overlay.hidden = true;
  });
})();

// Tell the map a start point was chosen in the panel (suggestion / my location).
function wrPicked(lat, lng, label) {
  var f = function (id, v) { var el = document.getElementById(id); if (el) el.value = v; };
  f('picked_lat', lat); f('picked_lng', lng); f('picked_label', label); f('location', label);
  try { window.dispatchEvent(new CustomEvent('wr:picked', { detail: { lat: +lat, lng: +lng } })); } catch (e) {}
}

// "Use my location" button.
(function () {
  var btn = document.getElementById('locate');
  if (!btn) return;
  if (!('geolocation' in navigator)) { btn.hidden = true; return; }
  btn.addEventListener('click', function () {
    btn.disabled = true;
    navigator.geolocation.getCurrentPosition(function (pos) {
      btn.disabled = false;
      var lat = pos.coords.latitude.toFixed(5), lng = pos.coords.longitude.toFixed(5);
      wrPicked(lat, lng, lat + ', ' + lng);
    }, function () { btn.disabled = false; }, { enableHighAccuracy: true, timeout: 10000 });
  });
})();

// Pace unit follows the distance unit (mph / km/h).
(function () {
  var lbl = document.getElementById('speed-unit');
  if (!lbl) return;
  document.querySelectorAll('input[name="unit"]').forEach(function (r) {
    r.addEventListener('change', function () { if (r.checked) lbl.textContent = r.value === 'km' ? 'km/h' : 'mph'; });
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

// Distance quick-pick chips: clicking one fills the distance field.
(function () {
  var dist = document.getElementById('distance');
  if (!dist) return;
  document.querySelectorAll('.chip[data-dist]').forEach(function (chip) {
    chip.addEventListener('click', function () {
      dist.value = chip.getAttribute('data-dist');
      dist.focus();
    });
  });
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
    wrPicked(items[i].lat, items[i].lng, items[i].label);
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
