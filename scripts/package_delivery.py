#!/usr/bin/env python3
"""Package source, documentation and synthetic test evidence; exclude secrets/models."""
import hashlib
import json
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo
from zipfile import ZipFile, ZIP_DEFLATED

ROOT = Path(__file__).resolve().parents[1]


def main():
    selected = []
    for folder in ("babybot", "static", "scripts", "tests", "docs", "reports"):
        for path in (ROOT / folder).rglob("*"):
            if path.is_file() and "__pycache__" not in path.parts and path.suffix not in {".pyc", ".zip"} and path.name != "source-manifest.json":
                selected.append(path)
    selected += [ROOT / name for name in ("README.md", "requirements.txt", "requirements-models.txt", "requirements-lock.txt", ".env.example", ".gitignore", "models/README.md", "models/manifest.json", "models/voice-manifest.json")]
    selected = sorted(set(path for path in selected if path.exists()))
    manifest = {"version": "0.3.0", "generated_at": datetime.now(ZoneInfo("Asia/Taipei")).isoformat(), "kind": "source-and-synthetic-test-evidence", "excluded": [".env", ".runtime", ".venv", "model weights", "Git metadata"], "files": {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest() for path in selected}}
    manifest_path = ROOT / "reports" / "source-manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    out = ROOT / "delivery" / "babybot-0.3.0-source.zip"
    out.parent.mkdir(exist_ok=True)
    with ZipFile(out, "w", compression=ZIP_DEFLATED) as archive:
        for path in [*selected, manifest_path]:
            archive.write(path, "babybot/" + str(path.relative_to(ROOT)))
    print(f"Created {out.name}: {len(selected) + 1} files, {out.stat().st_size} bytes")


if __name__ == "__main__":
    main()
