"""Export only the offline news fixture for the formal frontend build.

No app startup, configuration, account database, model calls, or HTTP requests.
The stdout contract is a single UTF-8 JSON document; errors go to stderr.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "design-preview" / "news-earth" / "serve_earth.py"
FORBIDDEN_MODULES = {
    "server.main", "server.access", "server.model_setup",
    "server.sms_auth", "server.sms_provider",
}


def export_bundle() -> dict:
    spec = importlib.util.spec_from_file_location("_formal_news_offline_export", SOURCE)
    if spec is None or spec.loader is None:
        raise RuntimeError("Offline news fixture is unavailable.")
    source = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(source)
    # This function rejects non-synthetic mode and applies size/JSON checks.
    payload = json.loads(source.news_data_bytes())
    if FORBIDDEN_MODULES.intersection(sys.modules):
        raise RuntimeError("News build must not load formal account/configuration modules.")
    return {
        "schema_version": 1,
        "assets": sorted(source.STATIC_TYPES),
        "payload": payload,
    }


def main() -> None:
    content = json.dumps(export_bundle(), ensure_ascii=False, allow_nan=False,
                         separators=(",", ":")).encode("utf-8")
    sys.stdout.buffer.write(content)


if __name__ == "__main__":
    main()
