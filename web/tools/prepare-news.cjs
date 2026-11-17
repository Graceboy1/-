"use strict";

// Build-time copies of an explicit public-asset allowlist. The formal site does
// not connect to, proxy, or start the separate design preview on port 3008.
const fs = require("node:fs");
const path = require("node:path");
const { spawnSync } = require("node:child_process");

const WEB_ROOT = path.resolve(__dirname, "..");
const PROJECT_ROOT = path.resolve(WEB_ROOT, "..");
const PREVIEW_ROOT = path.join(PROJECT_ROOT, "design-preview", "news-earth");
const OUTPUT_ROOT = path.join(WEB_ROOT, "public", "news-earth");
const STATIC_ASSETS = Object.freeze([
  "index.html", "earth.css", "earth-app.js", "earth-renderer.js", "geo-core.js", "sphere-core.js",
  "vendor/d3-array.min.js", "vendor/d3-geo.min.js", "vendor/topojson-client.min.js",
  "vendor/countries-110m.json", "vendor/d3-array.LICENSE", "vendor/d3-geo.LICENSE",
  "vendor/topojson-client.LICENSE", "vendor/world-atlas.LICENSE",
]);
const MAX_ASSET_BYTES = 2_000_000;
const MAX_PAYLOAD_BYTES = 1_000_000;

function inside(root, target) {
  const relative = path.relative(root, target);
  return relative !== "" && !relative.startsWith(`..${path.sep}`) && relative !== ".." && !path.isAbsolute(relative);
}

function sourcePath(name) {
  if (!STATIC_ASSETS.includes(name)) throw new Error("Unknown news asset.");
  return name === "sphere-core.js"
    ? path.join(PROJECT_ROOT, "design-preview", "news-spheres", name)
    : path.join(PREVIEW_ROOT, name);
}

function validateBundle(bundle) {
  if (!bundle || bundle.schema_version !== 1 || !Array.isArray(bundle.assets)
      || JSON.stringify([...bundle.assets].sort()) !== JSON.stringify([...STATIC_ASSETS].sort())) {
    throw new Error("News export asset contract does not match the public allowlist.");
  }
  const p = bundle.payload;
  if (!p || p.preview_only !== true || p.news_mode !== "synthetic" || p.model_called !== false
      || p.impact_mode !== "synthetic_ordinal" || p.geo_mode !== "synthetic_locations") {
    throw new Error("Only explicitly synthetic offline news may be bundled.");
  }
  for (const key of ["news", "sectors", "stocks", "news_links", "stock_links", "seats"]) {
    if (!Array.isArray(p[key]) || !p[key].length) throw new Error(`News export has no valid ${key}.`);
  }
  for (const key of ["news", "sectors", "stocks"]) {
    if (p[key].some(item => !item || item.synthetic !== true)) {
      throw new Error(`News export contains an unmarked ${key} item.`);
    }
  }
  const content = Buffer.from(`${JSON.stringify(p)}\n`, "utf8");
  if (content.length > MAX_PAYLOAD_BYTES) throw new Error("News export is too large.");
  return content;
}

function pythonCommand() {
  if (process.env.CONTEST_PYTHON) return process.env.CONTEST_PYTHON;
  const local = path.join(PROJECT_ROOT, ".venv", process.platform === "win32" ? "Scripts/python.exe" : "bin/python");
  return fs.existsSync(local) ? local : "python";
}

function exportPayload() {
  const result = spawnSync(pythonCommand(), ["-X", "utf8", path.join(PROJECT_ROOT, "tools", "export_news_payload.py")], {
    cwd: PROJECT_ROOT, encoding: "utf8", windowsHide: true, timeout: 60_000, maxBuffer: 2_000_000,
  });
  if (result.error || result.status !== 0) {
    throw new Error("Offline news export failed. Install the project Python requirements or set CONTEST_PYTHON to its Python executable.");
  }
  let bundle;
  try { bundle = JSON.parse(result.stdout); }
  catch { throw new Error("Offline news exporter did not return valid JSON."); }
  return validateBundle(bundle);
}

function prepareNews() {
  // Validate every input before changing generated public output.
  const files = new Map();
  for (const name of STATIC_ASSETS) {
    const source = fs.realpathSync(sourcePath(name));
    if (!inside(fs.realpathSync(PROJECT_ROOT), source) || !fs.statSync(source).isFile()) {
      throw new Error("News asset source is outside this project.");
    }
    const bytes = fs.readFileSync(source);
    if (bytes.length > MAX_ASSET_BYTES) throw new Error(`News asset is too large: ${name}`);
    files.set(name, bytes);
  }
  files.set("news-data.json", exportPayload());
  fs.mkdirSync(OUTPUT_ROOT, { recursive: true });
  if (!inside(fs.realpathSync(WEB_ROOT), fs.realpathSync(OUTPUT_ROOT))) {
    throw new Error("News asset output is outside this frontend.");
  }
  for (const [name, bytes] of files) {
    const target = path.join(OUTPUT_ROOT, name);
    fs.mkdirSync(path.dirname(target), { recursive: true });
    if (!inside(fs.realpathSync(OUTPUT_ROOT), path.join(fs.realpathSync(path.dirname(target)), path.basename(target)))
        || (fs.existsSync(target) && fs.lstatSync(target).isSymbolicLink())) {
      throw new Error("News asset output must be a regular file within its generated directory.");
    }
    fs.writeFileSync(target, bytes);
  }
  return { directory: OUTPUT_ROOT, count: files.size, bytes: [...files.values()].reduce((sum, item) => sum + item.length, 0) };
}

if (require.main === module) {
  try {
    const result = prepareNews();
    process.stdout.write(`Prepared ${result.count} local news assets (${result.bytes} bytes).\n`);
  } catch (error) {
    process.stderr.write(`News asset build failed: ${error.message}\n`);
    process.exitCode = 1;
  }
}

module.exports = { STATIC_ASSETS, OUTPUT_ROOT, sourcePath, validateBundle, prepareNews };
