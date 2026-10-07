// Share links. A plan's routes are packed into the link's #fragment — simplified,
// delta-encoded, deflated and base64url'd — so a link works forever, nothing is
// stored server-side, and the server never even sees the route (fragments aren't
// sent in requests). The /share page decodes it and draws the same map + cards.
(function () {
  var VERSION = 1;
  var SIMPLIFY_M = 4;                         // max deviation when thinning a route

  // ---- geometry: Douglas-Peucker on a local metre grid ----
  function simplifyIdx(coords, tolM) {
    var n = coords.length;
    if (n < 3) return coords.map(function (_, i) { return i; });
    var lat0 = coords[0][0] * Math.PI / 180;
    var kx = 111320 * Math.cos(lat0), ky = 110540;
    var xy = coords.map(function (c) { return [c[1] * kx, c[0] * ky]; });
    var keep = new Uint8Array(n); keep[0] = keep[n - 1] = 1;
    var stack = [[0, n - 1]], tol2 = tolM * tolM;
    while (stack.length) {
      var seg = stack.pop(), a = seg[0], b = seg[1];
      var ax = xy[a][0], ay = xy[a][1], dx = xy[b][0] - ax, dy = xy[b][1] - ay;
      var len2 = dx * dx + dy * dy, best = -1, bi = -1;
      for (var i = a + 1; i < b; i++) {
        var px = xy[i][0] - ax, py = xy[i][1] - ay, d2;
        if (len2 === 0) d2 = px * px + py * py;
        else {
          var t = Math.max(0, Math.min(1, (px * dx + py * dy) / len2));
          var ex = px - t * dx, ey = py - t * dy;
          d2 = ex * ex + ey * ey;
        }
        if (d2 > best) { best = d2; bi = i; }
      }
      if (best > tol2) { keep[bi] = 1; stack.push([a, bi], [bi, b]); }
    }
    var out = [];
    for (var j = 0; j < n; j++) if (keep[j]) out.push(j);
    return out;
  }
  function deltas(vals, scale) {
    var out = [], prev = 0;
    vals.forEach(function (v) { var q = Math.round(v * scale); out.push(q - prev); prev = q; });
    return out;
  }
  function undeltas(ds, scale) {
    var out = [], acc = 0;
    ds.forEach(function (d) { acc += d; out.push(acc / scale); });
    return out;
  }

  // ---- bytes <-> base64url, optional deflate ----
  function b64url(bytes) {
    var s = "", CH = 0x8000;
    for (var i = 0; i < bytes.length; i += CH) s += String.fromCharCode.apply(null, bytes.subarray(i, i + CH));
    return btoa(s).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  }
  function unb64url(str) {
    var s = atob(str.replace(/-/g, "+").replace(/_/g, "/"));
    var out = new Uint8Array(s.length);
    for (var i = 0; i < s.length; i++) out[i] = s.charCodeAt(i);
    return out;
  }
  function pipe(bytes, stream) {
    return new Response(new Blob([bytes]).stream().pipeThrough(stream)).arrayBuffer()
      .then(function (buf) { return new Uint8Array(buf); });
  }
  var canZip = typeof CompressionStream !== "undefined" && typeof DecompressionStream !== "undefined";

  // ---- encode: page payload -> fragment string ----
  function encode(payload, selectedId) {
    var routes = payload.routes.filter(function (r) { return r.pick || r.id === selectedId; });
    var packed = {
      v: VERSION, u: payload.unit, rt: payload.ride_type, p: payload.plan, f: payload.field,
      s: Math.max(0, routes.map(function (r) { return r.id; }).indexOf(selectedId)),
      r: routes.map(function (r) {
        var idx = simplifyIdx(r.coords, SIMPLIFY_M);
        var lat = [], lng = [], ele = [];
        idx.forEach(function (i) {
          lat.push(r.coords[i][0]); lng.push(r.coords[i][1]);
          if (r.eles && r.eles.length === r.coords.length) ele.push(r.eles[i]);
        });
        return { c: r.card, co: r.color, t: r.title, m: r.meta, pk: r.pick ? 1 : 0,
                 la: deltas(lat, 1e5), ln: deltas(lng, 1e5), el: deltas(ele, 1) };
      })
    };
    var bytes = new TextEncoder().encode(JSON.stringify(packed));
    if (!canZip) return Promise.resolve("j" + b64url(bytes));
    return pipe(bytes, new CompressionStream("deflate-raw")).then(function (z) { return "z" + b64url(z); });
  }

  // ---- decode: fragment string -> page payload ----
  function decode(code) {
    code = (code || "").replace(/^#/, "");
    var kind = code.charAt(0), body = code.slice(1);
    var bytesP;
    try {
      if (kind === "z") {
        if (!canZip) return Promise.reject(new Error("This browser can't open compressed share links."));
        bytesP = pipe(unb64url(body), new DecompressionStream("deflate-raw"));
      } else if (kind === "j") bytesP = Promise.resolve(unb64url(body));
      else return Promise.reject(new Error("not a share link"));
    } catch (e) { return Promise.reject(e); }
    return bytesP.then(function (bytes) {
      var d = JSON.parse(new TextDecoder().decode(bytes));
      if (!d || d.v !== VERSION || !Array.isArray(d.r) || !d.r.length) throw new Error("bad share link");
      var routes = d.r.map(function (r, i) {
        var lat = undeltas(r.la, 1e5), lng = undeltas(r.ln, 1e5);
        return { id: i, color: r.co, pick: !!r.pk, title: r.t, meta: r.m, card: r.c || {},
                 coords: lat.map(function (a, k) { return [a, lng[k]]; }),
                 eles: undeltas(r.el || [], 1) };
      });
      return { unit: d.u, ride_type: d.rt, plan: d.p || {}, field: d.f || null,
               routes: routes, selected: d.s || 0 };
    });
  }

  // ---- GPX built in the browser (shared pages have no server-side files) ----
  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }
  function gpxBlobUrl(r) {
    var pts = r.coords.map(function (c, i) {
      var e = r.eles && r.eles.length === r.coords.length ? "<ele>" + r.eles[i] + "</ele>" : "";
      return '<trkpt lat="' + c[0].toFixed(5) + '" lon="' + c[1].toFixed(5) + '">' + e + "</trkpt>";
    }).join("");
    var xml = '<?xml version="1.0" encoding="UTF-8"?>\n<gpx version="1.1" creator="windroute" ' +
      'xmlns="http://www.topografix.com/GPX/1/1"><trk><name>' + esc(r.title) + "</name><trkseg>" +
      pts + "</trkseg></trk></gpx>\n";
    return URL.createObjectURL(new Blob([xml], { type: "application/gpx+xml" }));
  }

  // ---- shared page panel (mirrors results.html's markup) ----
  var IC = {
    dist: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 12h18M3 12l4-4M3 12l4 4M21 12l-4-4M21 12l-4 4"/></svg>',
    time: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></svg>',
    climb: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 18l6-8 4 5 3-4 5 7z"/></svg>',
    dl: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 3v12M7 10l5 5 5-5M5 21h14"/></svg>'
  };
  function stat(cls, html) { return '<span class="stat ' + (cls || "") + '">' + html + "</span>"; }
  function cardHtml(r, rideType) {
    var c = r.card, ws = +c.wind_score || 0;
    var s = stat("", IC.dist + esc(c.dist));
    if (c.ride_time) s += stat("", IC.time + esc(c.ride_time));
    s += stat("", IC.climb + "+" + esc(c.climb));
    s += stat(ws > 0.2 ? "good" : ws < -0.2 ? "bad" : "", esc(c.verdict));
    if (c.gravel_pct >= 1) s += stat(rideType === "gravel" ? "good" : "", Math.round(c.gravel_pct) + "% " + (rideType === "gravel" ? "unpaved" : "gravel"));
    if (c.hwy_pct >= 5) s += stat("warn", Math.round(c.hwy_pct) + "% busy road");
    if (c.lane_pct >= 1) s += stat("good", Math.round(c.lane_pct) + "% bike lane");
    if (c.unrideable_pct > 0) s += stat("bad", Math.round(c.unrideable_pct) + "% unrideable");
    var why = (c.reasons || []).map(function (x) { return "<li>" + esc(x) + "</li>"; }).join("");
    return '<div class="rcard' + (r.pick ? "" : " compact") + '" role="button" tabindex="0" data-route="' + r.id +
      '" style="--c: ' + esc(r.color) + '" aria-label="' + esc(c.headline || r.title) + '">' +
      '<div class="top">' + (c.role === "recommended" ? '<span class="badge rec">Top pick</span>' : "") +
      '<span class="name">' + esc(c.headline || r.title) + "</span>" +
      (c.rank ? '<span class="rank">#' + esc(c.rank) + "</span>" : "") + "</div>" +
      '<div class="windline">' + esc(c.wind_line) + "</div>" +
      '<div class="stats">' + s + "</div>" + (why ? '<ul class="why">' + why + "</ul>" : "") +
      '<div class="acts"><a class="btn sm primary" href="' + gpxBlobUrl(r) + '" download="' +
      esc(c.dlname || "windroute.gpx") + '">' + IC.dl + " Download GPX</a></div></div>";
  }
  function windHtml(w) {
    if (!w || !w.known) return "";
    return '<div class="wind"><svg class="compass" viewBox="0 0 60 60" aria-hidden="true">' +
      '<circle cx="30" cy="30" r="27" style="fill:var(--surface);stroke:var(--line-2)"/>' +
      '<g stroke-width="1.5" stroke-linecap="round" style="stroke:var(--line-2)"><path d="M30 5v4M30 51v4M5 30h4M51 30h4"/></g>' +
      '<text x="30" y="17" text-anchor="middle" font-size="7.5" font-weight="700" style="fill:var(--faint)">N</text>' +
      '<g transform="rotate(' + (+w.deg || 0) + ' 30 30)"><line x1="30" y1="12" x2="30" y2="40" stroke-width="3" stroke-linecap="round" style="stroke:var(--brand)"/>' +
      '<path d="M30 47 l-6 -9 h12 z" style="fill:var(--brand)"/></g></svg>' +
      '<div style="min-width:0"><div class="big">' + Math.round(w.mph) + " mph from " + esc(w.from) + "</div>" +
      '<div class="sub">gusts ' + Math.round(w.gust) + " mph · forecast for " + esc(w.when) + "</div></div></div>";
  }
  function timelineHtml(tl) {
    if (!tl || !tl.length) return "";
    return '<div class="eyebrow">Wind at the start, hour by hour</div><div class="timeline">' +
      tl.map(function (t) {
        return '<div class="tl" title="' + Math.round(t.mph) + " mph from " + esc(t.from) + '">' +
          '<span class="t">' + esc(t.time) + '</span><svg viewBox="0 0 24 24" aria-hidden="true"><g transform="rotate(' + (+t.deg || 0) + ' 12 12)">' +
          '<line x1="12" y1="3" x2="12" y2="16" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"/><path d="M12 21 l-4.5 -6 h9 z" fill="currentColor"/></g></svg>' +
          '<span class="v">' + Math.round(t.mph) + '</span><span class="d">' + esc(t.from) + "</span></div>";
      }).join("") + "</div>";
  }
  function renderPanel(p) {
    var plan = p.plan || {};
    var label = String(plan.label || "Shared routes");
    var parts = /^[-\d]/.test(label) ? [label] : label.split(", ");
    document.getElementById("sh-title").textContent = parts[0];
    document.title = "windroute — " + parts[0];
    var lede = document.getElementById("sh-lede");
    lede.textContent = "";
    if (parts.length > 1) { lede.appendChild(document.createTextNode(parts.slice(1).join(", "))); lede.appendChild(document.createElement("br")); }
    lede.appendChild(document.createTextNode([plan.when, plan.meta].filter(Boolean).join(" · ")));
    document.getElementById("sh-wind").innerHTML = windHtml(plan.wind);
    document.getElementById("sh-timeline").innerHTML = timelineHtml(plan.timeline);
    document.getElementById("sh-count").textContent = p.routes.length;
    document.getElementById("sh-routes").innerHTML = p.routes.map(function (r) { return cardHtml(r, p.ride_type); }).join("");
  }
  function showError() {
    var root = document.getElementById("share-root"), err = document.getElementById("share-error");
    if (root) root.hidden = true;
    if (err) err.hidden = false;
  }

  // ---- Share button (results page) ----
  function toast(msg) {
    var t = document.getElementById("toast");
    if (!t) return;
    t.textContent = msg; t.hidden = false;
    clearTimeout(toast._t);
    toast._t = setTimeout(function () { t.hidden = true; }, 2600);
  }
  var btn = document.getElementById("share-btn");
  if (btn) btn.addEventListener("click", function () {
    var st = window.wrState;
    if (!st || !st.payload) return;
    btn.disabled = true;
    encode(st.payload, st.selected).then(function (code) {
      var url = location.origin + "/share#" + code;
      var title = "windroute — " + ((st.payload.plan || {}).label || "routes");
      var touch = window.matchMedia("(pointer: coarse)").matches;
      if (touch && navigator.share) return navigator.share({ title: title, url: url }).catch(function () {});
      return navigator.clipboard.writeText(url).then(function () {
        toast("Link copied — " + Math.round(url.length / 100) / 10 + "k characters, no account needed");
      }, function () { window.prompt("Copy this link:", url); });
    }).catch(function () { toast("Couldn't build a share link in this browser."); })
      .then(function () { btn.disabled = false; });
  });

  window.wrShare = { encode: encode, decode: decode, renderPanel: renderPanel,
                     showError: showError, toast: toast, simplifyIdx: simplifyIdx };
})();
