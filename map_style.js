/* Kairos – one base-map style for every map surface (founder audit 2026-11-03).
 *
 * The main map (map_vault.html) and the viewer's former mini-map page (now native, mini_map.py) built their
 * styles separately: the mini-map loaded OpenFreeMap "dark", which at street zoom is an almost
 * pitch-black surface and looked nothing like the main map. Both pages now take the same
 * OpenFreeMap "liberty" style through the same pure transforms defined here:
 *   localize (labels in the app language, null-safe numeric filters)
 *   guardBuildings (null building heights)
 *   pastelize (soft parks / woods so photos and routes stand out)
 * Pure functions only – no map, no DOM – so both pages (and tests) can call them.
 */
(function (root) {
  'use strict';

  var STYLE_URL = 'https://tiles.openfreemap.org/styles/liberty';
  var STYLE_CACHE_KEY = 'omni.vectorStyle.liberty';

  function clone(o) { return JSON.parse(JSON.stringify(o)); }

  // Place names in the app language (OpenMapTiles name:xx), else Latin script, else local name
  function nameField(lang) {
    return ['coalesce', ['get', 'name:' + String(lang || 'en').split('-')[0]], ['get', 'name:latin'], ['get', 'name']];
  }
  function isNameField(tf) {
    var s = JSON.stringify(tf || '');
    return s.indexOf('name') >= 0 && s.indexOf('"ref"') < 0;
  }

  // liberty compares properties some features lack ([">=", ["get", "rank"], 20] on POIs); MapLibre
  // then logs "Expected value to be of type number, but found null" per feature. A typeof guard
  // (the same one the style itself uses on boundary_3) drops such features silently – same result.
  var NUMERIC_CMP = { '<': 1, '<=': 1, '>': 1, '>=': 1 };
  function guardNumericFilter(f) {
    if (!Array.isArray(f)) return f;
    if (NUMERIC_CMP[f[0]] && f.length === 3) {
      var guards = [f[1], f[2]].filter(function (a) { return Array.isArray(a) && a[0] === 'get'; })
        .map(function (a) { return ['==', ['typeof', a], 'number']; });
      return guards.length ? ['all'].concat(guards, [f]) : f;
    }
    if (f[0] === 'all' || f[0] === 'any') {        // under "!" the guard would invert the result
      return [f[0]].concat(f.slice(1).map(guardNumericFilter));
    }
    return f;
  }

  function localize(style, nameExpr) {
    var s = clone(style);
    s.layers.forEach(function (l) {
      if (l.layout && l.layout['text-field'] && isNameField(l.layout['text-field'])) l.layout['text-field'] = nameExpr;
      if (l.filter) l.filter = guardNumericFilter(l.filter);
    });
    return s;
  }

  // Soft Standard map: parks / woods only faintly tinted, so content (roads, water, pins) leads
  var PASTEL_FILLS = { park: 0.32, landcover_wood: 0.26, landcover_grass: 0.22 };
  function pastelize(style) {
    style.layers.forEach(function (l) {
      if (PASTEL_FILLS[l.id] !== undefined && l.type === 'fill') {
        l.paint = Object.assign({}, l.paint, { 'fill-opacity': PASTEL_FILLS[l.id] });
        if (l.id === 'park') delete l.paint['fill-outline-color'];
      }
    });
    return style;
  }

  // Buildings without a height reported "found null" (render_height = null): coalesce(…, 0)
  var HEIGHT_PROPS = { render_height: 1, render_min_height: 1 };
  function guardHeights(v) {
    if (!Array.isArray(v)) return v;
    if (v[0] === 'get' && HEIGHT_PROPS[v[1]] && v.length === 2) return ['coalesce', v, 0];
    return v.map(guardHeights);
  }
  function guardBuildings(style) {
    style.layers.forEach(function (l) {
      if (l.type !== 'fill-extrusion' || !l.paint) return;
      Object.keys(l.paint).forEach(function (k) { l.paint[k] = guardHeights(l.paint[k]); });
    });
    return style;
  }

  // The Standard base map exactly as the main map draws it (minus globe / relief / underlay)
  function standard(style, nameExpr) {
    return pastelize(guardBuildings(localize(style, nameExpr)));
  }

  function validStyle(s) { return !!(s && Array.isArray(s.layers) && s.sources); }

  // Tile store (tile_store.py): allowed hosts go through the app's local server, same key ->
  // same port as map_vault.html, so the mini-map hits tiles the main map already stored.
  function storeUrl(store, url) {
    if (!store || typeof url !== 'string') return url;
    var m = /^https:\/\/([^\/?#]+)\/(.*)$/.exec(url);
    if (!m || !store.hosts[m[1].toLowerCase()]) return url;
    var key = m[1].toLowerCase() + '/' + m[2], sum = 0;
    for (var i = 0; i < key.length; i++) sum += key.charCodeAt(i);
    return store.bases[sum % store.bases.length] + key;
  }
  function makeStore(cfg) {
    if (!cfg || !Array.isArray(cfg.bases) || !cfg.bases.length) return null;
    var hosts = {};
    (cfg.hosts || []).forEach(function (h) { hosts[String(h).toLowerCase()] = true; });
    return { bases: cfg.bases.slice(), hosts: hosts };
  }

  root.KairosMapStyle = {
    STYLE_URL: STYLE_URL, STYLE_CACHE_KEY: STYLE_CACHE_KEY,
    clone: clone, nameField: nameField, isNameField: isNameField, guardNumericFilter: guardNumericFilter,
    localize: localize, pastelize: pastelize, guardBuildings: guardBuildings, standard: standard,
    validStyle: validStyle, storeUrl: storeUrl, makeStore: makeStore
  };
})(typeof window !== 'undefined' ? window : this);
