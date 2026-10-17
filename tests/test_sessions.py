import time

import pytest

from conftest import ORIGIN, acknowledge, login, receive_until, start, turn


def assistant_messages(messages):
    return [message['content'] for message in messages if message['role'] == 'assistant']


def test_unplayed_reply_is_not_claimed_as_heard(factory):
    client, providers = factory()
    login(client)
    with client.websocket_connect('ws://localhost:8765/ws', headers={'Origin': ORIGIN}) as ws:
        start(ws)
        first = turn(ws)
        turn(ws, '接着呢')
    assert first['text'] not in assistant_messages(providers.generated[-1])


def test_valid_playback_ack_commits_once(factory):
    client, providers = factory()
    login(client)
    with client.websocket_connect('ws://localhost:8765/ws', headers={'Origin': ORIGIN}) as ws:
        start(ws)
        first = turn(ws)
        acknowledge(ws, first)
        acknowledge(ws, first)
        turn(ws, '接着呢')
    assert assistant_messages(providers.generated[-1]).count(first['text']) == 1


def test_wrong_turn_or_chunk_ack_does_not_commit_reply(factory):
    client, providers = factory()
    login(client)
    with client.websocket_connect('ws://localhost:8765/ws', headers={'Origin': ORIGIN}) as ws:
        start(ws)
        first = turn(ws)
        ws.send_json({'type': 'played', 'turn_id': 'old-turn', 'chunk_id': first['chunk_id']})
        ws.send_json({'type': 'played', 'turn_id': first['turn_id'], 'chunk_id': 'wrong-chunk'})
        turn(ws, '接着呢')
    assert first['text'] not in assistant_messages(providers.generated[-1])


def test_late_upstream_result_after_interrupt_cannot_play(factory):
    client, providers = factory()
    providers.generate_delay = .15
    providers.ignore_cancel = True
    login(client)
    with client.websocket_connect('ws://localhost:8765/ws', headers={'Origin': ORIGIN}) as ws:
        start(ws)
        ws.send_json({'type': 'text', 'text': '旧故事'})
        # Allow upstream to start; the fake deliberately returns after cancellation.
        time.sleep(.04)
        ws.send_json({'type': 'interrupt'})
        ws.send_json({'type': 'text', 'text': '新故事'})
        audio, events = receive_until(ws, 'audio')
        assert audio['text'] != providers.answer, events
        assert all(event.get('text') != providers.answer for event in events if event.get('type') == 'audio')


def test_stop_ends_session_and_discards_delayed_reply(factory):
    client, providers = factory()
    providers.generate_delay = .15
    login(client)
    with client.websocket_connect('ws://localhost:8765/ws', headers={'Origin': ORIGIN}) as ws:
        start(ws)
        ws.send_json({'type': 'text', 'text': '讲个故事'})
        ws.send_json({'type': 'stop'})
        ended, events = receive_until(ws, 'ended')
        assert all(event.get('type') != 'audio' for event in events)


def test_sessions_do_not_share_conversation_context(factory):
    client, providers = factory()
    login(client)
    with client.websocket_connect('ws://localhost:8765/ws', headers={'Origin': ORIGIN}) as ws:
        start(ws)
        first = turn(ws, '我给兔子起名叫阿布达卡拉')
        acknowledge(ws, first)
        ws.send_json({'type': 'stop'})
        receive_until(ws, 'ended')
    with client.websocket_connect('ws://localhost:8765/ws', headers={'Origin': ORIGIN}) as ws:
        start(ws)
        turn(ws, '小兔子叫什么名字')
    assert '阿布达卡拉' not in str(providers.generated[-1])


@pytest.mark.parametrize('utterance', ['停一下', '停止。', '不玩了。', '再见！'])
def test_voice_stop_ends_without_self_cancellation(factory, utterance):
    client, providers = factory()
    login(client)
    with client.websocket_connect('ws://localhost:8765/ws', headers={'Origin': ORIGIN}) as ws:
        start(ws)
        ws.send_json({'type': 'text', 'text': utterance})
        ended, _ = receive_until(ws, 'ended')
        assert ended['reason'] == 'voice_stop'
    assert not providers.generated


def test_inflight_overload_is_explicit_and_recovers(factory):
    client, providers = factory(max_connections=2, max_inflight=1)
    providers.generate_delay = .12
    login(client)
    with client.websocket_connect('ws://localhost:8765/ws', headers={'Origin': ORIGIN}) as first:
        start(first)
        with client.websocket_connect('ws://localhost:8765/ws', headers={'Origin': ORIGIN}) as second:
            start(second)
            first.send_json({'type': 'text', 'text': '讲兔子的故事'})
            time.sleep(.03)
            second.send_json({'type': 'text', 'text': '讲小猫的故事'})
            error, _ = receive_until(second, 'error')
            assert error['code'] == 'overloaded'
            audio, _ = receive_until(first, 'audio')
            acknowledge(first, audio)
            recovered = turn(second, '讲小猫的故事')
            assert recovered['source'] == 'generated'
    assert client.app.state.runtime.inflight == 0


def test_connection_limit_rejects_extra_and_frees_slot(factory):
    client, _ = factory(max_connections=1, max_inflight=1)
    login(client)
    with client.websocket_connect('ws://localhost:8765/ws', headers={'Origin': ORIGIN}) as first:
        start(first)
        with client.websocket_connect('ws://localhost:8765/ws', headers={'Origin': ORIGIN}) as second:
            error, _ = receive_until(second, 'error')
            assert error['code'] == 'overloaded'
    with client.websocket_connect('ws://localhost:8765/ws', headers={'Origin': ORIGIN}) as recovered:
        assert start(recovered)['type'] == 'session_started'


def test_lost_heartbeat_closes_session(factory):
    client, _ = factory(heartbeat_timeout_s=.05)
    login(client)
    with client.websocket_connect('ws://localhost:8765/ws', headers={'Origin': ORIGIN}) as ws:
        start(ws)
        ended, _ = receive_until(ws, 'ended')
        assert ended['reason'] == 'heartbeat_timeout'


def test_daily_limit_blocks_start_before_generation(factory):
    client, providers = factory()
    login(client)
    client.app.state.runtime.store.charge(20 * 60)
    with client.websocket_connect('ws://localhost:8765/ws', headers={'Origin': ORIGIN}) as ws:
        ws.send_json({'type': 'start', 'mode': 'story'})
        ended, _ = receive_until(ws, 'ended')
        assert ended['reason'] == 'daily_limit'
    assert not providers.generated


def test_elapsed_usage_is_committed_to_database(factory):
    from babybot.storage import Store
    client, _ = factory()
    login(client)
    with client.websocket_connect('ws://localhost:8765/ws', headers={'Origin': ORIGIN}) as ws:
        start(ws)
        time.sleep(.03)
        ws.send_json({'type': 'stop'})
        receive_until(ws, 'ended')
    reopened = Store(client.app.state.runtime.settings)
    try:
        assert reopened.used() >= .02
    finally:
        reopened.close()


def test_riddle_answer_advances_only_after_heard_question(factory):
    client, providers = factory()
    login(client)
    with client.websocket_connect('ws://localhost:8765/ws', headers={'Origin': ORIGIN}) as ws:
        start(ws, mode='riddle')
        question = turn(ws, '出个谜语')
        assert question['source'] == 'curated'
        assert '长长的耳朵' in question['text']
        assert not providers.generated
        acknowledge(ws, question)
        solved = turn(ws, '我猜是兔子')
        assert '你猜到了' in solved['text'] and '兔子' in solved['text']
        acknowledge(ws, solved)
        next_question = turn(ws, '下一个')
        assert '挡雨' in next_question['text']
    assert len(providers.reviews) == 3


def test_unplayed_riddle_does_not_advance_game_state(factory):
    client, _ = factory()
    login(client)
    with client.websocket_connect('ws://localhost:8765/ws', headers={'Origin': ORIGIN}) as ws:
        start(ws, mode='riddle')
        first = turn(ws, '出个谜语')
        ws.send_json({'type': 'interrupt'})
        repeated = turn(ws, '兔子')
        assert repeated['text'] == first['text']


def test_negative_guess_is_not_treated_as_correct_answer(factory):
    client, _ = factory()
    login(client)
    with client.websocket_connect('ws://localhost:8765/ws', headers={'Origin': ORIGIN}) as ws:
        start(ws, mode='riddle')
        question = turn(ws, '出个谜语')
        acknowledge(ws, question)
        response = turn(ws, '不是兔子，我猜小猫')
        assert '你猜到了' not in response['text']
