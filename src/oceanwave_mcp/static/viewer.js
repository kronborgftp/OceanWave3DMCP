/*
 * OceanWave3D interactive viewer.
 *
 * Pure client-side rendering of solver output served by /api/data/<run_id>.
 * Every pixel is derived from fort.1XX snapshot data and params.json — nothing
 * is interpolated, smoothed, or invented. Three views (cross-section, x–t
 * heatmap, perspective surface), annotation toggles, and side-by-side
 * comparison of runs with linked time, toggles, and scales.
 */
'use strict';

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------

const state = {
  selected: [],            // run ids in display order; [0] is the primary run
  data: new Map(),         // run_id -> /api/data payload
  runsList: [],            // /api/runs entries
  showAll: false,          // include runs from earlier sessions in the picker
  t: 0,                    // current time [s] (or snapshot index if untimed)
  playing: true,
  speed: 1,
  view: 'section',         // 'section' | 'heatmap' | 'surface'
  opts: {
    fill: true, seabed: true, zones: true, scalebar: true, person: false,
    axes: true, plainTitle: true, fullDepth: true, lockScales: true,
    kinematics: false,   // animated subsurface flow-arrow overlay (section view)
    orbits: false,       // particle-orbit (kinematics-over-time) overlay
  },
};

const panels = [];         // [{id, canvas, ctx}]
const heatmapCache = new Map();   // run_id -> {peak, canvas}
let dirty = true;
let lastTick = null;

const $ = (sel) => document.querySelector(sel);

// ---------------------------------------------------------------------------
// Small utilities
// ---------------------------------------------------------------------------

function clamp(v, lo, hi) { return v < lo ? lo : (v > hi ? hi : v); }

function niceNum(v) {
  if (!(v > 0)) return 1;
  const e = Math.floor(Math.log10(v));
  const f = v / Math.pow(10, e);
  const nf = f < 1.5 ? 1 : f < 3.5 ? 2 : f < 7.5 ? 5 : 10;
  return nf * Math.pow(10, e);
}

function fmtNum(v) { return String(parseFloat(Number(v).toPrecision(3))); }

function haloText(ctx, str, x, y, opts) {
  const o = opts || {};
  ctx.save();
  ctx.font = o.font || '12px system-ui, sans-serif';
  ctx.textAlign = o.align || 'left';
  ctx.textBaseline = o.baseline || 'alphabetic';
  ctx.lineWidth = 3;
  ctx.strokeStyle = 'rgba(255,255,255,0.85)';
  ctx.lineJoin = 'round';
  ctx.strokeText(str, x, y);
  ctx.fillStyle = o.fill || '#222';
  ctx.fillText(str, x, y);
  ctx.restore();
}

// Diverging blue–white–red (ColorBrewer RdBu, reversed): blue = below still
// water, white = still water, red = above. Input in [-1, 1].
const RDBU = [
  [-1.00, 0x05, 0x30, 0x61], [-0.75, 0x21, 0x66, 0xac], [-0.50, 0x43, 0x93, 0xc3],
  [-0.25, 0x92, 0xc5, 0xde], [-0.10, 0xd1, 0xe5, 0xf0], [0.00, 0xf7, 0xf7, 0xf7],
  [0.10, 0xfd, 0xdb, 0xc7], [0.25, 0xf4, 0xa5, 0x82], [0.50, 0xd6, 0x60, 0x4d],
  [0.75, 0xb2, 0x18, 0x2b], [1.00, 0x67, 0x00, 0x1f],
];

function divergingColor(v) {
  const t = clamp(v, -1, 1);
  for (let i = 1; i < RDBU.length; i++) {
    if (t <= RDBU[i][0]) {
      const a = RDBU[i - 1], b = RDBU[i];
      const f = (t - a[0]) / (b[0] - a[0]);
      return [
        Math.round(a[1] + f * (b[1] - a[1])),
        Math.round(a[2] + f * (b[2] - a[2])),
        Math.round(a[3] + f * (b[3] - a[3])),
      ];
    }
  }
  const last = RDBU[RDBU.length - 1];
  return [last[1], last[2], last[3]];
}

function lerpColor(c1, c2, f) {
  return [
    Math.round(c1[0] + f * (c2[0] - c1[0])),
    Math.round(c1[1] + f * (c2[1] - c1[1])),
    Math.round(c1[2] + f * (c2[2] - c1[2])),
  ];
}

function rgb(c) { return 'rgb(' + c[0] + ',' + c[1] + ',' + c[2] + ')'; }

// ---------------------------------------------------------------------------
// Run helpers (linked-scale environment)
// ---------------------------------------------------------------------------

function loadedRuns() {
  return state.selected.map((id) => state.data.get(id)).filter(Boolean);
}

function runPeak(run) {
  return Math.max(Math.abs(run.max_elevation), Math.abs(run.min_elevation), 1e-9);
}

function sharedPeak() {
  return loadedRuns().reduce((m, r) => Math.max(m, runPeak(r)), 1e-9);
}

function peakFor(run) {
  return state.opts.lockScales ? sharedPeak() : runPeak(run);
}

function runDuration(run) { return run.times[run.times.length - 1]; }

function maxDuration() {
  return loadedRuns().reduce((m, r) => Math.max(m, runDuration(r)), 0);
}

function anyTimed() { return loadedRuns().some((r) => r.has_time); }

function frameIndex(run, t) {
  const dt = run.has_time ? run.dt_snapshot : 1;
  return clamp(Math.round(t / dt), 0, run.eta.length - 1);
}

function titleFor(run) {
  return state.opts.plainTitle ? run.title : run.run_id;
}

// ---------------------------------------------------------------------------
// Cross-section view
// ---------------------------------------------------------------------------

function sectionMargins(W, H) {
  const o = state.opts;
  return { ml: o.axes ? 58 : 16, mr: 16, mt: 28, mb: o.axes ? 42 : 16 };
}

// Kinematics overlays (velocity field + particle orbits), fetched once per run.
// Map value: data object | null (none/failed) | undefined (loading).
const kinFields = new Map();
const kinOrbits = new Map();

function _overlayFor(cache, run, endpoint, key) {
  const id = run.run_id;
  if (cache.has(id)) return cache.get(id) || null;
  cache.set(id, undefined);                       // loading — don't refetch
  fetch(endpoint + encodeURIComponent(id))
    .then((r) => (r.ok ? r.json() : {}))
    .then((p) => { cache.set(id, p[key] || null); scheduleRender(); })
    .catch(() => { cache.set(id, null); });
  return null;
}

const flowFieldFor = (run) => _overlayFor(kinFields, run, '/api/kinematics_field/', 'field');
const orbitsFor = (run) => _overlayFor(kinOrbits, run, '/api/kinematics_orbits/', 'orbits');

// Speed colour ramp (slow → fast): teal → green → yellow → orange → red.
const SPEED_STOPS = [
  [0.0, [56, 178, 172]], [0.25, [102, 187, 106]], [0.5, [255, 213, 79]],
  [0.75, [255, 138, 60]], [1.0, [229, 57, 53]],
];
function speedColor(t) {
  t = clamp(t, 0, 1);
  for (let i = 1; i < SPEED_STOPS.length; i++) {
    if (t <= SPEED_STOPS[i][0]) {
      const a = SPEED_STOPS[i - 1], b = SPEED_STOPS[i];
      const f = (t - a[0]) / (b[0] - a[0]);
      return rgb(lerpColor(a[1], b[1], f));
    }
  }
  return rgb(SPEED_STOPS[SPEED_STOPS.length - 1][1]);
}

function drawArrow(ctx, x0, y0, x1, y1) {
  const dx = x1 - x0, dy = y1 - y0, len = Math.hypot(dx, dy);
  ctx.beginPath();
  ctx.moveTo(x0, y0); ctx.lineTo(x1, y1); ctx.stroke();
  if (len < 2.5) return;
  const ah = Math.min(6, len * 0.5), a = Math.atan2(dy, dx);
  ctx.beginPath();
  ctx.moveTo(x1, y1);
  ctx.lineTo(x1 - ah * Math.cos(a - 0.42), y1 - ah * Math.sin(a - 0.42));
  ctx.lineTo(x1 - ah * Math.cos(a + 0.42), y1 - ah * Math.sin(a + 0.42));
  ctx.closePath(); ctx.fill();
}

// Animated subsurface velocity field over the cross-section. Arrows sit on an
// evenly-spaced grid, are coloured by speed, and have a dark halo for contrast.
// All u/w data is the solver's own; only the arrow tail uses the data mapping —
// the vector is scaled in screen pixels so direction is honest under the plot's
// vertical exaggeration, with a min/max length so every arrow stays readable.
function drawFlowField(ctx, run, px, py) {
  if (!run.has_kinematics_data) return;
  const f = flowFieldFor(run);
  if (!f || !f.frames.length) return;

  let fi = 0, best = Infinity;
  for (let i = 0; i < f.times.length; i++) {
    const d = Math.abs(f.times[i] - state.t);
    if (d < best) { best = d; fi = i; }
  }
  const fr = f.frames[fi], X = f.x, Hd = f.h, sigma = f.sigma;

  if (f._vmax == null) {                 // field-wide peak speed (consistent scale)
    let m = 1e-6;
    for (const g of f.frames)
      for (let c = 0; c < g.u.length; c++)
        for (let k = 0; k < g.u[c].length; k++)
          m = Math.max(m, Math.hypot(g.u[c][k], g.w[c][k]));
    f._vmax = m;
  }
  const colPx = X.length > 1 ? Math.abs(px(X[1]) - px(X[0])) : 24;
  const maxLen = colPx * 0.95, minLen = 6;

  ctx.save();
  ctx.lineCap = 'round';
  for (let c = 0; c < X.length; c++) {
    const h = Hd[c], eta = fr.eta[c], xp = px(X[c]);
    for (let k = 0; k < sigma.length; k++) {
      const z = -h + sigma[k] * (h + eta);
      if (z > eta) continue;
      const u = fr.u[c][k], w = fr.w[c][k], sp = Math.hypot(u, w);
      if (sp < 1e-5) continue;
      const yp = py(z), ang = Math.atan2(-w, u);   // screen angle (+w is up)
      const L = clamp((sp / f._vmax) * maxLen, minLen, maxLen);
      const x1 = xp + L * Math.cos(ang), y1 = yp + L * Math.sin(ang);
      // dark halo first, then the speed-coloured arrow on top
      ctx.strokeStyle = 'rgba(10,20,30,0.55)'; ctx.fillStyle = 'rgba(10,20,30,0.55)';
      ctx.lineWidth = 3.2; drawArrow(ctx, xp, yp, x1, y1);
      const col = speedColor(sp / f._vmax);
      ctx.strokeStyle = col; ctx.fillStyle = col;
      ctx.lineWidth = 1.6; drawArrow(ctx, xp, yp, x1, y1);
    }
  }
  ctx.restore();
}

// Particle orbits: the looping path a water particle traces over a wave period,
// integrated from the solver's velocity field (kinematics over time). Real
// orbits are only centimetres wide, so each loop is MAGNIFIED about its centre
// and drawn in uniform screen scale (so a circle reads as a circle, an ellipse
// as an ellipse) — the magnification factor is labelled. A dot animates around
// each loop in sync with the playback.
function drawOrbits(ctx, run, px, py, ml, mt) {
  if (!run.has_kinematics_data) return;
  const o = orbitsFor(run);
  if (!o || !o.orbits.length) return;
  const period = o.period > 0 ? o.period : 1;

  // One uniform px-per-metre scale for every orbit's shape (cached). Sized so the
  // largest orbit is a readable ~30 px radius.
  if (o._S == null) {
    let maxExt = 1e-4;
    for (const orb of o.orbits) {
      const a = orb.path;
      let cx = 0, cz = 0;
      for (const q of a) { cx += q[0]; cz += q[1]; }
      cx /= a.length; cz /= a.length;
      orb._c = [cx, cz];
      for (const q of a) maxExt = Math.max(maxExt, Math.hypot(q[0] - cx, q[1] - cz));
    }
    o._S = 30 / maxExt;
  }
  const S = o._S;

  ctx.save();
  ctx.lineCap = 'round'; ctx.lineJoin = 'round';
  for (const orb of o.orbits) {
    const a = orb.path, c = orb._c, ax = px(c[0]), az = py(c[1]);
    ctx.beginPath();
    for (let i = 0; i < a.length; i++) {
      const sx = ax + (a[i][0] - c[0]) * S, sy = az - (a[i][1] - c[1]) * S;
      if (i === 0) ctx.moveTo(sx, sy); else ctx.lineTo(sx, sy);
    }
    ctx.strokeStyle = 'rgba(10,20,30,0.45)'; ctx.lineWidth = 3; ctx.stroke();
    ctx.strokeStyle = 'rgba(120,230,255,0.95)'; ctx.lineWidth = 1.6; ctx.stroke();
    // particle dot, cycling around the (magnified) loop with the clock
    const frac = ((state.t / period) % 1 + 1) % 1;
    const pt = a[Math.min(a.length - 1, Math.floor(frac * a.length))];
    const dx = ax + (pt[0] - c[0]) * S, dy = az - (pt[1] - c[1]) * S;
    ctx.beginPath(); ctx.arc(dx, dy, 3.2, 0, 2 * Math.PI);
    ctx.fillStyle = '#fff'; ctx.fill();
    ctx.strokeStyle = 'rgba(10,30,50,0.9)'; ctx.lineWidth = 1; ctx.stroke();
  }
  // magnification label (S vs the true horizontal pixels-per-metre)
  const pxPerM = Math.abs(px(1) - px(0)) || 1;
  const mag = Math.round(S / pxPerM);
  if (mag > 1) {
    haloText(ctx, 'orbits ×' + mag + ' (magnified)', ml + 8, mt + 14,
             { font: '10px system-ui, sans-serif', fill: '#06506a' });
  }
  ctx.restore();
}

function drawSection(ctx, W, H, run, idx) {
  const o = state.opts;
  const { ml, mr, mt, mb } = sectionMargins(W, H);
  const pw = W - ml - mr, ph = H - mt - mb;
  const xs = run.x, eta = run.eta[idx];
  const x0 = xs[0], x1 = xs[xs.length - 1];
  const peak = peakFor(run);
  const depth = run.depth;

  let y0, y1;
  if (o.fullDepth && depth != null) {
    y1 = Math.max(1.6 * peak, 0.22 * depth);
    y0 = -depth - 0.10 * depth;        // sand band below the seabed
  } else {
    y1 = 1.5 * peak; y0 = -1.5 * peak;
  }
  const px = (x) => ml + ((x - x0) / (x1 - x0)) * pw;
  const py = (y) => mt + ((y1 - y) / (y1 - y0)) * ph;

  ctx.fillStyle = '#ffffff';
  ctx.fillRect(0, 0, W, H);

  ctx.save();
  ctx.beginPath();
  ctx.rect(ml, mt, pw, ph);
  ctx.clip();

  // Sky
  if (o.fill) {
    const sky = ctx.createLinearGradient(0, mt, 0, py(0));
    sky.addColorStop(0, '#cfe8f7');
    sky.addColorStop(1, '#eaf6fd');
    ctx.fillStyle = sky;
    ctx.fillRect(ml, mt, pw, ph);
  }

  // Water: solid fill from the surface down to the seabed (or out of view)
  const bottomY = depth != null ? -depth : y0 - (y1 - y0);
  if (o.fill) {
    ctx.beginPath();
    ctx.moveTo(px(xs[0]), py(eta[0]));
    for (let i = 1; i < xs.length; i++) ctx.lineTo(px(xs[i]), py(eta[i]));
    ctx.lineTo(px(x1), py(bottomY));
    ctx.lineTo(px(x0), py(bottomY));
    ctx.closePath();
    const wat = ctx.createLinearGradient(0, py(peak), 0, py(bottomY));
    wat.addColorStop(0, '#54aade');
    wat.addColorStop(1, '#16486f');
    ctx.fillStyle = wat;
    ctx.fill();
  }

  // Seabed: sand below the bottom profile
  if (o.seabed && depth != null) {
    ctx.beginPath();
    ctx.rect(ml, py(-depth), pw, mt + ph - py(-depth));
    ctx.fillStyle = '#d8bf8f';
    ctx.fill();
    ctx.beginPath();
    ctx.moveTo(ml, py(-depth));
    ctx.lineTo(ml + pw, py(-depth));
    ctx.strokeStyle = '#a8916a';
    ctx.lineWidth = 2;
    ctx.stroke();
  }

  // Still-water level
  ctx.beginPath();
  ctx.setLineDash([5, 4]);
  ctx.moveTo(ml, py(0));
  ctx.lineTo(ml + pw, py(0));
  ctx.strokeStyle = 'rgba(80,90,100,0.55)';
  ctx.lineWidth = 1;
  ctx.stroke();
  ctx.setLineDash([]);

  // Free surface line (always drawn — it IS the data)
  ctx.beginPath();
  ctx.moveTo(px(xs[0]), py(eta[0]));
  for (let i = 1; i < xs.length; i++) ctx.lineTo(px(xs[i]), py(eta[i]));
  ctx.strokeStyle = o.fill ? '#0a4d80' : '#1f77b4';
  ctx.lineWidth = 2;
  ctx.stroke();

  // Generation / absorption zones
  if (o.zones && run.zones.length) drawZones(ctx, run, px, mt, ph);

  // Subsurface kinematics overlays (toggles; cross-section only)
  if (o.kinematics) drawFlowField(ctx, run, px, py);
  if (o.orbits) drawOrbits(ctx, run, px, py, ml, mt);

  // Reference person (1.8 m): stands on the seabed when the bottom is in
  // view, otherwise floats at the still-water line so it stays a usable
  // scale reference in zoomed or deep-water views, where the seabed is far
  // below the bottom of the frame.
  if (o.person) {
    const seabedInView = depth != null && -depth >= y0 && -depth <= y1;
    const footZ = seabedInView ? -depth : 0;
    const hPx = py(footZ) - py(footZ + 1.8);
    drawPerson(ctx, px(x0 + 0.58 * (x1 - x0)), py(footZ), hPx);
  }

  // Scale bars (separate horizontal/vertical — honest under exaggeration)
  if (o.scalebar) {
    const hLen = niceNum((x1 - x0) / 5);
    const hPxLen = (hLen / (x1 - x0)) * pw;
    const bx = ml + 12, by = mt + ph - 14;
    ctx.strokeStyle = '#222';
    ctx.lineWidth = 1.5;
    ctx.beginPath();
    ctx.moveTo(bx, by); ctx.lineTo(bx + hPxLen, by);
    ctx.moveTo(bx, by - 4); ctx.lineTo(bx, by + 4);
    ctx.moveTo(bx + hPxLen, by - 4); ctx.lineTo(bx + hPxLen, by + 4);
    ctx.stroke();
    haloText(ctx, fmtNum(hLen) + ' m', bx + hPxLen / 2, by - 6,
             { align: 'center', font: '11px system-ui, sans-serif' });

    const vLen = niceNum((y1 - y0) / 5);
    const vPxLen = (vLen / (y1 - y0)) * ph;
    const vy = by - 14;
    ctx.beginPath();
    ctx.moveTo(bx, vy); ctx.lineTo(bx, vy - vPxLen);
    ctx.moveTo(bx - 4, vy); ctx.lineTo(bx + 4, vy);
    ctx.moveTo(bx - 4, vy - vPxLen); ctx.lineTo(bx + 4, vy - vPxLen);
    ctx.stroke();
    haloText(ctx, fmtNum(vLen) + ' m', bx + 10, vy - vPxLen / 2 + 4,
             { font: '11px system-ui, sans-serif' });
  }

  ctx.restore();

  // Frame, axes, timestamp
  ctx.strokeStyle = '#999';
  ctx.lineWidth = 1;
  ctx.strokeRect(ml, mt, pw, ph);

  if (o.axes) {
    drawXAxis(ctx, ml, mt, pw, ph, x0, x1, 'x [m]');
    drawYAxis(ctx, ml, mt, pw, ph, y0, y1, 'elevation [m]');
    // Timestamp lives in the top margin so it never collides with zone labels
    haloText(ctx, timeLabel(run, idx), ml + pw, mt - 8,
             { align: 'right', font: 'bold 13px system-ui, sans-serif' });
    const vex = (ph / (y1 - y0)) / (pw / (x1 - x0));
    if (vex >= 1.5) {
      haloText(ctx, 'vertical ×' + fmtNum(vex) + ' exaggeration',
               ml + pw - 8, mt + ph - 8,
               { align: 'right', font: '10px system-ui, sans-serif', fill: '#555' });
    }
  }
}

function drawZones(ctx, run, px, mt, ph) {
  run.zones.forEach((z) => {
    const zx0 = px(z.x0), zx1 = px(z.x1);
    const gen = z.kind === 'generation';
    ctx.fillStyle = gen ? 'rgba(46,160,67,0.13)' : 'rgba(214,40,40,0.10)';
    ctx.fillRect(zx0, mt, zx1 - zx0, ph);
    ctx.setLineDash([3, 4]);
    ctx.strokeStyle = gen ? 'rgba(30,110,45,0.5)' : 'rgba(160,40,40,0.5)';
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(zx0, mt); ctx.lineTo(zx0, mt + ph);
    ctx.moveTo(zx1, mt); ctx.lineTo(zx1, mt + ph);
    ctx.stroke();
    ctx.setLineDash([]);

    const label = z.label || z.kind;
    let size = 12;
    ctx.font = size + 'px system-ui, sans-serif';
    while (size > 8 && ctx.measureText(label).width > zx1 - zx0 - 8) {
      size -= 1;
      ctx.font = size + 'px system-ui, sans-serif';
    }
    if (ctx.measureText(label).width <= zx1 - zx0 - 8) {
      haloText(ctx, label, (zx0 + zx1) / 2, mt + 16, {
        align: 'center', font: size + 'px system-ui, sans-serif',
        fill: gen ? '#1c6e30' : '#9c2a2a',
      });
    }
  });
}

function drawPerson(ctx, cx, footY, hPx) {
  if (!(hPx > 0)) return;
  // Below the size where a silhouette reads as a human, fall back to an
  // honest, to-scale reference bar + label so the person never silently
  // vanishes — e.g. a 1.8 m figure in a full-depth view of deep water.
  if (hPx < 14) { drawPersonTick(ctx, cx, footY, hPx); return; }
  const u = hPx / 100;            // person drawn in a 100-unit-tall box
  ctx.save();
  ctx.fillStyle = 'rgba(40,40,45,0.85)';
  ctx.beginPath();                 // head
  ctx.arc(cx, footY - 91 * u, 8 * u, 0, 2 * Math.PI);
  ctx.fill();
  ctx.beginPath();                 // torso
  ctx.roundRect(cx - 9 * u, footY - 80 * u, 18 * u, 38 * u, 6 * u);
  ctx.fill();
  ctx.beginPath();                 // legs
  ctx.roundRect(cx - 8 * u, footY - 45 * u, 7 * u, 45 * u, 3 * u);
  ctx.roundRect(cx + 1 * u, footY - 45 * u, 7 * u, 45 * u, 3 * u);
  ctx.fill();
  ctx.beginPath();                 // arms
  ctx.roundRect(cx - 14 * u, footY - 78 * u, 5 * u, 30 * u, 2.5 * u);
  ctx.roundRect(cx + 9 * u, footY - 78 * u, 5 * u, 30 * u, 2.5 * u);
  ctx.fill();
  ctx.restore();
  haloText(ctx, '1.8 m', cx + 14 * u, footY - 50 * u,
           { font: '10px system-ui, sans-serif', fill: '#333' });
}

// Too small for a silhouette: a to-scale 1.8 m bar with end caps + label, so
// the human reference is honest about scale yet still findable on the canvas.
function drawPersonTick(ctx, cx, footY, hPx) {
  ctx.save();
  ctx.strokeStyle = 'rgba(40,40,45,0.9)';
  ctx.lineWidth = 2;
  ctx.beginPath();
  ctx.moveTo(cx, footY);
  ctx.lineTo(cx, footY - hPx);       // exactly 1.8 m at the current scale
  ctx.moveTo(cx - 3, footY);
  ctx.lineTo(cx + 3, footY);         // foot cap
  ctx.moveTo(cx - 3, footY - hPx);
  ctx.lineTo(cx + 3, footY - hPx);   // head cap
  ctx.stroke();
  ctx.restore();
  haloText(ctx, '1.8 m', cx + 7, footY - hPx - 4,
           { font: '10px system-ui, sans-serif', fill: '#333' });
}

// ---------------------------------------------------------------------------
// Heatmap (x–t) view
// ---------------------------------------------------------------------------

function heatmapBitmap(run, peak) {
  const cached = heatmapCache.get(run.run_id);
  if (cached && cached.peak === peak) return cached.canvas;
  const n = run.eta.length, m = run.x.length;
  const off = document.createElement('canvas');
  off.width = m; off.height = n;
  const octx = off.getContext('2d');
  const img = octx.createImageData(m, n);
  for (let r = 0; r < n; r++) {
    const snap = run.eta[n - 1 - r];      // row 0 (top) = latest time
    for (let i = 0; i < m; i++) {
      const c = divergingColor(snap[i] / peak);
      const k = (r * m + i) * 4;
      img.data[k] = c[0]; img.data[k + 1] = c[1];
      img.data[k + 2] = c[2]; img.data[k + 3] = 255;
    }
  }
  octx.putImageData(img, 0, 0);
  heatmapCache.set(run.run_id, { peak, canvas: off });
  return off;
}

function heatmapMargins(W, H) {
  const o = state.opts;
  return { ml: o.axes ? 58 : 16, mr: 84, mt: 28, mb: o.axes ? 42 : 16 };
}

function drawHeatmap(ctx, W, H, run, idx) {
  const o = state.opts;
  const { ml, mr, mt, mb } = heatmapMargins(W, H);
  const pw = W - ml - mr, ph = H - mt - mb;
  const x0 = run.x[0], x1 = run.x[run.x.length - 1];
  const peak = peakFor(run);
  const tMax = Math.max(maxDuration(), 1e-9);
  const dur = runDuration(run);

  const px = (x) => ml + ((x - x0) / (x1 - x0)) * pw;
  const py = (t) => mt + ((tMax - t) / tMax) * ph;   // t=0 at the bottom

  ctx.fillStyle = '#ffffff';
  ctx.fillRect(0, 0, W, H);
  ctx.fillStyle = '#f2f2f2';
  ctx.fillRect(ml, mt, pw, ph);

  // Bitmap occupies this run's own duration; shorter runs leave headroom
  const bmp = heatmapBitmap(run, peak);
  const yTop = py(dur), yBot = py(0);
  ctx.imageSmoothingEnabled = true;
  ctx.drawImage(bmp, ml, yTop, pw, yBot - yTop);

  if (o.zones && run.zones.length) drawZones(ctx, run, px, mt, ph);

  // Current-time marker (click / drag to scrub)
  const tNow = clamp(state.t, 0, tMax);
  ctx.save();
  ctx.setLineDash([6, 4]);
  ctx.lineWidth = 1.5;
  ctx.strokeStyle = 'rgba(0,0,0,0.7)';
  ctx.beginPath();
  ctx.moveTo(ml, py(tNow)); ctx.lineTo(ml + pw, py(tNow));
  ctx.stroke();
  ctx.setLineDash([]);
  ctx.restore();

  ctx.strokeStyle = '#999';
  ctx.lineWidth = 1;
  ctx.strokeRect(ml, mt, pw, ph);

  // Colorbar — diverging, centred on still-water level, labelled in metres
  const cbX = ml + pw + 14, cbW = 13;
  for (let yPix = 0; yPix < ph; yPix++) {
    const v = 1 - 2 * (yPix / (ph - 1));     // +peak at top → −peak at bottom
    ctx.fillStyle = rgb(divergingColor(v));
    ctx.fillRect(cbX, mt + yPix, cbW, 1.5);
  }
  ctx.strokeStyle = '#999';
  ctx.strokeRect(cbX, mt, cbW, ph);
  const cbT = (f, y) => haloText(ctx, f, cbX + cbW + 4, y,
    { font: '10px system-ui, sans-serif', fill: '#333' });
  cbT('+' + fmtNum(peak) + ' m', mt + 9);
  cbT('above', mt + 21);
  cbT('0 — still water', mt + ph / 2 + 3);
  cbT('below', mt + ph - 13);
  cbT('−' + fmtNum(peak) + ' m', mt + ph - 1);

  if (o.axes) {
    drawXAxis(ctx, ml, mt, pw, ph, x0, x1, 'x [m]');
    drawYAxis(ctx, ml, mt, pw, ph, 0, tMax,
              run.has_time ? 't [s]' : 'snapshot');
    haloText(ctx, timeLabel(run, idx), ml + pw, mt - 8,
             { align: 'right', font: 'bold 13px system-ui, sans-serif' });
  }
}

// ---------------------------------------------------------------------------
// Perspective 3D surface view
// ---------------------------------------------------------------------------

function drawSurface(ctx, W, H, run, idx) {
  const o = state.opts;
  const eta = run.eta[idx];
  const m = run.x.length;
  const peak = peakFor(run);
  const stride = Math.max(1, Math.ceil(m / 200));
  const ROWS = 20;
  const WAVE_H = 0.16;               // scene height of the elevation peak

  // Axonometric projection: u along x [-1,1], v lateral [0,1], w up
  const ex = { x: 0.96, y: 0.10 };
  const ev = { x: 0.34, y: -0.34 };
  const S = (W - 50) / (2 * ex.x + ev.x);
  const cx = W / 2 - (ev.x * S) / 2;
  const cy = H * 0.60;
  const proj = (u, v, w) => ({
    x: cx + (u * ex.x + v * ev.x) * S,
    y: cy + (u * ex.y + v * ev.y) * S - w * S,
  });

  const sky = ctx.createLinearGradient(0, 0, 0, H);
  sky.addColorStop(0, '#a8d4f0');
  sky.addColorStop(0.55, '#e8f4fb');
  sky.addColorStop(1, '#f6fbfe');
  ctx.fillStyle = sky;
  ctx.fillRect(0, 0, W, H);

  const u = (i) => -1 + 2 * (i / (m - 1));
  const w = (i) => (eta[i] / peak) * WAVE_H;

  const DEEP = [18, 76, 122], LIGHT = [124, 196, 234];
  const SPEC = [232, 246, 255], HAZE = [205, 230, 244];
  const L = { x: -0.42, y: 0, z: 0.85 };       // light direction (normalized-ish)
  const lLen = Math.hypot(L.x, L.z);

  for (let j = ROWS; j >= 1; j--) {            // far rows first (painter's)
    const vFar = j / ROWS, vNear = (j - 1) / ROWS;
    const fade = 0.22 * (j / ROWS);            // haze toward the horizon
    for (let i = 0; i + stride < m; i += stride) {
      const i2 = Math.min(i + stride, m - 1);
      const du = u(i2) - u(i);
      const slope = (w(i2) - w(i)) / (du || 1e-9);
      // Long-crested surface: the normal has no lateral component
      const nLen = Math.hypot(slope, 1);
      let b = (-slope * L.x + 1 * L.z) / (nLen * lLen);
      b = clamp(0.5 + 0.5 * b, 0, 1);
      let col = lerpColor(DEEP, LIGHT, b);
      if (b > 0.94) col = lerpColor(col, SPEC, (b - 0.94) / 0.06);
      col = lerpColor(col, HAZE, fade);

      const p1 = proj(u(i), vFar, w(i));
      const p2 = proj(u(i2), vFar, w(i2));
      const p3 = proj(u(i2), vNear, w(i2));
      const p4 = proj(u(i), vNear, w(i));
      ctx.beginPath();
      ctx.moveTo(p1.x, p1.y); ctx.lineTo(p2.x, p2.y);
      ctx.lineTo(p3.x, p3.y); ctx.lineTo(p4.x, p4.y);
      ctx.closePath();
      ctx.fillStyle = rgb(col);
      ctx.strokeStyle = rgb(col);   // hide hairline seams between quads
      ctx.lineWidth = 0.6;
      ctx.fill();
      ctx.stroke();
    }
  }

  // Front face: water body below the near edge of the surface
  const frontBottom = cy + (1 * ex.y) * S + WAVE_H * S * 1.7;
  ctx.beginPath();
  let p = proj(u(0), 0, w(0));
  ctx.moveTo(p.x, p.y);
  for (let i = stride; i < m; i += stride) {
    const ii = Math.min(i, m - 1);
    p = proj(u(ii), 0, w(ii));
    ctx.lineTo(p.x, p.y);
  }
  p = proj(u(m - 1), 0, w(m - 1));
  ctx.lineTo(p.x, p.y);
  ctx.lineTo(p.x, frontBottom);
  const pl = proj(u(0), 0, w(0));
  ctx.lineTo(pl.x, frontBottom);
  ctx.closePath();
  const body = ctx.createLinearGradient(0, cy - WAVE_H * S, 0, frontBottom);
  body.addColorStop(0, '#2a6fa8');
  body.addColorStop(1, '#123a5c');
  ctx.fillStyle = body;
  ctx.fill();

  if (o.axes) {
    haloText(ctx, timeLabel(run, idx), W - 12, 22,
             { align: 'right', font: 'bold 13px system-ui, sans-serif' });
    const x0 = run.x[0], x1 = run.x[run.x.length - 1];
    const vex = (WAVE_H / peak) / (2 * ex.x / (x1 - x0));
    haloText(ctx,
      'long-crested surface from η(x) — vertical ×' + fmtNum(vex) + ' exaggeration',
      W - 12, H - 10,
      { align: 'right', font: '10px system-ui, sans-serif', fill: '#555' });
    haloText(ctx, fmtNum(x1 - x0) + ' m domain', 12, H - 10,
             { font: '10px system-ui, sans-serif', fill: '#555' });
  }
}

// ---------------------------------------------------------------------------
// Axes
// ---------------------------------------------------------------------------

function drawXAxis(ctx, ml, mt, pw, ph, x0, x1, label) {
  const step = niceNum((x1 - x0) / 6);
  ctx.strokeStyle = '#666';
  ctx.lineWidth = 1;
  for (let x = Math.ceil(x0 / step) * step; x <= x1 + 1e-9; x += step) {
    const xp = ml + ((x - x0) / (x1 - x0)) * pw;
    ctx.beginPath();
    ctx.moveTo(xp, mt + ph); ctx.lineTo(xp, mt + ph + 5);
    ctx.stroke();
    haloText(ctx, fmtNum(x), xp, mt + ph + 17,
             { align: 'center', font: '10px system-ui, sans-serif', fill: '#444' });
  }
  haloText(ctx, label, ml + pw / 2, mt + ph + 32,
           { align: 'center', font: '11px system-ui, sans-serif', fill: '#333' });
}

function drawYAxis(ctx, ml, mt, pw, ph, y0, y1, label) {
  const step = niceNum((y1 - y0) / 5);
  ctx.strokeStyle = '#666';
  ctx.lineWidth = 1;
  for (let y = Math.ceil(y0 / step) * step; y <= y1 + 1e-9; y += step) {
    const yp = mt + ((y1 - y) / (y1 - y0)) * ph;
    ctx.beginPath();
    ctx.moveTo(ml - 5, yp); ctx.lineTo(ml, yp);
    ctx.stroke();
    haloText(ctx, fmtNum(Math.abs(y) < 1e-12 ? 0 : y), ml - 8, yp + 3,
             { align: 'right', font: '10px system-ui, sans-serif', fill: '#444' });
  }
  ctx.save();
  ctx.translate(12, mt + ph / 2);
  ctx.rotate(-Math.PI / 2);
  haloText(ctx, label, 0, 0,
           { align: 'center', font: '11px system-ui, sans-serif', fill: '#333' });
  ctx.restore();
}

function timeLabel(run, idx) {
  return run.has_time
    ? 't = ' + run.times[idx].toFixed(2) + ' s'
    : 'snapshot ' + (idx + 1) + '/' + run.eta.length;
}

// ---------------------------------------------------------------------------
// Rendering loop
// ---------------------------------------------------------------------------

function render() {
  panels.forEach((panel) => {
    const run = state.data.get(panel.id);
    if (!run) return;
    const ctx = panel.ctx;
    const dpr = window.devicePixelRatio || 1;
    const W = panel.canvas.width / dpr, H = panel.canvas.height / dpr;
    ctx.save();
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    const idx = frameIndex(run, state.t);
    if (state.view === 'section') drawSection(ctx, W, H, run, idx);
    else if (state.view === 'heatmap') drawHeatmap(ctx, W, H, run, idx);
    else drawSurface(ctx, W, H, run, idx);
    ctx.restore();
  });
}

function tick(now) {
  if (lastTick == null) lastTick = now;
  const dtWall = (now - lastTick) / 1000;
  lastTick = now;

  const dur = maxDuration();
  if (state.playing && dur > 0 && panels.length) {
    // Untimed runs animate at ~8 snapshots per second
    const rate = anyTimed() ? state.speed : 8 * state.speed;
    state.t += dtWall * rate;
    if (state.t > dur) state.t = 0;
    syncPlaybar();
    dirty = true;
  }
  if (dirty) { render(); dirty = false; }
  requestAnimationFrame(tick);
}

function scheduleRender() { dirty = true; }

// ---------------------------------------------------------------------------
// Playback controls
// ---------------------------------------------------------------------------

function syncPlaybar() {
  const slider = $('#time-slider');
  slider.max = String(Math.max(maxDuration(), 1e-9));
  slider.value = String(state.t);
  $('#time-readout').textContent = anyTimed()
    ? 't = ' + state.t.toFixed(2) + ' s'
    : 'snapshot ' + (Math.round(state.t) + 1);
  $('#play').textContent = state.playing ? '⏸' : '▶';
}

function setPlaying(p) {
  state.playing = p;
  syncPlaybar();
}

// ---------------------------------------------------------------------------
// Panels / layout
// ---------------------------------------------------------------------------

function sizeCanvases() {
  const dpr = window.devicePixelRatio || 1;
  panels.forEach((panel) => {
    const w = panel.canvas.clientWidth || panel.canvas.parentElement.clientWidth;
    const h = Math.max(240, Math.round(w * 0.46));
    panel.canvas.width = Math.round(w * dpr);
    panel.canvas.height = Math.round(h * dpr);
    panel.canvas.style.height = h + 'px';
  });
  scheduleRender();
}

function rebuildPanels() {
  const plots = $('#plots');
  plots.innerHTML = '';
  panels.length = 0;

  state.selected.forEach((id) => {
    const run = state.data.get(id);
    if (!run) return;
    const panel = document.createElement('div');
    panel.className = 'plot-panel';

    const head = document.createElement('div');
    head.className = 'plot-head';
    const title = document.createElement('span');
    title.className = 'plot-title';
    title.textContent = titleFor(run);
    const sub = document.createElement('span');
    sub.className = 'plot-id';
    sub.textContent = state.opts.plainTitle ? run.run_id : '';
    head.appendChild(title);
    head.appendChild(sub);
    if (state.selected.length > 1) {
      const close = document.createElement('button');
      close.className = 'plot-close';
      close.title = 'Remove from comparison';
      close.textContent = '✕';
      close.addEventListener('click', () => toggleRun(id, false));
      head.appendChild(close);
    }
    panel.appendChild(head);

    const canvas = document.createElement('canvas');
    attachScrub(canvas);
    panel.appendChild(canvas);
    plots.appendChild(panel);
    panels.push({ id, canvas, ctx: canvas.getContext('2d') });
  });

  $('#empty-state').hidden = panels.length > 0;
  updateHeader();
  updateDownloads();
  sizeCanvases();
  syncPlaybar();
  applyViewVisibility();   // refresh the kinematics gallery if it's the active view
}

function updateHeader() {
  const runs = loadedRuns();
  if (!runs.length) {
    $('#main-title').textContent = 'OceanWave3D viewer';
    $('#subtitle').textContent = '';
  } else if (runs.length === 1) {
    $('#main-title').textContent = titleFor(runs[0]);
    $('#subtitle').textContent = runs[0].run_id;
  } else {
    $('#main-title').textContent = 'Comparing ' + runs.length + ' runs';
    $('#subtitle').textContent = 'time, view, toggles and scales are linked';
  }
  document.title = 'OceanWave3D — ' +
    (runs.length === 1 ? titleFor(runs[0]) : 'viewer');
}

function updateDownloads() {
  const el = $('#downloads');
  const run = loadedRuns()[0];
  if (!run) { el.innerHTML = ''; return; }
  const links = [];
  if (run.has_gif) {
    links.push('<a href="/files/' + encodeURIComponent(run.run_id) +
               '/animation.gif" download>download GIF</a>');
  }
  if (run.has_png) {
    links.push('<a href="/files/' + encodeURIComponent(run.run_id) +
               '/final.png" download>download final PNG</a>');
  }
  links.push('<a href="/api/data/' + encodeURIComponent(run.run_id) +
             '" target="_blank">raw data (JSON)</a>');
  el.innerHTML = 'Primary run: ' + links.join(' · ') +
    ' · served from this machine only (localhost)';
}

function syncURL() {
  let url = '/';
  if (state.selected.length) {
    url = '/view/' + encodeURIComponent(state.selected[0]);
    if (state.selected.length > 1) {
      url += '?compare=' +
        state.selected.slice(1).map(encodeURIComponent).join(',');
    }
  }
  history.replaceState(null, '', url);
}

// ---------------------------------------------------------------------------
// Heatmap scrubbing
// ---------------------------------------------------------------------------

function attachScrub(canvas) {
  let scrubbing = false;
  const apply = (ev) => {
    if (state.view !== 'heatmap') return;
    const rect = canvas.getBoundingClientRect();
    const W = rect.width, H = rect.height;
    const { mt, mb } = heatmapMargins(W, H);
    const ph = H - mt - mb;
    const y = clamp(ev.clientY - rect.top, mt, mt + ph);
    state.t = ((mt + ph - y) / ph) * maxDuration();
    syncPlaybar();
    scheduleRender();
  };
  canvas.addEventListener('pointerdown', (ev) => {
    if (state.view !== 'heatmap') return;
    scrubbing = true;
    setPlaying(false);
    canvas.setPointerCapture(ev.pointerId);
    apply(ev);
  });
  canvas.addEventListener('pointermove', (ev) => { if (scrubbing) apply(ev); });
  canvas.addEventListener('pointerup', () => { scrubbing = false; });
}

// ---------------------------------------------------------------------------
// Run selection
// ---------------------------------------------------------------------------

async function fetchRunData(id) {
  if (state.data.has(id)) return state.data.get(id);
  const resp = await fetch('/api/data/' + encodeURIComponent(id));
  if (!resp.ok) return null;
  const payload = await resp.json();
  state.data.set(id, payload);
  return payload;
}

async function toggleRun(id, on) {
  if (on) {
    if (!state.selected.includes(id)) {
      const data = await fetchRunData(id);
      if (data) state.selected.push(id);
    }
  } else {
    state.selected = state.selected.filter((r) => r !== id);
  }
  heatmapCache.clear();    // shared peak may have changed
  syncURL();
  rebuildPanels();
  renderRunsList();
}

async function refreshRunsList() {
  try {
    const resp = await fetch('/api/runs');
    state.runsList = (await resp.json()).runs || [];
  } catch (e) {
    state.runsList = [];
  }
  renderRunsList();
}

function renderRunsList() {
  const ul = $('#runs-list');
  ul.innerHTML = '';
  const anySession = state.runsList.some((r) => r.session);
  const visible = state.runsList.filter(
    (r) => state.showAll || !anySession || r.session || state.selected.includes(r.id));
  if (!visible.length) {
    const li = document.createElement('li');
    li.textContent = 'No rendered runs found.';
    ul.appendChild(li);
    return;
  }
  visible.forEach((r) => {
    const li = document.createElement('li');
    const label = document.createElement('label');
    const cb = document.createElement('input');
    cb.type = 'checkbox';
    cb.checked = state.selected.includes(r.id);
    cb.addEventListener('change', () => toggleRun(r.id, cb.checked));
    const txt = document.createElement('span');
    txt.innerHTML =
      (r.title === r.id ? '' : escapeHtml(r.title)) +
      (r.session ? ' <span class="badge">this session</span>' : '') +
      '<span class="run-id">' + escapeHtml(r.id) + '</span>';
    label.appendChild(cb);
    label.appendChild(txt);
    li.appendChild(label);
    ul.appendChild(li);
  });
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c]));
}

// ---------------------------------------------------------------------------
// Kinematics view — OceanWave3D's own ReadKinematics.m figures (static PNGs)
// ---------------------------------------------------------------------------

function applyViewVisibility() {
  const kin = state.view === 'kinematics';
  $('#plots').hidden = kin;
  $('#kinematics').hidden = !kin;
  // The cross-section annotations, display options and playback timeline are
  // meaningless for the static ReadKinematics.m figures — hide them so the
  // Kinematics view only shows controls that actually do something (the view
  // selector and the run picker).
  ['#annot-controls', '#display-controls', '#playbar'].forEach((sel) => {
    const el = $(sel);
    if (el) el.hidden = kin;
  });
  if (kin) {
    renderKinematics();
  } else {
    // #plots may have been hidden (display:none) when its canvases were last
    // sized — e.g. the page opened straight on the Kinematics tab — leaving
    // them 0-width and blank. Re-measure now that they're visible, then redraw.
    sizeCanvases();
  }
}

function renderKinematics() {
  const container = $('#kinematics');
  container.innerHTML = '';
  const runs = loadedRuns();
  if (!runs.length) {
    container.innerHTML =
      '<p class="kin-note">No run selected. Open the <strong>Runs</strong> ' +
      'panel (top right) and pick a run.</p>';
    return;
  }
  runs.forEach((run) => {
    const box = document.createElement('div');
    box.className = 'kin-run';
    const h = document.createElement('h3');
    h.textContent = titleFor(run);
    const sub = document.createElement('div');
    sub.className = 'kin-sub';
    sub.textContent = run.run_id;
    const body = document.createElement('div');
    box.appendChild(h);
    box.appendChild(sub);
    box.appendChild(body);
    container.appendChild(box);
    loadKinematics(run, body, false);
  });
}

async function loadKinematics(run, body, generate) {
  if (generate) {
    body.innerHTML = '<p class="kin-note">Running OceanWave3D ' +
      'ReadKinematics.m via Octave… (a few seconds)</p>';
  } else {
    body.innerHTML = '<p class="kin-note">Loading…</p>';
  }
  let payload;
  try {
    const url = '/api/kinematics/' + encodeURIComponent(run.run_id) +
      (generate ? '?generate=1' : '');
    payload = await (await fetch(url)).json();
  } catch (e) {
    body.innerHTML = '<p class="kin-err">Could not reach the viewer server.</p>';
    return;
  }

  body.innerHTML = '';
  if (payload.figures && payload.figures.length) {
    payload.figures.forEach((f) => {
      const fig = document.createElement('figure');
      fig.className = 'kin-fig';
      const img = document.createElement('img');
      img.src = f.url;
      img.alt = f.title || f.file;
      img.loading = 'lazy';
      const cap = document.createElement('figcaption');
      cap.textContent = f.title || f.file;
      const dl = document.createElement('a');     // per-panel PNG download
      dl.href = f.url;
      dl.download = f.file;
      dl.className = 'kin-dl';
      dl.textContent = 'download PNG';
      cap.appendChild(document.createTextNode(' · '));
      cap.appendChild(dl);
      fig.appendChild(img);
      fig.appendChild(cap);
      body.appendChild(fig);
    });
    const actions = document.createElement('div');
    actions.className = 'kin-actions';
    const dlAll = document.createElement('a');     // grab every panel at once
    dlAll.href = '/api/kinematics/' + encodeURIComponent(run.run_id) + '/zip';
    dlAll.download = run.run_id + '_kinematics.zip';
    dlAll.className = 'kin-dl';
    dlAll.textContent = 'download all (ZIP)';
    const re = document.createElement('button');
    re.textContent = 'Regenerate';
    re.addEventListener('click', () => loadKinematics(run, body, true));
    actions.appendChild(dlAll);
    actions.appendChild(re);
    body.appendChild(actions);
    return;
  }

  if (payload.error) {
    const p = document.createElement('p');
    p.className = 'kin-err';
    p.textContent = payload.error;
    body.appendChild(p);
    return;
  }
  if (!payload.octave) {
    body.innerHTML = '<p class="kin-note">GNU Octave is required to render ' +
      'OceanWave3D\'s kinematics figures. Install it on this machine, or build ' +
      'the Docker sandbox (which bundles Octave) to render them inside the ' +
      'container.</p>';
    return;
  }
  if (!run.has_kinematics_data) {
    body.innerHTML = '<p class="kin-note">This run has no kinematics output ' +
      '(Kinematics01.bin) to plot.</p>';
    return;
  }
  const note = document.createElement('p');
  note.className = 'kin-note';
  note.textContent = 'OceanWave3D\'s ReadKinematics.m can plot the subsurface ' +
    'velocity, velocity-potential and shear profiles for this run.';
  const btn = document.createElement('button');
  btn.textContent = 'Generate kinematics figures';
  btn.addEventListener('click', () => loadKinematics(run, body, true));
  body.appendChild(note);
  body.appendChild(btn);
}

// ---------------------------------------------------------------------------
// Wiring
// ---------------------------------------------------------------------------

function wireControls() {
  document.querySelectorAll('input[name="view"]').forEach((r) => {
    r.addEventListener('change', () => {
      state.view = r.value;
      applyViewVisibility();
      scheduleRender();
    });
  });

  const optMap = {
    'opt-fill': 'fill', 'opt-seabed': 'seabed', 'opt-zones': 'zones',
    'opt-scalebar': 'scalebar', 'opt-person': 'person', 'opt-axes': 'axes',
    'opt-plaintitle': 'plainTitle', 'opt-fulldepth': 'fullDepth',
    'opt-lockscales': 'lockScales', 'opt-kinematics': 'kinematics',
    'opt-orbits': 'orbits',
  };
  Object.entries(optMap).forEach(([elId, key]) => {
    const el = document.getElementById(elId);
    el.checked = state.opts[key];
    el.addEventListener('change', () => {
      state.opts[key] = el.checked;
      if (key === 'lockScales') heatmapCache.clear();
      if (key === 'plainTitle') rebuildPanels();
      scheduleRender();
    });
  });

  $('#play').addEventListener('click', () => setPlaying(!state.playing));
  $('#time-slider').addEventListener('input', (ev) => {
    setPlaying(false);
    state.t = parseFloat(ev.target.value);
    syncPlaybar();
    scheduleRender();
  });
  $('#speed').addEventListener('change', (ev) => {
    state.speed = parseFloat(ev.target.value);
  });

  const runsBtn = $('#runs-btn');
  const runsPanel = $('#runs-panel');
  runsBtn.addEventListener('click', async (ev) => {
    ev.stopPropagation();
    runsPanel.hidden = !runsPanel.hidden;
    if (!runsPanel.hidden) await refreshRunsList();
  });
  runsPanel.addEventListener('click', (ev) => ev.stopPropagation());
  document.addEventListener('click', () => { runsPanel.hidden = true; });

  $('#show-all-runs').addEventListener('change', (ev) => {
    state.showAll = ev.target.checked;
    renderRunsList();
  });

  window.addEventListener('resize', sizeCanvases);
}

async function init() {
  wireControls();

  // /view/<run_id>?compare=a,b&format=gif|png&view=section|heatmap|surface
  const parts = location.pathname.split('/').filter(Boolean);
  const params = new URLSearchParams(location.search);
  const view = params.get('view');
  if (['section', 'heatmap', 'surface', 'kinematics'].includes(view)) {
    state.view = view;
    const radio = document.querySelector('input[name="view"][value="' + view + '"]');
    if (radio) radio.checked = true;
  }
  const ids = [];
  if (parts[0] === 'view' && parts[1]) ids.push(decodeURIComponent(parts[1]));
  (params.get('compare') || '').split(',').filter(Boolean)
    .forEach((id) => ids.push(decodeURIComponent(id)));

  for (const id of ids) {
    const data = await fetchRunData(id);
    if (data && !state.selected.includes(id)) state.selected.push(id);
  }

  await refreshRunsList();
  rebuildPanels();

  if (params.get('format') === 'png') {
    // Legacy "still image" link: open paused on the final snapshot
    state.playing = false;
    state.t = maxDuration();
  }
  syncPlaybar();

  if (!state.selected.length) $('#runs-panel').hidden = false;

  requestAnimationFrame(tick);
}

init();
