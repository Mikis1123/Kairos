(function () { try {
  if (window.__probe) return 'already';
  var vraf = window.__vraf || window.requestAnimationFrame.bind(window);   // true vsync callbacks
  var P = { on: false, vs: [], rend: [], cost: [], loaf: [], long: [], res: [], warns: [], heap: [] };
  var lastV = 0, lastR = 0;
  function v(t) {
    if (P.on) { if (lastV) P.vs.push(t - lastV); lastV = t;
      if (performance.memory) P.heap.push(performance.memory.usedJSHeapSize); }
    vraf(v);
  }
  vraf(v);
  var origWarn = console.warn;
  console.warn = function () { P.warns.push([performance.now(), Array.prototype.join.call(arguments, ' ')]); return origWarn.apply(console, arguments); };
  try {
    new PerformanceObserver(function (l) { l.getEntries().forEach(function (e) {
      if (!P.on) return;
      P.loaf.push({ t: e.startTime, d: e.duration, block: e.blockingDuration, render: e.renderStart ? e.duration - (e.renderStart - e.startTime) : 0,
        scripts: (e.scripts || []).map(function (s) { return [Math.round(s.duration), s.invoker || '', (s.sourceFunctionName || '') + '@' + (s.sourceURL || '').split('/').pop() + ':' + s.sourceCharPosition]; }) });
    }); }).observe({ type: 'long-animation-frame', buffered: false });
  } catch (e) { P.noLoaf = String(e); }
  try {
    new PerformanceObserver(function (l) { l.getEntries().forEach(function (e) {
      if (P.on) P.long.push([e.startTime, e.duration]); }); }).observe({ type: 'longtask', buffered: false });
  } catch (e) { }
  new PerformanceObserver(function (l) { l.getEntries().forEach(function (e) {
    P.res.push({ n: e.name, s: e.startTime, d: e.duration, wait: e.responseStart - e.requestStart, q: e.requestStart - e.startTime, sz: e.transferSize, enc: e.encodedBodySize, proto: e.nextHopProtocol });
  }); }).observe({ type: 'resource', buffered: true });
  function hook() {
    var m = window.map;
    if (!m) return setTimeout(hook, 200);
    var orig = m._render;
    m._render = function (t) {
      var a = performance.now(), r = orig.apply(this, arguments), c = performance.now() - a;
      if (P.on) { if (lastR) P.rend.push(a - lastR); lastR = a; P.cost.push(c); }
      return r;
    };
  }
  hook();
  var canvas = function () { return window.map.getCanvas(); };
  function fire(type, x, y, opts) {
    var c = canvas(), r = c.getBoundingClientRect();
    var o = Object.assign({ bubbles: true, cancelable: true, clientX: r.left + x, clientY: r.top + y, view: window }, opts || {});
    var E = type.indexOf('wheel') === 0 ? WheelEvent : type.indexOf('pointer') === 0 ? PointerEvent : MouseEvent;
    c.dispatchEvent(new E(type, o));
  }
  // gestures, one input event per vsync (Chromium aligns real mouse input to frames the same way)
  function gesture(kind, ms, done) {
    var c = canvas(), W = c.clientWidth, H = c.clientHeight, cx = W / 2, cy = H / 2, t0 = 0, last = null;
    var btn = kind === 'orbit' ? 2 : 0, btns = kind === 'orbit' ? 2 : 1;
    function step(t) {
      if (!t0) {
        t0 = t;
        if (kind === 'pan' || kind === 'orbit') { fire('mousedown', cx, cy, { button: btn, buttons: btns }); last = [cx, cy]; }
      }
      var k = Math.min(1, (t - t0) / ms);
      if (kind === 'pan') {                    // figure-eight drag, ~700 px/s
        var x = cx + Math.sin(k * Math.PI * 2) * W * 0.28, y = cy + Math.sin(k * Math.PI * 4) * H * 0.18;
        fire('mousemove', x, y, { buttons: btns, movementX: x - last[0], movementY: y - last[1] }); last = [x, y];
      } else if (kind === 'orbit') {
        var x2 = cx + k * W * 0.6 * Math.sin(k * 3), y2 = cy - Math.sin(k * Math.PI) * H * 0.15;
        fire('mousemove', x2, y2, { buttons: btns, button: 2 }); last = [x2, y2];
      } else if (kind === 'wheelin' || kind === 'wheelout') {   // notched mouse wheel: one notch / 90 ms
        var n = Math.floor((t - t0) / 90);
        if (n !== gesture.n) { gesture.n = n; fire('wheel', cx + 60, cy - 40, { deltaY: kind === 'wheelin' ? -100 : 100, deltaMode: 0 }); }
      } else if (kind === 'trackpad') {                          // smooth trackpad zoom every frame
        fire('wheel', cx, cy, { deltaY: Math.sin(k * Math.PI * 2) * -6, deltaMode: 0, ctrlKey: true });
      }
      if (k < 1) { vraf(step); return; }
      if (kind === 'pan' || kind === 'orbit') fire('mouseup', last[0], last[1], { button: btn, buttons: 0 });
      gesture.n = -1;
      done && done();
    }
    gesture.n = -1;
    vraf(step);
  }
  window.__probe = {
    P: P,
    start: function () { P.vs = []; P.rend = []; P.cost = []; P.loaf = []; P.long = []; P.heap = []; P.warns = []; lastV = lastR = 0; P.t0 = performance.now(); P.on = true; },
    stop: function () {
      P.on = false;
      var t0 = P.t0, out = { vs: P.vs, rend: P.rend, cost: P.cost, loaf: P.loaf, long: P.long, warns: P.warns,
        heapDrops: 0, ratio: window.map.getPixelRatio(), zoom: window.map.getZoom(), pitch: window.map.getPitch(),
        proj: (window.map.style && window.map.style.projection && window.map.style.projection.name) || '',
        terrain: !!window.map.getTerrain(), cap: window.getFrameCap ? window.getFrameCap() : null,
        res: P.res.filter(function (r) { return r.s >= t0; }) };
      for (var i = 1; i < P.heap.length; i++) if (P.heap[i] < P.heap[i - 1] - 2e6) out.heapDrops++;
      return JSON.stringify(out);
    },
    gesture: function (kind, ms) { window.__gdone = false; gesture(kind, ms, function () { window.__gdone = true; }); return 'ok'; },
    dbl: function () { var c = canvas(); fire('dblclick', c.clientWidth / 2, c.clientHeight / 2, { button: 0 }); return 'ok'; }
  };
  return 'installed';
 } catch (e) { return 'ERR ' + e + ' ' + e.stack; } })();
