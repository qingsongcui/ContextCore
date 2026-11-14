// A quiet rotating point cloud. Six stationary labels remain readable at every depth.
// The static SVG remains the no-JS / no-Canvas fallback.
const TAU = Math.PI * 2;
const HOSTS = [['codex', .23, .25], ['cursor', .77, .25], ['antigravity', .83, .5], ['opencode', .77, .75], ['omp', .23, .75], ['claude-code', .17, .5]];
function fibonacciSphere(count) {
  const golden = Math.PI * (3 - Math.sqrt(5));
  return Array.from({ length: count }, (_, i) => {
    const y = 1 - i / (count - 1) * 2, r = Math.sqrt(1 - y * y);
    return [Math.cos(golden * i) * r, y, Math.sin(golden * i) * r];
  });
}
function rotate([x, y, z], yaw, pitch) {
  const cy = Math.cos(yaw), sy = Math.sin(yaw), cp = Math.cos(pitch), sp = Math.sin(pitch);
  const rx = x * cy - z * sy, rz = x * sy + z * cy;
  return [rx, y * cp - rz * sp, y * sp + rz * cp];
}
export function mountSphere(region) {
  const canvas = document.createElement('canvas');
  canvas.className = 'sphere-canvas';
  canvas.setAttribute('aria-hidden', 'true');
  const ctx = canvas.getContext('2d');
  if (!ctx) {
    region.classList.add('no-sphere');
    return;
  }
  region.prepend(canvas);
  region.classList.add('has-sphere');
  const lattice = fibonacciSphere(760);
  const hosts = HOSTS.map(([id, x, y]) => ({ el: region.querySelector(`.host-${id}`), x, y })).filter(host => host.el);
  const motion = window.matchMedia('(prefers-reduced-motion: reduce)');
  let w = 0, h = 0, dpr = 1, radius = 0, cx = 0, cy = 0;
  let frame = 0, last = 0, clock = 0, visible = true;
  let pointerX = 0, pointerY = 0, targetX = 0, targetY = 0;
  let anchors = [];
  function layout() {
    const box = region.getBoundingClientRect();
    w = box.width; h = box.height;
    if (!w || !h) return;
    dpr = Math.min(window.devicePixelRatio || 1, 2);
    canvas.width = Math.round(w * dpr); canvas.height = Math.round(h * dpr);
    canvas.style.width = `${w}px`; canvas.style.height = `${h}px`;
    radius = Math.min(w, h) * .286; cx = w / 2; cy = h / 2;
    anchors = hosts.map(host => {
      const half = host.el.offsetWidth / 2;
      const x = Math.max(half + 6, Math.min(w - half - 6, w * host.x)), y = h * host.y;
      host.el.style.left = `${x}px`; host.el.style.top = `${y}px`;
      return { x, y, half, height: host.el.offsetHeight / 2 };
    });
    draw();
  }
  function point(p, yaw, pitch) {
    const [x, y, z] = rotate(p, yaw, pitch), scale = 5 / (5 - z);
    return [cx + x * radius * scale, cy - y * radius * scale, z];
  }
  function connection(anchor, progress, lit) {
    const dx = anchor.x - cx, dy = anchor.y - cy, length = Math.hypot(dx, dy);
    const ux = dx / length, uy = dy / length;
    const edge = Math.min(anchor.half / Math.max(.001, Math.abs(ux)), anchor.height / Math.max(.001, Math.abs(uy))) + 7;
    const start = { x: anchor.x - ux * edge, y: anchor.y - uy * edge };
    const end = { x: cx + ux * radius * .34, y: cy + uy * radius * .34 };
    ctx.beginPath(); ctx.moveTo(start.x, start.y); ctx.lineTo(end.x, end.y);
    ctx.strokeStyle = lit ? 'rgba(184, 205, 157, .22)' : 'rgba(184, 195, 172, .065)';
    ctx.lineWidth = .65; ctx.stroke();
    if (!lit || motion.matches) return;
    const at = t => [start.x + (end.x - start.x) * t, start.y + (end.y - start.y) * t];
    const [sx, sy] = at(Math.max(0, progress - .15)), [ex, ey] = at(progress);
    const ink = ctx.createLinearGradient(sx, sy, ex, ey);
    ink.addColorStop(0, 'rgba(198, 219, 171, 0)'); ink.addColorStop(1, 'rgba(212, 233, 185, .85)');
    ctx.strokeStyle = ink; ctx.lineWidth = 1.35;
    ctx.beginPath(); ctx.moveTo(sx, sy); ctx.lineTo(ex, ey); ctx.stroke();
    ctx.fillStyle = '#d2e5b9'; ctx.beginPath(); ctx.arc(ex, ey, 1.6, 0, TAU); ctx.fill();
  }
  function draw() {
    if (!w || !anchors.length) return;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0); ctx.clearRect(0, 0, w, h);
    const yaw = .35 + clock * .000045 + pointerX, pitch = -.25 + pointerY;
    const atmosphere = ctx.createRadialGradient(cx - radius * .28, cy - radius * .32, 0, cx, cy, radius * 1.2);
    atmosphere.addColorStop(0, 'rgba(147, 169, 120, .07)'); atmosphere.addColorStop(.7, 'rgba(135, 157, 111, .025)'); atmosphere.addColorStop(1, 'rgba(135, 157, 111, 0)');
    ctx.fillStyle = atmosphere; ctx.fillRect(0, 0, w, h);
    // One fine silhouette and one equator replace the intersecting orbital rings.
    const rim = ctx.createLinearGradient(cx - radius, cy - radius, cx + radius, cy + radius);
    rim.addColorStop(0, 'rgba(197, 214, 177, .30)'); rim.addColorStop(.5, 'rgba(151, 172, 128, .07)'); rim.addColorStop(1, 'rgba(197, 214, 177, .16)');
    ctx.strokeStyle = rim; ctx.lineWidth = .75; ctx.beginPath(); ctx.arc(cx, cy, radius * 1.02, 0, TAU); ctx.stroke();
    ctx.strokeStyle = 'rgba(180, 199, 158, .09)'; ctx.beginPath(); ctx.ellipse(cx, cy, radius * 1.09, radius * .32, -.34, 0, TAU); ctx.stroke();
    const projected = lattice.map(p => point(p, yaw, pitch)).sort((a, b) => a[2] - b[2]);
    for (const [x, y, z] of projected) {
      const depth = (z + 1) / 2;
      const clear = Math.min(1, Math.max(0, (Math.hypot(x - cx, y - cy) / radius - .26) / .35));
      ctx.fillStyle = `rgba(190, 206, 170, ${(.035 + depth * depth * .43) * clear})`;
      ctx.beginPath(); ctx.arc(x, y, .4 + depth * .48, 0, TAU); ctx.fill();
    }
    const beat = clock / 2200, active = Math.floor(beat) % hosts.length;
    anchors.forEach((anchor, i) => {
      const lit = !motion.matches && i === active;
      connection(anchor, beat % 1, lit);
      const value = lit ? 'true' : 'false';
      if (hosts[i].el.dataset.active !== value) hosts[i].el.dataset.active = value;
    });
  }
  function tick(now) {
    frame = 0;
    const dt = Math.min(64, now - (last || now)); last = now; clock += dt;
    pointerX += (targetX - pointerX) * .045; pointerY += (targetY - pointerY) * .045;
    draw(); schedule();
  }
  function schedule() { if (!frame && visible && !document.hidden && !motion.matches) frame = requestAnimationFrame(tick); }
  function stop() { if (frame) cancelAnimationFrame(frame); frame = 0; last = 0; }
  region.addEventListener('pointermove', event => {
    const box = region.getBoundingClientRect();
    targetX = ((event.clientX - box.left) / box.width - .5) * .18;
    targetY = ((event.clientY - box.top) / box.height - .5) * -.12;
  }, { passive: true });
  region.addEventListener('pointerleave', () => { targetX = 0; targetY = 0; });
  const resize = new ResizeObserver(layout); resize.observe(region); hosts.forEach(host => resize.observe(host.el));
  if ('IntersectionObserver' in window) new IntersectionObserver(([entry]) => { visible = entry.isIntersecting; visible ? schedule() : stop(); }).observe(region);
  document.addEventListener('visibilitychange', () => (document.hidden ? stop() : schedule()));
  motion.addEventListener('change', () => { stop(); draw(); schedule(); });
  document.fonts?.ready.then(layout);
  layout(); schedule();
}
