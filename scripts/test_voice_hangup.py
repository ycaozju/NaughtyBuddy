#!/usr/bin/env python3
"""Real local TTS → ASR → exact phone hangup command; no browser playback claim."""
import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path

from test_e2e import LocalProviders, load_code, login, voice_turn, wav_to_pcm

ROOT = Path(__file__).resolve().parents[1]


async def main():
    url = "http://127.0.0.1:8765"
    cookie, health = await login(url, load_code("BABYBOT_FAMILY_CODE"))
    provider = LocalProviders()
    rows = []
    try:
        for text in ("挂断。", "结束通话。"):
            pcm = wav_to_pcm(await provider.synthesize(text))
            result = await voice_turn(url, cookie, pcm, expect_stop=True, timeout=20)
            rows.append({"synthetic_utterance": text, **result})
    finally:
        await provider.close()
    report = {"timestamp": datetime.now(timezone.utc).isoformat(),
              "kind": "real-local-model-voice-hangup", "synthetic_speech_only": True,
              "server_health": health, "cases": rows,
              "passed": all(row["ok"] for row in rows),
              "limitations": "Uploads synthetic PCM over WebSocket; browser VAD and physical microphone are covered separately, not asserted here."}
    destination = ROOT / "reports" / "call-voice-hangup.json"
    if destination.exists():
        history = destination.parent / "history"
        history.mkdir(exist_ok=True)
        destination.rename(history / f"call-voice-hangup-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%f')}.json")
    destination.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"report": str(destination), "passed": report["passed"],
                      "transcripts": [row.get("transcript") for row in rows]}, ensure_ascii=False))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
