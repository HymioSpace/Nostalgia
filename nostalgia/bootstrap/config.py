import json
import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    tokens: dict[str, str] = field(repr=False)
    database: Path = Path("data/nostalgia.sqlite3")
    host: str = "0.0.0.0"
    port: int = 8000
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
        for name in cls.__dataclass_fields__:
            value = os.environ.get(f"NOSTALGIA_{name.upper()}")
            if value is not None and name != "tokens":
                kwargs[name] = (
                    Path(value) if name == "database" else (value if name == "host" else int(value))
                )
        return cls(**kwargs)
