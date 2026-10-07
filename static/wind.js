// The forecast wind along a route, as you'll actually meet it. Rides the route at
// the rider's pace through the same hourly wind field the scorer uses (hourly
// forecasts at the start + a ring of points, interpolated linearly in time and by
// inverse distance in space), slowing into headwinds and speeding up with
// tailwinds exactly like the scorer (`ground_speed_mph`), so each stretch gets the
// wind at that place at the time you'd be there.
// Exposes window.WrWind = { rideWind(coords, field, paceMph), effect(hw, mph), ... }.
(function () {
  var WIND_SPEED_EFFECT = 0.25;        // == scoring.WIND_SPEED_EFFECT
  var MIN_FRAC = 0.5, MAX_FRAC = 1.6;  // == scoring.GROUND_SPEED_{MIN,MAX}_FRAC
  var MPH_TO_KMH = 1.609344;

  // FROM-vector (u east, v north, mph) at a place and time (hours since start).
  function vectorAt(f, lat, lng, h) {
    var hs = f.hours, i0, i1, t = 0;
    if (h <= hs[0]) { i0 = i1 = 0; }
    else if (h >= hs[hs.length - 1]) { i0 = i1 = hs.length - 1; }
    else {
      i1 = 1; while (hs[i1] < h) i1++;
      i0 = i1 - 1; t = (h - hs[i0]) / (hs[i1] - hs[i0]);
    }
    var coslat = Math.cos(lat * Math.PI / 180), u = 0, v = 0, wsum = 0;
    for (var p = 0; p < f.points.length; p++) {
      var dy = (f.points[p][0] - lat) * 111, dx = (f.points[p][1] - lng) * 111 * coslat;
      var d = Math.sqrt(dx * dx + dy * dy);
      var up = f.u[p][i0] + t * (f.u[p][i1] - f.u[p][i0]);
      var vp = f.v[p][i0] + t * (f.v[p][i1] - f.v[p][i0]);
      if (d < 0.05) return [up, vp];
      var w = 1 / (d * d);
      u += w * up; v += w * vp; wsum += w;
    }
    return [u / wsum, v / wsum];
  }

  function haversineKm(a, b) {
    var R = 6371, k = Math.PI / 180;
    var dLat = (b[0] - a[0]) * k, dLng = (b[1] - a[1]) * k;
    var s = Math.sin(dLat / 2) * Math.sin(dLat / 2) +
      Math.cos(a[0] * k) * Math.cos(b[0] * k) * Math.sin(dLng / 2) * Math.sin(dLng / 2);
    return 2 * R * Math.asin(Math.min(1, Math.sqrt(s)));
  }
  function bearingRad(a, b) {
    var k = Math.PI / 180, y = Math.sin((b[1] - a[1]) * k) * Math.cos(b[0] * k);
    var x = Math.cos(a[0] * k) * Math.sin(b[0] * k) - Math.sin(a[0] * k) * Math.cos(b[0] * k) * Math.cos((b[1] - a[1]) * k);
    return Math.atan2(y, x);
  }
  function groundMph(pace, hw) {
    return Math.max(MIN_FRAC * pace, Math.min(MAX_FRAC * pace, pace - WIND_SPEED_EFFECT * hw));
  }

  // Ride the route: per segment i (coords[i] -> coords[i+1]) the wind you meet.
  // Returns { seg: [{u, v, hw, mph, t}], hours } where hw is the headwind (+) /
  // tailwind (-) component in mph and t the hours since the start at the segment.
  function rideWind(coords, field, paceMph) {
    var pace = Math.max(1, paceMph || 17), kmh = pace * MPH_TO_KMH, t = 0, seg = [];
    for (var i = 0; i + 1 < coords.length; i++) {
      var a = coords[i], b = coords[i + 1], d = haversineKm(a, b);
      var uv = vectorAt(field, (a[0] + b[0]) / 2, (a[1] + b[1]) / 2, t + d / 2 / kmh);
      var brg = bearingRad(a, b);
      var hw = d > 0 ? uv[0] * Math.sin(brg) + uv[1] * Math.cos(brg) : 0;
      seg.push({ u: uv[0], v: uv[1], hw: hw, mph: Math.hypot(uv[0], uv[1]), t: t });
      if (d > 0) t += d / (groundMph(pace, hw) * MPH_TO_KMH);
    }
    return { seg: seg, hours: t };
  }

  // How the wind hits you: "head" | "cross" | "tail" | "calm".
  function effect(hw, mph) {
    if (mph < 3) return "calm";
    var r = hw / mph;                       // cos(angle between travel and wind-from)
    return r > 0.38 ? "head" : r < -0.38 ? "tail" : "cross";
  }
  var COLORS = { head: "#dc2626", cross: "#d97706", tail: "#16a34a", calm: "#64748b" };
  var LABELS = { head: "headwind", cross: "crosswind", tail: "tailwind", calm: "calm" };
  var COMPASS = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE", "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"];
  function fromDeg(u, v) { return (Math.atan2(u, v) * 180 / Math.PI + 360) % 360; }
  function compass(deg) { return COMPASS[Math.round(deg / 22.5) % 16]; }

  window.WrWind = { vectorAt: vectorAt, rideWind: rideWind, effect: effect,
                    COLORS: COLORS, LABELS: LABELS, fromDeg: fromDeg, compass: compass };
})();
