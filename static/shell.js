// App-shell chrome (static file so the CSP can stay script-src 'self').
//  - Scroll edges: the panel's header and action bar float over the scrolling
//    content as translucent bars; they only show their edge (blur + hairline) while
//    content is actually underneath them, instead of a permanent divider.
//  - iOS Safari only applies :active (the press feedback on buttons, chips and
//    cards) when the page has a touch listener, so register an empty one.
(function () {
  document.addEventListener("touchstart", function () {}, { passive: true });

  var body = document.getElementById("panel-body");
  var panel = body && body.closest(".panel");
  if (!panel) return;
  var queued = false;
  function update() {
    queued = false;
    var top = body.scrollTop;
    panel.classList.toggle("edge-top", top > 2);
    panel.classList.toggle("edge-bottom", body.scrollHeight - top - body.clientHeight > 2);
  }
  function soon() { if (!queued) { queued = true; requestAnimationFrame(update); } }
  body.addEventListener("scroll", soon, { passive: true });
  window.addEventListener("resize", soon);
  // details opening/closing and cards expanding change the content height
  if ("ResizeObserver" in window) new ResizeObserver(soon).observe(body.querySelector(".panel-content") || body);
  update();
})();
