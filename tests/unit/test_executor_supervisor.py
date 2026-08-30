import io

from backup_system.manager import executor_supervisor


class _Process:
    stdin = io.BytesIO()

    def wait(self) -> int:
        return 17


def test_supervisor_requires_start_frame(monkeypatch) -> None:
    monkeypatch.setattr(executor_supervisor.sys, "stdin", _TextInput(b"wrong\n"))
    assert executor_supervisor.main(["--", "executor.exe"]) == 2


class _TextInput:
    def __init__(self, value: bytes) -> None:
        self.buffer = io.BytesIO(value)


def test_supervisor_starts_exact_argv_without_shell(monkeypatch) -> None:
    monkeypatch.setattr(executor_supervisor.sys, "stdin", _TextInput(b"start\n"))
    monkeypatch.setattr(executor_supervisor.sys, "stdout", _TextInput(b""))
    monkeypatch.setattr(executor_supervisor.sys, "stderr", _TextInput(b""))
    captured = {}

    def popen(argv, **kwargs):
        captured.update(argv=argv, kwargs=kwargs)
        return _Process()

    monkeypatch.setattr(executor_supervisor.subprocess, "Popen", popen)
    monkeypatch.setattr(executor_supervisor.threading.Thread, "start", lambda self: None)
    assert executor_supervisor.main(["--", "python.exe", "-m", "worker"]) == 17
    assert captured["argv"] == ["python.exe", "-m", "worker"]
    assert captured["kwargs"]["shell"] is False
