"""Build a reviewer archive by explicit allowlist, never by zipping the workspace."""

import argparse
import hashlib
from pathlib import Path
import re
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo


ROOT = Path(__file__).resolve().parents[1]
# Explicitly mirrored from the geographic preview's public-asset contract.
# Source files support clean npm builds; generated public files support the
# launcher's direct Next dev fallback without running another preview service.
NEWS_STATIC_ASSETS = (
    "index.html", "earth.css", "earth-app.js", "earth-renderer.js", "geo-core.js", "sphere-core.js",
    "vendor/d3-array.min.js", "vendor/d3-geo.min.js", "vendor/topojson-client.min.js",
    "vendor/countries-110m.json", "vendor/d3-array.LICENSE", "vendor/d3-geo.LICENSE",
    "vendor/topojson-client.LICENSE", "vendor/world-atlas.LICENSE",
)
FILES = (
    ".env.example", ".gitignore", "README.md", "launch.py", "requirements.txt", "requirements-authoring.txt", "启动参赛版.cmd", "启动智慧交易助手.cmd",
    "server/__init__.py", "server/agent.py", "server/domain.py", "server/fixtures.py", "server/main.py",
    "server/seats.py", "server/seat_livermore.py", "server/seat_buffett.py",
    "server/access.py", "server/model_setup.py",
    "server/news_research.py", "server/research_store.py", "server/market_history.py", "server/global_news.py", "server/news_relations.py", "server/news_ai.py",
    "server/historical_seats.py", "server/historical_fundamentals.py",
    "server/paper_engine.py", "server/paper_market.py", "server/paper_trading.py",
    "server/paper_statistics.py", "server/paper_playback.py",
    "tests/test_paper_engine.py", "tests/test_paper_market.py", "tests/test_paper_trading_api.py",
    "tests/test_paper_statistics.py", "tests/test_paper_playback.py",
    "tests/test_api_agent.py", "tests/test_domain.py", "tests/test_livermore.py", "tests/test_replay_policy.py", "tests/test_seat_api.py",
    "tests/test_model_setup.py", "tests/test_workspace.py", "tests/test_registration.py", "tests/test_sms_auth.py",
    "tests/test_buffett.py", "tests/test_evidence_freeze.py", "tests/conftest.py", "tests/test_news_research.py", "tests/test_research_archive.py", "tests/test_release_package.py",
    "web/package.json", "web/package-lock.json", "web/next.config.ts", "web/tsconfig.json",
    "web/app/layout.tsx", "web/app/page.tsx", "web/app/decision-desk.tsx", "web/app/globals.css",
    "web/app/price-chart.tsx", "web/app/workspace-gate.tsx", "web/app/workspace-gate.css", "web/app/buffett-evidence.tsx",
    "web/app/brand.ts", "web/app/startup-gate.tsx", "web/app/startup-gate.css", "web/app/startup-particles.ts",
    "web/tests/startup-particles.test.cjs", "web/tests/startup-gate.test.cjs", "web/tests/workspace-gate.test.cjs",
    "web/tests/desk-presentation.test.cjs",
    "web/app/news-desk.tsx", "web/app/research-history.tsx", "web/tools/prepare-news.cjs", "web/tests/news-assets.test.cjs", "web/tests/news-desk.test.cjs", "web/tests/research-flow.test.cjs",
    "web/app/historical-market.tsx", "web/tests/historical-market.test.cjs", "tests/test_market_history.py",
    "web/app/paper-trading-desk.tsx", "web/app/paper-trading.css", "web/tests/paper-trading.test.cjs", "docs/自主模拟交易.md",
    "web/app/paper-replay.tsx", "web/app/paper-replay.css", "web/tests/paper-replay.test.cjs",
    "tools/export_news_payload.py", "tools/download_market_history.py", "docs/历史行情使用说明.md", "docs/全球新闻使用说明.md",
    "tools/download_historical_fundamentals.py", "tests/test_historical_seats.py", "tests/test_historical_fundamentals.py", "docs/三席位真实数据接入.md",
    "tools/download_all_fundamentals.py", "tests/test_bulk_fundamentals.py",
    "tools/download_buffett_originals.py", "tests/test_buffett_originals.py", "docs/巴菲特数据补齐说明.md",
    "server/buffett_verified.py", "tools/verify_buffett_originals.py", "tests/test_buffett_verification.py",
    "tests/test_buffett_verified_reader.py", "tests/test_buffett_field_gates.py", "docs/巴菲特数据接入验收.md",
    "server/livermore_data.py", "server/livermore_reference.py", "tools/download_livermore_data.py",
    "tools/import_livermore_evidence.py", "tests/test_livermore_data.py", "tests/test_livermore_download.py",
    "tests/test_livermore_import.py", "tests/test_livermore_engine_gate.py", "tests/test_livermore_reference.py",
    "docs/利弗莫尔数据接入验收.md",
    "tests/test_global_news.py", "tests/test_news_relations.py", "tests/test_news_ai.py",
    "design-preview/news-earth/serve_earth.py", "design-preview/news-earth/README.md", "design-preview/news-earth/DATA-SOURCES.md",
    "design-preview/news-earth/test_geo_core.cjs", "design-preview/news-earth/test_earth_app.cjs", "design-preview/news-earth/test_earth_server.py",
    "design-preview/news-network/serve_news.py", "design-preview/news-spheres/sphere-core.js",
    "docs/技术说明.md", "docs/录屏脚本.md", "docs/提交清单.md", "docs/利弗莫尔席位.md", "docs/巴菲特席位.md",
    "docs/环境配置.md", "docs/团队信息.md", "docs/开发过程与Git提交说明.md", "docs/AI实测记录模板.md", "docs/测试与交付验证.md",
    "docs/材料总览.md", "docs/报名项目简介.md", "docs/启动入口修复说明.md",
    "tools/render_submission_pdf.py", "tools/assemble_submission.py", "tools/make_release.py", "tools/verify_submission.py",
    "tests/test_submission_bundle.py", "output/pdf/智慧交易助手-参赛技术文档-20261117.pdf",
) + tuple(f"design-preview/news-earth/{name}" for name in NEWS_STATIC_ASSETS if name != "sphere-core.js") + tuple(
    f"web/public/news-earth/{name}" for name in (*NEWS_STATIC_ASSETS, "news-data.json")
)


def build_archive(archive: Path):
    files = {}
    for name in FILES:
        parts = Path(name).parts
        if (any(part in {".git", ".venv", "node_modules", "runlogs", "tmp", ".next", ".next-seat"} for part in parts)
                or (Path(name).name.startswith(".env") and name != ".env.example")
                or Path(name).suffix.lower() in {".db", ".sqlite", ".sqlite3", ".log", ".zip"}):
            raise ValueError(f"Private/runtime path is not publishable: {name}")
        path = (ROOT / name).resolve(strict=True)
        if not path.is_relative_to(ROOT) or not path.is_file():
            raise ValueError(f"Unexpected release path: {name}")
        content = path.read_bytes()
        if path.suffix.lower() in {".py", ".js", ".cjs", ".tsx", ".ts", ".md", ".txt"} or name == ".env.example":
            if re.search(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|sk-[A-Za-z0-9]{32,}", content):
                # Report only the filename, never the matched credential material.
                raise ValueError(f"Potential credential detected; review before publishing: {name}")
        files[name] = content
    manifest = "".join(f"{hashlib.sha256(content).hexdigest()}  {name}\n" for name, content in sorted(files.items()))
    files["RELEASE.sha256"] = manifest.encode("utf-8")
    archive = archive.resolve()
    if not archive.is_relative_to(ROOT):
        raise ValueError("Create archives inside the selected project output directory.")
    archive.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(archive, "x") as output:
        for name, content in sorted(files.items()):
            item = ZipInfo(f"contest-edition/{name}")
            item.compress_type = ZIP_DEFLATED
            output.writestr(item, content)
    with ZipFile(archive) as output:
        assert output.testzip() is None
        assert set(output.namelist()) == {f"contest-edition/{name}" for name in files}
        assert all(output.read(f"contest-edition/{name}") == content for name, content in files.items())
    print(f"Verified release: {len(files)} files, {archive.stat().st_size} bytes")
    print(hashlib.sha256(archive.read_bytes()).hexdigest())
    return archive


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path,
                        default=ROOT / "output" / "submission-20261117" / "01_智慧交易助手-源码快照-20261117.zip")
    args = parser.parse_args()
    build_archive(args.output)


if __name__ == "__main__":
    main()
