/*
 * confetti.js — lightweight canvas confetti for ClassRally (no dependencies).
 *
 * Exposes a single global: window.Confetti
 *
 *   Confetti.burst({x, y, count, spread})
 *       One-shot explosion. x/y in CSS px (default: horizontal centre, top
 *       third of the viewport), count = particle count (default 120),
 *       spread = emission cone in degrees centred upwards (default 120).
 *       Call with no arguments for the default celebratory burst.
 *
 *   Confetti.rain(durationMs)
 *       Confetti falling from the top edge across the full width for
 *       durationMs (default 3000 ms).
 *
 *   Confetti.stop()
 *       Immediately removes all particles and clears the canvas.
 *
 * Details:
 *   - Creates its own fixed fullscreen canvas lazily on first use
 *     (z-index 9999, pointer-events: none), DPR-aware, follows resize.
 *   - Physics: gravity, air drag, spin + flutter of small rectangles and
 *     triangles in the Kahoot palette + gold/white.
 *   - The rAF loop only runs while particles exist.
 *   - Honours `prefers-reduced-motion: reduce` → every call is a no-op.
 *   - Hard cap ~400 particles so projector-grade laptops stay smooth.
 */
(function () {
  'use strict';

  const COLORS = [
    '#e21b3c', '#1368ce', '#d89e00', '#26890c', // kahoot palette
    '#ffd700', '#ffffff'                        // gold + white
  ];
  const MAX_PARTICLES = 400;
  const GRAVITY = 1150;      // px/s²
  const DRAG = 0.90;         // velocity kept per second (^dt applied)
  const TERMINAL_VY = 900;   // px/s

  let canvas = null;
  let ctx = null;
  let rafId = null;
  let lastTs = 0;
  let particles = [];
  let rainUntil = 0;         // performance.now() timestamp; 0 = not raining
  let rainCarry = 0;         // fractional particle accumulator for rain

  function reducedMotion() {
    try {
      return window.matchMedia &&
        window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    } catch (_) { return false; }
  }

  function resize() {
    if (!canvas) return;
    const dpr = Math.max(1, Math.min(2, window.devicePixelRatio || 1));
    canvas.width = Math.floor(window.innerWidth * dpr);
    canvas.height = Math.floor(window.innerHeight * dpr);
    canvas.style.width = window.innerWidth + 'px';
    canvas.style.height = window.innerHeight + 'px';
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  }

  function ensureCanvas() {
    if (canvas) return true;
    try {
      canvas = document.createElement('canvas');
      canvas.setAttribute('aria-hidden', 'true');
      canvas.style.cssText =
        'position:fixed;top:0;left:0;width:100vw;height:100vh;' +
        'z-index:9999;pointer-events:none;';
      ctx = canvas.getContext('2d');
      if (!ctx) { canvas = null; return false; }
      document.body.appendChild(canvas);
      window.addEventListener('resize', resize);
      resize();
      return true;
    } catch (_) { canvas = null; ctx = null; return false; }
  }

  function makeParticle(x, y, vx, vy) {
    const size = 6 + Math.random() * 6;
    return {
      x: x, y: y, vx: vx, vy: vy,
      w: size, h: size * (0.55 + Math.random() * 0.5),
      tri: Math.random() < 0.35,                       // triangle or rectangle
      color: COLORS[(Math.random() * COLORS.length) | 0],
      rot: Math.random() * Math.PI * 2,                // in-plane rotation
      vrot: (Math.random() - 0.5) * 12,                // rad/s
      flip: Math.random() * Math.PI * 2,               // flutter phase
      vflip: 6 + Math.random() * 10,                   // flutter speed
      sway: 30 + Math.random() * 60                    // horizontal flutter drift
    };
  }

  function spawnBurst(x, y, count, spreadDeg) {
    const room = MAX_PARTICLES - particles.length;
    count = Math.max(0, Math.min(count, room));
    const spread = (spreadDeg * Math.PI) / 180;
    const center = -Math.PI / 2; // upwards
    for (let i = 0; i < count; i++) {
      const angle = center + (Math.random() - 0.5) * spread;
      const speed = 260 + Math.random() * 620;
      particles.push(makeParticle(
        x, y,
        Math.cos(angle) * speed,
        Math.sin(angle) * speed
      ));
    }
  }

  function spawnRain(dt) {
    const rate = 140; // particles per second across the width
    rainCarry += rate * dt;
    let n = Math.floor(rainCarry);
    rainCarry -= n;
    n = Math.min(n, MAX_PARTICLES - particles.length);
    const w = window.innerWidth;
    for (let i = 0; i < n; i++) {
      const p = makeParticle(
        Math.random() * w,
        -20 - Math.random() * 40,
        (Math.random() - 0.5) * 60,
        120 + Math.random() * 240
      );
      particles.push(p);
    }
  }

  function drawParticle(p) {
    const flipScale = Math.sin(p.flip); // -1..1 → 3D-ish tumbling
    ctx.setTransform(
      Math.cos(p.rot), Math.sin(p.rot),
      -Math.sin(p.rot) * flipScale, Math.cos(p.rot) * flipScale,
      p.x, p.y
    );
    ctx.fillStyle = p.color;
    // darken slightly while "edge-on" for depth
    ctx.globalAlpha = 0.72 + 0.28 * Math.abs(flipScale);
    if (p.tri) {
      ctx.beginPath();
      ctx.moveTo(0, -p.h / 2);
      ctx.lineTo(p.w / 2, p.h / 2);
      ctx.lineTo(-p.w / 2, p.h / 2);
      ctx.closePath();
      ctx.fill();
    } else {
      ctx.fillRect(-p.w / 2, -p.h / 2, p.w, p.h);
    }
  }

  function frame(ts) {
    rafId = null;
    if (!canvas) return;
    const dt = Math.min(0.032, lastTs ? (ts - lastTs) / 1000 : 0.016);
    lastTs = ts;

    const now = performance.now();
    if (rainUntil && now < rainUntil) {
      spawnRain(dt);
    } else {
      rainUntil = 0;
      rainCarry = 0;
    }

    const h = window.innerHeight;
    const w = window.innerWidth;
    const keep = Math.pow(DRAG, dt);

    ctx.setTransform(1, 0, 0, 1, 0, 0);
    // note: canvas backing store is DPR-scaled; reset+scale for clearing
    const dpr = Math.max(1, Math.min(2, window.devicePixelRatio || 1));
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, h);

    const alive = [];
    for (let i = 0; i < particles.length; i++) {
      const p = particles[i];
      p.vx *= keep;
      p.vy = Math.min(TERMINAL_VY, p.vy * keep + GRAVITY * dt);
      p.flip += p.vflip * dt;
      p.rot += p.vrot * dt;
      p.x += (p.vx + Math.cos(p.flip) * p.sway) * dt;
      p.y += p.vy * dt;
      if (p.y < h + 40 && p.x > -60 && p.x < w + 60) {
        drawParticle(p);
        alive.push(p);
      }
    }
    ctx.globalAlpha = 1;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    particles = alive;

    if (particles.length > 0 || rainUntil) {
      rafId = requestAnimationFrame(frame);
    } else {
      lastTs = 0;
      ctx.clearRect(0, 0, w, h); // leave a clean transparent canvas
    }
  }

  function kick() {
    if (rafId == null) {
      lastTs = 0;
      rafId = requestAnimationFrame(frame);
    }
  }

  window.Confetti = {
    burst: function (opts) {
      try {
        if (reducedMotion() || !ensureCanvas()) return;
        opts = opts || {};
        const x = isFinite(opts.x) ? opts.x : window.innerWidth / 2;
        const y = isFinite(opts.y) ? opts.y : window.innerHeight / 3;
        const count = isFinite(opts.count) && opts.count > 0
          ? Math.floor(opts.count) : 120;
        const spread = isFinite(opts.spread) && opts.spread > 0
          ? opts.spread : 120;
        spawnBurst(x, y, count, spread);
        kick();
      } catch (_) {}
    },
    rain: function (durationMs) {
      try {
        if (reducedMotion() || !ensureCanvas()) return;
        const ms = isFinite(durationMs) && durationMs > 0
          ? Math.min(durationMs, 60000) : 3000;
        rainUntil = performance.now() + ms;
        kick();
      } catch (_) {}
    },
    stop: function () {
      try {
        particles = [];
        rainUntil = 0;
        rainCarry = 0;
        if (rafId != null) { cancelAnimationFrame(rafId); rafId = null; }
        lastTs = 0;
        if (canvas && ctx) {
          const dpr = Math.max(1, Math.min(2, window.devicePixelRatio || 1));
          ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
          ctx.clearRect(0, 0, window.innerWidth, window.innerHeight);
        }
      } catch (_) {}
    }
  };
})();
