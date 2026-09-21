"""Fast-forward-only production updates from the fixed stable branch."""
from __future__ import annotations

from datetime import datetime, timezone
import os
from pathlib import Path
import re
import subprocess


def update_from_main(repository: Path, environment: dict[str, str]) -> tuple[bool, list[dict]]:
    steps: list[dict] = []

    def git(*arguments: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", "-c", f"core.hooksPath={os.devnull}", *arguments],
            cwd=repository, env=environment, capture_output=True, text=True,
            timeout=180, check=False,
        )

    def stop(message: str) -> tuple[bool, list[dict]]:
        steps.append({"name": "Stable update", "rc": 1, "out": message})
        return False, steps

    try:
        status = git("status", "--porcelain", "--untracked-files=normal")
        if status.returncode or status.stdout.strip():
            return stop("Update stopped: the checkout must be clean. Local changes were not removed.")
        branch = git("symbolic-ref", "--quiet", "--short", "HEAD")
        if branch.returncode:
            return stop("Update stopped: detached or rollback checkouts require an explicit operator decision.")
        before = git("rev-parse", "--verify", "HEAD")
        if before.returncode or not re.fullmatch(r"[0-9a-f]{40,64}", before.stdout.strip()):
            return stop("Update stopped: current commit could not be verified.")
        previous = before.stdout.strip()
        fetched = git("fetch", "--no-tags", "origin", "refs/heads/main:refs/remotes/origin/main")
        if fetched.returncode:
            return stop("Could not fetch stable origin/main. No other branch was used.")
        resolved = git("rev-parse", "--verify", "refs/remotes/origin/main^{commit}")
        target = resolved.stdout.strip()
        if resolved.returncode or not re.fullmatch(r"[0-9a-f]{40,64}", target):
            return stop("Update stopped: stable origin/main could not be verified.")
        if git("merge-base", "--is-ancestor", previous, target).returncode:
            return stop("Update stopped: local commits are ahead of or diverge from origin/main. Nothing was reset or merged.")
        local_main = git("show-ref", "--verify", "--quiet", "refs/heads/main")
        if local_main.returncode not in (0, 1):
            return stop("Update stopped: local main could not be inspected.")
        if local_main.returncode == 0 and git("merge-base", "--is-ancestor", "refs/heads/main", target).returncode:
            return stop("Update stopped: local main diverges from origin/main. Nothing was reset or merged.")
        if previous != target or branch.stdout.strip() != "main":
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            rollback = f"rollback/pre-update-{stamp}-{previous[:12]}"
            if git("branch", rollback, previous).returncode:
                return stop("Update stopped: the previous commit could not be retained for rollback.")
            steps.append({"name": "Rollback checkpoint", "rc": 0, "out": f"{rollback}: {previous}"})
        if branch.stdout.strip() != "main":
            switched = git("switch", "--no-guess", "main") if local_main.returncode == 0 else git("switch", "-c", "main", previous)
            if switched.returncode:
                return stop("Update stopped: could not switch to stable main. The previous commit is retained.")
        if git("merge", "--ff-only", target).returncode:
            return stop("Update stopped: stable main could not be fast-forwarded. The rollback checkpoint is retained.")
        if git("branch", "--set-upstream-to=origin/main", "main").returncode:
            return stop("Stable files were selected, but tracking origin/main failed. Restart was not requested.")
        current = git("rev-parse", "--verify", "HEAD")
        selected = git("symbolic-ref", "--quiet", "--short", "HEAD")
        if current.returncode or selected.returncode or current.stdout.strip() != target or selected.stdout.strip() != "main":
            return stop("Stable checkout verification failed. Restart was not requested.")
        steps.append({"name": "Stable origin/main", "rc": 0, "out": f"Verified main at {target}. Restart separately after dependencies succeed."})
        return True, steps
    except (OSError, subprocess.SubprocessError):
        return stop("Stable update command failed or timed out. Inspect the checkout before retrying; no restart was requested.")
