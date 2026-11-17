"""Read-only validation of reviewer archives, without importing the app/secrets."""
from __future__ import annotations

import argparse
import hashlib
from io import BytesIO
from pathlib import Path, PurePosixPath
import re
from zipfile import ZipFile

try:
    from .make_release import FILES
    from .assemble_submission import COPIES
except ImportError:
    from make_release import FILES
    from assemble_submission import COPIES

MAX_FILE, MAX_TOTAL = 20_000_000, 200_000_000


def contents(source) -> dict[str, bytes]:
    with ZipFile(source) as archive:
        info = archive.infolist()
        names = [item.filename for item in info]
        if len(names) != len(set(names)) or sum(item.file_size for item in info) > MAX_TOTAL:
            raise ValueError("Duplicate entries or oversized archive")
        for item in info:
            path = PurePosixPath(item.filename)
            if (item.is_dir() or path.is_absolute() or ".." in path.parts or "\\" in item.filename
                    or ":" in item.filename or item.file_size > MAX_FILE or item.flag_bits & 1):
                raise ValueError("Unsafe archive entry")
        return {item.filename: archive.read(item) for item in info}


def manifest(raw: bytes) -> dict[str, str]:
    result = {}
    for line in raw.decode("utf-8").splitlines():
        digest, name = line.split("  ", 1)
        if re.fullmatch(r"[a-f0-9]{64}", digest) is None or not name or name in result:
            raise ValueError("Invalid checksum manifest")
        result[name] = digest
    return result


def verify_source(source) -> dict:
    files = contents(source)
    prefix = "contest-edition/"
    expected = {prefix + name for name in (*FILES, "RELEASE.sha256")}
    if set(files) != expected:
        raise ValueError("Source ZIP differs from the explicit release allowlist")
    hashes = manifest(files[prefix + "RELEASE.sha256"])
    if set(hashes) != set(FILES):
        raise ValueError("Source checksum entries differ from allowlist")
    for name, digest in hashes.items():
        if hashlib.sha256(files[prefix + name]).hexdigest() != digest:
            raise ValueError("Source checksum mismatch")
        parts = PurePosixPath(name).parts
        if any(part in {".git", ".venv", "node_modules", "runlogs", "tmp", "data", ".next", ".next-seat"} for part in parts):
            raise ValueError("Private/runtime data included")
        if (PurePosixPath(name).name.startswith(".env") and name != ".env.example") or PurePosixPath(name).suffix.lower() in {".db", ".sqlite", ".sqlite3", ".log", ".zip"}:
            raise ValueError("Private storage included")
    template = files[prefix + ".env.example"].decode("utf-8")
    if not re.search(r"^CONTEST_LLM_API_KEY=\s*$", template, re.M):
        raise ValueError("API template must contain an empty key")
    return {"source_files": len(files), "all_checksums_match": True, "private_runtime_files": 0}


def verify_bundle(path: Path) -> dict:
    files = contents(path)
    prefix = "智慧交易助手-参赛材料/"
    if not files or any(not name.startswith(prefix) or "/" in name[len(prefix):] for name in files):
        raise ValueError("Unexpected material bundle layout")
    receipt = prefix + "SHA256SUMS.txt"
    hashes = manifest(files[receipt])
    if {prefix + name for name in hashes} != set(files) - {receipt}:
        raise ValueError("Materials checksum coverage mismatch")
    for name, digest in hashes.items():
        if hashlib.sha256(files[prefix + name]).hexdigest() != digest:
            raise ValueError("Materials checksum mismatch")
    source_names = [name for name in files if re.fullmatch(prefix + r"01_智慧交易助手-源码快照-\d{8}\.zip", name)]
    if len(source_names) != 1:
        raise ValueError("Exactly one source snapshot is required")
    expected = {prefix + name for name in COPIES.values()} | {receipt, source_names[0]}
    if set(files) != expected:
        raise ValueError("Required materials missing or unapproved extras present")
    inner = contents(BytesIO(files[source_names[0]]))
    for source, target in COPIES.items():
        if inner.get("contest-edition/" + source) != files[prefix + target]:
            raise ValueError("Outer material differs from source snapshot")
    return {"materials": len(files), **verify_source(BytesIO(files[source_names[0]])),
            "bundle_sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--source", type=Path)
    group.add_argument("--bundle", type=Path)
    args = parser.parse_args()
    result = verify_bundle(args.bundle) if args.bundle else verify_source(args.source)
    import json
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
