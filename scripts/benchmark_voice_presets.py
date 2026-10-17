#!/usr/bin/env python3
"""Choose Chinese presets against the longest unchanged fixed safety sentence."""
import io
import json
from pathlib import Path
import sys
import time
import wave

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from babybot.safety import SAFE_RESPONSES
from babybot.voice_engine import VoiceEngine

text = max(SAFE_RESPONSES.values(), key=len)
engine = VoiceEngine(ROOT / 'models', 2)
started = time.perf_counter()
engine.load('kokoro')
report = {'engine': 'kokoro', 'threads': 2, 'text': text, 'text_chars': len(text),
          'cold_load_ms': (time.perf_counter() - started) * 1000,
          'prior_rejected_preset': {'speaker_id': 74, 'report': 'voice-engine-preparation.json'}, 'samples': []}
for sid in (3, 20, 58, 59):
    for trial in range(3):
        started = time.perf_counter()
        data = engine.synthesize_wav(text, {'engine': 'kokoro', 'speaker_id': sid})
        elapsed = (time.perf_counter() - started) * 1000
        destination = ROOT / f'.runtime/voice-benchmark/final-preset-{sid}-{trial}.wav'
        destination.write_bytes(data)
        with wave.open(io.BytesIO(data)) as wav:
            duration = wav.getnframes() / wav.getframerate()
        row = {'speaker_id': sid, 'trial': trial, 'generation_ms': elapsed,
               'audio_seconds': duration, 'within_8_seconds': elapsed < 8000, 'wav': str(destination)}
        report['samples'].append(row)
        report['passed'] = all(sample['within_8_seconds'] for sample in report['samples'])
        (ROOT / 'reports/voice-engine-final-presets.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
        print(json.dumps(row), flush=True)
