#!/usr/bin/env python3
"""Real-model CPU boundary checks with a 69-character sentence and synthetic reference speech."""
import io
import json
from pathlib import Path
import sys
import time
import wave

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from babybot.voice_engine import VoiceEngine
from benchmark_voices import REFERENCE_TEXT, TARGET_TEXT, pcm16_at_rate, write_pcm

TEXT = '小兔子沿着弯弯的小路向前走，看见一片漂亮的花丛，花瓣上还有亮晶晶的小水珠。它停下来轻轻地挥手，和身边的朋友一起听小鸟唱歌，准备去树下休息。'
OUT = ROOT / '.runtime' / 'voice-benchmark'
OUT.mkdir(parents=True, exist_ok=True)
reference_text = REFERENCE_TEXT + REFERENCE_TEXT + TARGET_TEXT
reference_path = OUT / 'reference-synthetic-melo-20s.wav'
engine = VoiceEngine(ROOT / 'models', 2)
if not reference_path.exists():
    pcm = pcm16_at_rate(engine.synthesize_wav(reference_text))
    if len(pcm) > 20 * 32000:
        raise RuntimeError('Reference too long; do not silently truncate matching speech')
    write_pcm(reference_path, pcm + bytes(20 * 32000 - len(pcm)))
results = {'text': TEXT, 'text_chars': len(TEXT), 'reference_text': reference_text,
           'reference_path': str(reference_path), 'synthetic_reference_only': True, 'samples': []}
for threads in (2, 4):
    tts = engine if threads == 2 else VoiceEngine(ROOT / 'models', threads)
    profiles = [
        {'id': 'kokoro-female-1', 'engine': 'kokoro', 'speaker_id': 3},
        {'id': 'kokoro-male-2', 'engine': 'kokoro', 'speaker_id': 74},
        {'id': 'synthetic-clone-20s', 'engine': 'zipvoice', 'reference_audio_path': str(reference_path), 'reference_text': reference_text},
    ]
    for profile in profiles:
        tts.load(profile['engine'])
        begin = time.perf_counter()
        data = tts.synthesize_wav(TEXT, profile)
        elapsed = (time.perf_counter() - begin) * 1000
        destination = OUT / f'boundary-{profile["id"]}-{threads}threads.wav'
        destination.write_bytes(data)
        with wave.open(io.BytesIO(data)) as source:
            duration = source.getnframes() / source.getframerate()
        row = {'voice_id': profile['id'], 'threads': threads, 'generation_ms': elapsed,
               'audio_seconds': duration, 'within_8_seconds': elapsed <= 8000, 'wav': str(destination)}
        results['samples'].append(row)
        print(json.dumps(row), flush=True)
        (ROOT / 'reports' / 'voice-engine-boundary.json').write_text(json.dumps(results, ensure_ascii=False, indent=2) + '\n')
