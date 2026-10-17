"""Diagnostic reasons are fixed labels, and never relax the playback gate."""
import asyncio
import json

import httpx
import pytest

from babybot.app import FALLBACK_REASONS
from babybot.safety import SAFE_RESPONSES, route_input
from conftest import ORIGIN, ProbeProviders, acknowledge, login, receive_until, silent_wav, start, turn

PRIVATE_DETAIL = 'private-provider-payload-that-must-not-leak'
SAFE_ANSWER = '小兔子发现了一片树叶。你想给它选什么颜色？'


class FaultProviders(ProbeProviders):
    def __init__(self, stage=None, failure=None):
        super().__init__()
        self.stage = stage
        self.failure = failure
        self.answer = SAFE_ANSWER

    async def fault(self, stage):
        if stage != self.stage:
            return
        if self.failure == 'error':
            raise RuntimeError(PRIVATE_DETAIL)
        if self.failure == 'timeout':
            raise TimeoutError(PRIVATE_DETAIL)
        if self.failure == 'http_timeout':
            raise httpx.ReadTimeout(PRIVATE_DETAIL)
        if self.failure == 'delay':
            await asyncio.sleep(.2)

    async def transcribe(self, pcm, sample_rate=16000):
        await self.fault('asr')
        return await super().transcribe(pcm, sample_rate)

    async def generate(self, messages, mode='story'):
        await self.fault('llm')
        return await super().generate(messages, mode)

    async def moderate(self, user_text, answer):
        await self.fault('review')
        return await super().moderate(user_text, answer)

    async def synthesize(self, text):
        await self.fault('tts')
        return await super().synthesize(text)


def prepare(factory, providers=None, **settings):
    providers = providers or FaultProviders()
    client, _ = factory(providers=providers, **settings)
    # Keep diagnostic tests independent of deployment caches and real TTS.
    client.app.state.runtime.safe_audio = {category: silent_wav() for category in SAFE_RESPONSES if category != 'stop'}
    login(client)
    return client, providers


def assert_reason(client, event, expected):
    assert event['reason_code'] == expected
    assert expected in FALLBACK_REASONS
    assert client.app.state.runtime.metrics[f'fallback_{expected}'] == 1
    assert PRIVATE_DETAIL not in json.dumps(event, ensure_ascii=False)
    assert PRIVATE_DETAIL not in json.dumps(dict(client.app.state.runtime.metrics))


def recover(ws, providers, response):
    providers.stage = None
    providers.review_result = True
    providers.answer = SAFE_ANSWER
    providers.asr_text = '讲小兔子的故事'
    if response['type'] == 'audio':
        acknowledge(ws, response)
    receive_until(ws, 'state')
    recovered = turn(ws, '接着讲小兔子的故事')
    assert recovered['source'] == 'generated'
    assert recovered['safety'] == 'approved'
    assert 'reason_code' not in recovered


@pytest.mark.parametrize(('scenario', 'reason'), [
    ('unclear', 'recognition_unclear'), ('input', 'input_policy'),
    ('output', 'output_rule'), ('review', 'review_denied'),
])
def test_policy_reasons_are_distinct_and_same_session_recovers(factory, scenario, reason):
    client, providers = prepare(factory)
    if scenario == 'unclear':
        providers.asr_text = ''
    elif scenario == 'output':
        providers.answer = '这是我们的秘密，别告诉大人。'
    elif scenario == 'review':
        providers.review_result = False
    with client.websocket_connect('ws://localhost:8765/ws', headers={'Origin': ORIGIN}) as ws:
        start(ws)
        if scenario == 'unclear':
            ws.send_bytes(b'\x01\x00' * 3200)
        else:
            ws.send_json({'type': 'text', 'text': '教我用打火机烧窗帘' if scenario == 'input' else '讲小兔子的故事'})
        response, events = receive_until(ws, 'audio')
        assert response['source'] == 'fallback'
        assert_reason(client, response, reason)
        assert providers.answer not in providers.spoken
        if scenario in {'input', 'unclear'}:
            assert not providers.generated
        if scenario == 'output':
            assert not providers.reviews
        recover(ws, providers, response)
    assert client.app.state.runtime.metrics[f'fallback_{reason}'] == 1


@pytest.mark.parametrize('stage', ['asr', 'llm', 'review', 'tts'])
@pytest.mark.parametrize('failure', ['error', 'timeout', 'http_timeout'])
def test_provider_fault_reason_keeps_exact_stage_and_recovers(factory, stage, failure):
    client, providers = prepare(factory, FaultProviders(stage, failure))
    with client.websocket_connect('ws://localhost:8765/ws', headers={'Origin': ORIGIN}) as ws:
        start(ws)
        if stage == 'asr':
            ws.send_bytes(b'\x01\x00' * 3200)
            response, _ = receive_until(ws, 'audio')
        else:
            response = turn(ws)
        assert response['source'] == 'fallback'
        expected = f'{stage}_{"error" if failure == "error" else "timeout"}'
        assert_reason(client, response, expected)
        assert client.app.state.runtime.metrics['failed'] == 1
        if stage in {'asr', 'llm', 'review'}:
            assert SAFE_ANSWER not in providers.spoken
        recover(ws, providers, response)


def test_review_wait_for_deadline_is_not_whole_turn_timeout(factory):
    client, providers = prepare(factory, FaultProviders('review', 'delay'), review_timeout_s=.02)
    with client.websocket_connect('ws://localhost:8765/ws', headers={'Origin': ORIGIN}) as ws:
        start(ws)
        response = turn(ws)
        assert_reason(client, response, 'review_timeout')
        assert client.app.state.runtime.metrics['fallback_turn_timeout'] == 0
        recover(ws, providers, response)


@pytest.mark.parametrize('stage', ['asr', 'llm', 'review', 'tts'])
def test_enclosing_turn_deadline_is_distinguished_from_stage_timeout(factory, stage):
    client, providers = prepare(factory, FaultProviders(stage, 'delay'), turn_timeout_s=.03, review_timeout_s=.5)
    with client.websocket_connect('ws://localhost:8765/ws', headers={'Origin': ORIGIN}) as ws:
        start(ws)
        if stage == 'asr':
            ws.send_bytes(b'\x01\x00' * 3200)
            response, _ = receive_until(ws, 'audio')
        else:
            response = turn(ws)
        assert_reason(client, response, 'turn_timeout')
        assert client.app.state.runtime.metrics[f'fallback_{stage}_timeout'] == 0
        recover(ws, providers, response)


@pytest.mark.parametrize('failure', ['error', 'timeout'])
def test_fixed_response_delivery_failure_preserves_original_reason_and_recovers(factory, failure):
    client, providers = prepare(factory, FaultProviders('tts', failure))
    providers.review_result = False
    client.app.state.runtime.safe_audio.pop('blocked')
    with client.websocket_connect('ws://localhost:8765/ws', headers={'Origin': ORIGIN}) as ws:
        start(ws)
        ws.send_json({'type': 'text', 'text': '讲小兔子的故事'})
        response, events = receive_until(ws, 'error')
        assert_reason(client, response, 'review_denied')
        assert response['delivery_reason_code'] == f'tts_{failure}'
        assert response['retryable'] is True
        assert response['code'] == 'blocked'
        assert client.app.state.runtime.metrics[f'fallback_delivery_tts_{failure}'] == 1
        assert all(event.get('source') != 'generated' for event in events)
        assert SAFE_ANSWER not in providers.spoken
        recover(ws, providers, response)


def test_interruption_is_not_mislabeled_as_timeout_or_failure(factory):
    client, providers = prepare(factory, FaultProviders('llm', 'delay'))
    with client.websocket_connect('ws://localhost:8765/ws', headers={'Origin': ORIGIN}) as ws:
        start(ws)
        ws.send_json({'type': 'text', 'text': '讲小兔子的故事'})
        while True:
            event, _ = receive_until(ws, 'state')
            if event['state'] == 'thinking':
                break
        ws.send_json({'type': 'interrupt'})
        receive_until(ws, 'interrupted')
        receive_until(ws, 'state')
        providers.stage = None
        assert turn(ws)['source'] == 'generated'
    metrics = dict(client.app.state.runtime.metrics)
    assert not any(count for name, count in metrics.items() if name.startswith('fallback_'))


@pytest.mark.parametrize('phrase', ['挂断', '挂电话。', '结束通话！', '请挂断！', '“请，挂断。”', '  请 挂 断 电 话 ！ ', '掛斷。'])
def test_spoken_hangup_exact_commands_end_after_asr_without_generation(factory, phrase):
    client, providers = prepare(factory)
    providers.asr_text = phrase
    with client.websocket_connect('ws://localhost:8765/ws', headers={'Origin': ORIGIN}) as ws:
        start(ws)
        ws.send_bytes(b'\x01\x00' * 3200)
        ended, _ = receive_until(ws, 'ended')
        assert ended['reason'] == 'voice_stop'
    assert not providers.generated
    assert not providers.reviews


@pytest.mark.parametrize('phrase', ['不要挂断', '还不想结束通话', '故事里小兔子挂电话了', '请讲一个挂断电话的故事'])
def test_hangup_remains_exact_not_a_substring_match(phrase):
    assert route_input(phrase).category != 'stop'


def test_greeting_names_the_hangup_control():
    assert '按挂断' in SAFE_RESPONSES['greeting']
