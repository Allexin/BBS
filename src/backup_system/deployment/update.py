"""Non-elevated local Dev-to-Stable application update."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import time
from collections.abc import Sequence
from pathlib import Path
from uuid import uuid4

from backup_system.deployment.manifest import load_deployment_manifest, stage_release

SERVICE_RUNNING = "SERVICE_RUNNING"
SERVICE_STOPPED = "SERVICE_STOPPED"


class UpdateError(RuntimeError):
    pass


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="bbs-update")
    parser.add_argument("--source", type=Path, default=Path.cwd())
    parser.add_argument("--stable", type=Path, required=True)
    parser.add_argument("--service", default="BBS")
    parser.add_argument("--nssm", type=Path, required=True)
    parser.add_argument("--uv", type=Path, required=True)
    return parser


def update(
    *, source: Path, stable: Path, service: str, nssm: Path, uv: Path
) -> str:
    print("[1/7] Validating Dev and Stable paths...", flush=True)
    source = source.resolve(strict=True)
    stable = stable.resolve(strict=True)
    if source == stable or source.is_relative_to(stable) or stable.is_relative_to(source):
        raise UpdateError("Dev and Stable trees must be separate")
    if not (stable / "backup-system.root").is_file():
        raise UpdateError("Stable root marker is missing")
    print(f"[2/7] Checking that service {service!r} is stopped...", flush=True)
    status = _service_status(nssm, service)
    if status != SERVICE_STOPPED:
        raise UpdateError(
            f"service {service!r} must be stopped manually; current status is {status}"
        )
    targets = {name: stable / name for name in ("app", ".venv", "web")}
    if any(not path.is_dir() or path.is_symlink() for path in targets.values()):
        raise UpdateError("Stable application targets are missing or unsafe")

    revision = _git_revision(source)
    staging = source / ".poc-work" / f"stable-update-{uuid4()}"
    try:
        print("[3/7] Staging files from the deployment manifest...", flush=True)
        manifest = load_deployment_manifest(source / "deployment-manifest.json")
        stage_release(source, staging, manifest)
        print("[4/7] Building the frozen Stable virtual environment...", flush=True)
        _run_checked(
            [
                str(uv),
                "sync",
                "--frozen",
                "--no-editable",
                "--project",
                str(staging / "app"),
            ],
            env={**os.environ, "UV_PROJECT_ENVIRONMENT": str(staging / ".venv")},
            visible=True,
        )
        for index, (name, target) in enumerate(targets.items(), start=1):
            print(f"[5/7] Replacing {name} ({index}/3)...", flush=True)
            prepared = staging / name
            if not prepared.is_dir():
                raise UpdateError(f"prepared release is missing {name}")
            replace_tree_contents(prepared, target)
    finally:
        if staging.exists():
            print("[6/7] Removing temporary staging files...", flush=True)
            shutil.rmtree(staging)

    print(
        f"[7/7] Stable application files replaced. Start service {service!r} manually; "
        "waiting for SERVICE_RUNNING.",
        flush=True,
    )
    while (status := _service_status(nssm, service)) != SERVICE_RUNNING:
        print(f"Waiting for {service!r}: current status is {status}", flush=True)
        time.sleep(1)
    return revision


def replace_tree_contents(source: Path, target: Path) -> None:
    for child in target.iterdir():
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child)
        else:
            child.unlink()
    for child in source.iterdir():
        destination = target / child.name
        if child.is_dir() and not child.is_symlink():
            shutil.copytree(child, destination, copy_function=shutil.copy2)
        else:
            shutil.copy2(child, destination, follow_symlinks=False)


def _service_status(nssm: Path, service: str) -> str:
    result = _run_checked([str(nssm), "status", service])
    return (result.stdout + "\n" + result.stderr).replace("\x00", "").strip()


def _git_revision(source: Path) -> str:
    result = _run_checked(["git", "-C", str(source), "rev-parse", "HEAD"])
    revision = result.stdout.strip()
    if len(revision) != 40:
        raise UpdateError("Git revision is not a full commit hash")
    return revision


def _run_checked(
    argv: Sequence[str],
    *,
    env: dict[str, str] | None = None,
    visible: bool = False,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        argv,
        check=False,
        capture_output=not visible,
        text=True,
        shell=False,
        env=env,
    )
    if result.returncode != 0:
        diagnostic = result.stderr.strip() if result.stderr else "see command output above"
        raise UpdateError(f"command failed ({result.returncode}): {diagnostic}")
    return result


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    try:
        revision = update(
            source=arguments.source,
            stable=arguments.stable,
            service=arguments.service,
            nssm=arguments.nssm,
            uv=arguments.uv,
        )
    except (OSError, ValueError, UpdateError) as error:
        print(json.dumps({"result": "failed", "error": str(error)}, ensure_ascii=False))
        return 1
    print(json.dumps({"result": "success", "revision": revision}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
