#!/usr/bin/env python3
"""Check the longest fixed safety response and preview under the 8s TTS budget."""
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
from benchmark_voices import REFERENCE_TEXT, TARGET_TEXT

reference = ROOT / '.runtime/voice-benchmark/reference-synthetic-melo-20s.wav'
if not reference.is_file():
    raise RuntimeError('Run benchmark_voice_limits.py to create the synthetic 20s reference')
texts = {'longest_fixed': max(SAFE_RESPONSES.values(), key=len),
         'preview': '我是小芽，一个AI玩伴。我们一起讲个温暖的小故事吧。'}
profiles = [{'id': 'kokoro-male-2', 'engine': 'kokoro', 'speaker_id': 74},
            {'id': 'synthetic-clone-20s', 'engine': 'zipvoice', 'reference_audio_path': str(reference),
             'reference_text': REFERENCE_TEXT + REFERENCE_TEXT + TARGET_TEXT}]
report = {'threads': 2, 'synthetic_reference_only': True, 'human_voice_cloned': False,
          'reference_seconds': 20, 'texts': texts, 'samples': []}
engine = VoiceEngine(ROOT / 'models', 2)
for voice in profiles:
    start = time.perf_counter()
    engine.load(voice['engine'])
    load_ms = (time.perf_counter() - start) * 1000
    for name, text in texts.items():
        for index in range(3):
            start = time.perf_counter()
            data = engine.synthesize_wav(text, voice)
            elapsed = (time.perf_counter() - start) * 1000
            destination = ROOT / f'.runtime/voice-benchmark/preparation-{voice["id"]}-{name}-{index}.wav'
            destination.write_bytes(data)
            with wave.open(io.BytesIO(data)) as wav:
                seconds = wav.getnframes() / wav.getframerate()
            row = {'voice_id': voice['id'], 'text_id': name, 'text_chars': len(text), 'sample': index,
                   'load_ms': load_ms, 'generation_ms': elapsed, 'audio_seconds': seconds,
                   'within_8_seconds': elapsed < 8000, 'wav': str(destination)}
            report['samples'].append(row)
            print(json.dumps(row), flush=True)
            report['passed'] = all(sample['within_8_seconds'] for sample in report['samples'])
            (ROOT / 'reports/voice-engine-preparation.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
