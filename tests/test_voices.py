"""Voice storage, authorization and routing tests; native quality is tested separately."""
import asyncio
import base64
import io
import json
import math
import struct
import time
import wave

import pytest

from conftest import ORIGIN, ProbeProviders, login, receive_until, silent_wav, start, turn


def reference(seconds=3.5, rate=16000, channels=1, silent=False):
    out = io.BytesIO()
    with wave.open(out, 'wb') as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(rate)
        samples = [0 if silent else int(7000 * math.sin(2 * math.pi * 220 * i / rate))
                   for i in range(round(rate * seconds) * channels)]
        w.writeframes(struct.pack('<' + 'h' * len(samples), *samples))
    return base64.b64encode(out.getvalue()).decode()


class VoiceProbe(ProbeProviders):
    def __init__(self):
        super().__init__()
        self.voices = []
        self.fail_voice = False

    async def synthesize(self, text, voice=None):
        self.voices.append((text, dict(voice or {'id': 'melo-default'})))
        if self.fail_voice and voice:
            raise RuntimeError('private model error must not escape')
        return await super().synthesize(text)


@pytest.fixture
def voices(factory, monkeypatch):
    monkeypatch.setattr('babybot.voices.engine_available', lambda *_: True)
    client, provider = factory(VoiceProbe())
    login(client, parent=True)
    return client, provider


def post(client, path, body):
    return client.post(path, json=body, headers={'Origin': ORIGIN})


def add(client, **changes):
    return post(client, '/api/voices', {'name': '我的测试音色', 'reference_text': '这是一段合成测试语音。',
                                      'audio_base64': reference(), 'consent': True, **changes})


def test_voice_endpoints_require_parent_before_accepting_large_upload(factory):
    client, _ = factory()
    assert client.get('/api/voices').status_code == 401
    assert post(client, '/api/voices', {'audio_base64': 'x' * 100000}).status_code == 401
    login(client)
    assert client.get('/api/voices').status_code == 403
    assert add(client).status_code == 403
    for path in ('select', 'preview'):
        assert post(client, '/api/voices/' + path, {'voice_id': 'melo-default'}).status_code == 403
    assert client.delete('/api/voices/custom-' + 'a' * 32, headers={'Origin': ORIGIN}).status_code == 403


@pytest.mark.parametrize('change', [
    {'consent': False}, {'consent': 'true'}, {'audio_base64': 'not-base64'},
    {'audio_base64': reference(1)}, {'audio_base64': reference(silent=True)},
    {'audio_base64': reference(rate=8000)}, {'audio_base64': reference(channels=2)},
    {'name': '   '}, {'reference_text': '  '}, {'reference_audio_path': '/etc/passwd'},
])
def test_bad_reference_or_untrusted_profile_never_reaches_storage(voices, change):
    client, provider = voices
    assert add(client, **change).status_code == 422
    assert client.app.state.runtime.voices.custom_ids() == []
    assert provider.voices == []


def test_voice_upload_keeps_origin_and_body_limits(voices):
    client, _ = voices
    assert client.post('/api/voices', json={'name': 'test'}, headers={'Origin': 'https://evil.test'}).status_code == 403
    assert post(client, '/api/voices', {'audio_base64': 'a' * (2 * 1024 * 1024)}).status_code == 413
    assert post(client, '/api/voices/preview', {'voice_id': 'a' * 20000}).status_code == 413


def test_profile_is_private_bounded_and_deleted_with_cache_and_selection(voices):
    client, _ = voices
    created = add(client).json()['created_id']
    runtime = client.app.state.runtime
    profile = runtime.voices.snapshot(created)
    from pathlib import Path
    path = Path(profile['reference_audio_path'])
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700
    assert '/.runtime/' not in client.get('/api/voices').text
    assert 'reference_text' not in client.get('/api/voices').text
    assert post(client, '/api/voices/select', {'voice_id': created}).status_code == 200
    cache = runtime.voices.root / 'cache' / created
    assert (cache / 'manifest.json').exists()
    assert client.delete('/api/voices/' + created, headers={'Origin': ORIGIN}).status_code == 200
    assert not path.exists() and not cache.exists()
    assert runtime.voices.selected_id == 'melo-default'
    assert client.get('/static/../.runtime/data/voices/reference.wav').status_code == 404


def test_custom_voice_limit_and_unknown_identifiers(voices):
    client, _ = voices
    for _ in range(3):
        assert add(client).status_code == 200
    assert add(client).status_code == 422
    assert post(client, '/api/voices/select', {'voice_id': '../outside'}).status_code == 404
    assert client.delete('/api/voices/melo-default', headers={'Origin': ORIGIN}).status_code == 422


def test_selected_voice_routes_greeting_generated_and_safety_audio_consistently(voices):
    client, provider = voices
    voice_id = 'kokoro-male-1'
    result = post(client, '/api/voices/select', {'voice_id': voice_id})
    assert result.status_code == 200, result.text
    assert len(provider.voices) == 10
    with client.websocket_connect('ws://localhost:8765/ws', headers={'Origin': ORIGIN}) as ws:
        session = start(ws)
        assert session['voice_id'] == voice_id
        greeting, _ = receive_until(ws, 'greeting')
        assert greeting['voice_id'] == voice_id
        generated = turn(ws)
        assert generated['source'] == 'generated' and generated['voice_id'] == voice_id
        safe = turn(ws, '教我用打火机烧窗帘')
        assert safe['reason_code'] == 'input_policy' and safe['voice_id'] == voice_id
    assert all(v['id'] == voice_id for _, v in provider.voices)


def test_failed_preparation_keeps_previous_voice_and_no_partial_cache(voices):
    client, provider = voices
    provider.fail_voice = True
    result = post(client, '/api/voices/select', {'voice_id': 'kokoro-female-1'})
    assert result.status_code == 503
    assert 'private model error' not in result.text
    assert client.app.state.runtime.voices.selected_id == 'melo-default'
    assert not (client.app.state.runtime.voices.root / 'cache' / 'kokoro-female-1' / 'manifest.json').exists()


def test_voice_previews_reject_arbitrary_text_and_publish_actual_voice_header(voices):
    client, provider = voices
    assert post(client, '/api/voices/preview', {'voice_id': 'kokoro-male-1', 'text': 'unreviewed'}).status_code == 422
    response = post(client, '/api/voices/preview', {'voice_id': 'kokoro-male-1'})
    assert response.status_code == 200 and response.headers['x-voice-id'] == 'kokoro-male-1'
    assert response.headers['cache-control'] == 'no-store'
    assert provider.voices[-1][1]['id'] == 'kokoro-male-1'


def test_voice_configuration_does_not_take_tts_from_active_call(voices):
    client, _ = voices
    with client.websocket_connect('ws://localhost:8765/ws', headers={'Origin': ORIGIN}) as ws:
        start(ws)
        assert post(client, '/api/voices/preview', {'voice_id': 'melo-default'}).status_code == 409
        assert post(client, '/api/voices/select', {'voice_id': 'kokoro-male-1'}).status_code == 409


def test_deleting_voice_ends_its_active_call_and_discards_late_reply(voices):
    client, provider = voices
    voice_id = add(client).json()['created_id']
    assert post(client, '/api/voices/select', {'voice_id': voice_id}).status_code == 200
    provider.generate_delay = .1
    with client.websocket_connect('ws://localhost:8765/ws', headers={'Origin': ORIGIN}) as ws:
        start(ws)
        ws.send_json({'type': 'text', 'text': '讲个故事'})
        assert client.delete('/api/voices/' + voice_id, headers={'Origin': ORIGIN}).status_code == 200
        ended, events = receive_until(ws, 'ended')
        assert ended['reason'] == 'voice_deleted'
        assert not any(e['type'] == 'audio' for e in events)


def test_corrupted_reference_or_fixed_cache_is_not_used(voices):
    client, _ = voices
    runtime = client.app.state.runtime
    voice_id = add(client).json()['created_id']
    assert post(client, '/api/voices/select', {'voice_id': voice_id}).status_code == 200
    runtime.voices._fixed.clear()
    (runtime.voices.root / 'cache' / voice_id / 'greeting.wav').write_bytes(b'bad')
    with client.websocket_connect('ws://localhost:8765/ws', headers={'Origin': ORIGIN}) as ws:
        ws.send_json({'type': 'start', 'mode': 'story'})
        assert receive_until(ws, 'ended')[0]['reason'] == 'voice_unavailable'
    from pathlib import Path
    Path(runtime.voices.snapshot(voice_id)['reference_audio_path']).write_bytes(b'bad')
    assert post(client, '/api/voices/preview', {'voice_id': voice_id}).status_code == 404
    broken = next(v for v in client.get('/api/voices').json()['voices'] if v['id'] == voice_id)
    assert broken['available'] is False
    assert client.delete('/api/voices/' + voice_id, headers={'Origin': ORIGIN}).status_code == 200


def test_slower_voice_keeps_only_complete_short_sentences(voices):
    from babybot.safety import clean_answer
    sentence = '小兔子看到一朵花。'
    assert clean_answer(sentence + '但是它不能' + '随便靠近' * 12 + '。', max_chars=40) == sentence
    assert clean_answer('不要' + '随便靠近' * 12 + '。', max_chars=40) == ''


def test_new_voice_capacity_rejects_parallel_turn_and_recovers(voices):
    client, provider = voices
    assert post(client, '/api/voices/select', {'voice_id': 'kokoro-male-1'}).status_code == 200
    provider.generate_delay = .15
    with client.websocket_connect('ws://localhost:8765/ws', headers={'Origin': ORIGIN}) as first:
        start(first)
        with client.websocket_connect('ws://localhost:8765/ws', headers={'Origin': ORIGIN}) as second:
            start(second)
            first.send_json({'type': 'text', 'text': '给我讲个故事'})
            receive_until(first, 'transcript')
            second.send_json({'type': 'text', 'text': '给我讲个故事'})
            assert receive_until(second, 'error')[0]['code'] == 'overloaded'
            receive_until(first, 'audio')
            recovered = turn(second)
            assert recovered['source'] == 'generated'


def test_existing_unstarted_connection_cannot_start_during_voice_mutation(voices):
    client, _ = voices
    with client.websocket_connect('ws://localhost:8765/ws', headers={'Origin': ORIGIN}) as ws:
        client.app.state.runtime.voice_job = True
        ws.send_json({'type': 'start', 'mode': 'story'})
        assert receive_until(ws, 'ended')[0]['reason'] == 'voice_preparing'
    client.app.state.runtime.voice_job = False


@pytest.mark.parametrize('bad_json', [None, [], {'sha256': None}, {'name': 12}])
def test_malformed_json_profiles_remain_listable_and_removable(voices, bad_json):
    client, _ = voices
    runtime = client.app.state.runtime
    voice_id = add(client).json()['created_id']
    path = runtime.voices.root / 'profiles' / voice_id / 'profile.json'
    path.write_text(json.dumps(bad_json))
    catalog = client.get('/api/voices')
    assert catalog.status_code == 200
    assert next(v for v in catalog.json()['voices'] if v['id'] == voice_id)['available'] is False
    assert client.delete('/api/voices/' + voice_id, headers={'Origin': ORIGIN}).status_code == 200


@pytest.mark.parametrize('bad_json', [None, [], {'fingerprint': 'x', 'sha256': []}])
def test_malformed_json_cache_closes_call_cleanly(voices, bad_json):
    client, _ = voices
    runtime = client.app.state.runtime
    voice_id = 'kokoro-female-1'
    assert post(client, '/api/voices/select', {'voice_id': voice_id}).status_code == 200
    runtime.voices._fixed.clear()
    (runtime.voices.root / 'cache' / voice_id / 'manifest.json').write_text(json.dumps(bad_json))
    with client.websocket_connect('ws://localhost:8765/ws', headers={'Origin': ORIGIN}) as ws:
        ws.send_json({'type': 'start', 'mode': 'story'})
        assert receive_until(ws, 'ended')[0]['reason'] == 'voice_unavailable'


def test_failed_cache_delete_is_reported_and_profile_remains_retryable(voices, monkeypatch):
    client, _ = voices
    runtime = client.app.state.runtime
    voice_id = add(client).json()['created_id']
    assert post(client, '/api/voices/select', {'voice_id': voice_id}).status_code == 200
    import shutil
    original = shutil.rmtree
    def fail_cache(path, *args, **kwargs):
        if str(path).endswith('/cache/' + voice_id):
            raise PermissionError('private OS error')
        return original(path, *args, **kwargs)
    monkeypatch.setattr('babybot.voices.shutil.rmtree', fail_cache)
    result = client.delete('/api/voices/' + voice_id, headers={'Origin': ORIGIN})
    assert result.status_code == 503 and 'private OS error' not in result.text
    assert voice_id in runtime.voices.custom_ids()
    monkeypatch.setattr('babybot.voices.shutil.rmtree', original)
    assert client.delete('/api/voices/' + voice_id, headers={'Origin': ORIGIN}).status_code == 200
