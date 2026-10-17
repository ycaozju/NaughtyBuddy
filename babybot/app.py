from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import json
import secrets
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import httpx

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .config import ROOT, Settings
from .games import riddle_reply
from .safety import MODE_PROMPTS, SAFE_RESPONSES, SYSTEM_PROMPT, clean_answer, output_allowed, route_input
from .storage import PREFERENCES, Store
from .voices import DEFAULT_VOICE, PREVIEW_TEXT, VoiceLibrary

COOKIE = "babybot_session"

# Fixed diagnostic vocabulary only: never include prompts, candidate answers,
# recordings, credentials, or provider exception text in diagnostic fields.
FALLBACK_REASONS = frozenset({
    "recognition_unclear", "input_policy", "output_rule", "review_denied",
    "asr_error", "asr_timeout", "llm_error", "llm_timeout",
    "review_error", "review_timeout", "tts_error", "tts_timeout",
    "turn_timeout", "internal_error",
})


class StageFailure(Exception):
    def __init__(self, reason_code: str):
        if reason_code not in FALLBACK_REASONS:
            raise ValueError("Unknown diagnostic reason")
        self.reason_code = reason_code
        super().__init__(reason_code)



class Login(BaseModel):
    code: str = Field(min_length=1, max_length=128)


class ParentLogin(BaseModel):
    pin: str = Field(min_length=1, max_length=128)


class VoiceSelection(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    voice_id: str = Field(min_length=1, max_length=64)


class VoiceCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    name: str = Field(min_length=1, max_length=24, pattern=r"^[^\x00-\x1f\x7f]+$")
    reference_text: str = Field(min_length=5, max_length=200)
    audio_base64: str = Field(min_length=44, max_length=900000)
    consent: bool = Field(strict=True)


class ConfigPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session_minutes: int | None = Field(default=None, ge=1, le=30)
    daily_minutes: int | None = Field(default=None, ge=1, le=120)
    vad_silence_ms: int | None = Field(default=None, ge=800, le=3000)
    allowed_start_hour: int | None = Field(default=None, ge=0, le=23)
    allowed_end_hour: int | None = Field(default=None, ge=1, le=24)


class MemoryPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool
    preferences: list[str] = Field(default_factory=list, max_length=5)


class Runtime:
    def __init__(self, settings, providers):
        self.settings = settings
        self.providers = providers
        self.store = Store(settings)
        self.voices = VoiceLibrary(settings, self.store)
        self.voice_job = False
        self.connections: set[Conversation] = set()
        self.opening_connections = 0
        self.inflight = 0
        self.tasks: set[asyncio.Task] = set()
        self.attempts: dict[str, deque] = defaultdict(deque)
        self.safe_audio: dict[str, bytes] = {}
        self.metrics = defaultdict(int)
        try:
            manifest = json.loads((ROOT / ".runtime" / "safe_audio" / "manifest.json").read_text())
        except (OSError, ValueError):
            manifest = {}
        for category in SAFE_RESPONSES:
            path = ROOT / ".runtime" / "safe_audio" / f"{category}.wav"
            if path.is_file():
                data = path.read_bytes()
                record = manifest.get(category, {})
                if record.get("text_sha256") == hashlib.sha256(SAFE_RESPONSES[category].encode()).hexdigest() and record.get("audio_sha256") == hashlib.sha256(data).hexdigest():
                    self.safe_audio[category] = data

    async def speak(self, text, voice):
        # Keep existing providers/tests compatible with the default voice.
        if voice["id"] == DEFAULT_VOICE:
            return await self.providers.synthesize(text)
        return await self.providers.synthesize(text, voice=voice)

    def fixed_audio(self, voice, category):
        return self.safe_audio.get(category) if voice["id"] == DEFAULT_VOICE else self.voices.fixed(voice, category)

    def rate_limit(self, key: str, limit: int, window: int = 60):
        now = time.monotonic()
        q = self.attempts[key]
        while q and q[0] < now - window:
            q.popleft()
        if len(q) >= limit:
            raise HTTPException(429, "操作太频繁，请稍后再试。")
        q.append(now)
        if len(self.attempts) > 2048:
            self.attempts = defaultdict(deque, {k: v for k, v in self.attempts.items() if v and v[-1] >= now - window})


class Conversation:
    def __init__(self, ws: WebSocket, runtime: Runtime, token: str):
        self.ws, self.runtime, self.token = ws, runtime, token
        self.settings, self.store, self.providers = runtime.settings, runtime.store, runtime.providers
        self.id = secrets.token_hex(8)
        self.started = False
        self.closed = False
        self.mode = "story"
        self.voice = self.runtime.voices.snapshot(DEFAULT_VOICE)
        self.epoch = 0
        self.turn_count = 0
        self.turn_task: asyncio.Task | None = None
        self.history: list[dict] = []
        self.game_state: dict | None = None
        self.pending: dict | None = None
        self.started_at = time.monotonic()
        self.last_seen = self.started_at
        self.last_charged = self.started_at
        self.limit_seconds = 0
        self.send_lock = asyncio.Lock()
        self.inputs = deque()
        self.watchdog: asyncio.Task | None = None

    async def send(self, kind, **data):
        if self.closed:
            return
        async with self.send_lock:
            if not self.closed:
                await self.ws.send_json({"type": kind, **data})

    def current(self, epoch):
        return self.started and not self.closed and epoch == self.epoch

    def remaining(self):
        elapsed = time.monotonic() - self.started_at
        config = self.store.config()
        return max(0, int(min(self.limit_seconds - elapsed, config["session_minutes"] * 60 - elapsed, config["daily_minutes"] * 60 - self.store.used())))

    def account(self):
        if self.started:
            now = time.monotonic()
            self.store.charge(now - self.last_charged)
            self.last_charged = now

    async def invalidate(self):
        self.epoch += 1
        self.pending = None
        if self.turn_task and not self.turn_task.done() and self.turn_task is not asyncio.current_task():
            self.turn_task.cancel()
            # Don't block the audio control path on upstream cancellation.
        self.turn_task = None

    async def end(self, reason):
        if self.closed:
            return
        self.account()
        self.started = False
        await self.invalidate()
        await self.send("ended", reason=reason)
        self.closed = True
        self.history.clear()
        with contextlib.suppress(Exception):
            await self.ws.close(code=1000)

    async def watch(self):
        while not self.closed:
            await asyncio.sleep(1)
            if not self.store.auth(self.token):
                await self.end("revoked")
                return
            if time.monotonic() - self.last_seen > self.settings.heartbeat_timeout_s:
                await self.end("heartbeat_timeout")
                return
            self.account()
            config = self.store.config()
            hour = datetime.now(ZoneInfo(self.settings.timezone)).hour
            if self.started and not config["allowed_start_hour"] <= hour < config["allowed_end_hour"]:
                await self.end("outside_hours")
                return
            if self.started and self.remaining() <= 0:
                await self.end("time_limit")
                return

    async def start(self, mode):
        if self.runtime.voice_job:
            await self.end("voice_preparing")
            return
        if self.started:
            await self.send("error", code="already_started", message="已经开始了。", retryable=False)
            return
        if mode not in MODE_PROMPTS:
            await self.send("error", code="invalid_mode", message="请选择一种玩法。", retryable=True)
            return
        try:
            self.voice = self.runtime.voices.snapshot(self.runtime.voices.selected_id)
            if self.voice["id"] != DEFAULT_VOICE and (not self.runtime.voices.available(self.voice) or not self.runtime.fixed_audio(self.voice, "greeting")):
                raise KeyError()
        except KeyError:
            await self.end("voice_unavailable")
            return
        config = self.store.config()
        hour = datetime.now(ZoneInfo(self.settings.timezone)).hour
        if not config["allowed_start_hour"] <= hour < config["allowed_end_hour"]:
            await self.end("outside_hours")
            return
        self.limit_seconds = int(min(config["session_minutes"] * 60, config["daily_minutes"] * 60 - self.store.used()))
        if self.limit_seconds <= 0:
            await self.end("daily_limit")
            return
        self.mode, self.started = mode, True
        self.started_at = self.last_charged = time.monotonic()
        await self.send("session_started", session_id=self.id, limit_seconds=self.limit_seconds, remaining_seconds=self.limit_seconds, voice_id=self.voice["id"])
        await self.send("state", state="listening", turn_id=self.epoch)
        greeting = self.runtime.fixed_audio(self.voice, "greeting")
        if greeting:
            await self.send("greeting", turn_id=0, chunk_id="greeting", text=SAFE_RESPONSES["greeting"], audio_base64=base64.b64encode(greeting).decode(), mime="audio/wav", safety="approved", source="fixed", voice_id=self.voice["id"])

    async def begin_turn(self, *, pcm=None, text=None):
        if not self.started:
            await self.send("error", code="not_started", message="请先点击开始。", retryable=True)
            return
        if pcm is not None and (len(pcm) < 3200 or len(pcm) % 2 or len(pcm) > self.settings.max_audio_seconds * 32000):
            await self.send("error", code="invalid_audio", message="请用一小段话再试一次。", retryable=True)
            return
        if text is not None and (not isinstance(text, str) or not text.strip() or len(text) > 300):
            await self.send("error", code="invalid_text", message="请说一小段话。", retryable=True)
            return
        now = time.monotonic()
        while self.inputs and self.inputs[0] < now - 60:
            self.inputs.popleft()
        if len(self.inputs) >= 20 or self.turn_count >= self.settings.max_turns_per_session:
            await self.send("error", code="rate_limit", message="我们先慢下来，休息一会儿。", retryable=False)
            return
        if self.turn_task and not self.turn_task.done():
            await self.send("error", code="turn_busy", message="我还在听这一句。", retryable=True)
            return
        # New voices can need several CPU seconds; admit one turn at a time
        # rather than queue two long syntheses past the existing deadline.
        capacity = self.settings.max_inflight if self.voice["engine"] == "melo" else 1
        if self.runtime.inflight >= capacity:
            self.runtime.metrics["overloaded"] += 1
            await self.send("error", code="overloaded", message="现在有点忙，请稍后再试。", retryable=True)
            return
        self.inputs.append(now)
        self.turn_count += 1
        self.epoch += 1
        self.pending = None
        epoch = self.epoch
        self.runtime.inflight += 1
        self.turn_task = asyncio.create_task(self.process(epoch, pcm, text))
        self.runtime.tasks.add(self.turn_task)
        def release(task):
            self.runtime.inflight -= 1
            self.runtime.tasks.discard(task)
            if not task.cancelled():
                # Retrieve exceptions without logging private provider payloads.
                task.exception()
        self.turn_task.add_done_callback(release)

    async def process(self, epoch, pcm, text):
        timings = {}
        started = time.monotonic()

        async def timed(name, awaitable, timeout=None):
            t = time.monotonic()
            try:
                return await asyncio.wait_for(awaitable, self.settings.turn_timeout_s if timeout is None else timeout)
            except (TimeoutError, httpx.TimeoutException):
                raise StageFailure(f"{name}_timeout") from None
            except asyncio.CancelledError:
                # Outer turn expiry and user interruption must retain their origin.
                raise
            except Exception:
                raise StageFailure(f"{name}_error") from None
            finally:
                timings[name + "_ms"] = round((time.monotonic() - t) * 1000, 2)

        async def pipeline():
            nonlocal pcm, text
            if pcm is not None:
                await self.send("state", state="transcribing", turn_id=epoch)
                text = await timed("asr", self.providers.transcribe(pcm, 16000))
                pcm = None  # audio is never written to logs or storage
                if text is not None and not isinstance(text, str):
                    raise StageFailure("asr_error")
            else:
                timings["asr_ms"] = 0
            if not self.current(epoch):
                return
            text = (text or "").strip()
            if not text or len(text) > 300:
                return "unclear", "recognition_unclear"
            await self.send("transcript", text=text, turn_id=epoch)
            decision = route_input(text)
            if decision.category == "stop":
                await self.end("voice_stop")
                return
            if decision.response is not None:
                return decision.category, "input_policy"
            memory = self.store.memory()
            prompt = SYSTEM_PROMPT + "\n本轮玩法：" + MODE_PROMPTS[self.mode]
            if self.voice["engine"] != "melo":
                prompt += "\n这次请只说一到两个很短的完整句子，每句最多20个字，总共最多40个字。"
            if memory["enabled"] and memory["preferences"]:
                prompt += "\n监护人选择的主题偏好：" + "、".join(memory["preferences"])
            messages = [{"role": "system", "content": prompt}, *self.history[-12:], {"role": "user", "content": text}]
            await self.send("state", state="thinking", turn_id=epoch)
            next_game_state = None
            source = "generated"
            if self.mode == "riddle":
                answer, next_game_state = riddle_reply(text, self.game_state)
                timings["llm_ms"] = 0
                source = "curated"
            else:
                answer = await timed("llm", self.providers.generate(messages, self.mode))
                if not isinstance(answer, str):
                    raise StageFailure("llm_error")
                answer = clean_answer(answer, max_chars=40 if self.voice["engine"] != "melo" else 80)
            if not self.current(epoch):
                return
            if not output_allowed(answer):
                self.runtime.metrics["rule_rejected"] += 1
                return "blocked", "output_rule"
            await self.send("state", state="checking", turn_id=epoch)
            # The whole short response passes a separate strict allow-only call.
            review_context = text if not self.history else json.dumps({"recent": self.history[-4:], "child": text}, ensure_ascii=False)
            approved = await timed("review", self.providers.moderate(review_context, answer), self.settings.review_timeout_s)
            if approved is not True:
                self.runtime.metrics["review_rejected"] += 1
                return "blocked", "review_denied"
            if not self.current(epoch):
                return
            await self.send("state", state="speaking", turn_id=epoch)
            wav = await timed("tts", self.runtime.speak(answer, self.voice))
            if not isinstance(wav, (bytes, bytearray)):
                raise StageFailure("tts_error")
            if not self.current(epoch):
                return
            chunk = secrets.token_hex(8)
            self.pending = {"turn_id": epoch, "chunk_id": chunk, "user": text, "answer": answer, "game_state": next_game_state}
            timings["total_ms"] = round((time.monotonic() - started) * 1000, 2)
            await self.send("audio", turn_id=epoch, chunk_id=chunk, text=answer, audio_base64=base64.b64encode(wav).decode(), mime="audio/wav", safety="approved", source=source, timings=timings, voice_id=self.voice["id"])
            self.runtime.metrics["completed"] += 1

        deadline = asyncio.timeout(self.settings.turn_timeout_s)
        fallback_plan = None
        try:
            async with deadline:
                fallback_plan = await pipeline()
        except asyncio.CancelledError:
            self.runtime.metrics["cancelled"] += 1
            raise
        except StageFailure as failure:
            self.runtime.metrics["failed"] += 1
            # An upstream cancellation handler may itself raise a timeout. The
            # enclosing deadline remains authoritative when it has expired.
            fallback_plan = "unavailable", "turn_timeout" if deadline.expired() else failure.reason_code
        except TimeoutError:
            self.runtime.metrics["failed"] += 1
            fallback_plan = "unavailable", "turn_timeout" if deadline.expired() else "internal_error"
        except Exception:
            # Unexpected application faults are not falsely attributed to a model.
            self.runtime.metrics["failed"] += 1
            fallback_plan = "unavailable", "internal_error"
        if fallback_plan and self.current(epoch):
            category, reason_code = fallback_plan
            try:
                # A fixed safety response has its own bounded delivery attempt;
                # it cannot replace or retry an unapproved generated answer.
                await self.fallback(epoch, category, timings, started, reason_code=reason_code)
            except asyncio.CancelledError:
                self.runtime.metrics["cancelled"] += 1
                raise

    async def fallback(self, epoch, category, timings, started, *, reason_code):
        if not self.current(epoch):
            return
        if reason_code not in FALLBACK_REASONS:
            raise ValueError("Unknown diagnostic reason")
        self.runtime.metrics[f"fallback_{reason_code}"] += 1
        text = SAFE_RESPONSES[category]
        wav = self.runtime.fixed_audio(self.voice, category)
        if wav is None:
            # Never synthesize rejected generated content. Only fixed local phrases.
            delivery_reason_code = None
            try:
                wav = await asyncio.wait_for(self.runtime.speak(text, self.voice), 10)
                if not isinstance(wav, (bytes, bytearray)):
                    raise TypeError("Invalid fixed audio")
                if self.voice["id"] == DEFAULT_VOICE:
                    self.runtime.safe_audio[category] = wav
            except (TimeoutError, httpx.TimeoutException):
                delivery_reason_code = "tts_timeout"
            except asyncio.CancelledError:
                raise
            except Exception:
                delivery_reason_code = "tts_error"
            if delivery_reason_code:
                self.runtime.metrics[f"fallback_delivery_{delivery_reason_code}"] += 1
                if self.current(epoch):
                    await self.send("error", code=category, message=text, retryable=True,
                                    reason_code=reason_code, delivery_reason_code=delivery_reason_code)
                    await self.send("state", state="listening", turn_id=epoch)
                return
        if not self.current(epoch):
            return
        chunk = secrets.token_hex(8)
        self.pending = {"turn_id": epoch, "chunk_id": chunk, "user": None, "answer": text}
        timings["total_ms"] = round((time.monotonic() - started) * 1000, 2)
        await self.send("audio", turn_id=epoch, chunk_id=chunk, text=text, audio_base64=base64.b64encode(wav).decode(), mime="audio/wav", safety="approved", source="fallback", category=category, reason_code=reason_code, timings=timings, voice_id=self.voice["id"])

    async def message(self, data):
        kind = data.get("type")
        if kind == "start":
            await self.start(data.get("mode", "story"))
        elif kind == "ping":
            await self.send("pong", remaining_seconds=self.remaining() if self.started else 0)
        elif kind == "stop":
            await self.end("user_stop")
        elif kind == "interrupt":
            canceled_turn_id = self.epoch
            await self.invalidate()
            self.runtime.metrics["interrupts"] += 1
            await self.send("interrupted", turn_id=self.epoch, canceled_turn_id=canceled_turn_id)
            await self.send("state", state="listening", turn_id=self.epoch)
        elif kind == "played":
            pending = self.pending
            if pending and data.get("turn_id") == pending["turn_id"] and data.get("chunk_id") == pending["chunk_id"]:
                if pending["user"] is not None:
                    self.history.extend([{"role": "user", "content": pending["user"]}, {"role": "assistant", "content": pending["answer"]}])
                    self.history = self.history[-12:]
                    if pending.get("game_state") is not None:
                        self.game_state = pending["game_state"]
                self.pending = None
                await self.send("state", state="listening", turn_id=self.epoch)
        elif kind == "text":
            await self.begin_turn(text=data.get("text"))
        else:
            await self.send("error", code="invalid_message", message="这次没有听明白，请再试一次。", retryable=True)

    async def run(self):
        self.watchdog = asyncio.create_task(self.watch())
        try:
            while not self.closed:
                message = await self.ws.receive()
                if message["type"] == "websocket.disconnect":
                    break
                self.last_seen = time.monotonic()
                if not self.store.auth(self.token):
                    await self.end("revoked")
                    break
                if message.get("bytes") is not None:
                    await self.begin_turn(pcm=message["bytes"])
                elif message.get("text") is not None:
                    raw = message["text"]
                    if len(raw) > 4096:
                        await self.send("error", code="message_too_large", message="这段话太长了，请说短一点。", retryable=True)
                        continue
                    try:
                        data = json.loads(raw)
                        if not isinstance(data, dict):
                            raise ValueError()
                    except (ValueError, TypeError):
                        await self.send("error", code="invalid_json", message="请重新开始。", retryable=True)
                        continue
                    await self.message(data)
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            self.account()
            self.started = False
            self.closed = True
            task = self.turn_task
            await self.invalidate()
            if task:
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
            self.watchdog.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self.watchdog
            self.history.clear()
            self.runtime.connections.discard(self)


def create_app(settings: Settings | None = None, providers=None):
    settings = settings or Settings.from_env()
    settings.validate()
    if providers is None:
        from .adapters import ConfiguredProviders
        providers = ConfiguredProviders(settings)
    runtime = Runtime(settings, providers)

    @asynccontextmanager
    async def lifespan(app):
        yield
        for connection in list(runtime.connections):
            await connection.end("shutdown")
        for task in list(runtime.tasks):
            task.cancel()
        if runtime.tasks:
            await asyncio.gather(*list(runtime.tasks), return_exceptions=True)
        await providers.close()
        runtime.store.close()

    app = FastAPI(title="小芽家庭语音玩伴", version="0.3.0", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.runtime = runtime
    hosts = list({urlparse(origin).hostname for origin in settings.allowed_origins})
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=hosts)

    @app.middleware("http")
    async def security(request: Request, call_next):
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            if request.headers.get("origin") not in settings.allowed_origins:
                return JSONResponse({"detail": "不允许的请求来源。"}, status_code=403)
            body_limit = 16384
            if request.url.path == "/api/voices" and request.method == "POST":
                role = runtime.store.auth(request.cookies.get(COOKIE, ""))
                if role != "parent":
                    return JSONResponse({"detail": "请先由大人解锁。"}, status_code=403 if role else 401)
                body_limit = 2 * 1024 * 1024
            body = bytearray()
            async for part in request.stream():
                body.extend(part)
                if len(body) > body_limit:
                    return JSONResponse({"detail": "请求过大。"}, status_code=413)
            request._body = bytes(body)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Permissions-Policy"] = "camera=(), geolocation=(), microphone=(self)"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; media-src 'self' blob: data:; connect-src 'self'; worker-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    def auth(request, parent=False):
        token = request.cookies.get(COOKIE, "")
        role = runtime.store.auth(token)
        if not role:
            raise HTTPException(401, "请先输入家庭口令。")
        if parent and role != "parent":
            raise HTTPException(403, "请先由大人解锁。")
        return token, role

    def me(token):
        role = runtime.store.auth(token)
        if not role:
            return {"authenticated": False, "role": None}
        return {"authenticated": True, "role": role, "config": runtime.store.config(), "memory": runtime.store.memory(), "preference_options": PREFERENCES, "daily_used_seconds": round(runtime.store.used()), "provider": getattr(providers, "mode", "local")}

    @app.get("/api/health")
    async def health():
        return {"status": "ok", "provider": getattr(providers, "mode", "local"), "backend": "local", "version": "0.3.0"}

    @app.get("/api/me")
    async def get_me(request: Request):
        return me(request.cookies.get(COOKIE, ""))

    @app.post("/api/auth/login")
    async def login(payload: Login, request: Request):
        runtime.rate_limit("login:" + request.client.host, 10)
        if not secrets.compare_digest(payload.code.encode(), settings.family_code.encode()):
            raise HTTPException(401, "家庭口令不正确。")
        old = request.cookies.get(COOKIE)
        if old:
            runtime.store.logout(old)
        token = runtime.store.login()
        response = JSONResponse(me(token))
        response.set_cookie(COOKIE, token, max_age=86400, httponly=True, secure=settings.secure_cookie, samesite="strict", path="/")
        return response

    @app.post("/api/auth/parent")
    async def parent(payload: ParentLogin, request: Request):
        token, _ = auth(request)
        runtime.rate_limit("parent:" + request.client.host, 5)
        if not secrets.compare_digest(payload.pin.encode(), settings.parent_pin.encode()):
            raise HTTPException(403, "大人密码不正确。")
        runtime.store.elevate(token)
        return me(token)

    @app.post("/api/auth/lock")
    async def lock(request: Request):
        token, _ = auth(request)
        runtime.store.db.execute("UPDATE auth SET parent_until=0 WHERE token=?", (runtime.store.digest(token),))
        runtime.store.db.commit()
        return me(token)

    @app.post("/api/auth/logout")
    async def logout(request: Request):
        token = request.cookies.get(COOKIE, "")
        if token:
            runtime.store.logout(token)
        response = JSONResponse({"ok": True})
        response.delete_cookie(COOKIE)
        return response

    @app.get("/api/settings")
    async def get_settings(request: Request):
        auth(request, True)
        return runtime.store.config()

    def resolve_voice(voice_id):
        try:
            voice = runtime.voices.snapshot(voice_id)
        except KeyError:
            raise HTTPException(404, "这个音色不存在或参考录音已损坏，请重新录入。")
        if not runtime.voices.available(voice):
            raise HTTPException(503, "这个音色的本地模型尚未安装，请运行音色模型安装脚本。")
        return voice

    @asynccontextmanager
    async def voice_operation(request, idle=True):
        token, _ = auth(request, True)
        if runtime.voice_job or (idle and (runtime.connections or runtime.opening_connections or runtime.inflight)):
            raise HTTPException(409, "请先挂断其他通话，并等待正在准备的音色完成。")
        runtime.rate_limit("voice_operations", 30)
        runtime.voice_job = True
        try:
            yield token
        finally:
            runtime.voice_job = False

    @app.get("/api/voices")
    async def get_voices(request: Request):
        auth(request, True)
        return runtime.voices.catalog()

    @app.post("/api/voices")
    async def add_voice(payload: VoiceCreate, request: Request):
        async with voice_operation(request):
            if not payload.consent:
                raise HTTPException(422, "请确认这是你本人或已获许可的录音，并同意在本机保存。")
            if not runtime.voices.catalog()["clone_available"]:
                raise HTTPException(503, "本地声音克隆模型尚未安装。")
            try:
                voice_id = runtime.voices.add(payload.name, payload.reference_text, payload.audio_base64)
            except ValueError as exc:
                raise HTTPException(422, str(exc))
            return {**runtime.voices.catalog(), "created_id": voice_id}

    @app.post("/api/voices/preview")
    async def preview_voice(payload: VoiceSelection, request: Request):
        async with voice_operation(request) as token:
            voice = resolve_voice(payload.voice_id)
            try:
                audio = await asyncio.wait_for(runtime.speak(PREVIEW_TEXT, voice), 30)
            except Exception:
                raise HTTPException(503, "这个音色暂时没有合成成功，请检查参考录音或稍后再试。")
            if runtime.store.auth(token) != "parent":
                raise HTTPException(403, "家长设置已锁定，请重新解锁。")
            return Response(audio, media_type="audio/wav", headers={"X-Voice-Id": voice["id"]})

    @app.post("/api/voices/select")
    async def select_voice(payload: VoiceSelection, request: Request):
        async with voice_operation(request) as token:
            voice = resolve_voice(payload.voice_id)
            if not runtime.fixed_audio(voice, "greeting"):
                audio = {}
                try:
                    async with asyncio.timeout(120):
                        for category, text in SAFE_RESPONSES.items():
                            if text:
                                audio[category] = await runtime.speak(text, voice)
                    # Only an entirely prepared voice becomes selectable.
                    runtime.voices.save_fixed(voice, audio)
                    if voice["id"] == DEFAULT_VOICE:
                        runtime.safe_audio.update(audio)
                except Exception:
                    raise HTTPException(503, "音色准备未完成，仍保留原音色。请稍后重试或换一段清晰录音。")
            if runtime.store.auth(token) != "parent":
                raise HTTPException(403, "家长设置已锁定，请重新解锁。")
            runtime.voices.select(voice["id"])
            return runtime.voices.catalog()

    @app.delete("/api/voices/{voice_id}")
    async def delete_voice(voice_id: str, request: Request):
        async with voice_operation(request, idle=False):
            try:
                if not voice_id.startswith("custom-"):
                    raise HTTPException(422, "预设音色不能删除。")
                for connection in list(runtime.connections):
                    if connection.voice["id"] == voice_id:
                        await connection.end("voice_deleted")
                runtime.voices.remove(voice_id)
            except KeyError:
                raise HTTPException(404, "这个自定义音色已不存在。")
            except OSError:
                raise HTTPException(503, "音色文件未能完整删除，请检查本机文件权限后重试。")
            return runtime.voices.catalog()

    @app.patch("/api/settings")
    async def patch_settings(payload: ConfigPatch, request: Request):
        auth(request, True)
        config = {**runtime.store.config(), **payload.model_dump(exclude_none=True)}
        if config["allowed_start_hour"] >= config["allowed_end_hour"]:
            raise HTTPException(422, "结束时间应晚于开始时间。")
        runtime.store.set("config", config)
        return config

    @app.get("/api/memory")
    async def get_memory(request: Request):
        auth(request)
        return runtime.store.memory()

    @app.put("/api/memory")
    async def put_memory(payload: MemoryPatch, request: Request):
        auth(request, True)
        if any(p not in PREFERENCES for p in payload.preferences):
            raise HTTPException(422, "请从提供的主题里选择，最多五个。")
        value = {"enabled": payload.enabled, "preferences": list(dict.fromkeys(payload.preferences))}
        runtime.store.set("memory", value)
        return value

    @app.delete("/api/memory")
    async def clear_memory(request: Request):
        auth(request, True)
        value = {"enabled": False, "preferences": []}
        runtime.store.set("memory", value)
        # Also clear active ephemeral context; future turns cannot replay old prefs.
        for connection in list(runtime.connections):
            connection.history.clear()
            canceled_turn_id = connection.epoch
            await connection.invalidate()
            await connection.send("interrupted", turn_id=connection.epoch, canceled_turn_id=canceled_turn_id)
        return value

    @app.get("/api/admin/status")
    async def admin_status(request: Request):
        auth(request, True)
        try:
            provider_health = await asyncio.wait_for(providers.health(), 5)
        except Exception:
            provider_health = {"ready": False}
        return {"provider": getattr(providers, "mode", "local"), "models": provider_health, "active_sessions": len(runtime.connections), "inflight": runtime.inflight, "voice_preparing": runtime.voice_job, "metrics": dict(runtime.metrics), "daily_used_seconds": round(runtime.store.used())}

    @app.post("/api/admin/warmup")
    async def warmup(request: Request):
        auth(request, True)
        runtime.rate_limit("warmup", 2)
        if runtime.voice_job or runtime.connections or runtime.opening_connections or runtime.inflight:
            raise HTTPException(409, "请先结束当前游戏，再准备模型。")
        runtime.voice_job = True
        try:
            model_health = await asyncio.wait_for(providers.health(), 60)
            if not model_health.get("ok", model_health.get("ready", False)):
                raise RuntimeError()
            await asyncio.wait_for(providers.synthesize("我是小芽，一个AI玩伴。"), 30)
            # Warm the same system prefix without storing a conversation.
            await asyncio.wait_for(providers.generate([{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": "你好"}], "story"), 30)
            await asyncio.wait_for(providers.moderate("你好", "你好，我们一起编故事吧。"), 15)
            return {"ok": True, "models": model_health}
        except Exception:
            raise HTTPException(503, "模型还没有准备好，请检查本机模型服务。")
        finally:
            runtime.voice_job = False

    @app.post("/api/admin/revoke")
    async def revoke(request: Request):
        token, _ = auth(request, True)
        runtime.store.revoke_others(token)
        for connection in list(runtime.connections):
            if connection.token != token:
                await connection.end("revoked")
        return {"ok": True}

    @app.post("/api/admin/pause")
    async def pause(request: Request):
        auth(request, True)
        for connection in list(runtime.connections):
            await connection.end("parent_pause")
        return {"ok": True}

    @app.websocket("/ws")
    async def websocket(ws: WebSocket):
        token = ws.cookies.get(COOKIE, "")
        if ws.headers.get("origin") not in settings.allowed_origins or not runtime.store.auth(token):
            await ws.close(code=1008)
            return
        if runtime.voice_job or len(runtime.connections) + runtime.opening_connections >= settings.max_connections:
            await ws.accept()
            await ws.send_json({"type": "error", "code": "overloaded", "message": "现在有点忙，请稍后再试。", "retryable": True})
            await ws.close(code=1013)
            return
        # Reserve before await: simultaneous handshakes must not bypass the cap.
        runtime.opening_connections += 1
        try:
            await ws.accept()
            connection = Conversation(ws, runtime, token)
            runtime.connections.add(connection)
        finally:
            runtime.opening_connections -= 1
        await connection.run()

    @app.get("/")
    async def index():
        return FileResponse(ROOT / "static" / "index.html")

    if (ROOT / "static").is_dir():
        app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")
    return app
