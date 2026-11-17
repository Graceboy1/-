"use strict";

// Exercise the actual TypeScript renderer without adding a browser-test or
// canvas dependency. The raster mock supplies deterministic glyph ink; these
// tests check renderer limits/lifecycle, not visual typography or React effects.
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const ts = require("typescript");

const source = fs.readFileSync(path.join(__dirname, "../app/startup-particles.ts"), "utf8");
const compiled = ts.transpileModule(source, {
  compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS, strict: true },
  fileName: "startup-particles.ts",
});

function harness({ dpr = 1, mainContext = true, rasterContext = true, textInk = true } = {}) {
  const calls = [];
  const rasters = [];
  const context = {
    globalAlpha: 1,
    fillStyle: "",
    globalCompositeOperation: "source-over",
    setTransform(...args) { calls.push(["transform", ...args]); },
    fillRect(...args) { calls.push(["rect", ...args, this.fillStyle, this.globalAlpha]); },
    beginPath() { calls.push(["path"]); },
    arc(...args) { calls.push(["arc", ...args, this.fillStyle, this.globalAlpha]); },
    fill() { calls.push(["fill", this.fillStyle, this.globalAlpha]); },
  };
  const canvas = {
    width: 0,
    height: 0,
    getContext(type) { assert.equal(type, "2d"); return mainContext ? context : null; },
  };
  const exports = {};
  const sandbox = {
    exports,
    module: { exports },
    window: { devicePixelRatio: dpr },
    document: {
      createElement(tag) {
        assert.equal(tag, "canvas");
        const raster = {
          width: 0,
          height: 0,
          getContext(type) {
            assert.equal(type, "2d");
            if (!rasterContext) return null;
            return {
              measureText(character) { return { width: character.length * 35 }; },
              fillText() {},
              getImageData(_x, _y, width, height) {
                const data = new Uint8ClampedArray(width * height * 4);
                if (textInk) {
                  for (let offset = 3; offset < data.length; offset += 4) data[offset] = 255;
                }
                return { data };
              },
            };
          },
        };
        rasters.push(raster);
        return raster;
      },
    },
  };
  // No RAF, timer, fetch, event or storage API is supplied. The renderer must
  // remain synchronous and let its React owner manage those resources.
  vm.runInNewContext(compiled.outputText, sandbox, { filename: "startup-particles.js" });
  return {
    canvas, context, calls, rasters,
    create: () => sandbox.module.exports.createStartupParticles(canvas, "智慧交易助手"),
  };
}

function textLayout(width, height) {
  return {
    x: width / 2,
    y: height / 2,
    fontSize: width < 720 ? 32 : 64,
    fontFamily: "test-font",
    fontWeight: "650",
    tracking: 1,
  };
}

function coreArcs(calls) {
  // The renderer draws one small core per particle, plus occasional larger
  // bloom circles. At the settled phase every core radius is below 2px and
  // every bloom radius exceeds 2px; do not count glows as extra particles.
  return calls.filter(call => call[0] === "arc" && call[3] < 2);
}

test("desktop and mobile use bounded particle pools", () => {
  for (const [width, height, expected] of [[1280, 720, 1000], [390, 844, 420]]) {
    const h = harness({ dpr: 2 });
    const renderer = h.create();
    renderer.resize(width, height, textLayout(width, height));
    renderer.draw(2.8);
    const cores = coreArcs(h.calls);
    assert.equal(cores.length, expected);
    assert.ok(cores.length <= (width < 720 ? 420 : 1000));
    assert.ok(cores.every(call => call.slice(1, 6).every(Number.isFinite)));
    renderer.destroy();
  }
});

test("resize changes the active pool without growing it across repeated resizes", () => {
  const h = harness();
  const renderer = h.create();
  for (const [width, height, expected] of [[1280, 720, 1000], [390, 844, 420], [1280, 720, 1000], [390, 844, 420]]) {
    renderer.resize(width, height, textLayout(width, height));
    h.calls.length = 0;
    renderer.draw(2.8);
    assert.equal(coreArcs(h.calls).length, expected);
    assert.ok(h.rasters.every(raster => raster.width === 1 && raster.height === 1));
  }
  renderer.destroy();
});

test("surface pixels are capped at 2.4 megapixels even on high-DPR 4K and 8K screens", () => {
  for (const [width, height, dpr] of [[390, 844, 3], [1920, 1080, 3], [3840, 2160, 4], [7680, 4320, 2]]) {
    const h = harness({ dpr });
    const renderer = h.create();
    renderer.resize(width, height, textLayout(width, height));
    assert.ok(h.canvas.width >= 1 && h.canvas.height >= 1);
    assert.ok(h.canvas.width * h.canvas.height <= 2_400_000);
    assert.ok(h.canvas.width <= width * 2 && h.canvas.height <= height * 2);
    renderer.destroy();
  }
});

test("drawing the same timeline frame is deterministic and does not accumulate trails", () => {
  const h = harness({ dpr: 1.5 });
  const renderer = h.create();
  renderer.resize(1280, 720, textLayout(1280, 720));
  renderer.draw(1.8);
  const first = JSON.stringify(h.calls);
  h.calls.length = 0;
  renderer.draw(1.8);
  assert.equal(JSON.stringify(h.calls), first);
  assert.equal(h.calls[0][0], "transform");
  assert.deepEqual(h.calls[1].slice(0, 6), ["rect", 0, 0, 1280, 720, "#121418"]);
  renderer.destroy();
});

test("a fresh renderer reproduces the same seeded frame", () => {
  const snapshots = [];
  for (let index = 0; index < 2; index += 1) {
    const h = harness();
    const renderer = h.create();
    renderer.resize(1280, 720, textLayout(1280, 720));
    renderer.draw(2.2);
    snapshots.push(JSON.stringify(h.calls));
    renderer.destroy();
  }
  assert.equal(snapshots[0], snapshots[1]);
});

test("finished frames render no particles", () => {
  const h = harness();
  const renderer = h.create();
  renderer.resize(1280, 720, textLayout(1280, 720));
  renderer.draw(5);
  assert.equal(coreArcs(h.calls).length, 0);
  assert.equal(h.context.globalAlpha, 1);
  renderer.destroy();
});

test("destroy frees the surface and makes later draws and resizes no-ops", () => {
  const h = harness();
  const renderer = h.create();
  renderer.resize(1280, 720, textLayout(1280, 720));
  renderer.draw(2.5);
  renderer.destroy();
  assert.equal(h.canvas.width, 1);
  assert.equal(h.canvas.height, 1);
  const callsBefore = h.calls.length;
  const rastersBefore = h.rasters.length;
  renderer.draw(3);
  renderer.resize(3840, 2160, textLayout(3840, 2160));
  renderer.destroy();
  assert.equal(h.calls.length, callsBefore);
  assert.equal(h.rasters.length, rastersBefore);
  assert.equal(h.canvas.width, 1);
  assert.equal(h.canvas.height, 1);
});

test("unavailable main canvas context reports a recoverable renderer error", () => {
  const h = harness({ mainContext: false });
  assert.throws(h.create, /Canvas unavailable/);
});

test("unavailable text sampling context and empty glyphs report errors instead of hanging", () => {
  for (const options of [{ rasterContext: false }, { textInk: false }]) {
    const h = harness(options);
    const renderer = h.create();
    assert.throws(() => renderer.resize(1280, 720, textLayout(1280, 720)), /Text raster unavailable|No text pixels/);
    renderer.destroy();
    assert.equal(h.canvas.width, 1);
    assert.equal(h.canvas.height, 1);
  }
});
