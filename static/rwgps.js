/* "Send to Ride with GPS" on route cards (only rendered when the host has it set up).
   If the host sets a passphrase, it's asked once and kept in this browser; a wrong one
   is forgotten. */
(function () {
  "use strict";
  var KEY = "wr.rwgps.pass";
  var toast = (window.wrShare && wrShare.toast) || function (m) { alert(m); };

  function stored() { try { return localStorage.getItem(KEY) || ""; } catch (e) { return ""; } }
  function remember(v) { try { v ? localStorage.setItem(KEY, v) : localStorage.removeItem(KEY); } catch (e) {} }

  function label(btn, text) { btn.lastChild.nodeValue = " " + text; }

  function done(btn, url) {
    var a = document.createElement("a");
    a.className = "btn sm soft";
    a.href = url; a.target = "_blank"; a.rel = "noopener";
    a.innerHTML = btn.innerHTML;
    a.lastChild.nodeValue = " Open in Ride with GPS";
    btn.replaceWith(a);
  }

  function send(btn, again) {
    var pass = "";
    if (btn.dataset.pass) {
      pass = stored() || prompt("Passphrase for sending to Ride with GPS:");
      if (!pass) return;
    }
    btn.disabled = true;
    label(btn, "Sending…");
    fetch("/rwgps/send", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ passphrase: pass, gpx: btn.dataset.gpx, filename: btn.dataset.file,
                             name: btn.dataset.name, description: btn.dataset.desc })
    }).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (d) { return { ok: r.ok, d: d }; });
    }).then(function (res) {
      btn.disabled = false;
      label(btn, "Send to Ride with GPS");
      if (res.ok && res.d.url) {
        if (pass) remember(pass);
        toast(res.d.note || "Added to Ride with GPS");
        done(btn, res.d.url);
        return;
      }
      if (res.d.passphrase) {
        remember("");
        if (!again) return send(btn, true);
      }
      toast(res.d.error || "Couldn't send that route.");
    }).catch(function () {
      btn.disabled = false;
      label(btn, "Send to Ride with GPS");
      toast("Couldn't reach the server.");
    });
  }

  document.querySelectorAll(".rwgps-send").forEach(function (btn) {
    btn.addEventListener("click", function () { send(btn, false); });
  });
})();
