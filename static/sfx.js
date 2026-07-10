/*
 * sfx.js — WebAudio game-show sound engine for ClassRally (no dependencies).
 *
 * Exposes a single global: window.SFX
 *
 *   SFX.unlock()            create/resume AudioContext — call ONLY from a user
 *                           gesture handler (click/keydown); safe to call repeatedly.
 *                           Nothing ever plays before unlock() succeeds.
 *   SFX.setEnabled(bool)    master on/off (off also stops music)
 *   SFX.enabled             getter
 *   SFX.setVolume(v)        effects volume 0..1 (default 0.5)
 *   SFX.volume              getter
 *   SFX.supported           getter — WebAudio available in this browser
 *
 *   Effects (each ~0.05–1.5 s, all no-ops until unlocked):
 *   SFX.join()              bubbly pop when a player joins
 *   SFX.blip(n)             countdown 3-2-1 (rising), n=0 = higher "go!"
 *   SFX.tick(urgent)        clock tick (~30 ms); urgent = higher + louder
 *   SFX.gong()              time's up — low strike with a tail
 *   SFX.correct()           bright major arpeggio ding (C5-E5-G5)
 *   SFX.wrong()             dull thud with pitch drop (~160→90 Hz)
 *   SFX.drumroll(ms)        filtered-noise crescendo roll (~30 hits/s);
 *                           returns a Promise resolved after ms (default 1200)
 *                           — resolves on schedule even when muted/locked,
 *                           so game flow timing stays consistent.
 *   SFX.fanfare()           celebratory ~1.5 s brass-like chord run C-F-G-C
 *   SFX.whoosh()            noise sweep (leaderboard slide-in)
 *   SFX.streak(level)       rising jingle, grows with streak level (3/4/5+)
 *   SFX.double()            golden bell ×2 (double points)
 *   SFX.pop()               generic tiny UI click
 *
 *   Music (synthesized Kahoot-style quiz loop, lookahead-scheduled):
 *   SFX.music.start()
 *   SFX.music.stop()
 *   SFX.music.setIntensity(v)   0..1 — 0 = calm ~112 BPM base; rising adds
 *                               hats/percussion and tempo (+20 BPM), ≥0.85
 *                               adds high octave arp + riser (final-seconds panic)
 *   SFX.music.setVolume(v)      music volume 0..1 (default 0.35)
 *   SFX.music.volume            getter
 *   SFX.music.playing           getter
 *
 * If WebAudio is unavailable every call is a silent no-op (no exceptions).
 */
(function () {
  'use strict';

  const AC = window.AudioContext || window.webkitAudioContext;
  const SUPPORTED = typeof AC === 'function';

  // ── State ─────────────────────────────────────────────────────────
  let ctx = null;            // AudioContext (created in unlock())
  let master = null;         // master gain → compressor → destination
  let sfxBus = null;         // effects bus (user volume)
  let musicBus = null;       // music bus (user volume)
  let musicFade = null;      // start/stop fade stage (independent of user volume)
  let noiseBuf = null;       // shared 2 s white-noise buffer
  let enabled = true;
  let sfxVol = 0.5;
  let musicVol = 0.35;

  function clamp01(v) {
    v = Number(v);
    return isFinite(v) ? Math.min(1, Math.max(0, v)) : 0;
  }

  function midi(m) { return 440 * Math.pow(2, (m - 69) / 12); }

  // ── Context / graph setup ─────────────────────────────────────────
  function buildGraph() {
    // Gentle limiter so overlapping fanfare + music never clip harshly.
    const comp = ctx.createDynamicsCompressor();
    try {
      comp.threshold.value = -16;
      comp.knee.value = 12;
      comp.ratio.value = 5;
      comp.attack.value = 0.003;
      comp.release.value = 0.25;
    } catch (_) { /* older impls: defaults are fine */ }
    comp.connect(ctx.destination);

    master = ctx.createGain();
    master.gain.value = 0.9;
    master.connect(comp);

    sfxBus = ctx.createGain();
    sfxBus.gain.value = sfxVol;
    sfxBus.connect(master);

    musicBus = ctx.createGain();
    musicBus.gain.value = musicVol;
    musicBus.connect(master);

    musicFade = ctx.createGain();
    musicFade.gain.value = 0;
    musicFade.connect(musicBus);

    // 2 s of white noise, reused by every noise-based sound.
    const len = Math.floor(ctx.sampleRate * 2);
    noiseBuf = ctx.createBuffer(1, len, ctx.sampleRate);
    const data = noiseBuf.getChannelData(0);
    for (let i = 0; i < len; i++) data[i] = Math.random() * 2 - 1;
  }

  function unlock() {
    if (!SUPPORTED) return;
    try {
      if (!ctx) {
        ctx = new AC();
        buildGraph();
      }
      if (ctx.state === 'suspended') {
        const p = ctx.resume();
        if (p && p.catch) p.catch(function () {});
      }
    } catch (_) { ctx = ctx || null; }
  }

  // ready = safe to schedule audible sound right now
  function ready() {
    return SUPPORTED && enabled && ctx && ctx.state === 'running';
  }

  // Wrap every public effect so no exception ever escapes.
  function safe(fn) {
    return function () {
      if (!ready()) return;
      try { fn.apply(null, arguments); } catch (_) {}
    };
  }

  // ── Small synthesis helpers (click-free envelopes everywhere) ─────
  const EPS = 0.0001;

  // Oscillator voice with attack/decay envelope. Returns nothing; self-stopping.
  function tone(o) {
    const t = o.t !== undefined ? o.t : ctx.currentTime;
    const attack = o.attack !== undefined ? o.attack : 0.005;
    const decay = o.decay !== undefined ? o.decay : 0.2;
    const vol = o.vol !== undefined ? o.vol : 0.3;
    const osc = ctx.createOscillator();
    osc.type = o.type || 'sine';
    osc.frequency.setValueAtTime(o.freq, t);
    if (o.glideTo) {
      osc.frequency.exponentialRampToValueAtTime(
        Math.max(1, o.glideTo), t + (o.glideTime || attack + decay));
    }
    if (o.detune) osc.detune.setValueAtTime(o.detune, t);
    if (o.vibrato) { // {freq, cents, delay}
      const lfo = ctx.createOscillator();
      const lfoGain = ctx.createGain();
      lfo.frequency.setValueAtTime(o.vibrato.freq || 6, t);
      lfoGain.gain.setValueAtTime(0, t);
      lfoGain.gain.linearRampToValueAtTime(
        o.vibrato.cents || 12, t + (o.vibrato.delay || 0.15));
      lfo.connect(lfoGain);
      lfoGain.connect(osc.detune);
      lfo.start(t);
      lfo.stop(t + attack + decay + 0.1);
    }
    const g = ctx.createGain();
    g.gain.setValueAtTime(EPS, t);
    g.gain.linearRampToValueAtTime(vol, t + attack);
    g.gain.exponentialRampToValueAtTime(EPS, t + attack + decay);
    let head = g;
    if (o.filter) { // {type, freq, q, sweepTo}
      const f = ctx.createBiquadFilter();
      f.type = o.filter.type || 'lowpass';
      f.frequency.setValueAtTime(o.filter.freq, t);
      if (o.filter.sweepTo) {
        f.frequency.exponentialRampToValueAtTime(
          Math.max(20, o.filter.sweepTo), t + attack + decay);
      }
      f.Q.value = o.filter.q || 1;
      g.connect(f);
      head = f;
    }
    osc.connect(g);
    head.connect(o.dest || sfxBus);
    osc.start(t);
    osc.stop(t + attack + decay + 0.08);
  }

  // Filtered noise burst from the shared buffer. Self-stopping.
  function noise(o) {
    const t = o.t !== undefined ? o.t : ctx.currentTime;
    const attack = o.attack !== undefined ? o.attack : 0.003;
    const decay = o.decay !== undefined ? o.decay : 0.15;
    const vol = o.vol !== undefined ? o.vol : 0.25;
    const src = ctx.createBufferSource();
    src.buffer = noiseBuf;
    src.loop = true;
    src.playbackRate.value = o.rate || 1;
    const f = ctx.createBiquadFilter();
    f.type = o.type || 'bandpass';
    f.frequency.setValueAtTime(o.freq || 1200, t);
    if (o.sweepTo) {
      f.frequency.exponentialRampToValueAtTime(
        Math.max(20, o.sweepTo), t + attack + decay);
    }
    f.Q.value = o.q !== undefined ? o.q : 0.9;
    const g = ctx.createGain();
    g.gain.setValueAtTime(EPS, t);
    g.gain.linearRampToValueAtTime(vol, t + attack);
    if (o.hold) g.gain.setValueAtTime(vol, t + attack + o.hold);
    g.gain.exponentialRampToValueAtTime(EPS, t + attack + (o.hold || 0) + decay);
    src.connect(f);
    f.connect(g);
    g.connect(o.dest || sfxBus);
    src.start(t, Math.random() * 1.2); // random buffer offset → no repetition
    src.stop(t + attack + (o.hold || 0) + decay + 0.05);
  }

  // ── Effects ───────────────────────────────────────────────────────

  const join = safe(function () {
    const t = ctx.currentTime;
    const jitter = 1 + (Math.random() * 0.08 - 0.04); // ±4 % so a rush of joins sparkles
    tone({ t: t, type: 'sine', freq: 420 * jitter, glideTo: 800 * jitter,
           glideTime: 0.07, attack: 0.004, decay: 0.16, vol: 0.32 });
    tone({ t: t + 0.01, type: 'triangle', freq: 840 * jitter, glideTo: 1600 * jitter,
           glideTime: 0.06, attack: 0.004, decay: 0.1, vol: 0.1 });
    noise({ t: t, type: 'bandpass', freq: 2400, q: 2.5,
            attack: 0.002, decay: 0.05, vol: 0.05 });
  });

  const blip = safe(function (n) {
    const t = ctx.currentTime;
    const map = { 3: midi(72), 2: midi(76), 1: midi(79) }; // C5 E5 G5
    if (n === 0) { // higher "go!"
      tone({ t: t, type: 'triangle', freq: midi(84), attack: 0.005, decay: 0.42, vol: 0.4 });
      tone({ t: t, type: 'sine', freq: midi(96), attack: 0.005, decay: 0.3, vol: 0.14 });
      tone({ t: t, type: 'sine', freq: midi(72), attack: 0.005, decay: 0.35, vol: 0.2 });
      noise({ t: t, type: 'highpass', freq: 5000, attack: 0.002, decay: 0.1, vol: 0.05 });
      return;
    }
    const f = map[n] || midi(72);
    tone({ t: t, type: 'triangle', freq: f, attack: 0.005, decay: 0.16, vol: 0.34 });
    tone({ t: t, type: 'sine', freq: f * 2, attack: 0.005, decay: 0.1, vol: 0.09 });
  });

  const tick = safe(function (urgent) {
    const t = ctx.currentTime;
    const f = urgent ? 2900 : 2100;
    const v = urgent ? 0.16 : 0.09;
    noise({ t: t, type: 'bandpass', freq: f, q: 6, attack: 0.001, decay: 0.028, vol: v });
    tone({ t: t, type: 'square', freq: urgent ? 1450 : 1050,
           attack: 0.001, decay: 0.022, vol: v * 0.4,
           filter: { type: 'bandpass', freq: f, q: 4 } });
  });

  const gong = safe(function () {
    const t = ctx.currentTime;
    // Slightly inharmonic partial stack = metallic strike
    const parts = [
      { f: 98.0, v: 0.5, d: 1.5 },
      { f: 147.2, v: 0.3, d: 1.3 },
      { f: 196.7, v: 0.22, d: 1.1 },
      { f: 293.1, v: 0.12, d: 0.9 },
      { f: 439.0, v: 0.07, d: 0.7 }
    ];
    for (let i = 0; i < parts.length; i++) {
      tone({ t: t, type: 'sine', freq: parts[i].f, attack: 0.008,
             decay: parts[i].d, vol: parts[i].v,
             vibrato: { freq: 3.2, cents: 6, delay: 0.4 } });
    }
    // mallet impact
    noise({ t: t, type: 'lowpass', freq: 420, q: 0.7,
            attack: 0.002, decay: 0.12, vol: 0.3 });
  });

  const correct = safe(function () {
    const t = ctx.currentTime;
    const notes = [midi(72), midi(76), midi(79)]; // C5 E5 G5
    for (let i = 0; i < notes.length; i++) {
      const nt = t + i * 0.07;
      tone({ t: nt, type: 'triangle', freq: notes[i], attack: 0.004, decay: 0.34, vol: 0.3 });
      tone({ t: nt, type: 'sine', freq: notes[i] * 2, attack: 0.004, decay: 0.22, vol: 0.08 });
    }
    // sparkle on top (C6)
    tone({ t: t + 0.21, type: 'sine', freq: midi(84), attack: 0.004, decay: 0.4, vol: 0.12 });
    noise({ t: t + 0.21, type: 'highpass', freq: 6000, attack: 0.003, decay: 0.18, vol: 0.04 });
  });

  const wrong = safe(function () {
    const t = ctx.currentTime;
    tone({ t: t, type: 'sine', freq: 160, glideTo: 90, glideTime: 0.26,
           attack: 0.004, decay: 0.4, vol: 0.45 });
    tone({ t: t, type: 'square', freq: 110, glideTo: 65, glideTime: 0.22,
           attack: 0.004, decay: 0.3, vol: 0.08,
           filter: { type: 'lowpass', freq: 320, q: 0.8 } });
    noise({ t: t, type: 'lowpass', freq: 260, q: 0.7,
            attack: 0.002, decay: 0.14, vol: 0.28 });
  });

  function drumroll(ms) {
    ms = (isFinite(ms) && ms > 0) ? Math.min(ms, 15000) : 1200;
    // Promise resolves after ms even when muted/locked → stable dramaturgy.
    return new Promise(function (resolve) {
      try {
        if (ready()) {
          const t0 = ctx.currentTime + 0.02;
          const dur = ms / 1000;
          const hits = Math.max(4, Math.floor(dur * 30)); // ~30 hits/s
          for (let i = 0; i < hits; i++) {
            const frac = i / hits;
            const ht = t0 + frac * dur + (Math.random() * 0.006 - 0.003);
            const v = (0.10 + 0.26 * frac) * (0.9 + Math.random() * 0.2); // crescendo
            noise({ t: ht, type: 'lowpass', freq: 640, q: 0.8,
                    attack: 0.002, decay: 0.045, vol: v });
            noise({ t: ht, type: 'bandpass', freq: 1750, q: 1.4,
                    attack: 0.001, decay: 0.03, vol: v * 0.35 });
          }
        }
      } catch (_) {}
      setTimeout(resolve, ms);
    });
  }

  const fanfare = safe(function () {
    const t = ctx.currentTime;
    // C - F - G - C(high), brass-ish saws through a lowpass, final chord held w/ vibrato
    const chords = [
      { at: 0.00, len: 0.24, notes: [60, 64, 67] },        // C4 E4 G4
      { at: 0.26, len: 0.24, notes: [60, 65, 69] },        // F: C4 F4 A4
      { at: 0.52, len: 0.24, notes: [62, 67, 71] },        // G: D4 G4 B4
      { at: 0.78, len: 0.72, notes: [60, 64, 67, 72], hold: true } // C + C5
    ];
    for (let c = 0; c < chords.length; c++) {
      const ch = chords[c];
      for (let i = 0; i < ch.notes.length; i++) {
        const f = midi(ch.notes[i]);
        const det = (i % 2 === 0) ? 4 : -4; // slight ensemble detune
        tone({ t: t + ch.at, type: 'sawtooth', freq: f, detune: det,
               attack: 0.015, decay: ch.len + (ch.hold ? 0.25 : 0.06),
               vol: 0.13,
               filter: { type: 'lowpass', freq: 2300, q: 0.7 },
               vibrato: ch.hold ? { freq: 5.5, cents: 14, delay: 0.2 } : undefined });
      }
      // low brass root an octave down
      tone({ t: t + ch.at, type: 'sawtooth', freq: midi(ch.notes[0] - 12),
             attack: 0.015, decay: ch.len + (ch.hold ? 0.3 : 0.06), vol: 0.12,
             filter: { type: 'lowpass', freq: 900, q: 0.7 } });
    }
    // timpani hit at start, cymbal shimmer on the final chord
    tone({ t: t, type: 'sine', freq: 130, glideTo: 85, glideTime: 0.2,
           attack: 0.003, decay: 0.4, vol: 0.35 });
    noise({ t: t + 0.78, type: 'highpass', freq: 6500,
            attack: 0.01, decay: 0.6, vol: 0.05 });
  });

  const whoosh = safe(function () {
    const t = ctx.currentTime;
    noise({ t: t, type: 'bandpass', freq: 320, sweepTo: 3600, q: 1.1,
            attack: 0.12, decay: 0.26, vol: 0.3, rate: 1.2 });
  });

  const streak = safe(function (level) {
    const t = ctx.currentTime;
    level = Math.max(3, Math.min(8, Math.floor(level) || 3));
    const count = Math.min(3 + (level - 3), 6);      // 3 notes @3 … 6 notes @6+
    const base = 76 + (level - 3) * 2;               // starts higher per level
    const penta = [0, 3, 5, 7, 10, 12];
    const step = Math.max(0.05, 0.075 - (level - 3) * 0.004); // faster per level
    for (let i = 0; i < count; i++) {
      const nt = t + i * step;
      tone({ t: nt, type: 'triangle', freq: midi(base + penta[i]),
             attack: 0.004, decay: 0.18, vol: 0.24 });
      tone({ t: nt, type: 'sine', freq: midi(base + penta[i] + 12),
             attack: 0.004, decay: 0.12, vol: 0.06 });
    }
    if (level >= 5) { // fire shimmer
      noise({ t: t + count * step, type: 'highpass', freq: 5200,
              attack: 0.01, decay: 0.3, vol: 0.06 });
      tone({ t: t + count * step, type: 'sine', freq: midi(base + 17),
             attack: 0.005, decay: 0.35, vol: 0.1,
             vibrato: { freq: 9, cents: 25, delay: 0.02 } });
    }
  });

  function bellStrike(t, f, vol, decay) {
    // golden-bell: fundamental + inharmonic partials, long decay
    tone({ t: t, type: 'sine', freq: f, attack: 0.003, decay: decay, vol: vol });
    tone({ t: t, type: 'sine', freq: f * 2.42, attack: 0.003, decay: decay * 0.6, vol: vol * 0.35 });
    tone({ t: t, type: 'sine', freq: f * 3.61, attack: 0.002, decay: decay * 0.35, vol: vol * 0.15 });
  }

  const dbl = safe(function () {
    const t = ctx.currentTime;
    bellStrike(t, midi(88), 0.3, 0.5);        // E6
    bellStrike(t + 0.19, midi(91), 0.34, 0.65); // G6
    noise({ t: t + 0.19, type: 'highpass', freq: 7000, attack: 0.005, decay: 0.25, vol: 0.04 });
  });

  const pop = safe(function () {
    const t = ctx.currentTime;
    tone({ t: t, type: 'sine', freq: 620, glideTo: 900, glideTime: 0.04,
           attack: 0.002, decay: 0.06, vol: 0.16 });
  });

  // ── Music: 64-step (4 bars × 16ths) lookahead sequencer ───────────
  // A-minor i-VI-III-VII: Am | F | C | G — catchy quiz-show loop.
  const BARS = [
    { root: 45, triad: [57, 60, 64] }, // Am  (A2; A3 C4 E4)
    { root: 41, triad: [53, 57, 60] }, // F   (F2; F3 A3 C4)
    { root: 48, triad: [60, 64, 67] }, // C   (C3; C4 E4 G4)
    { root: 43, triad: [55, 59, 62] }  // G   (G2; G3 B3 D4)
  ];
  // Bass degree offsets per 8th (index = step/2): root/fifth/octave movement
  const BASS_PATTERN = [0, 0, 7, 0, 12, 0, 7, 5];
  const ARP_PATTERN = [0, 1, 2, 3, 2, 1]; // indices into [root, triad…]

  const music = (function () {
    let timer = null;
    let step = 0;
    let nextTime = 0;
    let intensity = 0;
    let playing = false;

    function bpm() { return 112 + 20 * intensity; }
    function s16() { return 60 / bpm() / 4; }

    function kick(t, v) {
      tone({ t: t, type: 'sine', freq: 150, glideTo: 48, glideTime: 0.09,
             attack: 0.002, decay: 0.16, vol: v, dest: musicFade });
    }
    function hat(t, v, open) {
      noise({ t: t, type: 'highpass', freq: 7500, q: 0.7,
              attack: 0.001, decay: open ? 0.14 : 0.035, vol: v, dest: musicFade });
    }
    function snare(t, v) {
      noise({ t: t, type: 'bandpass', freq: 1900, q: 0.9,
              attack: 0.001, decay: 0.09, vol: v, dest: musicFade });
      tone({ t: t, type: 'triangle', freq: 190, attack: 0.001, decay: 0.07,
             vol: v * 0.5, dest: musicFade });
    }
    function bass(t, m, v, len) {
      tone({ t: t, type: 'sawtooth', freq: midi(m), attack: 0.004, decay: len,
             vol: v, dest: musicFade,
             filter: { type: 'lowpass', freq: 520, q: 1.1 } });
      tone({ t: t, type: 'sine', freq: midi(m), attack: 0.004, decay: len,
             vol: v * 0.6, dest: musicFade });
    }
    function stab(t, triad, v, len) {
      for (let i = 0; i < triad.length; i++) {
        tone({ t: t, type: 'sawtooth', freq: midi(triad[i]),
               detune: i % 2 ? -5 : 5, attack: 0.004, decay: len, vol: v,
               dest: musicFade,
               filter: { type: 'lowpass', freq: 1800, q: 0.8 } });
      }
    }
    function arpNote(t, m, v) {
      tone({ t: t, type: 'triangle', freq: midi(m), attack: 0.002, decay: 0.09,
             vol: v, dest: musicFade,
             filter: { type: 'lowpass', freq: 4200, q: 0.7 } });
    }
    function riser(t, barLen) {
      noise({ t: t, type: 'bandpass', freq: 500, sweepTo: 5200, q: 2.2,
              attack: barLen * 0.7, decay: barLen * 0.3, vol: 0.07,
              dest: musicFade });
    }

    function scheduleStep(st, t) {
      const bar = BARS[Math.floor(st / 16) % 4];
      const pos = st % 16;         // 16th within the bar
      const beat = pos % 4 === 0;  // quarter beats
      const dur16 = s16();

      // Bass on 8ths — always present (the backbone)
      if (pos % 2 === 0) {
        const deg = BASS_PATTERN[(pos / 2) | 0];
        const accent = pos === 0 ? 1.15 : 1;
        bass(t, bar.root + deg, 0.2 * accent, dur16 * 1.7);
      }
      // Chord stabs on the off-beats of beats 2 & 4
      if (intensity >= 0.2 && (pos === 6 || pos === 14)) {
        stab(t, bar.triad, 0.075, dur16 * 1.4);
      }
      // Hi-hats: 8ths from 0.35, 16ths from 0.6; offbeat accents; open hat @ ≥0.5
      if (intensity >= 0.35) {
        const sixteenths = intensity >= 0.6;
        if (pos % 2 === 0 || sixteenths) {
          const off = pos % 4 === 2;
          hat(t, (off ? 0.075 : 0.045) * (0.7 + 0.6 * intensity), false);
        }
        if (intensity >= 0.5 && pos === 14) hat(t, 0.06, true);
      }
      // Kick on quarters from 0.5
      if (intensity >= 0.5 && beat) kick(t, 0.3);
      // Snare backbeat from 0.55
      if (intensity >= 0.55 && (pos === 4 || pos === 12)) snare(t, 0.12);
      // Arp 16ths from 0.7, jumps an octave in panic mode
      if (intensity >= 0.7) {
        const oct = intensity >= 0.85 ? 24 : 12;
        const seq = ARP_PATTERN[st % ARP_PATTERN.length];
        const note = seq === 0 ? bar.root + 12 + oct : bar.triad[(seq - 1) % 3] + oct;
        arpNote(t, note, 0.055 + 0.03 * intensity);
      }
      // Per-bar riser in panic mode
      if (intensity >= 0.85 && pos === 0) riser(t, dur16 * 16);
    }

    function pump() {
      try {
        if (!ctx || ctx.state !== 'running') return; // keep timer; resume catches up
        if (nextTime < ctx.currentTime - 0.25) nextTime = ctx.currentTime + 0.05;
        while (nextTime < ctx.currentTime + 0.12) {  // schedule-ahead window
          scheduleStep(step, nextTime);
          nextTime += s16();                          // tempo follows intensity smoothly
          step = (step + 1) % 64;
        }
      } catch (_) {}
    }

    function start() {
      if (!ready() || playing) return;
      try {
        playing = true;
        step = 0;
        nextTime = ctx.currentTime + 0.06;
        const g = musicFade.gain;
        g.cancelScheduledValues(ctx.currentTime);
        g.setValueAtTime(Math.max(EPS, g.value), ctx.currentTime);
        g.linearRampToValueAtTime(1, ctx.currentTime + 0.25);
        pump();
        timer = setInterval(pump, 25);
      } catch (_) { playing = false; }
    }

    function stop() {
      if (!playing) return;
      playing = false;
      if (timer) { clearInterval(timer); timer = null; }
      try {
        if (ctx && musicFade) {
          const g = musicFade.gain;
          g.cancelScheduledValues(ctx.currentTime);
          g.setValueAtTime(Math.max(EPS, g.value), ctx.currentTime);
          g.linearRampToValueAtTime(0, ctx.currentTime + 0.12); // click-free out
        }
      } catch (_) {}
    }

    return {
      start: function () { try { start(); } catch (_) {} },
      stop: function () { try { stop(); } catch (_) {} },
      setIntensity: function (v) { intensity = clamp01(v); },
      setVolume: function (v) {
        musicVol = clamp01(v);
        try { if (musicBus) musicBus.gain.setTargetAtTime(musicVol, ctx.currentTime, 0.03); } catch (_) {}
      },
      get volume() { return musicVol; },
      get playing() { return playing; }
    };
  })();

  // ── Public API ────────────────────────────────────────────────────
  window.SFX = {
    unlock: unlock,
    get supported() { return SUPPORTED; },
    get enabled() { return enabled; },
    setEnabled: function (on) {
      enabled = !!on;
      if (!enabled) { try { music.stop(); } catch (_) {} }
    },
    setVolume: function (v) {
      sfxVol = clamp01(v);
      try { if (sfxBus) sfxBus.gain.setTargetAtTime(sfxVol, ctx.currentTime, 0.03); } catch (_) {}
    },
    get volume() { return sfxVol; },

    join: join,
    blip: blip,
    tick: tick,
    gong: gong,
    correct: correct,
    wrong: wrong,
    drumroll: drumroll,
    fanfare: fanfare,
    whoosh: whoosh,
    streak: streak,
    double: dbl,
    pop: pop,

    music: music
  };
})();
