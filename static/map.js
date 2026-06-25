// Interactive route maps + elevation profiles + a fullscreen route view.
// Leaflet draws each route on OpenStreetMap tiles; clicking the map opens Google
// Street View at that point (link-out, no API key); clicking a route's title opens
// a fullscreen map + larger elevation profile. Kept in a static file so the page's
// CSP can stay script-src 'self'.
(function () {
  if (typeof L === "undefined") return;                 // Leaflet failed to load

  // ---- route data (one <script class="route-data" data-route> per route) ----
  var routes = [];
  document.querySelectorAll("script.route-data").forEach(function (el) {
    var idx = parseInt(el.getAttribute("data-route"), 10);
    try {
      var d = JSON.parse(el.textContent || "{}");
      d.title = el.getAttribute("data-title") || "Route";
      routes[idx] = d;
    } catch (e) { /* skip malformed */ }
  });

  var TILE_URL = "https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png";
  var ATTRIB = '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors';

  function streetViewUrl(lat, lng) {
    return "https://www.google.com/maps/@?api=1&map_action=pano&viewpoint=" +
      lat.toFixed(6) + "," + lng.toFixed(6);
  }
  function svPopupHtml(lat, lng) {
    var sv = streetViewUrl(lat, lng);
    var osm = "https://www.openstreetmap.org/?mlat=" + lat.toFixed(6) +
      "&mlon=" + lng.toFixed(6) + "#map=17/" + lat.toFixed(5) + "/" + lng.toFixed(5);
    return '<div style="font-size:13px; line-height:1.7">' +
      '<a href="' + sv + '" target="_blank" rel="noopener">Street View here ↗</a><br>' +
      '<a href="' + osm + '" target="_blank" rel="noopener">Open in OpenStreetMap ↗</a></div>';
  }

  function makeMap(el, coords) {
    var map = L.map(el, { scrollWheelZoom: true });
    L.tileLayer(TILE_URL, { maxZoom: 19, attribution: ATTRIB }).addTo(map);
    var line = L.polyline(coords, { color: "#1d4ed8", weight: 4, opacity: 0.9 }).addTo(map);
    map.fitBounds(line.getBounds(), { padding: [22, 22] });
    var start = coords[0], end = coords[coords.length - 1];
    L.circleMarker(start, { radius: 7, color: "#fff", weight: 2,
      fillColor: "#15803d", fillOpacity: 1 }).addTo(map).bindPopup("Start / finish");
    if (Math.abs(end[0] - start[0]) > 1e-4 || Math.abs(end[1] - start[1]) > 1e-4) {
      L.circleMarker(end, { radius: 6, color: "#fff", weight: 2,
        fillColor: "#b45309", fillOpacity: 1 }).addTo(map).bindPopup("Turnaround");
    }
    function openAt(latlng) {
      L.popup().setLatLng(latlng).setContent(svPopupHtml(latlng.lat, latlng.lng)).openOn(map);
    }
    map.on("click", function (e) { openAt(e.latlng); });
    line.on("click", function (e) { L.DomEvent.stopPropagation(e); openAt(e.latlng); });
    return map;
  }

  // ---- elevation profile (lightweight inline SVG, no chart lib) ----
  function haversineKm(a, b) {
    var R = 6371, toRad = Math.PI / 180;
    var dLat = (b[0] - a[0]) * toRad, dLng = (b[1] - a[1]) * toRad;
    var s = Math.sin(dLat / 2) * Math.sin(dLat / 2) +
      Math.cos(a[0] * toRad) * Math.cos(b[0] * toRad) * Math.sin(dLng / 2) * Math.sin(dLng / 2);
    return 2 * R * Math.asin(Math.min(1, Math.sqrt(s)));
  }
  function smooth(vals, win) {
    if (win < 2) return vals.slice();
    var out = [], half = Math.floor(win / 2);
    for (var i = 0; i < vals.length; i++) {
      var lo = Math.max(0, i - half), hi = Math.min(vals.length - 1, i + half), s = 0;
      for (var j = lo; j <= hi; j++) s += vals[j];
      out.push(s / (hi - lo + 1));
    }
    return out;
  }
  function renderElev(container, coords, eles, height) {
    container.innerHTML = "";
    if (!eles || eles.length < 3 || eles.length !== coords.length) return;
    var W = Math.max(220, Math.round(container.clientWidth || 600));
    var padX = 6, padTop = 8, padBot = 16;
    var dist = [0];
    for (var i = 1; i < coords.length; i++) dist.push(dist[i - 1] + haversineKm(coords[i - 1], coords[i]));
    var total = dist[dist.length - 1] || 1;
    var ev = smooth(eles, Math.max(3, Math.round(eles.length / 80)));
    var lo = Math.min.apply(null, ev), hi = Math.max.apply(null, ev);
    var span = Math.max(1, hi - lo);
    var gain = 0;
    for (i = 1; i < ev.length; i++) { var d = ev[i] - ev[i - 1]; if (d > 0) gain += d; }
    var x = function (k) { return padX + (dist[k] / total) * (W - 2 * padX); };
    var y = function (v) { return padTop + (1 - (v - lo) / span) * (height - padTop - padBot); };
    var d2 = "M" + x(0).toFixed(1) + "," + y(ev[0]).toFixed(1);
    for (i = 1; i < ev.length; i++) d2 += "L" + x(i).toFixed(1) + "," + y(ev[i]).toFixed(1);
    var base = height - padBot;
    var area = d2 + "L" + x(ev.length - 1).toFixed(1) + "," + base + "L" + x(0).toFixed(1) + "," + base + "Z";
    var km = (total * 0.621371).toFixed(1);
    var svg =
      '<svg viewBox="0 0 ' + W + " " + height + '" width="' + W + '" height="' + height + '" role="img" aria-label="elevation profile">' +
      '<path d="' + area + '" fill="rgba(37,111,235,0.12)"/>' +
      '<path d="' + d2 + '" fill="none" stroke="#2563eb" stroke-width="2" stroke-linejoin="round"/>' +
      '<line x1="' + padX + '" y1="' + base + '" x2="' + (W - padX) + '" y2="' + base + '" stroke="#e7ecf3"/>' +
      '<text x="' + padX + '" y="' + (height - 4) + '" font-size="10" fill="#8b97a6">0</text>' +
      '<text x="' + (W - padX) + '" y="' + (height - 4) + '" font-size="10" fill="#8b97a6" text-anchor="end">' + km + ' mi</text>' +
      "</svg>";
    container.innerHTML = svg;
    var cap = document.createElement("div");
    cap.className = "elev-cap";
    cap.textContent = "↑ " + Math.round(gain) + " m climb · " + Math.round(lo) + "–" + Math.round(hi) + " m elevation";
    container.appendChild(cap);
  }

  // ---- inline maps + profiles ----
  document.querySelectorAll(".map[data-route]").forEach(function (el) {
    var r = routes[parseInt(el.getAttribute("data-route"), 10)];
    if (!r || !r.coords || r.coords.length < 2) return;
    makeMap(el, r.coords);
    setTimeout(function () { /* settle grid/flex sizing handled by Leaflet */ }, 0);
  });
  document.querySelectorAll(".elev[data-route]").forEach(function (el) {
    var r = routes[parseInt(el.getAttribute("data-route"), 10)];
    if (r) renderElev(el, r.coords, r.eles, 70);
  });

  // ---- fullscreen modal ----
  var modal = document.getElementById("route-modal");
  var modalMapEl = document.getElementById("modal-map");
  var modalElev = document.getElementById("modal-elev");
  var modalTitle = document.getElementById("modal-title");
  var modalMap = null;

  function openModal(idx) {
    var r = routes[idx];
    if (!r || !modal) return;
    modalTitle.textContent = r.title || "Route";
    modal.hidden = false;
    document.body.style.overflow = "hidden";
    if (modalMap) { modalMap.remove(); modalMap = null; }
    modalMap = makeMap(modalMapEl, r.coords);
    setTimeout(function () {
      modalMap.invalidateSize();
      modalMap.fitBounds(L.polyline(r.coords).getBounds(), { padding: [30, 30] });
      renderElev(modalElev, r.coords, r.eles, 130);
    }, 60);
  }
  function closeModal() {
    if (!modal) return;
    modal.hidden = true;
    document.body.style.overflow = "";
    if (modalMap) { modalMap.remove(); modalMap = null; }
    modalElev.innerHTML = "";
  }

  document.querySelectorAll(".opt-title[data-route]").forEach(function (el) {
    var idx = parseInt(el.getAttribute("data-route"), 10);
    el.addEventListener("click", function () { openModal(idx); });
    el.addEventListener("keydown", function (e) {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); openModal(idx); }
    });
  });
  if (modal) {
    modal.querySelectorAll("[data-close]").forEach(function (el) {
      el.addEventListener("click", closeModal);
    });
    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape" && !modal.hidden) closeModal();
    });
  }
})();
