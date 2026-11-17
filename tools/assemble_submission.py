"""Assemble only explicitly approved reviewer materials, never the workspace.

No uploads, Git operations, credentials, local account data or model requests.
The source ZIP is built by make_release's explicit allowlist. This outer bundle
also includes honest completion status; it is not a submission receipt.
"""
from __future__ import annotations

import hashlib
import argparse
from datetime import datetime
from pathlib import Path
import shutil
from zipfile import ZIP_DEFLATED, ZipFile

try:
    from .make_release import build_archive
except ImportError:  # Direct command-line execution from the tools directory.
    from make_release import build_archive

ROOT = Path(__file__).resolve().parents[1]
COPIES = {
    "output/pdf/智慧交易助手-参赛技术文档-20261117.pdf": "02_智慧交易助手-参赛技术文档.pdf",
    "docs/技术说明.md": "02_技术文档可编辑源稿.md",
    "docs/录屏脚本.md": "03_录屏脚本.md",
    "docs/提交清单.md": "04_提交检查清单.md",
    "docs/团队信息.md": "06_团队信息与待补项.md",
    "docs/开发过程与Git提交说明.md": "07_仓库发布与开发历史说明.md",
    "docs/AI实测记录模板.md": "08_AI实测记录模板.md",
    "docs/测试与交付验证.md": "09_测试与交付验证.md",
    "docs/环境配置.md": "10_环境配置说明.md",
    "docs/材料总览.md": "00_先读我_提交状态.md",
    "docs/报名项目简介.md": "05_报名项目简介.md",
    "docs/启动入口修复说明.md": "11_启动入口修复说明.md",
    "docs/巴菲特数据接入验收.md": "12_巴菲特数据接入与限制.md",
    "docs/利弗莫尔数据接入验收.md": "13_利弗莫尔数据接入与限制.md",
    "docs/历史行情使用说明.md": "14_历史行情准备与授权说明.md",
    "docs/全球新闻使用说明.md": "15_全球新闻与AI关联说明.md",
    "docs/自主模拟交易.md": "16_自主模拟交易说明.md",
}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", default="20261117", help="Snapshot date YYYYMMDD; never overwrite an existing bundle")
    args = parser.parse_args(argv)
    if len(args.date) != 8 or not args.date.isascii() or not args.date.isdecimal():
        parser.error("date must be exactly YYYYMMDD")
    try:
        datetime.strptime(args.date, "%Y%m%d")
    except ValueError:
        parser.error("date must be a valid calendar date")
    output = ROOT / "output" / f"submission-{args.date}"
    archive = ROOT.parent / f"智慧交易助手-参赛材料包-{args.date}.zip"
    if archive.exists():
        raise SystemExit("The final bundle already exists; preserve it or choose a new dated version.")
    output.mkdir(parents=True, exist_ok=True)
    source_zip = output / f"01_智慧交易助手-源码快照-{args.date}.zip"
    if source_zip.exists():
        raise SystemExit("The source snapshot already exists; do not overwrite a frozen delivery.")
    sources = {}
    for source, name in COPIES.items():
        resolved = (ROOT / source).resolve(strict=True)
        if not resolved.is_relative_to(ROOT) or not resolved.is_file():
            raise ValueError("Unexpected material path")
        sources[name] = resolved
    # Validate all final destinations before writing the source ZIP; a partial
    # previous attempt is never silently merged into a frozen delivery.
    if any((output / name).exists() for name in (*sources, "SHA256SUMS.txt")):
        raise SystemExit("Preserve existing material files; choose a fresh snapshot date.")
    build_archive(source_zip)
    selected = [source_zip]
    for name, source in sources.items():
        target = output / name
        if target.exists():
            raise SystemExit(f"Preserve existing material: {name}")
        shutil.copyfile(source, target)
        selected.append(target)
    checksums = output / "SHA256SUMS.txt"
    if checksums.exists():
        raise SystemExit("Preserve existing checksums; choose a new snapshot date.")
    checksums.write_text("".join(f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}\n"
                                 for path in sorted(selected)), encoding="utf-8")
    selected.append(checksums)
    with ZipFile(archive, "x", compression=ZIP_DEFLATED) as bundle:
        for path in selected:
            bundle.write(path, f"智慧交易助手-参赛材料/{path.name}")
    with ZipFile(archive) as bundle:
        assert bundle.testzip() is None
        assert len(bundle.namelist()) == len(selected)
    print(f"Materials: {output}")
    print(f"Bundle: {archive} ({archive.stat().st_size} bytes)")
    print(f"SHA256: {hashlib.sha256(archive.read_bytes()).hexdigest()}")


if __name__ == "__main__":
    main()
