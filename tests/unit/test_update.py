from pathlib import Path

from backup_system.deployment import update as update_module
from backup_system.deployment.update import replace_tree_contents


def test_replace_tree_contents_preserves_target_root(tmp_path: Path) -> None:
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.mkdir()
    target.mkdir()
    (source / "new.txt").write_text("new", encoding="ascii")
    (source / "nested").mkdir()
    (source / "nested" / "file.txt").write_text("nested", encoding="ascii")
    (target / "old.txt").write_text("old", encoding="ascii")
    target_identity = target.stat().st_ino

    replace_tree_contents(source, target)

    assert target.stat().st_ino == target_identity
    assert not (target / "old.txt").exists()
    assert (target / "new.txt").read_text(encoding="ascii") == "new"
    assert (target / "nested" / "file.txt").read_text(encoding="ascii") == "nested"


def test_update_installs_portable_launcher(monkeypatch, tmp_path: Path) -> None:
    source = tmp_path / "dev"
    stable = tmp_path / "stable"
    source.mkdir()
    stable.mkdir()
    (stable / "backup-system.root").touch()
    for name in ("app", ".venv", "web"):
        (stable / name).mkdir()
    (source / "deployment-manifest.json").write_text("{}", encoding="ascii")

    def stage(_source: Path, staging: Path, _manifest: object) -> None:
        for name in ("app", ".venv", "web"):
            (staging / name).mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(update_module, "load_deployment_manifest", lambda path: object())
    monkeypatch.setattr(update_module, "stage_release", stage)
    monkeypatch.setattr(update_module, "_service_status", lambda nssm, service: "SERVICE_STOPPED")
    monkeypatch.setattr(update_module, "_git_revision", lambda root: "a" * 40)
    monkeypatch.setattr(update_module, "_run_checked", lambda *args, **kwargs: None)
    statuses = iter(("SERVICE_STOPPED", "SERVICE_RUNNING"))
    monkeypatch.setattr(update_module, "_service_status", lambda nssm, service: next(statuses))
    monkeypatch.setattr(update_module.time, "sleep", lambda seconds: None)

    update_module.update(
        source=source,
        stable=stable,
        service="BBS-Test",
        nssm=tmp_path / "nssm.exe",
        uv=tmp_path / "uv.exe",
    )

    assert (stable / "backupctl.bat").is_file()
