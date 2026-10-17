#!/usr/bin/env python3
"""Real CPU voice benchmark using only explicitly synthetic reference speech."""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import io
import json
from pathlib import Path
import sys
import time
import wave

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from babybot.providers import LocalProviders
from babybot.voice_engine import BUILTIN_VOICES

REFERENCE_TEXT = '今天的阳光很温暖，小兔子和朋友一起在草地上散步。我们慢慢说话，听见小鸟在唱歌。'
TARGET_TEXT = '你好呀，我们一起讲一个小兔子的故事。'


def pcm16_at_rate(wav_bytes: bytes, target: int = 16000) -> bytes:
    with wave.open(io.BytesIO(wav_bytes)) as source:
        assert source.getnchannels() == 1 and source.getsampwidth() == 2
        rate = source.getframerate()
        samples = np.frombuffer(source.readframes(source.getnframes()), dtype='<i2').astype(np.float32)
    if rate != target:
        count = round(len(samples) * target / rate)
        samples = np.interp(np.arange(count) * rate / target, np.arange(len(samples)), samples)
    return np.clip(samples, -32768, 32767).astype('<i2').tobytes()


def write_pcm(path: Path, pcm: bytes, rate: int = 16000):
    with wave.open(str(path), 'wb') as writer:
        writer.setnchannels(1); writer.setsampwidth(2); writer.setframerate(rate)
        writer.writeframes(pcm)


def main(engine: str, repeats: int, threads: int):
    output = ROOT / '.runtime' / 'voice-benchmark'
    output.mkdir(parents=True, exist_ok=True)
    report_path = ROOT / 'reports' / 'voice-engine-benchmark.json'
    report_path.parent.mkdir(exist_ok=True)
    report = json.loads(report_path.read_text()) if report_path.exists() else {
        'synthetic_reference_only': True, 'human_voice_cloned': False,
        'reference_text': REFERENCE_TEXT, 'target_text': TARGET_TEXT, 'runs': []}
    provider = LocalProviders({'models_dir': ROOT / 'models', 'speech_threads': threads})
    model = provider._voice_engine
    reference_path = output / 'reference-synthetic-melo-16k.wav'
    try:
        if not reference_path.exists():
            begin = time.perf_counter()
            reference_wav = model.synthesize_wav(REFERENCE_TEXT)
            write_pcm(reference_path, pcm16_at_rate(reference_wav))
            report['reference_creation_ms'] = (time.perf_counter() - begin) * 1000
        report['reference_audio_path'] = str(reference_path)
        report['reference_sha256'] = hashlib.sha256(reference_path.read_bytes()).hexdigest()
        with wave.open(str(reference_path)) as source:
            report['reference_duration_seconds'] = source.getnframes() / source.getframerate()
        profiles = [dict(v) for v in BUILTIN_VOICES if v['engine'] == engine]
        if engine == 'zipvoice':
            profiles = [{'id': 'synthetic-melo-clone', 'engine': 'zipvoice', 'reference_audio_path': str(reference_path), 'reference_text': REFERENCE_TEXT}]
        begin = time.perf_counter()
        model.load(engine)
        load_ms = (time.perf_counter() - begin) * 1000
        for profile in profiles:
            row = {'engine': engine, 'voice_id': profile['id'], 'threads': threads, 'load_ms': load_ms, 'samples': []}
            for index in range(repeats):
                begin = time.perf_counter()
                data = model.synthesize_wav(TARGET_TEXT, profile)
                elapsed = (time.perf_counter() - begin) * 1000
                destination = output / f'{profile["id"]}-{index}.wav'
                destination.write_bytes(data)
                with wave.open(io.BytesIO(data)) as wav:
                    seconds = wav.getnframes() / wav.getframerate()
                    sample_rate = wav.getframerate()
                row['samples'].append({'generation_ms': elapsed, 'audio_seconds': seconds,
                    'real_time_factor': elapsed / (seconds * 1000), 'sample_rate': sample_rate,
                    'path': str(destination), 'sha256': hashlib.sha256(data).hexdigest()})
                print(json.dumps({'voice': profile['id'], 'sample': index, 'generation_ms': round(elapsed, 2), 'audio_seconds': round(seconds, 3)}, ensure_ascii=False), flush=True)
            # Independent real local ASR gives a reproducible intelligibility check;
            # it is not a perceptual speaker-similarity score.
            row['recognized_text'] = provider._transcribe(pcm16_at_rate(data), 16000)
            row['max_generation_ms'] = max(sample['generation_ms'] for sample in row['samples'])
            report['runs'].append(row)
            report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
        print(json.dumps({'report': str(report_path), 'reference_seconds': report['reference_duration_seconds']}, ensure_ascii=False), flush=True)
    finally:
        asyncio.run(provider.close())


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--engine', choices=['melo', 'kokoro', 'zipvoice'], required=True)
    parser.add_argument('--repeats', type=int, default=3)
    parser.add_argument('--threads', type=int, default=2)
    args = parser.parse_args()
    main(args.engine, args.repeats, args.threads)
