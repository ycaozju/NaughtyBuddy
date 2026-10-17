#!/usr/bin/env python3
"""Measured real-model speech load at concurrency 1/2/4 and post-load recovery."""
from __future__ import annotations

import asyncio
from datetime import datetime
import json
import os
from pathlib import Path
import statistics
import time
from zoneinfo import ZoneInfo

import psutil

from test_e2e import (LocalProviders, common_parser, json_safe_turn, load_code,
                      login, voice_turn, wav_to_pcm)


OVERLOAD_CODES = {'busy', 'overloaded', 'capacity', 'too_many_connections',
                  'turn_busy', 'server_busy', 'connection_limit', 'model_busy'}


def percentile(values, quantile):
    if not values:
        return None
    values = sorted(values)
    rank = (len(values) - 1) * quantile
    lower, upper = int(rank), min(int(rank) + 1, len(values) - 1)
    return round(values[lower] + (values[upper] - values[lower]) * (rank - lower), 2)


def is_overload(result):
    return not result['ok'] and str(result.get('error', '')).lower() in OVERLOAD_CODES


def summarize(results, elapsed_s):
    completed = [result for result in results if result['ok']]
    overloaded = [result for result in results if is_overload(result)]
    failures = [result for result in results if not result['ok'] and not is_overload(result)]
    model_fallbacks = [result for result in completed if result.get('source') == 'fallback']
    unavailable = [result for result in model_fallbacks if result.get('category') == 'unavailable']
    latency = [result['latency_ms'] for result in completed]
    return {
        'requests': len(results), 'completed': len(completed),
        'generated': sum(result.get('source') == 'generated' for result in completed),
        'fallback': sum(result.get('source') == 'fallback' for result in completed),
        'overload_rejections': len(overloaded), 'transport_failures': len(failures),
        'model_failures': len(unavailable),
        'generation_failures': len(model_fallbacks) + len(failures),
        'unexpected_failures': len(failures) + len(unavailable),
        'failure_rate_including_overload': round((len(results) - len(completed) + len(model_fallbacks)) / len(results), 4),
        'unexpected_failure_rate': round((len(failures) + len(unavailable)) / len(results), 4),
        'generation_failure_rate_for_admitted': round((len(model_fallbacks) + len(failures)) / max(1, len(completed) + len(failures)), 4),
        'completion_per_second': round(len(completed) / elapsed_s, 4),
        'elapsed_s': round(elapsed_s, 2), 'latency_p50_ms': percentile(latency, .5),
        'latency_p95_ms': percentile(latency, .95), 'latency_max_ms': max(latency, default=None),
    }


def discover_pids(explicit):
    if explicit:
        return explicit
    found = []
    for process in psutil.process_iter(['pid', 'cmdline']):
        try:
            command = process.info['cmdline'] or []
            if not command:
                continue
            executable = Path(command[0]).name
            if executable == 'llama-server' or (
                any(item.startswith('babybot.app') for item in command)
                and ('uvicorn' in command or any('uvicorn' in item for item in command[:2]))
            ):
                found.append(process.pid)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return sorted(set(found))


def memory_snapshot(pids):
    processes = {}
    cpu_times = {}
    for pid in pids:
        try:
            root = psutil.Process(pid)
            for process in [root, *root.children(recursive=True)]:
                try:
                    processes[process.pid] = process.memory_info().rss
                    cpu = process.cpu_times()
                    cpu_times[process.pid] = round(cpu.user + cpu.system, 3)
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    return {'t_monotonic': round(time.monotonic(), 3), 'rss_bytes': sum(processes.values()),
            'per_pid_rss_bytes': processes, 'per_pid_cpu_seconds': cpu_times,
            'host_load_average': [round(value, 3) for value in os.getloadavg()],
            'host_available_memory_bytes': psutil.virtual_memory().available}


async def sample_memory(pids, samples):
    while True:
        samples.append(memory_snapshot(pids))
        await asyncio.sleep(.25)


async def run(args):
    code = load_code(args.code_env)
    cookie, health = await login(args.url, code)
    provider = LocalProviders({'models_dir': args.models_dir, 'llm_url': args.llm_url,
                               'model_timeout_s': args.timeout})
    pids = discover_pids(args.pid)
    report = {'kind': 'real-local-model-speech-stress', 'synthetic_speech_only': True,
              'started_at': datetime.now(ZoneInfo('Asia/Taipei')).isoformat(), 'url': args.url,
              'server_health': health, 'concurrency_levels': args.concurrency,
              'rounds_per_level': args.rounds, 'memory_process_ids': pids,
              'status': 'running', 'levels': [], 'limitations': [
                  'Small household capacity test; not a long-duration soak or production capacity certification',
                  'p95 is interpolated from the recorded small sample and must not be treated as a stable service SLO',
                  'Synthetic speech, no physical browser capture/playback or child voices',
                  'RSS sums selected server processes and descendants; shared pages may be counted more than once',
              ]}
    samples = []
    sampler = None
    try:
        report['local_model_health'] = await provider.health()
        pcm = wav_to_pcm(await provider.synthesize('我们一起讲一个小兔子的故事。'))
        report['input_audio_s'] = round(len(pcm) / 32000, 3)
        # Warmup is explicitly recorded and excluded from scored latency samples.
        warmup = await voice_turn(args.url, cookie, pcm, timeout=args.timeout)
        report['warmup'] = json_safe_turn(warmup)
        if not warmup['ok']:
            report['passed'] = False
            return 1
        sampler = asyncio.create_task(sample_memory(pids, samples))
        for concurrency in args.concurrency:
            level_start = time.perf_counter()
            results = []
            level_record = {'concurrency': concurrency, 'samples': results}
            report['levels'].append(level_record)
            for round_index in range(args.rounds):
                batch = await asyncio.gather(*[
                    voice_turn(args.url, cookie, pcm, timeout=args.timeout)
                    for _ in range(concurrency)
                ])
                for request_index, result in enumerate(batch):
                    results.append({'round': round_index + 1, 'request': request_index + 1,
                                    **json_safe_turn(result)})
                level_record.update(summarize(results, time.perf_counter() - level_start))
                checkpoint = Path(args.output)
                checkpoint.parent.mkdir(parents=True, exist_ok=True)
                checkpoint.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
            summary = summarize(results, time.perf_counter() - level_start)
            level_record.update(summary)
            print(json.dumps({'concurrency': concurrency, **summary}, ensure_ascii=False), flush=True)
        recovery = await voice_turn(args.url, cookie, pcm, timeout=args.timeout)
        report['recovery'] = json_safe_turn(recovery)
        summary_levels = report['levels']
        report['passed'] = (
            all(level['generation_failures'] == 0 and level['generated'] > 0 for level in summary_levels)
            and recovery['ok'] and recovery.get('source') == 'generated'
            and (not args.require_overload or any(level['overload_rejections'] > 0 for level in summary_levels))
        )
        report['overload_observed'] = any(level['overload_rejections'] > 0 for level in summary_levels)
        report['status'] = 'completed'
    finally:
        report.setdefault('passed', False)
        if report['status'] != 'completed':
            report['status'] = 'incomplete'
        if sampler:
            sampler.cancel()
            try:
                await sampler
            except asyncio.CancelledError:
                pass
        samples.append(memory_snapshot(pids))
        report['memory'] = {
            'available': bool(pids), 'sample_interval_s': .25,
            'baseline_rss_bytes': samples[0]['rss_bytes'] if samples else None,
            'peak_rss_bytes': max((sample['rss_bytes'] for sample in samples), default=None),
            'final_rss_bytes': samples[-1]['rss_bytes'] if samples else None,
            'samples': samples,
        }
        await provider.close()
        path = Path(args.output)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    return 0 if report.get('passed') else 1


if __name__ == '__main__':
    parser = common_parser(__doc__)
    parser.add_argument('--output', default='reports/stress.json')
    parser.add_argument('--rounds', type=int, default=3)
    parser.add_argument('--concurrency', type=int, nargs='+', default=[1, 2, 4])
    parser.add_argument('--pid', type=int, action='append', help='App/llama-server PID; repeat as needed')
    parser.add_argument('--require-overload', action='store_true',
                        help='Fail unless at least one request receives explicit capacity rejection')
    args = parser.parse_args()
    if args.rounds < 1 or any(value < 1 for value in args.concurrency):
        parser.error('rounds and concurrency must be positive')
    raise SystemExit(asyncio.run(run(args)))
