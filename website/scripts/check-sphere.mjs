import { readFileSync } from 'node:fs';
import { runInNewContext } from 'node:vm';
import test from 'node:test';
import assert from 'node:assert/strict';

const source = readFileSync(new URL('../assets/sphere.js', import.meta.url), 'utf8').replace('export function mountSphere', 'function mountSphere');
function fixture({ size = 640, reduced = false, canvasAvailable = true } = {}) {
  let next = 0, draws = 0;
  const frames = new Map(), listeners = {}, ids = ['codex', 'cursor', 'antigravity', 'opencode', 'omp', 'claude-code'];
  const hosts = ids.map((id, i) => ({ id, offsetWidth: [94, 94, 126, 116, 80, 132][i], offsetHeight: 36, style: {}, dataset: {} }));
  const gradient = { addColorStop() {} };
  const ctx = new Proxy({}, { get(_, key) {
    if (key === 'clearRect') return () => { draws++; };
    if (key.startsWith('create')) return () => gradient;
    return (...args) => args.forEach(value => { if (typeof value === 'number') assert(Number.isFinite(value), `Nonfinite ${key} argument`); });
  }, set() { return true; } });
  const canvas = { style: {}, setAttribute() {}, getContext: () => canvasAvailable ? ctx : null };
  const region = { classList: { add() {} }, prepend() { this.mounted = true; },
    getBoundingClientRect: () => ({ width: size, height: size, left: 0, top: 0 }),
    querySelector: selector => hosts.find(host => selector === `.host-${host.id}`),
    addEventListener(name, handler) { listeners[name] = handler; } };
  const media = { matches: reduced, addEventListener(_, handler) { listeners.motion = handler; } };
  const document = { hidden: false, createElement: () => canvas, addEventListener(_, handler) { listeners.visibility = handler; } };
  const context = { window: { matchMedia: () => media, devicePixelRatio: 2 }, document,
    ResizeObserver: class { constructor(handler) { listeners.resize = handler; } observe() {} },
    IntersectionObserver: class { constructor(handler) { listeners.intersection = handler; } observe() {} },
    requestAnimationFrame: handler => { frames.set(++next, handler); return next; }, cancelAnimationFrame: id => frames.delete(id) };
  context.window.IntersectionObserver = context.IntersectionObserver;
  runInNewContext(`${source}\nmountSphere(region);`, { ...context, region });
  return { frames, listeners, media, document, region, hosts, get draws() { return draws; },
    step(time) { const callbacks = [...frames.values()]; frames.clear(); callbacks.forEach(callback => callback(time)); } };
}

test('six labels remain at stable positions throughout a complete relay cycle', () => {
  const f = fixture(), seen = new Set();
  const positions = f.hosts.map(h => ({ ...h.style }));
  for (let time = 1; time < 14000; time += 32) {
    f.step(time);
    f.hosts.filter(h => h.dataset.active === 'true').forEach(h => seen.add(h.id));
  }
  assert.equal(seen.size, 6);
  f.hosts.forEach((host, i) => assert.deepEqual(host.style, positions[i]));
});

test('label placement clamps to the frame with measured label widths', () => {
  for (const size of [284, 342, 440, 640]) {
    const f = fixture({ size });
    for (const host of f.hosts) {
      const x = parseFloat(host.style.left), y = parseFloat(host.style.top);
      assert(x - host.offsetWidth / 2 >= 5.9);
      assert(x + host.offsetWidth / 2 <= size - 5.9);
      assert(y > host.offsetHeight / 2 && y < size - host.offsetHeight / 2);
    }
  }
});

test('reduced motion renders without starting a frame loop, including live changes', () => {
  const f = fixture({ reduced: true });
  assert.equal(f.frames.size, 0);
  assert.equal(f.draws, 1);
  assert(f.hosts.every(h => h.dataset.active === 'false'));
  f.media.matches = false; f.listeners.motion(); assert.equal(f.frames.size, 1);
  f.media.matches = true; f.listeners.motion(); assert.equal(f.frames.size, 0);
});

test('hidden and offscreen diagrams pause and resume without duplicate loops', () => {
  const f = fixture();
  f.document.hidden = true; f.listeners.visibility(); assert.equal(f.frames.size, 0);
  f.document.hidden = false; f.listeners.visibility(); f.listeners.visibility(); assert.equal(f.frames.size, 1);
  f.listeners.intersection([{ isIntersecting: false }]); assert.equal(f.frames.size, 0);
  f.listeners.intersection([{ isIntersecting: true }]); assert.equal(f.frames.size, 1);
});

test('unavailable Canvas leaves the static fallback intact', () => {
  const f = fixture({ canvasAvailable: false });
  assert.equal(f.region.mounted, undefined);
  assert.equal(f.frames.size, 0);
});
