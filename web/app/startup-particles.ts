/* Original startup renderer. Graphite/ice-blue tokens match the live desk;
   this bounded Canvas 2D pool owns no timers, events, networking or storage. */

type Point = { x: number; y: number };
type Particle = { angle: number; radius: number; phase: number; size: number; light: number };

export type StartupTextLayout = {
  x: number; y: number; fontSize: number; fontFamily: string; fontWeight: string; tracking: number;
};
export type StartupParticles = {
  resize: (width: number, height: number, text: StartupTextLayout) => void;
  draw: (seconds: number) => void;
  destroy: () => void;
};

const TAU = Math.PI * 2;
const clamp = (n: number, low: number, high: number) => Math.max(low, Math.min(high, n));
const ease = (from: number, to: number, n: number) => {
  const p = clamp((n - from) / (to - from), 0, 1);
  return p * p * (3 - 2 * p);
};

function randomSource(seed: number) {
  let state = seed >>> 0;
  return () => {
    state += 0x6d2b79f5;
    let value = state;
    value = Math.imul(value ^ (value >>> 15), value | 1);
    value ^= value + Math.imul(value ^ (value >>> 7), value | 61);
    return ((value ^ (value >>> 14)) >>> 0) / 4294967296;
  };
}

export function createStartupParticles(canvas: HTMLCanvasElement, title: string): StartupParticles {
  const ctx = canvas.getContext("2d", { alpha: false });
  if (!ctx) throw new Error("Canvas unavailable");
  const random = randomSource(20261111);
  let particles: Particle[] = Array.from({ length: 1000 }, () => ({
    angle: random() * TAU, radius: Math.sqrt(random()), phase: random() * TAU,
    size: .65 + random() * 1.05, light: .4 + random() * .6,
  }));
  let targets: Point[] = [];
  let width = 1, height = 1, scale = 1, count = 1000;
  let text: StartupTextLayout = { x: 0, y: 0, fontSize: 60, fontFamily: "sans-serif", fontWeight: "650", tracking: 0 };
  let destroyed = false;

  function sampleTitle() {
    const raster = document.createElement("canvas");
    // The headline is capped at 72px. This offscreen sampling surface stays
    // below 0.2 megapixels and is released as soon as target points are made.
    raster.width = Math.ceil(Math.min(1600, (text.fontSize + text.tracking) * title.length + 30));
    raster.height = Math.ceil(text.fontSize * 1.7);
    const ink = raster.getContext("2d", { willReadFrequently: true });
    if (!ink) throw new Error("Text raster unavailable");
    ink.font = `${text.fontWeight} ${text.fontSize}px ${text.fontFamily}`;
    ink.textBaseline = "middle";
    ink.fillStyle = "#fff";
    const characters = Array.from(title);
    const lengths = characters.map(character => ink.measureText(character).width);
    const total = lengths.reduce((a, b) => a + b, 0) + text.tracking * (characters.length - 1);
    let cursor = (raster.width - total) / 2;
    for (let i = 0; i < characters.length; i += 1) {
      ink.fillText(characters[i], cursor, raster.height / 2);
      cursor += lengths[i] + text.tracking;
    }
    const pixels = ink.getImageData(0, 0, raster.width, raster.height).data;
    const samples: Point[] = [];
    const step = text.fontSize < 38 ? 2 : 3;
    for (let y = 0; y < raster.height; y += step) {
      for (let x = 0; x < raster.width; x += step) {
        if (pixels[(y * raster.width + x) * 4 + 3] > 100) samples.push({ x: x - raster.width / 2, y: y - raster.height / 2 });
      }
    }
    if (!samples.length) throw new Error("No text pixels");
    const shuffle = randomSource(3284);
    for (let i = samples.length - 1; i > 0; i -= 1) {
      const other = Math.floor(shuffle() * (i + 1));
      [samples[i], samples[other]] = [samples[other], samples[i]];
    }
    targets = Array.from({ length: count }, (_, i) => samples[Math.floor(i * samples.length / count)]);
    raster.width = raster.height = 1;
  }

  function resize(nextWidth: number, nextHeight: number, nextText: StartupTextLayout) {
    if (destroyed) return;
    width = Math.max(1, nextWidth);
    height = Math.max(1, nextHeight);
    scale = Math.min(2, window.devicePixelRatio || 1, Math.sqrt(2_400_000 / (width * height)));
    count = width < 720 ? 420 : 1000;
    canvas.width = Math.max(1, Math.floor(width * scale));
    canvas.height = Math.max(1, Math.floor(height * scale));
    text = nextText;
    sampleTitle();
  }

  function draw(seconds: number) {
    if (destroyed) return;
    const t = clamp(seconds, 0, 5);
    const settled = ease(.2, 2.65, t);
    const out = 1 - ease(4.48, 5, t);
    const particleFade = 1 - ease(3.3, 4.15, t) * .72;
    ctx!.setTransform(scale, 0, 0, scale, 0, 0);
    ctx!.globalCompositeOperation = "source-over";
    ctx!.globalAlpha = 1;
    ctx!.fillStyle = "#121418";
    ctx!.fillRect(0, 0, width, height);
    if (out <= 0) return;
    for (let i = 0; i < count; i += 1) {
      const particle = particles[i];
      const target = targets[i];
      if (!target) continue;
      const angle = particle.angle + t * .11;
      const sx = Math.cos(angle) * particle.radius * width * .5;
      const sy = Math.sin(angle) * particle.radius * height * .4;
      const swirl = (1 - settled) * settled * 4;
      const x = text.x + sx * (1 - settled) + target.x * settled + Math.sin(angle) * swirl * Math.min(width, height) * .12;
      const y = text.y + sy * (1 - settled) + target.y * settled - Math.cos(angle) * swirl * Math.min(width, height) * .08
        + Math.sin(t * 1.2 + particle.phase) * (1 - settled) * 2;
      if (x < -10 || y < -10 || x > width + 10 || y > height + 10) continue;
      const opacity = (.25 + particle.light * .55) * out * particleFade;
      const radius = particle.size * (.72 + settled * .2);
      ctx!.fillStyle = i % 9 === 0 ? "#afc1db" : "#cddbf0";
      if (i % 23 === 0) {
        ctx!.globalAlpha = opacity * .07;
        ctx!.beginPath();
        ctx!.arc(x, y, radius * 4.8, 0, TAU);
        ctx!.fill();
      }
      ctx!.globalAlpha = opacity;
      ctx!.beginPath();
      ctx!.arc(x, y, radius, 0, TAU);
      ctx!.fill();
    }
    ctx!.globalAlpha = 1;
  }

  function destroy() {
    if (destroyed) return;
    destroyed = true;
    particles = [];
    targets = [];
    canvas.width = canvas.height = 1;
  }
  return { resize, draw, destroy };
}
