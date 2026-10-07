// The full-screen map behind both pages (static file so the CSP can stay
// script-src 'self').
//  - Plan form: click the map (or pick a suggestion / "my location") to set the
//    start; the map follows the location field.
//  - Results: every route on one map. Selecting a route (its card, its line, the
//    compare table, or arrow keys; swiping the carousel on phones) highlights it,
//    fits the map to it, adds direction-of-travel arrows, and shows its elevation
//    profile — hovering the profile tracks the point on the map.
(function () {
  if (typeof L === "undefined") return;                 // Leaflet failed to load
  var mapEl = document.getElementById("map");
  if (!mapEl) return;

  var TILE_URL = "https://tile.openstreetmap.org/{z}/{x}/{y}.png";
  var ATTRIB = '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors';
  var LAST_KEY = "windroute:last-start";

  function store(key, val) { try { localStorage.setItem(key, JSON.stringify(val)); } catch (e) {} }
  function load(key) { try { return JSON.parse(localStorage.getItem(key) || "null"); } catch (e) { return null; } }

  function makeMap(zoomPos) {
    var map = L.map(mapEl, { zoomControl: false, scrollWheelZoom: true });
    L.control.zoom({ position: zoomPos }).addTo(map);
    L.tileLayer(TILE_URL, { maxZoom: 19, attribution: ATTRIB }).addTo(map);
    return map;
  }
  function startPin(latlng) {
    return L.marker(latlng, { keyboard: false, zIndexOffset: 1000,
      icon: L.divIcon({ className: "", html: '<div class="start-pin"></div>', iconSize: [18, 18], iconAnchor: [9, 9] }) });
  }
  function svPopupHtml(lat, lng) {
    var sv = "https://www.google.com/maps/@?api=1&map_action=pano&viewpoint=" + lat.toFixed(6) + "," + lng.toFixed(6);
    var osm = "https://www.openstreetmap.org/?mlat=" + lat.toFixed(6) + "&mlon=" + lng.toFixed(6) +
      "#map=17/" + lat.toFixed(5) + "/" + lng.toFixed(5);
    return '<div style="font-size:13px; line-height:1.8">' +
      '<a href="' + sv + '" target="_blank" rel="noopener">Street View here ↗</a><br>' +
      '<a href="' + osm + '" target="_blank" rel="noopener">Open in OpenStreetMap ↗</a></div>';
  }

  var dataEl = document.getElementById("route-data");
  if (dataEl) results(); else planForm();

  // ======================================================== plan form
  function planForm() {
    var map = makeMap("topright");
    var marker = null;
    var tip = document.getElementById("map-tip");
    var loc = document.getElementById("location");
    var pLat = document.getElementById("picked_lat");
    var pLng = document.getElementById("picked_lng");
    var pLabel = document.getElementById("picked_label");

    function setStart(lat, lng, zoom) {
      if (marker) marker.setLatLng([lat, lng]); else marker = startPin([lat, lng]).addTo(map);
      if (zoom) map.flyTo([lat, lng], Math.max(map.getZoom(), zoom), { duration: 0.8 });
      if (tip) tip.style.display = "none";
    }

    var lat0 = parseFloat(pLat && pLat.value), lng0 = parseFloat(pLng && pLng.value);
    var last = load(LAST_KEY);
    if (isFinite(lat0) && isFinite(lng0)) { map.setView([lat0, lng0], 12); setStart(lat0, lng0); }
    else if (last && isFinite(last.lat)) map.setView([last.lat, last.lng], 11);
    else map.setView([39.5, -96], 4);                    // continental US

    map.on("click", function (e) {
      var lat = e.latlng.lat, lng = e.latlng.lng;
      var label = lat.toFixed(5) + ", " + lng.toFixed(5);
      loc.value = label;
      pLat.value = lat; pLng.value = lng; pLabel.value = label;
      setStart(lat, lng);
    });
    // a suggestion picked from the location field, or "use my location"
    window.addEventListener("wr:picked", function (e) {
      if (e.detail) setStart(e.detail.lat, e.detail.lng, 12);
    });
  }

  // ======================================================== results
  function results() {
    var payload;
    try { payload = JSON.parse(dataEl.textContent || "{}"); } catch (e) { return; }
    var routes = payload.routes || [];
    if (!routes.length) return;
    var unitMi = payload.unit !== "km";
    var map = makeMap("topleft");
    var mobile = window.matchMedia("(max-width: 900px)");
    var showAll = false, selected = -1;
    var arrows = L.layerGroup().addTo(map);
    var hoverPin = null;

    // per-route cumulative distance (km) for the profile + arrows
    function haversineKm(a, b) {
      var R = 6371, k = Math.PI / 180;
      var dLat = (b[0] - a[0]) * k, dLng = (b[1] - a[1]) * k;
      var s = Math.sin(dLat / 2) * Math.sin(dLat / 2) +
        Math.cos(a[0] * k) * Math.cos(b[0] * k) * Math.sin(dLng / 2) * Math.sin(dLng / 2);
      return 2 * R * Math.asin(Math.min(1, Math.sqrt(s)));
    }
    function bearing(a, b) {
      var k = Math.PI / 180, y = Math.sin((b[1] - a[1]) * k) * Math.cos(b[0] * k);
      var x = Math.cos(a[0] * k) * Math.sin(b[0] * k) - Math.sin(a[0] * k) * Math.cos(b[0] * k) * Math.cos((b[1] - a[1]) * k);
      return (Math.atan2(y, x) / k + 360) % 360;
    }
    routes.forEach(function (r) {
      var d = [0];
      for (var i = 1; i < r.coords.length; i++) d.push(d[i - 1] + haversineKm(r.coords[i - 1], r.coords[i]));
      r.cum = d;
      r.casing = L.polyline(r.coords, { color: "#fff", weight: 9, opacity: 0.95, interactive: false });
      r.line = L.polyline(r.coords, { color: r.color, weight: 4, opacity: 0.6 });
      r.line.on("click", function (e) {
        L.DomEvent.stopPropagation(e);
        if (selected === r.id) openSV(e.latlng); else select(r.id, { from: "map" });
      });
      r.line.bindTooltip(r.title, { sticky: true, direction: "top", opacity: 0.9 });
    });

    var start = routes[0].coords[0];
    startPin(start).addTo(map).bindPopup("Start / finish");
    store(LAST_KEY, { lat: start[0], lng: start[1] });

    function openSV(latlng) {
      L.popup().setLatLng(latlng).setContent(svPopupHtml(latlng.lat, latlng.lng)).openOn(map);
    }
    map.on("click", function (e) { openSV(e.latlng); });

    function styleAll() {
      routes.forEach(function (r) {
        var isSel = r.id === selected;
        var visible = isSel || r.pick || showAll;
        if (!visible) {
          map.removeLayer(r.line); map.removeLayer(r.casing);
          return;
        }
        if (isSel) {
          r.casing.addTo(map);
          r.line.setStyle({ weight: 5.5, opacity: 1 }).addTo(map);
        } else {
          map.removeLayer(r.casing);
          r.line.setStyle({ weight: r.pick ? 3.5 : 2.5, opacity: r.pick ? 0.55 : 0.4 }).addTo(map);
        }
      });
      var sel = routes[selected];
      if (sel) { sel.casing.bringToFront(); sel.line.bringToFront(); }
    }

    function drawArrows(r) {
      arrows.clearLayers();
      var total = r.cum[r.cum.length - 1];
      if (!total) return;
      var n = Math.max(6, Math.min(16, Math.round(total / 4)));
      var j = 1;
      for (var k = 1; k < n; k++) {
        var target = (total * k) / n;
        while (j < r.cum.length - 1 && r.cum[j] < target) j++;
        var a = r.coords[j - 1], b = r.coords[j];
        var deg = bearing(a, b);
        L.marker(b, { interactive: false, keyboard: false, icon: L.divIcon({
          className: "dir-arrow", iconSize: [16, 16], iconAnchor: [8, 8],
          html: '<svg viewBox="0 0 16 16" style="transform:rotate(' + deg.toFixed(0) + 'deg)">' +
            '<circle cx="8" cy="8" r="7.5" fill="' + r.color + '"/>' +
            '<path d="M4.8 9.6 8 5.4l3.2 4.2" fill="none" stroke="#fff" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>' }) })
          .addTo(arrows);
      }
    }

    function fitTo(r) {
      var bounds = r.line.getBounds();
      var pad = mobile.matches ? { tl: [20, 60], br: [20, 90] } : { tl: [50, 70], br: [50, 190] };
      map.flyToBounds(bounds, { paddingTopLeft: pad.tl, paddingBottomRight: pad.br, duration: 0.6, maxZoom: 15 });
    }

    var cards = Array.prototype.slice.call(document.querySelectorAll(".rcard[data-route]"));
    var rows = Array.prototype.slice.call(document.querySelectorAll("tr[data-route]"));

    function select(id, opts) {
      opts = opts || {};
      var r = routes[id];
      if (!r) return;
      selected = id;
      styleAll();
      drawArrows(r);
      if (!opts.noFit) fitTo(r);
      cards.forEach(function (c) { c.classList.toggle("active", +c.getAttribute("data-route") === id); });
      rows.forEach(function (c) { c.classList.toggle("active", +c.getAttribute("data-route") === id); });
      var card = cards.filter(function (c) { return +c.getAttribute("data-route") === id; })[0];
      if (card && opts.from !== "carousel") {
        card.scrollIntoView({ block: "nearest", inline: "center", behavior: opts.instant ? "auto" : "smooth" });
      }
      renderProfile(r);
    }

    cards.forEach(function (c) {
      var id = +c.getAttribute("data-route");
      c.addEventListener("click", function (e) {
        if (e.target.closest("a")) return;              // the GPX link
        select(id, { from: "card" });
      });
      c.addEventListener("keydown", function (e) {
        if (e.key === "Enter" || e.key === " ") { e.preventDefault(); select(id, { from: "card" }); }
      });
    });
    rows.forEach(function (row) {
      row.addEventListener("click", function () { select(+row.getAttribute("data-route"), { from: "table" }); });
    });
    document.addEventListener("keydown", function (e) {
      if (/^(INPUT|SELECT|TEXTAREA)$/.test((e.target.tagName || ""))) return;
      if (e.key === "ArrowDown" || e.key === "ArrowUp") {
        e.preventDefault();
        var order = cards.map(function (c) { return +c.getAttribute("data-route"); });
        var at = order.indexOf(selected);
        var next = order[(at + (e.key === "ArrowDown" ? 1 : order.length - 1)) % order.length];
        select(next, { from: "key" });
        var card = cards[order.indexOf(next)];
        if (card) card.focus({ preventScroll: true });
      }
    });

    document.querySelectorAll('input[name="layers"]').forEach(function (inp) {
      inp.addEventListener("change", function () { showAll = inp.value === "all" && inp.checked; styleAll(); });
    });

    // phones: the card lists are swipeable carousels — the card you settle on is selected
    if ("IntersectionObserver" in window) {
      document.querySelectorAll(".routes.carousel").forEach(function (track) {
        var io = new IntersectionObserver(function (entries) {
          if (!mobile.matches) return;
          entries.forEach(function (en) {
            if (en.isIntersecting && en.intersectionRatio > 0.75) {
              var id = +en.target.getAttribute("data-route");
              if (id !== selected) select(id, { from: "carousel" });
            }
          });
        }, { root: track, threshold: [0.75] });
        track.querySelectorAll(".rcard").forEach(function (c) { io.observe(c); });
      });
    }

    // ---- elevation profile (inline SVG) with hover-tracking on the map ----
    var elevEl = document.getElementById("elev");
    var nameEl = document.getElementById("elev-name");
    var metaEl = document.getElementById("elev-meta");
    var dotEl = document.getElementById("elev-dot");

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

    function renderProfile(r) {
      if (nameEl) nameEl.textContent = r.title;
      if (metaEl) metaEl.textContent = r.meta || "";
      if (dotEl) dotEl.style.setProperty("--c", r.color);
      if (!elevEl) return;
      elevEl.innerHTML = "";
      var eles = r.eles || [];
      if (eles.length < 3 || eles.length !== r.coords.length) {
        elevEl.innerHTML = '<div class="elev-empty">No elevation data for this route.</div>';
        return;
      }
      var H = mobile.matches ? 64 : 96;
      var W = Math.max(260, Math.round(elevEl.clientWidth || 600));
      var padL = 34, padR = 8, padT = 6, padB = 16;
      var total = r.cum[r.cum.length - 1] || 1;
      var ev = smooth(eles, Math.max(3, Math.round(eles.length / 90)));
      var lo = Math.min.apply(null, ev), hi = Math.max.apply(null, ev);
      var span = Math.max(10, hi - lo);
      lo -= span * 0.08; span *= 1.16;
      var X = function (km) { return padL + (km / total) * (W - padL - padR); };
      var Y = function (v) { return padT + (1 - (v - lo) / span) * (H - padT - padB); };
      var d = "M" + X(0).toFixed(1) + "," + Y(ev[0]).toFixed(1);
      for (var i = 1; i < ev.length; i++) d += "L" + X(r.cum[i]).toFixed(1) + "," + Y(ev[i]).toFixed(1);
      var base = H - padB;
      var area = d + "L" + X(total).toFixed(1) + "," + base + "L" + X(0).toFixed(1) + "," + base + "Z";
      var unitLen = unitMi ? total * 0.621371 : total;
      var gid = "eg" + r.id;
      var svg =
        '<svg viewBox="0 0 ' + W + " " + H + '" width="' + W + '" height="' + H + '" role="img" aria-label="Elevation profile">' +
        '<defs><linearGradient id="' + gid + '" x1="0" y1="0" x2="0" y2="1">' +
        '<stop offset="0" stop-color="' + r.color + '" stop-opacity=".35"/><stop offset="1" stop-color="' + r.color + '" stop-opacity=".03"/></linearGradient></defs>' +
        '<path d="' + area + '" fill="url(#' + gid + ')"/>' +
        '<path d="' + d + '" fill="none" stroke="' + r.color + '" stroke-width="2" stroke-linejoin="round"/>' +
        '<line x1="' + padL + '" y1="' + base + '" x2="' + (W - padR) + '" y2="' + base + '" style="stroke:var(--line-2)"/>' +
        '<text x="' + (padL - 5) + '" y="' + (Y(hi) + 4).toFixed(1) + '" font-size="10" text-anchor="end" style="fill:var(--faint)">' + Math.round(hi) + '</text>' +
        '<text x="' + (padL - 5) + '" y="' + (base).toFixed(1) + '" font-size="10" text-anchor="end" style="fill:var(--faint)">' + Math.round(Math.min.apply(null, ev)) + ' m</text>' +
        '<text x="' + padL + '" y="' + (H - 3) + '" font-size="10" style="fill:var(--faint)">0</text>' +
        '<text x="' + (W - padR) + '" y="' + (H - 3) + '" font-size="10" text-anchor="end" style="fill:var(--faint)">' + unitLen.toFixed(1) + (unitMi ? " mi" : " km") + '</text>' +
        '<g id="elev-hover" style="display:none"><line y1="' + padT + '" y2="' + base + '" style="stroke:var(--dim)" stroke-dasharray="3 3"/>' +
        '<circle r="4" fill="#fff" stroke="' + r.color + '" stroke-width="2.5"/>' +
        '<text font-size="11" font-weight="700" style="fill:var(--ink)"></text></g>' +
        '<rect x="' + padL + '" y="0" width="' + (W - padL - padR) + '" height="' + H + '" fill="transparent" style="cursor:crosshair"/>' +
        "</svg>";
      elevEl.innerHTML = svg;

      var svgEl = elevEl.querySelector("svg");
      var g = svgEl.querySelector("#elev-hover");
      var hl = g.querySelector("line"), hc = g.querySelector("circle"), ht = g.querySelector("text");
      function at(clientX) {
        var box = svgEl.getBoundingClientRect();
        var x = ((clientX - box.left) / box.width) * W;
        var km = Math.max(0, Math.min(total, ((x - padL) / (W - padL - padR)) * total));
        var lo2 = 0, hi2 = r.cum.length - 1;               // nearest point by distance
        while (hi2 - lo2 > 1) { var mid = (lo2 + hi2) >> 1; if (r.cum[mid] < km) lo2 = mid; else hi2 = mid; }
        var k = (km - r.cum[lo2] < r.cum[hi2] - km) ? lo2 : hi2;
        var px = X(r.cum[k]), py = Y(ev[k]);
        g.style.display = "";
        hl.setAttribute("x1", px); hl.setAttribute("x2", px);
        hc.setAttribute("cx", px); hc.setAttribute("cy", py);
        var dist = unitMi ? r.cum[k] * 0.621371 : r.cum[k];
        ht.textContent = Math.round(eles[k]) + " m · " + dist.toFixed(1) + (unitMi ? " mi" : " km");
        var right = px > W * 0.7;
        ht.setAttribute("x", right ? px - 8 : px + 8);
        ht.setAttribute("text-anchor", right ? "end" : "start");
        ht.setAttribute("y", Math.max(padT + 10, py - 8));
        var ll = r.coords[k];
        if (!hoverPin) {
          hoverPin = L.marker(ll, { interactive: false, keyboard: false, zIndexOffset: 2000,
            icon: L.divIcon({ className: "", html: '<div class="hover-pin"></div>', iconSize: [14, 14], iconAnchor: [7, 7] }) });
        }
        hoverPin.setLatLng(ll).addTo(map);
        var pin = hoverPin.getElement && hoverPin.getElement();
        if (pin && pin.firstChild) pin.firstChild.style.setProperty("--c", r.color);
      }
      function out() { g.style.display = "none"; if (hoverPin) map.removeLayer(hoverPin); }
      svgEl.addEventListener("mousemove", function (e) { at(e.clientX); });
      svgEl.addEventListener("mouseleave", out);
      svgEl.addEventListener("touchmove", function (e) { if (e.touches[0]) at(e.touches[0].clientX); }, { passive: true });
      svgEl.addEventListener("touchend", out);
    }

    var resizeT = null;
    window.addEventListener("resize", function () {
      clearTimeout(resizeT);
      resizeT = setTimeout(function () { map.invalidateSize(); if (routes[selected]) renderProfile(routes[selected]); }, 150);
    });

    // initial view: everything, then settle on the top pick
    var all = L.featureGroup(routes.filter(function (r) { return r.pick; }).map(function (r) { return r.line; }));
    map.fitBounds(all.getBounds(), { padding: [40, 40] });
    select(0, { instant: true });
  }
})();
