from pathlib import Path

import pytest

from backup_system.manager.telegram_credentials import (
    TelegramCredentialsError,
    load_telegram_credentials,
)


def test_credentials_support_chat_and_topic_modes(tmp_path: Path) -> None:
    path = tmp_path / "telegram.json"
    path.write_text(
        '{"bot_token":"token","chat_id":"chat","message_thread_id":42}',
        encoding="utf-8",
    )
    credentials = load_telegram_credentials(tmp_path, "telegram.json")
    assert credentials.bot_token == "token"
    assert credentials.chat_id == "chat"
    assert credentials.message_thread_id == 42


def test_credentials_filename_cannot_escape_config(tmp_path: Path) -> None:
    with pytest.raises(TelegramCredentialsError, match="filename"):
        load_telegram_credentials(tmp_path, r"..\telegram.json")


def test_credentials_reject_unknown_fields(tmp_path: Path) -> None:
    (tmp_path / "telegram.json").write_text(
        '{"bot_token":"token","chat_id":"chat","extra":"value"}',
        encoding="utf-8",
    )
    with pytest.raises(TelegramCredentialsError, match="invalid"):
        load_telegram_credentials(tmp_path, "telegram.json")
