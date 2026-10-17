#!/usr/bin/env python3
"""Prepare fixed local fallback speech before accepting child sessions."""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import io
import json
from pathlib import Path
import sys
import wave

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from babybot.providers import LocalProviders
from babybot.safety import SAFE_RESPONSES


async def prepare(force: bool = False) -> None:
    destination = ROOT / ".runtime" / "safe_audio"
    destination.mkdir(parents=True, exist_ok=True)
    manifest_file = destination / "manifest.json"
    previous = json.loads(manifest_file.read_text()) if manifest_file.exists() else {}
    current = {}
    providers = LocalProviders()
    try:
        for category, text in SAFE_RESPONSES.items():
            if category == "stop" or not text:
                continue
            path = destination / f"{category}.wav"
            digest = hashlib.sha256(text.encode()).hexdigest()
            record = previous.get(category, {})
            if not force and path.exists() and record.get("text_sha256") == digest:
                audio = path.read_bytes()
                if hashlib.sha256(audio).hexdigest() != record.get("audio_sha256"):
                    audio = await providers.synthesize(text)
            else:
                audio = await providers.synthesize(text)
            with wave.open(io.BytesIO(audio)) as wav:
                if wav.getnchannels() != 1 or wav.getsampwidth() != 2:
                    raise ValueError(f"Invalid generated WAV for {category}")
                seconds = wav.getnframes() / wav.getframerate()
                sample_rate = wav.getframerate()
            temporary = path.with_suffix(".wav.tmp")
            temporary.write_bytes(audio)
            temporary.replace(path)
            current[category] = {"text_sha256": digest, "audio_sha256": hashlib.sha256(audio).hexdigest(),
                                 "sample_rate": sample_rate, "seconds": round(seconds, 3)}
            print(f"{category}: {seconds:.2f}s, {len(audio)} bytes")
        temporary = manifest_file.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(current, indent=2) + "\n")
        temporary.replace(manifest_file)
    finally:
        await providers.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force", action="store_true", help="Regenerate even when text and audio hashes match")
    asyncio.run(prepare(parser.parse_args().force))
