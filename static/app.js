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
    // the map's area, so a typed address without a town resolves locally
    var c = window.wrMapCenter && window.wrMapCenter();
    if (c) {
      document.getElementById('near_lat').value = c.lat.toFixed(4);
      document.getElementById('near_lng').value = c.lng.toFixed(4);
    }
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

// Location field: type-ahead suggestions from the same-origin /suggest proxy.
// Built to feel instant even though the geocoder can take 1-3 s per lookup:
//  - results already fetched are cached here, so as you keep typing the list
//    narrows immediately from the closest earlier lookup while a fresh one runs;
//  - lookups are biased toward the area the map is showing (local places first);
//  - superseded lookups are cancelled, a spinner shows while one is in flight;
//  - focusing the field when it's empty offers your recent starts, no lookup at all.
(function () {
  var input = document.getElementById('location');
  if (!input) return;
  var holder = input.closest('.ac') || input.parentNode;
  var list = document.createElement('ul');
  list.className = 'ac-list';
  list.hidden = true;
  list.setAttribute('role', 'listbox');
  holder.appendChild(list);
  var spin = document.createElement('span');
  spin.className = 'ac-spin';
  spin.hidden = true;
  holder.appendChild(spin);

  var pLat = document.getElementById('picked_lat');
  var pLng = document.getElementById('picked_lng');
  var pLabel = document.getElementById('picked_label');
  function clearPicked() {            // typing invalidates any previously picked point
    if (pLat) pLat.value = '';
    if (pLng) pLng.value = '';
    if (pLabel) pLabel.value = '';
  }

  var RECENT_KEY = 'windroute:recent-starts', RECENT_MAX = 5, DEBOUNCE_MS = 120;
  var cache = {};                     // 'query|lat,lng' -> items
  var items = [], sel = -1, timer = null, lastQ = '', ctrl = null, showingRecent = false;

  function loadRecent() {
    try { var r = JSON.parse(localStorage.getItem(RECENT_KEY) || '[]'); return Array.isArray(r) ? r : []; }
    catch (e) { return []; }
  }
  function saveRecent(it) {
    if (!it || !it.label || /^[-\d]/.test(it.label)) return;    // skip raw lat,lng pins
    var r = loadRecent().filter(function (x) { return x.label !== it.label; });
    r.unshift({ label: it.label, lat: +it.lat, lng: +it.lng });
    try { localStorage.setItem(RECENT_KEY, JSON.stringify(r.slice(0, RECENT_MAX))); } catch (e) {}
  }

  function norm(s) { return s.toLowerCase().normalize('NFD').replace(/[̀-ͯ]/g, ''); }
  function tokens(q) { return norm(q).split(/[\s,]+/).filter(Boolean); }
  // where token t starts a word in normalized label l (-1 if nowhere), so "p"
  // matches "Park" but not "carPenter"
  function wordAt(l, t) {
    for (var at = l.indexOf(t); at !== -1; at = l.indexOf(t, at + 1)) {
      if (at === 0 || !/[a-z0-9]/.test(l.charAt(at - 1))) return at;
    }
    return -1;
  }
  function matches(label, toks) {
    var l = norm(label);
    return toks.every(function (t) { return wordAt(l, t) !== -1; });
  }
  function bias() {
    var c = window.wrMapCenter && window.wrMapCenter();
    return c ? c.lat.toFixed(1) + ',' + c.lng.toFixed(1) : '';
  }
  // The best cached answer for q: the longest earlier query that q starts with,
  // narrowed to labels that still contain every word typed.
  function fromCache(q) {
    var nq = norm(q), b = bias(), best = null;
    Object.keys(cache).forEach(function (k) {
      var cut = k.lastIndexOf('|'), kq = k.slice(0, cut);
      if (k.slice(cut + 1) === b && nq.indexOf(kq) === 0 && (best === null || kq.length > best.length)) best = kq;
    });
    if (best === null) return null;
    var toks = tokens(q);
    return cache[best + '|' + b].filter(function (it) { return matches(it.label, toks); });
  }

  function close() { list.hidden = true; list.innerHTML = ''; items = []; sel = -1; showingRecent = false; }
  function choose(i) {
    if (i < 0 || i >= items.length) return;
    var it = items[i];
    saveRecent(it);
    wrPicked(it.lat, it.lng, it.label);
    close();
  }

  // the label with the typed words in bold, built from DOM nodes (no innerHTML)
  function labelNode(label, toks) {
    var frag = document.createDocumentFragment(), l = norm(label), marks = [];
    toks.forEach(function (t) { var at = wordAt(l, t); if (at !== -1) marks.push([at, at + t.length]); });
    marks.sort(function (a, b) { return a[0] - b[0]; });
    var pos = 0;
    marks.forEach(function (m) {
      if (m[0] < pos) return;
      if (m[0] > pos) frag.appendChild(document.createTextNode(label.slice(pos, m[0])));
      var b = document.createElement('b');
      b.textContent = label.slice(m[0], m[1]);
      frag.appendChild(b);
      pos = m[1];
    });
    frag.appendChild(document.createTextNode(label.slice(pos)));
    return frag;
  }
  function render(q) {
    list.innerHTML = '';
    if (!items.length) { list.hidden = true; return; }
    var toks = showingRecent ? [] : tokens(q || '');
    if (showingRecent) {
      var hd = document.createElement('li');
      hd.className = 'ac-head';
      hd.textContent = 'Recent starts';
      list.appendChild(hd);
    }
    items.forEach(function (it, i) {
      var li = document.createElement('li');
      li.setAttribute('role', 'option');
      li.appendChild(labelNode(it.label, toks));
      if (i === sel) { li.className = 'sel'; li.setAttribute('aria-selected', 'true'); }
      li.addEventListener('mousedown', function (e) { e.preventDefault(); choose(i); });
      list.appendChild(li);
    });
    list.hidden = false;
  }
  // A typed house number that no suggestion has (OSM often lacks suburban house
  // numbers): say so, so the rider picks the street knowingly or drops a pin.
  function addressHint(q, data) {
    var m = /^\s*(\d+[a-z]?)\s+\S/i.exec(q);
    if (!m) return;
    var num = m[1].toLowerCase();
    if (data.some(function (it) {
      var lead = /^\s*(\d+[a-z]?)\b/i.exec(it.label);
      return lead && lead[1].toLowerCase() === num;
    })) return;
    var li = document.createElement('li');
    li.className = 'ac-hint';
    li.textContent = data.length
      ? 'No exact match for house number ' + m[1] + ' — pick the street, or click the map to drop a pin at your door.'
      : 'Can’t find that address — try adding the town, or click the map to drop a pin at your door.';
    list.appendChild(li);
    list.hidden = false;
  }
  function showRecent() {
    var r = loadRecent();
    if (!r.length) return;
    items = r; sel = -1; showingRecent = true;
    render('');
  }

  function lookup(q) {
    if (ctrl) ctrl.abort();
    ctrl = typeof AbortController !== 'undefined' ? new AbortController() : null;
    var b = bias(), key = norm(q) + '|' + b;
    var url = '/suggest?q=' + encodeURIComponent(q) +
      (b ? '&lat=' + b.split(',')[0] + '&lng=' + b.split(',')[1] : '');
    spin.hidden = false;
    fetch(url, ctrl ? { signal: ctrl.signal } : undefined)
      .then(function (r) { return r.ok ? r.json() : []; })
      .then(function (data) {
        data = Array.isArray(data) ? data : [];
        cache[key] = data;
        if (q !== lastQ) return;                       // a newer keystroke owns the list
        spin.hidden = true;
        if (!data.length && items.length) return;      // keep the narrowed list
        items = data; sel = -1; showingRecent = false;
        render(q);
        addressHint(q, data);
      })
      .catch(function (e) { if (!e || e.name !== 'AbortError') spin.hidden = true; });
  }

  input.addEventListener('input', function () {
    var q = input.value.trim();
    lastQ = q;
    clearPicked();
    clearTimeout(timer);
    // Skip anything that looks like a lat,lng pair (two numbers split by a comma).
    // House-number addresses ("123 Main St") are NOT skipped.
    if (/^[-+]?\d{1,3}(\.\d+)?\s*,\s*[-+]?\d{1,3}(\.\d+)?/.test(q)) { close(); spin.hidden = true; return; }
    if (q.length < 2) {
      spin.hidden = true;
      if (ctrl) ctrl.abort();
      close();
      if (!q) showRecent();
      return;
    }
    if (showingRecent) close();                       // typing replaces the recents
    var cached = fromCache(q);                        // instant: narrow what we have
    if (cached && cached.length) { items = cached; sel = -1; showingRecent = false; render(q); }
    if (cache[norm(q) + '|' + bias()]) { spin.hidden = true; return; }   // exact hit: done
    timer = setTimeout(function () { lookup(q); }, DEBOUNCE_MS);
  });

  // Clicking into a filled field selects it (typing replaces it) and offers recents.
  input.addEventListener('focus', function () {
    if (input.value) input.select();
    showRecent();
  });

  input.addEventListener('keydown', function (e) {
    if (list.hidden || !items.length) return;
    if (e.key === 'ArrowDown') { e.preventDefault(); sel = Math.min(sel + 1, items.length - 1); render(lastQ); }
    else if (e.key === 'ArrowUp') { e.preventDefault(); sel = Math.max(sel - 1, 0); render(lastQ); }
    else if (e.key === 'Enter' && sel >= 0) { e.preventDefault(); choose(sel); }
    else if (e.key === 'Escape') { close(); }
  });

  input.addEventListener('blur', function () { setTimeout(close, 120); });
})();
