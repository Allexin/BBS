"""Strict loading of ACL-protected Telegram credentials from Stable config."""

from __future__ import annotations

import json
from pathlib import Path, PureWindowsPath
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

_MAX_CREDENTIAL_BYTES = 64 * 1024
_FILE_ATTRIBUTE_REPARSE_POINT = 0x400


class TelegramCredentialsError(RuntimeError):
    pass


class TelegramCredentials(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    bot_token: str = Field(min_length=1)
    chat_id: str = Field(min_length=1)
    message_thread_id: int | None = Field(default=None, gt=0)
    proxy_url: str | None = None

    @field_validator("proxy_url")
    @classmethod
    def validate_proxy_url(cls, value: str | None) -> str | None:
        if value is not None:
            parsed = urlsplit(value)
            if parsed.scheme not in {"http", "https", "socks5", "socks5h"} or not parsed.hostname:
                raise ValueError("proxy_url must be an HTTP or SOCKS5 URL")
        return value


def load_telegram_credentials(config_directory: Path, filename: str) -> TelegramCredentials:
    relative = PureWindowsPath(filename)
    if relative.name != filename or relative.suffix.casefold() != ".json":
        raise TelegramCredentialsError("Telegram credentials filename is invalid")
    path = config_directory / filename
    if not path.is_file() or _is_reparse(path):
        raise TelegramCredentialsError("Telegram credentials file is missing or unsafe")
    size = path.stat().st_size
    if size <= 0 or size > _MAX_CREDENTIAL_BYTES:
        raise TelegramCredentialsError("Telegram credentials file has invalid size")
    try:
        return TelegramCredentials.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValidationError, json.JSONDecodeError) as error:
        raise TelegramCredentialsError("Telegram credentials file is invalid") from error


def _is_reparse(path: Path) -> bool:
    attributes = getattr(path.lstat(), "st_file_attributes", 0)
    return bool(attributes & _FILE_ATTRIBUTE_REPARSE_POINT)
