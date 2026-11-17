"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { spawnSync } = require("node:child_process");
const vm = require("node:vm");
const ts = require("typescript");
const { STATIC_ASSETS, OUTPUT_ROOT, sourcePath, validateBundle, prepareNews } = require("../tools/prepare-news.cjs");
const PROJECT_ROOT = path.resolve(__dirname, "../..");

// Generated fixtures are safe public output, never an account/model API dump.
prepareNews();
const readAsset = name => fs.readFileSync(path.join(OUTPUT_ROOT, name), "utf8");
const payload = JSON.parse(readAsset("news-data.json"));
const bundle = () => ({ schema_version: 1, assets: [...STATIC_ASSETS], payload: structuredClone(payload) });

test("news AI POST has an explicit same-origin production proxy", async () => {
  const filename = path.join(PROJECT_ROOT, "web/next.config.ts");
  const compiled = ts.transpileModule(fs.readFileSync(filename, "utf8"), {
    compilerOptions: {module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022},
  }).outputText;
  const exports = {};
  vm.runInNewContext(compiled, {exports, process: {env: {}}}, {filename});
  const routes = await exports.default.rewrites();
  for (const name of ["feed", "status", "refresh", "analyze"]) {
    const route = routes.find(row => row.source === `/contest-api/news/${name}`);
    assert.equal(route?.destination, `http://127.0.0.1:8001/news/${name}`);
  }
  for (const name of ["dashboard", "start", "stop", "performance", "playback"]) {
    const route = routes.find(row => row.source === `/contest-api/paper/${name}`);
    assert.equal(route?.destination, `http://127.0.0.1:8001/paper/${name}`);
  }
  assert.equal(routes.some(route => route.source.includes(":path*")), false);
});

function tree(directory, prefix = "") {
  return fs.readdirSync(directory, { withFileTypes: true }).flatMap(item => {
    const name = `${prefix}${item.name}`;
    assert.equal(item.isSymbolicLink(), false, `Generated asset cannot be a link: ${name}`);
    return item.isDirectory() ? tree(path.join(directory, item.name), `${name}/`) : [name];
  });
}

test("formal news output is exactly the public allowlist and current source bytes", () => {
  assert.deepEqual(tree(OUTPUT_ROOT).sort(), [...STATIC_ASSETS, "news-data.json"].sort());
  for (const name of STATIC_ASSETS) {
    assert.deepEqual(fs.readFileSync(path.join(OUTPUT_ROOT, name)), fs.readFileSync(sourcePath(name)), name);
  }
  assert.throws(() => sourcePath("../.env"));
  assert.throws(() => sourcePath("serve_earth.py"));
});

test("nested iframe dependencies are relative and packaged locally", () => {
  const html = readAsset("index.html");
  const scripts = [...html.matchAll(/<script\b[^>]*src="([^"]+)"/g)].map(match => match[1]);
  const styles = [...html.matchAll(/<link\b[^>]*rel="stylesheet"[^>]*href="([^"]+)"/g)].map(match => match[1]);
  for (const resource of [...scripts, ...styles]) {
    assert.doesNotMatch(resource, /^(?:[a-z]+:|\/|\.\.)/i);
    assert.ok(STATIC_ASSETS.includes(resource), `${resource} must be bundled`);
  }
  assert.equal(scripts.length, 7);
  assert.equal(styles.length, 1);
  assert.match(readAsset("earth-app.js"), /fetchJSON\(liveMode\?'\/contest-api\/news\/feed\?limit=40&offset=0&q=':'news-data\.json'/);
  assert.match(readAsset("earth-app.js"), /fetchJSON\('vendor\/countries-110m\.json',/);
  for (const name of ["index.html", "earth.css", "earth-app.js", "earth-renderer.js"]) {
    assert.doesNotMatch(readAsset(name), /(?:localhost|127\.0\.0\.1):3008|fetch\(\s*['"]https?:/);
  }
});

test("formal payload retains simulation status, city metadata and independent seats", () => {
  assert.equal(payload.preview_only, true);
  assert.equal(payload.news_mode, "synthetic");
  assert.equal(payload.model_called, false);
  assert.equal(payload.geo_mode, "synthetic_locations");
  assert.equal(payload.news.length, 15);
  assert.deepEqual(payload.news.find(item => item.id === "g14").geo, {
    lon: 114.0579, lat: 22.5431, country: "中国", label: "深圳", province: "广东省", city: "深圳市", country_id: "156",
  });
  assert.equal(payload.news.find(item => item.id === "g13").geo, null);
  for (const stock of payload.stocks) {
    assert.equal(stock.synthetic, true);
    assert.deepEqual(Object.keys(stock.rule_results).sort(), ["baseline", "buffett", "livermore"]);
  }
  assert.match(readAsset("index.html"), /数据状态：模拟/);
});

test("build rejects real/ambiguous data and a changed public asset whitelist", () => {
  for (const key of ["preview_only", "news_mode", "model_called", "impact_mode", "geo_mode"]) {
    const item = bundle(); delete item.payload[key];
    assert.throws(() => validateBundle(item), /synthetic offline/);
  }
  for (const key of ["news", "sectors", "stocks"]) {
    const item = bundle(); item.payload[key][0].synthetic = false;
    assert.throws(() => validateBundle(item), /unmarked/);
  }
  const secret = bundle(); secret.assets.push(".env");
  assert.throws(() => validateBundle(secret), /allowlist/);
  const empty = bundle(); empty.payload.news = [];
  assert.throws(() => validateBundle(empty), /no valid news/);
});

test("offline export is deterministic and retains library license files", () => {
  const before = readAsset("news-data.json");
  prepareNews();
  assert.equal(readAsset("news-data.json"), before);
  for (const license of STATIC_ASSETS.filter(name => name.endsWith(".LICENSE"))) {
    assert.ok(readAsset(license).trim().length > 50);
  }
});

test("dev/build scripts prepare news assets before Next starts", () => {
  const pkg = JSON.parse(fs.readFileSync(path.join(PROJECT_ROOT, "web/package.json"), "utf8"));
  assert.equal(pkg.scripts.predev, "node tools/prepare-news.cjs");
  assert.equal(pkg.scripts.prebuild, "node tools/prepare-news.cjs");
});

test("source release allowlist carries the generator and its exact asset inputs", () => {
  const release = fs.readFileSync(path.join(PROJECT_ROOT, "tools/make_release.py"), "utf8");
  for (const name of ["tools/export_news_payload.py", "web/tools/prepare-news.cjs", "web/tests/news-assets.test.cjs",
    "design-preview/news-earth/serve_earth.py", "design-preview/news-network/serve_news.py", "design-preview/news-spheres/sphere-core.js"]) {
    assert.ok(release.includes(`"${name}"`), `Release must contain ${name}`);
  }
  // Check the concrete expanded Python allowlist without building an archive.
  const localPython = path.join(PROJECT_ROOT, ".venv", process.platform === "win32" ? "Scripts/python.exe" : "bin/python");
  const python = process.env.CONTEST_PYTHON || (fs.existsSync(localPython) ? localPython : "python");
  const result = spawnSync(python, ["-X", "utf8", "-c", "import importlib.util,json; s=importlib.util.spec_from_file_location('release_list','tools/make_release.py'); m=importlib.util.module_from_spec(s); s.loader.exec_module(m); print(json.dumps(m.FILES))"], {
    cwd: PROJECT_ROOT, encoding: "utf8", windowsHide: true, timeout: 10_000,
  });
  assert.equal(result.status, 0, "Release allowlist must be readable without creating a ZIP");
  const files = JSON.parse(result.stdout);
  assert.equal(new Set(files).size, files.length);
  for (const name of [...STATIC_ASSETS, "news-data.json"]) assert.ok(files.includes(`web/public/news-earth/${name}`));
  for (const name of STATIC_ASSETS.filter(name => name !== "sphere-core.js")) assert.ok(files.includes(`design-preview/news-earth/${name}`));
  assert.ok(!files.includes(".env") && !files.some(name => /sqlite|runlogs|\.venv/.test(name)));
});
