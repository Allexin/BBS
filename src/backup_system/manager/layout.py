"""Fixed paths and idempotent creation of the mutable Stable data tree."""

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class RuntimeLayout:
    root: Path

    @property
    def data(self) -> Path:
        return self.root / "data"

    @property
    def config(self) -> Path:
        return self.data / "config"

    @property
    def jobs_config(self) -> Path:
        return self.config / "jobs"

    @property
    def state(self) -> Path:
        return self.data / "state"

    @property
    def database(self) -> Path:
        return self.state / "manager.sqlite3"

    @property
    def executor_state(self) -> Path:
        return self.state / "executor"

    @property
    def commands(self) -> Path:
        return self.data / "commands"

    @property
    def commands_incoming(self) -> Path:
        return self.commands / "incoming"

    @property
    def commands_accepted(self) -> Path:
        return self.commands / "accepted"

    @property
    def commands_completed(self) -> Path:
        return self.commands / "completed"

    @property
    def commands_rejected(self) -> Path:
        return self.commands / "rejected"

    @property
    def logs(self) -> Path:
        return self.data / "logs"

    @property
    def public(self) -> Path:
        return self.data / "public"

    @property
    def public_logs(self) -> Path:
        return self.public / "logs"

    @property
    def temp(self) -> Path:
        return self.data / "temp"

    def mutable_directories(self) -> tuple[Path, ...]:
        return (
            self.jobs_config,
            self.executor_state,
            self.commands_incoming,
            self.commands_accepted,
            self.commands_completed,
            self.commands_rejected,
            self.logs,
            self.public_logs,
            self.temp,
        )


def initialize_data_layout(layout: RuntimeLayout) -> None:
    """Create only the fixed mutable directories; never remove existing data."""
    if not (layout.root / "backup-system.root").is_file():
        raise ValueError("runtime root marker is missing")
    for directory in layout.mutable_directories():
        directory.mkdir(parents=True, exist_ok=True)
