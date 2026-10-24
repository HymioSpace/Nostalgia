import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit


@dataclass(frozen=True)
class Settings:
    tokens: dict[str, str] = field(repr=False)
    database: Path = Path("data/nostalgia.sqlite3")
    host: str = "0.0.0.0"
    port: int = 8000
    dreaming_enabled: bool = False
    dreaming_interval_seconds: int = 600
    dreaming_provider_url: str = ""
    dreaming_provider_token_file: Path | None = None
    dreaming_batch_max_records: int = 100
    dreaming_batch_max_chars: int = 200_000
    dreaming_draft_timeout_seconds: int = 1_800
    history_limit: int = 5
    max_json_bytes: int = 262_144
    max_asset_bytes: int = 10_485_760
    max_page_size: int = 200
    index_max_chars: int = 4000
    buffer_max_items: int = 32
    preview_max_chars: int = 512
    injection_max_chars: int = 16_000

    def __post_init__(self):
        if not self.tokens or any(
            not isinstance(k, str) or len(k) < 16 or not isinstance(v, str) or not v
            for k, v in self.tokens.items()
        ):
            raise ValueError("Configure tokens of at least 16 characters mapped to user IDs")
        if self.history_limit < 2:
            raise ValueError("history_limit must be at least 2")
        if not 1 <= self.port <= 65535:
            raise ValueError("port must be between 1 and 65535")
        for name in (
            "max_json_bytes",
            "max_asset_bytes",
            "max_page_size",
            "index_max_chars",
            "buffer_max_items",
            "preview_max_chars",
            "injection_max_chars",
        ):
            if getattr(self, name) < 1:
                raise ValueError(f"{name} must be positive")
        if self.injection_max_chars < self.index_max_chars + 1024:
            raise ValueError("injection_max_chars must allow index_max_chars + 1024")
        if self.dreaming_interval_seconds < 60:
            raise ValueError("dreaming_interval_seconds must be at least 60")
        if self.dreaming_batch_max_records < 1 or self.dreaming_batch_max_chars < 1024:
            raise ValueError("Dreaming input limits must be positive")
        if self.dreaming_draft_timeout_seconds < self.dreaming_interval_seconds:
            raise ValueError("Dreaming draft timeout must not be shorter than its interval")
        if self.dreaming_enabled:
            provider = urlsplit(self.dreaming_provider_url)
            if (
                provider.scheme not in {"http", "https"}
                or not provider.hostname
                or provider.username
                or provider.password
            ):
                raise ValueError("Dreaming provider URL must be HTTP(S)")

    @property
    def dreaming_users(self) -> tuple[str, ...]:
        return tuple(sorted(set(self.tokens.values())))

    @classmethod
    def from_env(cls):
        raw = os.environ.get("NOSTALGIA_TOKENS_JSON", "")
        try:
            tokens = json.loads(raw)
        except ValueError as exc:
            raise ValueError("NOSTALGIA_TOKENS_JSON must be a token-to-user JSON object") from exc
        if not isinstance(tokens, dict):
            raise ValueError("NOSTALGIA_TOKENS_JSON must be an object")
        kwargs = {"tokens": tokens}
        for name, field_info in cls.__dataclass_fields__.items():
            value = os.environ.get(f"NOSTALGIA_{name.upper()}")
            if value is not None and name != "tokens":
                annotation = field_info.type
                if annotation in {Path, Path | None}:
                    kwargs[name] = Path(value) if value else None
                elif annotation is bool:
                    if value.lower() not in {"1", "0", "true", "false"}:
                        raise ValueError(f"NOSTALGIA_{name.upper()} must be a boolean")
                    kwargs[name] = value.lower() in {"1", "true"}
                elif annotation is int:
                    kwargs[name] = int(value)
                else:
                    kwargs[name] = value
        return cls(**kwargs)
