/* Kairos Director – sdílený filmový engine (Sprint 4).
 *
 * Vytaženo z Cesty (map_vault.html), aby stejné jádro řídilo i Throwback Reels (reel_player.html)
 * a export MP4 (director.py je jeho věrný protějšek v Pythonu – testy hlídají shodu):
 *
 *   • časová osa  – úseky {dur} -> t0 / celkem, hledání úseku v čase, zkrácení dlouhého filmu
 *                    s podlahami (layout / indexAt / compress)
 *   • kamera       – křivka s pevným časovým krokem: geometrie se navzorkuje po dt, každý kanál se
 *                    vyhladí bez fázového posunu (exponenciála tam a zpět) a čte se jako uniformní
 *                    kubický B-spline -> spojitá poloha, rychlost i zrychlení (CameraSpline)
 *   • beat         – mřížka dob a těžkých dob (první doba taktu) z hudby, přichycení střihů na doby
 *                    (snapCuts) a fázový zámek času filmu na hodiny hudby (BeatClock)
 *
 * Bez závislostí, bez DOM – jen čistá matematika nad čísly (testovatelné v jakékoli stránce).
 */
(function (root) {
  'use strict';

  // ── easing ──
  function clamp01(u) { return Math.max(0, Math.min(1, u)); }
  function smootherstep(u) { u = clamp01(u); return u * u * u * (u * (6 * u - 15) + 10); }
  function smoothstep(u) { u = clamp01(u); return u * u * (3 - 2 * u); }
  function lerp(a, b, t) { return a + (b - a) * t; }
  function angleTo(from, to) { return ((to - from + 540) % 360) - 180; }
  function lerpAngle(a, b, t) { return a + angleTo(a, b) * t; }
  function hermite(u, m0, m1) {       // 0 -> 1 se sklonem m0 na začátku a m1 na konci
    u = clamp01(u);
    var u2 = u * u, u3 = u2 * u;
    return (u3 - 2 * u2 + u) * m0 + (3 * u2 - 2 * u3) + (u3 - u2) * m1;
  }

  // ── časová osa ──
  function layout(segs) {             // t0 každého úseku, vrací celkovou délku
    var t = 0;
    segs.forEach(function (g) { g.t0 = t; t += g.dur; });
    return t;
  }
  function indexAt(segs, t) {         // poslední úsek s t0 <= t (binárně)
    var lo = 0, hi = segs.length - 1;
    while (lo < hi) {
      var mid = (lo + hi + 1) >> 1;
      if (segs[mid].t0 <= t) lo = mid; else hi = mid - 1;
    }
    return lo;
  }
  // Dlouhý film zkrátit na maxS: pevné úseky (fixed(g)) zůstanou, ostatní úměrně, ale ne pod floor(g)
  function compress(segs, maxS, fixed, floor) {
    var total = segs.reduce(function (a, g) { return a + g.dur; }, 0);
    if (total <= maxS) return total;
    var keep = segs.reduce(function (a, g) { return a + (fixed(g) ? g.dur : 0); }, 0);
    var k = (maxS - keep) / Math.max(1e-9, total - keep);
    segs.forEach(function (g) { if (!fixed(g)) g.dur = Math.max(floor(g), g.dur * k); });
    return segs.reduce(function (a, g) { return a + g.dur; }, 0);
  }

  // ── kamera: křivka s pevným časovým krokem ──
  function smoothChannel(raw, tau, dt) {   // exponenciála dopředu a zpět = nulové zpoždění
    var n = raw.length, a = 1 - Math.exp(-dt / tau), fwd = new Array(n), out = new Array(n);
    if (!n) return out;
    fwd[0] = raw[0];
    for (var k = 1; k < n; k++) fwd[k] = fwd[k - 1] + a * (raw[k] - fwd[k - 1]);
    out[n - 1] = fwd[n - 1];
    for (k = n - 2; k >= 0; k--) out[k] = out[k + 1] + a * (fwd[k] - out[k + 1]);
    return out;
  }
  function bspline(arr, t, dt) {      // uniformní kubický B-spline (C2) přes vzorky po dt
    var n = arr.length, s = t / dt, k = Math.floor(s), u = s - k;
    var i0 = Math.max(0, Math.min(n - 1, k - 1)), i1 = Math.max(0, Math.min(n - 1, k)),
        i2 = Math.max(0, Math.min(n - 1, k + 1)), i3 = Math.max(0, Math.min(n - 1, k + 2));
    var u2 = u * u, u3 = u2 * u;
    return ((1 - u) * (1 - u) * (1 - u) * arr[i0] + (3 * u3 - 6 * u2 + 4) * arr[i1]
            + (-3 * u3 + 3 * u2 + 3 * u + 1) * arr[i2] + u3 * arr[i3]) / 6;
  }
  /* opts: {dt, keys: ['x', ...], tau: {x: s, ...}, sample(t, prev) -> {x: ...},
   *        prefilter: {key: fn(raw[]) -> raw[]} (volitelné), unwrap(s, prev) (volitelné – antimeridián…)}
   * Začátek a konec drží přesně (první snímek = výchozí póza, žádný skok; konec = cíl). */
  function CameraSpline(opts) {
    this.dt = opts.dt;
    this.keys = opts.keys;
    this.tau = opts.tau;
    this.sample = opts.sample;
    this.prefilter = opts.prefilter || {};
    this.unwrap = opts.unwrap || null;
    this.raw = null;
    this.cam = null;
    this.total = 0;
  }
  CameraSpline.prototype.build = function (total) {
    var n = Math.max(2, Math.ceil(total / this.dt) + 1), raw = [], prev = null;
    this.total = total;
    for (var k = 0; k < n; k++) { prev = this.sample(Math.min(total, k * this.dt), prev); raw.push(prev); }
    this.raw = raw;
    this.smooth();
    return this;
  };
  CameraSpline.prototype.smooth = function () {
    var self = this, cam = {}, raw = this.raw;
    this.keys.forEach(function (key) {
      var ch = raw.map(function (s) { return s[key]; });
      if (self.prefilter[key]) ch = self.prefilter[key](ch);   // před vyhlazením: křivka zůstane C2
      cam[key] = smoothChannel(ch, self.tau[key], self.dt);
    });
    var first = raw[0], last = raw[raw.length - 1];
    this.keys.forEach(function (key) { cam[key][0] = first[key]; cam[key][cam[key].length - 1] = last[key]; });
    this.cam = cam;
    return this;
  };
  CameraSpline.prototype.resample = function (t0, t1) {   // úsek se změnil -> přepočítat jen jeho vzorky
    if (!this.raw) return this;
    var raw = this.raw, dt = this.dt, k0 = Math.max(1, Math.floor(t0 / dt)), k1 = Math.min(raw.length - 1, Math.ceil(t1 / dt));
    for (var k = k0; k <= k1; k++) raw[k] = this.sample(Math.min(this.total, k * dt), raw[k - 1]);
    if (this.unwrap) for (k = k1 + 1; k < raw.length; k++) this.unwrap(raw[k], raw[k - 1]);   // navázat rozbalení
    return this.smooth();
  };
  CameraSpline.prototype.at = function (t) {
    var out = {}, cam = this.cam, dt = this.dt;
    this.keys.forEach(function (key) { out[key] = bspline(cam[key], t, dt); });
    return out;
  };

  // ── beat: mřížka dob z hudby ──
  /* map: {period: s jedné doby, bar: dob v taktu, offset: s první těžké doby, length: s skladby (smyčka)}
   * Vrací {beats: [t], downbeats: [t], period, bar} na intervalu <0, until>. Smyčka skladby začíná
   * mřížku znovu (skladba nemusí mít celý počet taktů). */
  function beatGrid(map, until) {
    var period = +map.period, bar = Math.max(1, Math.round(+map.bar || 4)), off = +map.offset || 0;
    var length = +map.length || 0, beats = [], downs = [];
    if (!(period > 0.05)) return { beats: beats, downbeats: downs, period: 0, bar: bar };
    for (var loop = 0; ; loop++) {
      var base = length > 0 ? loop * length : 0;
      if (base > until) break;
      for (var k = 0; ; k++) {
        var t = base + off + k * period;
        if (t > until || (length > 0 && t >= base + length)) break;
        beats.push(t);
        if (k % bar === 0) downs.push(t);
      }
      if (!(length > 0)) break;
    }
    return { beats: beats, downbeats: downs, period: period, bar: bar };
  }
  function nearest(list, t) {
    var lo = 0, hi = list.length - 1;
    if (hi < 0) return null;
    while (lo < hi) {
      var mid = (lo + hi) >> 1;
      if (list[mid] < t) lo = mid + 1; else hi = mid;
    }
    var best = list[lo];
    if (lo > 0 && Math.abs(list[lo - 1] - t) <= Math.abs(best - t)) best = list[lo - 1];
    return best;
  }
  /* Střihy na doby: úsek s cut(g) = true má začít na době. Posouvá se natažením / zkrácením
   * předchozích pružných úseků (stretch(g)) v mezích minK–maxK jejich délky. Přednost má těžká
   * doba v toleranci downTol (s), jinak nejbližší doba. Vrací počet přichycených střihů. */
  function snapCuts(segs, grid, opts) {
    opts = opts || {};
    var cut = opts.cut, stretch = opts.stretch, minK = opts.minK || 0.75, maxK = opts.maxK || 1.35;
    var downTol = opts.downTol === undefined ? grid.period * 1.1 : opts.downTol;
    var snapped = 0, t = 0, pool = [];
    if (!grid.beats.length) { layout(segs); return 0; }
    for (var i = 0; i < segs.length; i++) {
      var g = segs[i];
      if (cut(g) && pool.length) {
        var down = nearest(grid.downbeats, t), beat = nearest(grid.beats, t);
        var target = down !== null && Math.abs(down - t) <= downTol ? down : beat;
        var flex = pool.reduce(function (a, s) { return a + s.dur; }, 0), shift = target - t;
        var lo = flex * (minK - 1), hi = flex * (maxK - 1);
        if (shift < lo || shift > hi) {       // těžká doba moc daleko -> nejbližší doba
          target = beat; shift = target - t;
        }
        if (shift >= lo && shift <= hi && flex > 0) {
          var k = (flex + shift) / flex;
          pool.forEach(function (s) { s.dur *= k; });
          t += shift;
          snapped++;
        }
        pool = [];
      }
      if (stretch(g)) pool.push(g);
      g.t0 = t;
      t += g.dur;
    }
    layout(segs);
    return snapped;
  }
  /* Fázový zámek: čas filmu jemně dotahuje hodiny hudby (zrychlení / zpomalení v mezích ±maxRate),
   * aby střihy naplánované na doby na dobách i zůstaly – i když film chvíli stál (dlaždice, pauza).
   * Velká odchylka (skok, přetočení) = zámek se přenastaví na nový posun. */
  function BeatClock(opts) {
    opts = opts || {};
    this.gain = opts.gain || 0.9;           // 1/s: jak rychle se odchylka dorovná
    this.maxRate = opts.maxRate || 0.12;    // nejvýš ±12 % rychlosti
    this.relock = opts.relock || 1.5;       // s: větší rozdíl = nový posun, ne dohánění
    this.initial = opts.offset === undefined ? null : +opts.offset;   // pevný posun (plán počítá s 0)
    this.offset = this.initial;             // čas hudby - čas filmu v okamžiku zámku
  }
  BeatClock.prototype.reset = function () { this.offset = this.initial; };
  BeatClock.prototype.rate = function (filmT, audioT) {
    if (!isFinite(audioT)) return 1;
    if (this.offset === null) { this.offset = audioT - filmT; return 1; }
    var err = (audioT - this.offset) - filmT;      // + = film je pozadu
    if (Math.abs(err) > this.relock) { this.offset = audioT - filmT; return 1; }
    return 1 + Math.max(-this.maxRate, Math.min(this.maxRate, err * this.gain));
  };

  root.KairosDirector = {
    clamp01: clamp01, smootherstep: smootherstep, smoothstep: smoothstep, lerp: lerp,
    angleTo: angleTo, lerpAngle: lerpAngle, hermite: hermite,
    layout: layout, indexAt: indexAt, compress: compress,
    smoothChannel: smoothChannel, bspline: bspline, CameraSpline: CameraSpline,
    beatGrid: beatGrid, nearest: nearest, snapCuts: snapCuts, BeatClock: BeatClock
  };
})(typeof window !== 'undefined' ? window : this);
