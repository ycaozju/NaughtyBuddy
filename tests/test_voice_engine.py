"""Voice-selection isolation and bounded local reference decoding."""
import asyncio
import io
import json
from types import SimpleNamespace
import wave

import numpy as np
import pytest

from babybot.providers import LocalProviders
from babybot.voice_engine import VoiceEngine, VoiceEngineError, engine_available


@pytest.mark.asyncio
async def test_queued_synthesis_uses_each_requests_snapshot_not_mutable_selection(tmp_path):
    provider = LocalProviders({'models_dir': tmp_path})
    ready = asyncio.Event()
    release = asyncio.Event()
    queued = []

    async def native(name, function, text, voice):
        queued.append(voice)
        if len(queued) == 2:
            ready.set()
        await release.wait()
        return json.dumps(voice).encode()

    provider._native = native
    first = {'engine': 'kokoro', 'speaker_id': 3, 'metadata': {'label': 'first'}}
    second = {'engine': 'kokoro', 'speaker_id': 58}
    try:
        first_task = asyncio.create_task(provider.synthesize('第一句话。', first))
        second_task = asyncio.create_task(provider.synthesize('第二句话。', second))
        await asyncio.wait_for(ready.wait(), 1)
        first['speaker_id'] = 74
        first['metadata']['label'] = 'changed'
        second['speaker_id'] = 20
        release.set()
        a, b = [json.loads(result) for result in await asyncio.gather(first_task, second_task)]
        assert a['speaker_id'] == 3 and a['metadata']['label'] == 'first'
        assert b['speaker_id'] == 58
    finally:
        release.set()
        await provider.close()


def test_preset_sid_is_passed_per_call_and_output_is_pcm16_wav(tmp_path):
    calls = []

    class FakeModel:
        def generate(self, text, sid, speed):
            calls.append((sid, speed))
            return SimpleNamespace(samples=np.array([.2, -.2] * 100, dtype=np.float32), sample_rate=24000)

    engine = VoiceEngine(tmp_path)
    engine._models['kokoro'] = FakeModel()
    for sid in (3, 58, 3):
        data = engine.synthesize_wav('你好。', {'engine': 'kokoro', 'speaker_id': sid})
        with wave.open(io.BytesIO(data)) as wav:
            assert (wav.getnchannels(), wav.getsampwidth(), wav.getframerate()) == (1, 2, 24000)
    assert calls == [(3, .95), (58, .95), (3, .95)]


@pytest.mark.parametrize(('seconds', 'channels', 'silent', 'accepted'), [
    (3, 1, False, True), (20, 1, False, True), (2.9, 1, False, False),
    (20.1, 1, False, False), (4, 2, False, False), (4, 1, True, False),
])
def test_reference_audio_boundaries_are_checked_before_native_inference(tmp_path, seconds, channels, silent, accepted):
    path = tmp_path / 'synthetic.wav'
    count = int(seconds * 16000)
    samples = np.zeros(count * channels, dtype='<i2') if silent else (np.sin(np.arange(count * channels) * .1) * 4000).astype('<i2')
    with wave.open(str(path), 'wb') as wav:
        wav.setnchannels(channels); wav.setsampwidth(2); wav.setframerate(16000)
        wav.writeframes(samples.tobytes())
    if accepted:
        decoded, rate = VoiceEngine.read_reference(path)
        assert len(decoded) == count and rate == 16000
    else:
        with pytest.raises(VoiceEngineError):
            VoiceEngine.read_reference(path)


def test_missing_or_unknown_engine_is_never_advertised_available(tmp_path):
    assert not engine_available(tmp_path, 'kokoro')
    assert not engine_available(tmp_path, 'zipvoice')
    assert not engine_available(tmp_path, 'remote-url')
    with pytest.raises(VoiceEngineError):
        VoiceEngine(tmp_path).synthesize_wav('你好。', {'engine': 'remote-url'})
