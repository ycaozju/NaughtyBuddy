from __future__ import annotations

import json
import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


@dataclass
class Settings:
    data_dir: Path = field(default_factory=lambda: ROOT / ".runtime" / "data")
    models_dir: Path = field(default_factory=lambda: ROOT / "models")
    family_code: str = ""
    parent_pin: str = ""
    allowed_origins: tuple[str, ...] = ("http://localhost:8765", "http://127.0.0.1:8765")
    secure_cookie: bool = False
    max_connections: int = 4
    max_inflight: int = 2
    heartbeat_timeout_s: float = 20
    turn_timeout_s: float = 12
    review_timeout_s: float = 4
    session_minutes: int = 10
    daily_minutes: int = 20
    model_timeout_s: float = 8
    speech_threads: int = 2
    llm_url: str = "http://127.0.0.1:8081/v1"
    llm_model: str = "qwen2.5-1.5b-instruct"
    llm_provider: str = "local"
    cloud_model: str = ""
    cloud_api_key: str = ""
    timezone: str = "Asia/Taipei"
    max_audio_seconds: int = 20
    max_turns_per_session: int = 60

    @classmethod
    def from_env(cls) -> "Settings":
        # A tiny .env reader: no shell evaluation, no variable interpolation.
        env_file = ROOT / ".env"
        if env_file.exists():
            for line in env_file.read_text().splitlines():
                if not line.strip() or line.lstrip().startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))
        credentials_path = ROOT / ".runtime" / "credentials.json"
        credentials_path.parent.mkdir(parents=True, exist_ok=True)
        if not credentials_path.exists():
            credentials = {"family_code": secrets.token_urlsafe(9), "parent_pin": str(secrets.randbelow(90_000_000) + 10_000_000)}
            fd = os.open(credentials_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as stream:
                json.dump(credentials, stream, ensure_ascii=False, indent=2)
        credentials = json.loads(credentials_path.read_text())
        return cls(
            family_code=os.getenv("BABYBOT_FAMILY_CODE", credentials["family_code"]),
            parent_pin=os.getenv("BABYBOT_PARENT_PIN", credentials["parent_pin"]),
            data_dir=Path(os.getenv("BABYBOT_DATA_DIR", str(ROOT / ".runtime" / "data"))),
            models_dir=Path(os.getenv("BABYBOT_MODELS_DIR", str(ROOT / "models"))),
            allowed_origins=tuple(x.strip() for x in os.getenv("BABYBOT_ORIGINS", "http://localhost:8765,http://127.0.0.1:8765").split(",") if x.strip()),
            secure_cookie=os.getenv("BABYBOT_SECURE_COOKIE", "0") == "1",
            max_connections=int(os.getenv("BABYBOT_MAX_CONNECTIONS", "4")),
            max_inflight=int(os.getenv("BABYBOT_MAX_INFLIGHT", "2")),
            turn_timeout_s=float(os.getenv("BABYBOT_TURN_TIMEOUT", "12")),
            review_timeout_s=float(os.getenv("BABYBOT_REVIEW_TIMEOUT", "4")),
            model_timeout_s=float(os.getenv("BABYBOT_MODEL_TIMEOUT", "8")),
            speech_threads=int(os.getenv("BABYBOT_SPEECH_THREADS", "2")),
            session_minutes=int(os.getenv("BABYBOT_SESSION_MINUTES", "10")),
            daily_minutes=int(os.getenv("BABYBOT_DAILY_MINUTES", "20")),
            llm_url=os.getenv("BABYBOT_LLM_URL", "http://127.0.0.1:8081/v1"),
            llm_model=os.getenv("BABYBOT_LLM_MODEL", "qwen2.5-1.5b-instruct"),
            llm_provider=os.getenv("BABYBOT_LLM_PROVIDER", "local"),
            cloud_model=os.getenv("BABYBOT_CLOUD_MODEL", ""),
            cloud_api_key=os.getenv("BABYBOT_CLOUD_API_KEY", ""),
        )

    def validate(self) -> None:
        if len(self.family_code) < 6 or len(self.parent_pin) < 6:
            raise ValueError("Family code and parent PIN must each have at least 6 characters")
        if self.family_code == self.parent_pin:
            raise ValueError("Family code and parent PIN must be different")
        if not self.allowed_origins or "*" in self.allowed_origins:
            raise ValueError("Explicit allowed origins are required")
        if not 1 <= self.max_inflight <= self.max_connections <= 16:
            raise ValueError("Expected 1 <= max_inflight <= max_connections <= 16")
        if self.llm_provider not in {"local", "qwen", "doubao", "deepseek"}:
            raise ValueError("Unsupported LLM adapter")
        if self.llm_provider != "local" and (not self.cloud_model or not self.cloud_api_key):
            raise ValueError("Cloud adapters require an explicit model and API key")
