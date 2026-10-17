#!/usr/bin/env python3
"""Download pinned local weights and build a project-local CPU llama.cpp runtime."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import tarfile
import tempfile

ROOT = Path(__file__).resolve().parents[1]
LLAMA_COMMIT = "d7bd3bfcad3e29c7e49fd26f38c79ee3e9a3fd6b"
LLM_SHA256 = "6a1a2eb6d15622bf3c96857206351ba97e1af16c30d7a74ee38970e434e9407e"
SENSE = "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17"
MELO = "vits-melo-tts-zh_en"
ASSETS = [
    {"file": "qwen2.5-1.5b-instruct-q4_k_m.gguf", "sha256": LLM_SHA256,
     "url": "https://modelscope.cn/models/Qwen/Qwen2.5-1.5B-Instruct-GGUF/resolve/master/qwen2.5-1.5b-instruct-q4_k_m.gguf"},
    {"file": "sensevoice.tar.bz2", "directory": SENSE,
     "sha256": "7d1efa2138a65b0b488df37f8b89e3d91a60676e416f515b952358d83dfd347e",
     "url": f"https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/{SENSE}.tar.bz2"},
    {"file": "melotts.tar.bz2", "directory": MELO,
     "sha256": "e58351ed7149f290a54534538badd4077cdbe6fddc964b24d0bee870415d1514",
     "url": f"https://github.com/k2-fsa/sherpa-onnx/releases/download/tts-models/{MELO}.tar.bz2"},
]


def sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def setup_models(direct: bool = False) -> None:
    models = ROOT / "models"
    models.mkdir(exist_ok=True)
    records = []
    for asset in ASSETS:
        destination = models / asset["file"]
        if not destination.exists():
            partial = destination.with_suffix(destination.suffix + ".part")
            command = ["curl", "--fail", "--location", "--retry", "3", "--connect-timeout", "20",
                       "--continue-at", "-", "--output", str(partial)]
            if direct or "modelscope.cn" in asset["url"]:
                command += ["--noproxy", "*"]
            subprocess.run([*command, asset["url"]], check=True)
            partial.rename(destination)
        digest = sha256(destination)
        if asset.get("sha256") and digest != asset["sha256"]:
            raise RuntimeError(f"Checksum mismatch: {destination}; remove this file and retry")
        if "directory" in asset and not (models / asset["directory"]).exists():
            # Python's data filter rejects traversal, devices, and unsafe links.
            with tempfile.TemporaryDirectory(dir=models) as temporary:
                with tarfile.open(destination) as archive:
                    archive.extractall(temporary, filter="data")
                (Path(temporary) / asset["directory"]).rename(models / asset["directory"])
        records.append({**asset, "sha256": digest, "bytes": destination.stat().st_size})
    (models / "manifest.json").write_text(json.dumps(records, ensure_ascii=False, indent=2) + "\n")


def setup_runtime(source: Path | None) -> None:
    runtime = ROOT / ".runtime"
    runtime.mkdir(exist_ok=True)
    if source is None:
        source = runtime / "llama-source"
        if not source.exists():
            subprocess.run(["git", "clone", "--filter=blob:none", "--no-checkout",
                            "https://github.com/ggml-org/llama.cpp.git", str(source)], check=True)
            subprocess.run(["git", "-C", str(source), "checkout", "--detach", LLAMA_COMMIT], check=True)
    revision = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip()
    if revision != LLAMA_COMMIT:
        raise RuntimeError(f"Expected llama.cpp {LLAMA_COMMIT}, found {revision}")
    build = runtime / "llama-cpu"
    subprocess.run(["cmake", "-S", str(source), "-B", str(build), "-DGGML_CUDA=OFF",
                    "-DGGML_HIP=OFF", "-DGGML_VULKAN=OFF", "-DLLAMA_CURL=OFF",
                    "-DBUILD_SHARED_LIBS=OFF", "-DGGML_NATIVE=ON", "-DLLAMA_BUILD_TESTS=OFF",
                    "-DLLAMA_BUILD_EXAMPLES=OFF", "-DLLAMA_BUILD_SERVER=ON", "-DCMAKE_BUILD_TYPE=Release"], check=True)
    subprocess.run(["cmake", "--build", str(build), "--target", "llama-server", "-j", "8"], check=True)
    (runtime / "llama-version.txt").write_text(revision + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--llama-source", type=Path, help="Use an existing checkout at the pinned commit")
    parser.add_argument("--skip-runtime", action="store_true")
    parser.add_argument("--skip-models", action="store_true")
    parser.add_argument("--direct", action="store_true", help="Bypass environment proxies for downloads")
    args = parser.parse_args()
    if not args.skip_models:
        setup_models(args.direct)
    if not args.skip_runtime:
        setup_runtime(args.llama_source)
