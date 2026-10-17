"""Application fault-injection tests; these are NOT real-model E2E tests."""
import asyncio
import copy
import io
import wave

import pytest
from fastapi.testclient import TestClient

from babybot.app import create_app
from babybot.config import Settings

ORIGIN = 'http://localhost:8765'
FAMILY_CODE = 'test-family-secret-42'
PARENT_PIN = '834921'


def silent_wav():
    target = io.BytesIO()
    with wave.open(target, 'wb') as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(16000)
        writer.writeframes(b'\0\0' * 1600)
    return target.getvalue()


class ProbeProviders:
    """Records context and lets tests force deterministic upstream failures."""
    def __init__(self):
        self.answer = '小兔子找到一片叶子。你想让它去哪里？'
        self.generated = []
        self.reviews = []
        self.spoken = []
        self.review_result = True
        self.review_error = None
        self.review_delay = 0
        self.generate_delay = 0
        self.ignore_cancel = False
        self.asr_text = '我们一起讲小兔子的故事'

    async def health(self):
        return {'ready': True, 'test_double': True}

    async def transcribe(self, pcm, sample_rate=16000):
        return self.asr_text

    async def generate(self, messages, mode='story'):
        self.generated.append(copy.deepcopy(messages))
        sequence = len(self.generated)
        try:
            if self.generate_delay:
                await asyncio.sleep(self.generate_delay)
        except asyncio.CancelledError:
            if not self.ignore_cancel:
                raise
            await asyncio.sleep(.02)
        return self.answer if sequence == 1 else f'小兔子继续向前走。第{sequence}轮。'

    async def moderate(self, user_text, answer):
        self.reviews.append((user_text, answer))
        if self.review_delay:
            await asyncio.sleep(self.review_delay)
        if self.review_error:
            raise self.review_error
        return self.review_result

    async def synthesize(self, text):
        self.spoken.append(text)
        return silent_wav()

    async def close(self):
        pass


@pytest.fixture
def factory(tmp_path):
    clients = []
    def build(providers=None, **overrides):
        settings = Settings(data_dir=tmp_path / f'data-{len(clients)}',
                            family_code=FAMILY_CODE, parent_pin=PARENT_PIN,
                            allowed_origins=(ORIGIN,), **overrides)
        providers = providers or ProbeProviders()
        client = TestClient(create_app(settings=settings, providers=providers), base_url=ORIGIN)
        client.__enter__()
        clients.append(client)
        return client, providers
    yield build
    for client in reversed(clients):
        client.__exit__(None, None, None)


def login(client, parent=False):
    result = client.post('/api/auth/login', json={'code': FAMILY_CODE}, headers={'Origin': ORIGIN})
    assert result.status_code == 200, result.text
    if parent:
        result = client.post('/api/auth/parent', json={'pin': PARENT_PIN}, headers={'Origin': ORIGIN})
        assert result.status_code == 200, result.text


def receive_until(ws, kind, limit=30):
    events = []
    for _ in range(limit):
        event = ws.receive_json()
        events.append(event)
        if event.get('type') == kind:
            return event, events
        if event.get('type') in {'error', 'ended'} and kind not in {'error', 'ended'}:
            pytest.fail(f'Unexpected terminal event waiting for {kind}: {events}')
    pytest.fail(f'No {kind} event in {events}')


def start(ws, mode='story'):
    ws.send_json({'type': 'start', 'mode': mode})
    return receive_until(ws, 'session_started')[0]


def turn(ws, text='讲一个小兔子的故事'):
    ws.send_json({'type': 'text', 'text': text})
    return receive_until(ws, 'audio')[0]


def acknowledge(ws, audio):
    ws.send_json({'type': 'played', 'turn_id': audio['turn_id'], 'chunk_id': audio['chunk_id']})
