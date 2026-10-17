"""Family-local voice references and fixed-speech caches. Never public assets."""
from __future__ import annotations

import base64
import hashlib
import io
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import wave

import numpy as np

from .safety import SAFE_RESPONSES
from .voice_engine import BUILTIN_VOICES, engine_available

DEFAULT_VOICE = "melo-default"
PREVIEW_TEXT = "我是小芽，一个AI玩伴。我们一起讲个温暖的小故事吧。"
LIMITS = {"min_seconds": 3, "max_seconds": 20, "max_custom": 3}


def private_write(path: Path, data: bytes):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(path.name + "." + secrets.token_hex(6) + ".tmp")
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as out:
            out.write(data)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def validate_reference(encoded: str) -> tuple[bytes, float]:
    try:
        data = base64.b64decode(encoded, validate=True)
        if not 44 < len(data) <= 650_000:
            raise ValueError()
        with wave.open(io.BytesIO(data)) as wav:
            if (wav.getnchannels(), wav.getsampwidth(), wav.getframerate(), wav.getcomptype()) != (1, 2, 16000, "NONE"):
                raise ValueError()
            frames = wav.getnframes()
            duration = frames / 16000
            if not LIMITS["min_seconds"] <= duration <= LIMITS["max_seconds"]:
                raise ValueError()
            pcm = wav.readframes(frames)
            if len(pcm) != frames * 2:
                raise ValueError()
        samples = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768
        if float(np.sqrt(np.mean(samples * samples))) < .003:
            raise ValueError()
    except (ValueError, EOFError, wave.Error, TypeError):
        raise ValueError("请提供3–20秒、有清晰人声的16kHz单声道PCM16 WAV。") from None
    # Strip optional chunks/metadata and keep only the bounded waveform.
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(pcm)
    return output.getvalue(), duration


class VoiceLibrary:
    def __init__(self, settings, store):
        self.settings, self.store = settings, store
        self.root = settings.data_dir / "voices"
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._fixed = {}

    @property
    def selected_id(self):
        try:
            value = self.store.get("voice", {"id": DEFAULT_VOICE})
            return value["id"] if isinstance(value, dict) and isinstance(value.get("id"), str) else DEFAULT_VOICE
        except (ValueError, TypeError):
            return DEFAULT_VOICE

    def select(self, voice_id):
        self.store.set("voice", {"id": voice_id})

    def _profile_dir(self, voice_id):
        if not re.fullmatch(r"custom-[a-f0-9]{32}", voice_id):
            raise KeyError(voice_id)
        return self.root / "profiles" / voice_id

    def snapshot(self, voice_id):
        for row in BUILTIN_VOICES:
            if row["id"] == voice_id:
                return dict(row)
        directory = self._profile_dir(voice_id)
        try:
            if directory.is_symlink():
                raise ValueError()
            record = json.loads((directory / "profile.json").read_text())
            if not isinstance(record, dict) or not isinstance(record.get("name"), str) or not isinstance(record.get("reference_text"), str):
                raise ValueError()
            reference = directory / "reference.wav"
            if reference.is_symlink() or hashlib.sha256(reference.read_bytes()).hexdigest() != record["reference_sha256"]:
                raise ValueError()
            return {"id": voice_id, "name": record["name"], "engine": "zipvoice", "kind": "custom",
                    "reference_audio_path": str(reference), "reference_text": record["reference_text"],
                    "reference_sha256": record["reference_sha256"], "speed": 1.0}
        except (OSError, ValueError, KeyError, TypeError):
            raise KeyError(voice_id) from None

    def custom_ids(self):
        return sorted(p.name for p in (self.root / "profiles").glob("custom-*") if p.is_dir() and not p.is_symlink())

    def available(self, voice):
        return engine_available(self.settings.models_dir, voice["engine"])

    def catalog(self):
        rows = []
        for voice_id in [*[v["id"] for v in BUILTIN_VOICES], *self.custom_ids()]:
            try:
                v = self.snapshot(voice_id)
                rows.append({"id": v["id"], "name": v["name"], "kind": "custom" if v["engine"] == "zipvoice" else "builtin",
                             "description": v.get("description", "使用本机参考录音合成；不代表本人正在讲话"),
                             "available": self.available(v)})
            except KeyError:
                # A broken reference must remain removable from the parent UI.
                rows.append({"id": voice_id, "name": "参考录音已损坏", "kind": "custom", "available": False,
                             "description": "无法使用，请删除后重新录入"})
        return {"selected_id": self.selected_id, "voices": rows,
                "clone_available": engine_available(self.settings.models_dir, "zipvoice"), "limits": LIMITS}

    def add(self, name, reference_text, encoded):
        if len(self.custom_ids()) >= LIMITS["max_custom"]:
            raise ValueError("最多保存3个自定义音色，请先删除不再使用的音色。")
        audio, duration = validate_reference(encoded)
        voice_id = "custom-" + secrets.token_hex(16)
        directory = self._profile_dir(voice_id)
        try:
            private_write(directory / "reference.wav", audio)
            private_write(directory / "profile.json", json.dumps({
                "name": name, "reference_text": reference_text, "duration_s": duration,
                "reference_sha256": hashlib.sha256(audio).hexdigest(), "consent": True,
            }, ensure_ascii=False).encode())
        except BaseException:
            shutil.rmtree(directory, ignore_errors=True)
            raise
        return voice_id

    def remove(self, voice_id):
        directory = self._profile_dir(voice_id)
        if not directory.is_dir() or directory.is_symlink():
            raise KeyError(voice_id)
        self._fixed.pop(voice_id, None)
        # Keep the profile directory as a retry handle if cache removal fails.
        try:
            shutil.rmtree(self.root / "cache" / voice_id)
        except FileNotFoundError:
            pass
        shutil.rmtree(directory)
        if self.selected_id == voice_id:
            self.select(DEFAULT_VOICE)

    @staticmethod
    def fingerprint(voice):
        return hashlib.sha256(json.dumps({"voice": voice, "text": SAFE_RESPONSES, "revision": 1},
                                         ensure_ascii=False, sort_keys=True).encode()).hexdigest()

    def fixed(self, voice, category):
        voice_id, fingerprint = voice["id"], self.fingerprint(voice)
        if self._fixed.get(voice_id, {}).get("fingerprint") != fingerprint:
            directory = self.root / "cache" / voice_id
            try:
                manifest = json.loads((directory / "manifest.json").read_text())
                if not isinstance(manifest, dict) or not isinstance(manifest.get("sha256"), dict):
                    return None
                if manifest["fingerprint"] != fingerprint:
                    return None
                audio = {}
                for key, text in SAFE_RESPONSES.items():
                    if not text:
                        continue
                    data = (directory / f"{key}.wav").read_bytes()
                    if hashlib.sha256(data).hexdigest() != manifest["sha256"][key]:
                        return None
                    audio[key] = data
                self._fixed[voice_id] = {"fingerprint": fingerprint, "audio": audio}
            except (OSError, ValueError, KeyError, TypeError):
                return None
        return self._fixed[voice_id]["audio"].get(category)

    def save_fixed(self, voice, audio):
        directory = self.root / "cache" / voice["id"]
        for key, data in audio.items():
            private_write(directory / f"{key}.wav", data)
        manifest = {"fingerprint": self.fingerprint(voice),
                    "sha256": {key: hashlib.sha256(data).hexdigest() for key, data in audio.items()}}
        private_write(directory / "manifest.json", json.dumps(manifest).encode())
        self._fixed.pop(voice["id"], None)
