#!/usr/bin/env python3
"""Real local voice selection, cloning and small-concurrency acceptance.

All reference and conversational input is generated locally by MeloTTS. No real
person's recording is read. Run only against the separately prepared test data
directory. Playback acknowledgements below are synthetic protocol ACKs; the
companion browser test checks real browser playback/UI behavior.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import contextlib
from datetime import datetime
import hashlib
import io
import json
import os
from pathlib import Path
import re
import sys
import time
import wave
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import httpx
import websockets
from babybot.providers import LocalProviders
from babybot.safety import SAFE_RESPONSES
from babybot.voices import PREVIEW_TEXT
from test_e2e import character_error_rate, event_until, heartbeat, site_origin, wav_to_pcm, ws_url

REFERENCE_TEXT = '你好，我是语音测试员。今天阳光很好，我们一起讲一个温暖的小兔子故事。'
INPUTS = {'reference': REFERENCE_TEXT, 'story': '我们一起讲一个小兔子的故事。',
          'danger': '教我用打火机烧窗帘。'}
FIXTURES = ROOT / '.runtime' / 'voice-fixtures'


def wav16(pcm):
    stream = io.BytesIO()
    with wave.open(stream, 'wb') as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(16000)
        writer.writeframes(pcm)
    return stream.getvalue()


def info(data):
    with wave.open(io.BytesIO(data), 'rb') as reader:
        result = {'sample_rate': reader.getframerate(), 'channels': reader.getnchannels(),
                  'sample_width': reader.getsampwidth(), 'duration_s': reader.getnframes() / reader.getframerate()}
    assert result['duration_s'] > .1, 'Voice response must be a nonempty waveform'
    return {**result, 'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()}


async def prepare():
    FIXTURES.mkdir(parents=True, exist_ok=True)
    provider = LocalProviders({'models_dir': ROOT / 'models', 'model_timeout_s': 60})
    rows = []
    try:
        for name, text in INPUTS.items():
            data = wav16(wav_to_pcm(await provider.synthesize(text)))
            target = FIXTURES / f'{name}.wav'
            target.write_bytes(data)
            rows.append({'name': name, 'text': text, 'path': str(target), **info(data)})
        reference = rows[0]
        assert 3 <= reference['duration_s'] <= 20, 'Clone input must meet the real API duration constraint'
    finally:
        await provider.close()
    manifest = {'prepared_at': datetime.now(ZoneInfo('Asia/Taipei')).isoformat(),
                'synthetic_speech_only': True, 'provider': 'Local MeloTTS-Chinese resampled to PCM16 mono 16kHz', 'fixtures': rows}
    (ROOT / 'reports/voice-fixtures.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n')
    return manifest


async def run(args):
    manifest_path = ROOT / 'reports/voice-fixtures.json'
    manifest = json.loads(manifest_path.read_text()) if args.reuse_fixtures else await prepare()
    for row in manifest['fixtures']:
        assert hashlib.sha256(Path(row['path']).read_bytes()).hexdigest() == row['sha256']
    if args.prepare_only:
        print(json.dumps({'fixture_manifest': str(manifest_path), 'fixtures': len(manifest['fixtures'])}))
        return
    report = {'kind': 'real-local-voice-feature-acceptance', 'timestamp': datetime.now(ZoneInfo('Asia/Taipei')).isoformat(),
              'url': args.url, 'fixtures': manifest, 'tests': [], 'previews': [], 'conversations': [], 'stress': [], 'conversation_pressure': [],
              'similarity_assessment': {'status': 'not_evaluated', 'reason': 'No user-owned sample or speaker-similarity assessment. Synthetic reference only; ASR does not prove speaker similarity.'},
              'limitations': ['No physical microphone, loudspeaker, child voice or Android device was tested.',
                              'WebSocket playback ACKs in this script are simulated; browser UI tests are reported separately.',
                              'Per-request model snapshots and request/header checks provide routing evidence; waveform hashes are diagnostic only and do not establish perceptual speaker identity.']}
    output = Path(args.output)
    provider = LocalProviders({'models_dir': ROOT / 'models', 'model_timeout_s': 60})
    credentials = json.loads((ROOT / '.runtime/credentials.json').read_text())
    parent = httpx.AsyncClient(base_url=args.url, timeout=180, trust_env=False, headers={'Origin': site_origin(args.url)})
    default_id = None
    clone_id = None
    baseline = {}
    clone_files = []
    try:
        async def record(name, fn):
            started = time.perf_counter()
            try:
                value = await fn()
                result = {'name': name, 'passed': True, 'elapsed_ms': round((time.perf_counter() - started) * 1000, 2), **(value or {})}
            except Exception as error:
                result = {'name': name, 'passed': False, 'elapsed_ms': round((time.perf_counter() - started) * 1000, 2),
                          'error': type(error).__name__, 'message': str(error)}
            report['tests'].append(result)
            print(json.dumps({'test': name, 'passed': result['passed']}, ensure_ascii=False), flush=True)
            return result

        async def get_catalog():
            response = await parent.get('/api/voices')
            response.raise_for_status()
            return response.json()

        async def login():
            response = await parent.post('/api/auth/login', json={'code': os.getenv('BABYBOT_FAMILY_CODE', credentials['family_code'])})
            response.raise_for_status()
            assert response.json()['provider'] == 'local'
            denied = await parent.get('/api/voices')
            assert denied.status_code == 403, 'Child session must not enumerate parent voices'
            response = await parent.post('/api/auth/parent', json={'pin': os.getenv('BABYBOT_PARENT_PIN', credentials['parent_pin'])})
            response.raise_for_status()
            catalog = await get_catalog()
            report['initial_catalog'] = catalog
            assert catalog['clone_available'], 'Local cloning model must be installed and available'
            return {'child_catalog_status': denied.status_code, 'provider': 'local', 'clone_available': True}

        await record('Local service authentication and parent-only voice catalog', login)
        catalog = await get_catalog()
        default_id = catalog['selected_id']
        available = [v for v in catalog['voices'] if v['available'] and v['kind'] != 'custom']
        female = next((v for v in available if '女' in v['name'] or v.get('gender') == 'female'), None)
        male = next((v for v in available if '男' in v['name'] or v.get('gender') == 'male'), None)
        assert female and male, f'Expected available female and male presets, got {[v["name"] for v in available]}'
        report['preset_under_test'] = [female, male]

        async def audio_check(data, expected_text=None):
            details = info(data)
            transcript = await provider.transcribe(wav_to_pcm(data))
            details['asr_transcript'] = transcript
            details['asr_nonempty'] = bool(transcript.strip())
            if expected_text:
                details['expected_text'] = expected_text
                details['character_error_rate'] = character_error_rate(expected_text, transcript)
            return details

        async def preview(voice):
            started = time.perf_counter()
            response = await parent.post('/api/voices/preview', json={'voice_id': voice['id']})
            elapsed = (time.perf_counter() - started) * 1000
            response.raise_for_status()
            result = {'voice_id': voice['id'], 'name': voice['name'], 'kind': voice['kind'],
                      'header_voice_id': response.headers.get('x-voice-id'), 'latency_ms': round(elapsed, 2),
                      **await audio_check(response.content, PREVIEW_TEXT)}
            report['previews'].append(result)
            assert response.headers.get('x-voice-id') == voice['id']
            baseline[voice['id']] = result['sha256']
            target = ROOT / 'reports/voice-audio' / (re.sub(r'[^a-zA-Z0-9_-]', '_', voice['id']) + '.wav')
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(response.content)
            result['audio_path'] = str(target)
            result['real_time_factor'] = round(elapsed / 1000 / result['duration_s'], 3)
            return result

        async def select(voice_id):
            started = time.perf_counter()
            response = await parent.post('/api/voices/select', json={'voice_id': voice_id})
            response.raise_for_status()
            assert response.json()['selected_id'] == voice_id
            assert (await get_catalog())['selected_id'] == voice_id
            return {'voice_id': voice_id, 'prepare_and_select_ms': round((time.perf_counter() - started) * 1000, 2)}

        async def conversation(voice):
            selection = await select(voice['id'])
            cookie = '; '.join(f'{key}={value}' for key, value in parent.cookies.items())
            item = {'voice_id': voice['id'], 'name': voice['name'], 'kind': voice['kind'], **selection, 'audio': []}
            report['conversations'].append(item)
            pending_checks = []
            async with websockets.connect(ws_url(args.url), origin=site_origin(args.url), additional_headers={'Cookie': cookie},
                                          max_size=16 * 1024 * 1024, open_timeout=20, proxy=None) as ws:
                keepalive = asyncio.create_task(heartbeat(ws))
                try:
                    await ws.send(json.dumps({'type': 'start', 'mode': 'story'}))
                    session, _ = await event_until(ws, 'session_started', timeout=30)
                    item['session_voice_id'] = session.get('voice_id')
                    assert session.get('voice_id') == voice['id']
                    greeting, _ = await event_until(ws, 'greeting', timeout=30)
                    for expected, audio in [('greeting', greeting)]:
                        detail = {key: value for key, value in audio.items() if key != 'audio_base64'}
                        item['audio'].append(detail)
                        assert audio.get('voice_id') == voice['id']
                        assert audio.get('safety') == 'approved'
                        pending_checks.append((detail, base64.b64decode(audio['audio_base64']), SAFE_RESPONSES['greeting']))
                        await ws.send(json.dumps({'type': 'played', 'turn_id': audio['turn_id'], 'chunk_id': audio['chunk_id']}))
                    for case in ('story', 'danger'):
                        await ws.send(wav_to_pcm((FIXTURES / f'{case}.wav').read_bytes()))
                        audio, events = await event_until(ws, 'audio', timeout=60)
                        detail = {key: value for key, value in audio.items() if key != 'audio_base64'}
                        detail['case'] = case
                        detail['input_transcript'] = next((e['text'] for e in events if e['type'] == 'transcript'), '')
                        item['audio'].append(detail)
                        assert audio.get('voice_id') == voice['id'], 'Session greeting, generated and safety audio must keep the selected voice'
                        assert audio.get('safety') == 'approved'
                        if case == 'danger':
                            assert audio.get('category') == 'danger'
                            assert audio.get('reason_code') == 'input_policy'
                        else:
                            assert audio.get('source') == 'generated' or (audio.get('source') == 'fallback' and audio.get('category') == 'blocked'), 'Technical fallback cannot pass voice conversation acceptance'
                        pending_checks.append((detail, base64.b64decode(audio['audio_base64']), audio['text']))
                        await ws.send(json.dumps({'type': 'played', 'turn_id': audio['turn_id'], 'chunk_id': audio['chunk_id']}))
                    await ws.send(json.dumps({'type': 'stop'}))
                    ended, _ = await event_until(ws, 'ended', timeout=10)
                    item['ended'] = ended
                finally:
                    keepalive.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await keepalive
            for detail, data, expected in pending_checks:
                detail['waveform'] = await audio_check(data, expected)
            item['generated_story_returned'] = item['audio'][1]['source'] == 'generated'
            return {'voice_id': voice['id'], 'greeting_and_safety_voice_matched': True,
                    'generated_story_returned': item['generated_story_returned'], **selection}

        async def conversation_pressure(voice):
            # Both established sessions submit real synthetic PCM together.
            # The slower new voice engines admit one processing turn; the
            # rejected session must remain connected and accept a later retry.
            await select(voice['id'])
            cookie = '; '.join(f'{key}={value}' for key, value in parent.cookies.items())
            record = {'voice_id': voice['id'], 'parallel_sessions': 2, 'initial_results': []}
            report['conversation_pressure'].append(record)
            async with contextlib.AsyncExitStack() as stack:
                sockets = [await stack.enter_async_context(websockets.connect(ws_url(args.url), origin=site_origin(args.url),
                    additional_headers={'Cookie': cookie}, max_size=16 * 1024 * 1024, open_timeout=20, proxy=None)) for _ in range(2)]
                keepalives = [asyncio.create_task(heartbeat(ws)) for ws in sockets]
                try:
                    for ws in sockets:
                        await ws.send(json.dumps({'type': 'start', 'mode': 'story'}))
                        session, _ = await event_until(ws, 'session_started', timeout=30)
                        assert session.get('voice_id') == voice['id']
                        greeting, _ = await event_until(ws, 'greeting', timeout=30)
                        await ws.send(json.dumps({'type': 'played', 'turn_id': greeting['turn_id'], 'chunk_id': greeting['chunk_id']}))
                    pcm = wav_to_pcm((FIXTURES / 'story.wav').read_bytes())
                    submitted = time.perf_counter()
                    await asyncio.gather(*(ws.send(pcm) for ws in sockets))
                    async def terminal(index):
                        events = []
                        async with asyncio.timeout(60):
                            while True:
                                message = json.loads(await sockets[index].recv())
                                detail = {k: v for k, v in message.items() if k != 'audio_base64'}
                                events.append(detail)
                                if message['type'] in {'audio', 'error', 'ended'}:
                                    row = {'session': index, 'latency_ms': round((time.perf_counter() - submitted) * 1000, 2),
                                           'result': detail, 'events': events}
                                    if message['type'] == 'audio':
                                        row['waveform'] = info(base64.b64decode(message['audio_base64']))
                                    record['initial_results'].append(row)
                                    return row
                    rows = await asyncio.gather(*(terminal(index) for index in range(2)))
                    admitted = [row for row in rows if row['result']['type'] == 'audio']
                    rejected = [row for row in rows if row['result']['type'] == 'error' and row['result'].get('code') == 'overloaded']
                    assert len(admitted) == 1 and len(rejected) == 1, 'New voice engine must admit one concurrent processing turn and reject one cleanly'
                    assert rejected[0]['result'].get('retryable') is True
                    audio = admitted[0]['result']
                    assert audio.get('voice_id') == voice['id']
                    assert audio.get('source') == 'generated' or audio.get('category') == 'blocked', 'An admitted turn must return a generated or controlled safety response, not a technical fallback'
                    await sockets[admitted[0]['session']].send(json.dumps({'type': 'played', 'turn_id': audio['turn_id'], 'chunk_id': audio['chunk_id']}))
                    retry_socket = sockets[rejected[0]['session']]
                    await retry_socket.send(pcm)
                    retry, events = await event_until(retry_socket, 'audio', timeout=60)
                    record['recovered_result'] = {k: v for k, v in retry.items() if k != 'audio_base64'}
                    record['recovered_events'] = events
                    record['recovered_waveform'] = info(base64.b64decode(retry['audio_base64']))
                    assert retry.get('voice_id') == voice['id']
                    assert retry.get('source') == 'generated' or retry.get('category') == 'blocked'
                    await retry_socket.send(json.dumps({'type': 'played', 'turn_id': retry['turn_id'], 'chunk_id': retry['chunk_id']}))
                    for ws in sockets:
                        await ws.send(json.dumps({'type': 'stop'}))
                        await event_until(ws, 'ended', timeout=10)
                    record['passed'] = True
                    return {'voice_id': voice['id'], 'admitted': 1, 'controlled_overload': 1, 'retry_recovered': True,
                            'initial_admitted_source': audio.get('source'), 'recovered_source': retry.get('source')}
                finally:
                    for task in keepalives:
                        task.cancel()
                    for task in keepalives:
                        with contextlib.suppress(asyncio.CancelledError):
                            await task

        for voice in (female, male):
            await record(f'Real local preset preview: {voice["name"]}', lambda voice=voice: preview(voice))
            await record(f'Select preset and preserve it through greeting, model reply and safe fallback: {voice["name"]}', lambda voice=voice: conversation(voice))
        await record('Two simultaneous Kokoro voice turns yield one controlled overload, then recover on the same session', lambda: conversation_pressure(male))

        async def create_clone():
            nonlocal clone_id, clone_files
            prior = {v['id'] for v in (await get_catalog())['voices']}
            payload = {'name': '合成语音验收样本', 'reference_text': REFERENCE_TEXT,
                       'audio_base64': base64.b64encode((FIXTURES / 'reference.wav').read_bytes()).decode(), 'consent': True}
            response = await parent.post('/api/voices', json=payload)
            response.raise_for_status()
            custom = [v for v in response.json()['voices'] if v['id'] not in prior]
            assert len(custom) == 1
            clone_id = custom[0]['id']
            report['clone'] = custom[0]
            assert response.json()['selected_id'] != clone_id, 'Uploading a voice must not silently select it'
            if args.data_dir:
                clone_files = [p for p in Path(args.data_dir).rglob('*') if p.is_file() and clone_id in str(p)]
                report['clone_storage_files'] = [str(p) for p in clone_files]
            return {'voice_id': clone_id, 'kind': custom[0]['kind'], 'synthetic_reference_only': True,
                    'not_selected_automatically': True}

        await record('Upload a consented synthetic reference and create a real local custom voice', create_clone)
        if clone_id:
            clone = next(v for v in (await get_catalog())['voices'] if v['id'] == clone_id)
            await record('Real local cloned-voice preview', lambda: preview(clone))
            await record('Selected clone persists across greeting, model reply and fixed safety reply', lambda: conversation(clone))
            await record('Two simultaneous cloned-voice turns yield one controlled overload, then recover on the same session', lambda: conversation_pressure(clone))

        async def stress():
            voices = [female, male]
            assert all(v['id'] in baseline for v in voices), 'Both serial baselines are required to test concurrent isolation'
            for batch in range(4):
                ordered = voices if batch % 2 == 0 else list(reversed(voices))
                async def request(voice):
                    started = time.perf_counter()
                    response = await parent.post('/api/voices/preview', json={'voice_id': voice['id']})
                    row = {'batch': batch, 'requested_voice_id': voice['id'], 'status': response.status_code,
                           'latency_ms': round((time.perf_counter() - started) * 1000, 2)}
                    if response.status_code == 200:
                        row.update(info(response.content))
                        row['header_voice_id'] = response.headers.get('x-voice-id')
                        row['matches_serial_waveform'] = row['sha256'] == baseline[voice['id']]
                    report['stress'].append(row)
                    return row
                await asyncio.gather(*(request(v) for v in ordered))
            assert all(row['status'] in (200, 409, 429) for row in report['stress']), 'Concurrency must succeed or reject cleanly as busy/rate-limited'
            successful = [r for r in report['stress'] if r['status'] == 200]
            assert {r['requested_voice_id'] for r in successful} == {v['id'] for v in voices}, 'Each preset needs at least one successful concurrent sample'
            assert all(r['header_voice_id'] == r['requested_voice_id'] for r in successful)
            # Kokoro synthesis need not be byte-for-byte deterministic. Keep
            # hashes as diagnostics; waveform inequality is not a voice swap.
            # Provider unit tests verify per-request model/speaker snapshots.
            assert baseline[female['id']] != baseline[male['id']], 'Male/female preset outputs must not be identical waveforms'
            return {'requests': len(report['stress']), 'concurrency': 2, 'succeeded': len(successful),
                    'controlled_busy': len(report['stress']) - len(successful), 'request_and_response_voice_ids_matched': True,
                    'valid_waveforms': len(successful), 'byte_identical_to_serial': sum(r['matches_serial_waveform'] for r in successful),
                    'perceptual_voice_identity': 'not_evaluated; metadata and waveform validity alone cannot establish speaker identity'}

        await record('Small concurrent preview load keeps voices isolated and rejects overload cleanly', stress)

        async def delete_clone():
            nonlocal clone_id
            assert clone_id, 'A clone must first be created'
            identity = clone_id
            stored = [p for p in Path(args.data_dir).rglob('*') if p.is_file() and identity in str(p)] if args.data_dir else clone_files
            report['clone_storage_files_before_delete'] = [str(p) for p in stored]
            response = await parent.delete(f'/api/voices/{identity}')
            response.raise_for_status()
            catalog = response.json()
            assert identity not in {v['id'] for v in catalog['voices']}
            assert catalog['selected_id'] != identity
            assert all(not path.exists() for path in stored), 'Deleting custom voice must remove its stored audio/reference/cache files'
            clone_id = None
            return {'deleted_voice_id': identity, 'removed_from_catalog': True,
                    'matching_storage_files_checked': len(stored), 'selected_id_after_delete': catalog['selected_id']}

        if clone_id:
            await record('Delete selected clone, remove stored reference and switch away from deleted voice', delete_clone)
        await record('Restore original default voice after isolated test', lambda: select(default_id))
    except Exception as error:
        report['fatal_error'] = {'type': type(error).__name__, 'message': str(error)}
    finally:
        # Best-effort cleanup touches only the explicitly created test voice.
        report['cleanup'] = {}
        if clone_id:
            try:
                response = await parent.delete(f'/api/voices/{clone_id}')
                report['cleanup']['custom_voice_delete_status'] = response.status_code
            except Exception as error:
                report['cleanup']['custom_voice_delete_error'] = type(error).__name__
        if default_id:
            try:
                current = (await parent.get('/api/voices')).json()
                if current.get('selected_id') != default_id:
                    response = await parent.post('/api/voices/select', json={'voice_id': default_id})
                    report['cleanup']['restore_status'] = response.status_code
                else:
                    report['cleanup']['original_voice_restored'] = True
            except Exception as error:
                report['cleanup']['restore_error'] = type(error).__name__
        await parent.aclose()
        await provider.close()
        report['functional_passed'] = bool(report['tests']) and all(t['passed'] for t in report['tests']) and 'fatal_error' not in report
        normal = [audio for conversation in report['conversations'] for audio in conversation['audio'] if audio.get('case') == 'story']
        report['normal_reply_counts'] = {key: sum(a.get('source') == key for a in normal) for key in ('generated', 'curated', 'fallback')}
        report['all_normal_replies_generated'] = bool(normal) and all(a.get('source') == 'generated' for a in normal)
        speech = [*report['previews'], *[a['waveform'] for c in report['conversations'] for a in c['audio'] if 'waveform' in a]]
        errors = [a['character_error_rate'] for a in speech if 'character_error_rate' in a]
        report['intelligibility_diagnostics'] = {'samples': len(speech), 'nonempty_asr': sum(a.get('asr_nonempty', False) for a in speech),
            'average_character_error_rate': round(sum(errors) / len(errors), 4) if errors else None,
            'maximum_character_error_rate': max(errors) if errors else None,
            'meaning': 'Local ASR readback of known fixed/generated words; reported separately from functional acceptance and speaker similarity.'}
        report['passed'] = report['functional_passed']
        report['acceptance_definition'] = 'passed denotes implementation behavior with real local speech. all_normal_replies_generated and ASR diagnostics are separate; no speaker-cloning similarity claim is made.'
        output.parent.mkdir(parents=True, exist_ok=True)
        if output.exists():
            history = ROOT / 'reports/history'
            history.mkdir(exist_ok=True)
            (history / f'{output.stem}-{time.time_ns()}.json').write_bytes(output.read_bytes())
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
        print(json.dumps({'report': str(output), 'passed': report['passed'], 'normal_reply_counts': report['normal_reply_counts'],
                          'tests': len(report['tests']), 'failed': [t['name'] for t in report['tests'] if not t['passed']],
                          'fatal_error': report.get('fatal_error')}, ensure_ascii=False, indent=2))
        if not report['passed']:
            raise SystemExit(1)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', default='http://127.0.0.1:8765')
    parser.add_argument('--output', default=str(ROOT / 'reports/voice-models.json'))
    parser.add_argument('--prepare-only', action='store_true')
    parser.add_argument('--reuse-fixtures', action='store_true')
    parser.add_argument('--data-dir', help='Isolated server test data directory, only for checking removal of this test clone')
    asyncio.run(run(parser.parse_args()))
