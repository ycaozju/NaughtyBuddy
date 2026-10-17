"""Offline CPU speech voices. Each request receives its own voice snapshot.

Only server-created profiles may supply reference paths. No speaker selection or
reference clip is kept as mutable global state. Native calls must run on the
provider's existing serialized TTS executor.
"""
from __future__ import annotations

import io
from pathlib import Path
from typing import Any
import wave

import numpy as np

MELO = 'vits-melo-tts-zh_en'
KOKORO = 'kokoro-int8-multi-lang-v1_1'
ZIPVOICE = 'sherpa-onnx-zipvoice-distill-int8-zh-en-emilia'

BUILTIN_VOICES = [
    {'id': 'melo-default', 'name': '小芽原声', 'description': '原来的中文合成音色', 'engine': 'melo', 'speaker_id': 0},
    {'id': 'kokoro-female-1', 'name': '中文女声一', 'description': 'Kokoro 中文女声预设', 'engine': 'kokoro', 'speaker_id': 3},
    {'id': 'kokoro-female-2', 'name': '中文女声二', 'description': '另一种 Kokoro 中文女声预设', 'engine': 'kokoro', 'speaker_id': 20},
    {'id': 'kokoro-male-1', 'name': '中文男声一', 'description': 'Kokoro 中文男声预设', 'engine': 'kokoro', 'speaker_id': 58},
    {'id': 'kokoro-male-2', 'name': '中文男声二', 'description': '另一种 Kokoro 中文男声预设', 'engine': 'kokoro', 'speaker_id': 59},
]

_REQUIRED = {
    'melo': [f'{MELO}/{name}' for name in ('model.onnx', 'tokens.txt', 'lexicon.txt', 'date.fst', 'number.fst', 'phone.fst')],
    'kokoro': [f'{KOKORO}/{name}' for name in ('model.int8.onnx', 'voices.bin', 'tokens.txt', 'lexicon-us-en.txt', 'lexicon-zh.txt', 'espeak-ng-data')],
    'zipvoice': [f'{ZIPVOICE}/{name}' for name in ('encoder.int8.onnx', 'decoder.int8.onnx', 'tokens.txt', 'lexicon.txt', 'espeak-ng-data')] + ['vocos_24khz.onnx'],
}


def engine_available(models_dir: Path | str, engine: str) -> bool:
    root = Path(models_dir)
    files = _REQUIRED.get(engine)
    return bool(files) and all((root / name).exists() for name in files)


class VoiceEngineError(RuntimeError):
    """A missing model, invalid server profile, or invalid generated waveform."""


class VoiceEngine:
    def __init__(self, models_dir: Path | str, threads: int = 2):
        self.models_dir = Path(models_dir)
        self.threads = max(1, min(int(threads), 8))
        self._models: dict[str, Any] = {}

    def load(self, engine: str):
        if engine in self._models:
            return self._models[engine]
        if not engine_available(self.models_dir, engine):
            raise VoiceEngineError('Requested local voice model is not installed')
        import sherpa_onnx
        common = dict(num_threads=self.threads, provider='cpu', debug=False)
        rule_fsts = ''
        if engine == 'melo':
            base = self.models_dir / MELO
            model = sherpa_onnx.OfflineTtsModelConfig(
                vits=sherpa_onnx.OfflineTtsVitsModelConfig(
                    model=str(base / 'model.onnx'), tokens=str(base / 'tokens.txt'), lexicon=str(base / 'lexicon.txt')),
                **common)
            rule_fsts = ','.join(str(base / name) for name in ('date.fst', 'number.fst', 'phone.fst'))
        elif engine == 'kokoro':
            base = self.models_dir / KOKORO
            model = sherpa_onnx.OfflineTtsModelConfig(
                kokoro=sherpa_onnx.OfflineTtsKokoroModelConfig(
                    model=str(base / 'model.int8.onnx'), voices=str(base / 'voices.bin'),
                    tokens=str(base / 'tokens.txt'), data_dir=str(base / 'espeak-ng-data'),
                    lexicon=','.join(str(base / name) for name in ('lexicon-us-en.txt', 'lexicon-zh.txt'))),
                **common)
            rule_fsts = ','.join(str(base / name) for name in ('date-zh.fst', 'number-zh.fst', 'phone-zh.fst') if (base / name).is_file())
        elif engine == 'zipvoice':
            base = self.models_dir / ZIPVOICE
            model = sherpa_onnx.OfflineTtsModelConfig(
                zipvoice=sherpa_onnx.OfflineTtsZipvoiceModelConfig(
                    tokens=str(base / 'tokens.txt'), encoder=str(base / 'encoder.int8.onnx'),
                    decoder=str(base / 'decoder.int8.onnx'), vocoder=str(self.models_dir / 'vocos_24khz.onnx'),
                    data_dir=str(base / 'espeak-ng-data'), lexicon=str(base / 'lexicon.txt')),
                **common)
        else:
            raise VoiceEngineError('Unsupported voice engine')
        config = sherpa_onnx.OfflineTtsConfig(model=model, rule_fsts=rule_fsts, max_num_sentences=1 if engine == 'kokoro' else 2)
        if not config.validate():
            raise VoiceEngineError('Invalid local voice model configuration')
        instance = sherpa_onnx.OfflineTts(config)
        self._models[engine] = instance
        return instance

    @staticmethod
    def read_reference(path: str | Path) -> tuple[np.ndarray, int]:
        # API code owns the allowlisted data-directory path and permission checks.
        # Reject compressed/stereo/unbounded inputs again before native inference.
        with wave.open(str(path), 'rb') as source:
            rate, frames = source.getframerate(), source.getnframes()
            if source.getnchannels() != 1 or source.getsampwidth() != 2 or source.getcomptype() != 'NONE':
                raise VoiceEngineError('Reference must be mono PCM16 WAV')
            if rate not in {16000, 24000, 48000} or not 3 <= frames / rate <= 20:
                raise VoiceEngineError('Reference must contain 3 to 20 seconds of supported-rate audio')
            pcm = source.readframes(frames)
        if len(pcm) != frames * 2:
            raise VoiceEngineError('Reference audio is incomplete')
        samples = np.frombuffer(pcm, dtype='<i2').astype(np.float32) / 32768.0
        if float(np.sqrt(np.mean(samples * samples))) < .002:
            raise VoiceEngineError('Reference audio is silent')
        return samples, rate

    def synthesize_wav(self, text: str, voice: dict | None = None) -> bytes:
        if not isinstance(text, str) or not text.strip() or len(text) > 300:
            raise VoiceEngineError('Speech text must contain 1 to 300 characters')
        profile = dict(voice) if voice else {'engine': 'melo', 'speaker_id': 0}
        engine = profile.get('engine', 'melo')
        speed = float(profile.get('speed', .95))
        if not np.isfinite(speed) or not .7 <= speed <= 1.3:
            raise VoiceEngineError('Invalid speech speed')
        tts = self.load(engine)
        if engine == 'zipvoice':
            import sherpa_onnx
            reference_text = profile.get('reference_text', '')
            if not isinstance(reference_text, str) or not reference_text.strip() or len(reference_text) > 400:
                raise VoiceEngineError('Matching reference transcript is required')
            path = profile.get('reference_audio_path')
            if not isinstance(path, (str, Path)):
                raise VoiceEngineError('Server reference audio path is required')
            reference, rate = self.read_reference(path)
            config = sherpa_onnx.GenerationConfig()
            config.reference_audio = reference
            config.reference_sample_rate = rate
            config.reference_text = reference_text
            config.num_steps = 4
            config.speed = speed
            config.extra['min_char_in_sentence'] = '30'
            audio = tts.generate(text, config)
        else:
            sid = profile.get('speaker_id', 0)
            maximum = 102 if engine == 'kokoro' else 0
            if type(sid) is not int or not 0 <= sid <= maximum:
                raise VoiceEngineError('Invalid preset speaker')
            audio = tts.generate(text, sid=sid, speed=speed)
        samples = np.asarray(audio.samples, dtype=np.float32)
        if not len(samples) or not np.isfinite(samples).all() or len(samples) > audio.sample_rate * 120:
            raise VoiceEngineError('Invalid synthesized audio')
        output = io.BytesIO()
        with wave.open(output, 'wb') as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(audio.sample_rate)
            wav.writeframes((np.clip(samples, -1, 1) * 32767).astype('<i2').tobytes())
        return output.getvalue()
