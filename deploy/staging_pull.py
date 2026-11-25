#!/usr/bin/env python3
"""Safely fast-forward the isolated vpn4us staging checkout after GitHub CI succeeds.

Does not edit /opt/vpn-bot, invoke sudo, restart services, or touch SQLite/3x-ui.
Run only as the unprivileged vpn4us-deploy user.
"""
import argparse
import json
import os
import pwd
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

STAGING = Path("/home/vpn4us-deploy/staging/repo")
REMOTE_URL = "https://github.com/lestat258/vpn_bot.git"
BRANCH = "automation/bootstrap-2026-11-25"
WORKFLOW = ".github/workflows/source-checks.yml"
WORKFLOW_ID = 430228354
WORKFLOW_BLOB = "2c9e86ffe85fc58f6d614bf5e32edc6d407479d5"
REGRESSION_WORKFLOW = ".github/workflows/vpn-regressions.yml"
REGRESSION_WORKFLOW_ID = 430273074
REGRESSION_WORKFLOW_BLOB = "786e99ffe04ac4a934ea6c748fa0337f19b116a7"
API = "https://api.github.com/repos/lestat258/vpn_bot/actions/runs"


class SyncError(Exception):
    pass


def git(*args, expect_success=True):
    try:
        p = subprocess.run(
            ["git", "-C", str(STAGING), *args],
            text=True,
            capture_output=True,
            timeout=90,
            check=False,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_PAGER": "cat"},
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SyncError("Git execution failed: " + type(exc).__name__) from exc
    if expect_success and p.returncode != 0:
        raise SyncError(f"Git {args[0]} failed (exit {p.returncode}): {p.stderr[-400:].strip()}")
    return p


def verified_ci(sha):
    params = urllib.parse.urlencode({"branch": BRANCH, "per_page": "100"})
    url = API + "?" + params
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "vpn4us-staging-pull/1.0",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            data = json.load(response)
    except (OSError, ValueError, urllib.error.HTTPError) as exc:
        raise SyncError("Unable to verify GitHub Actions: " + type(exc).__name__) from exc
    for workflow_id, workflow_path in (
        (WORKFLOW_ID, WORKFLOW),
        (REGRESSION_WORKFLOW_ID, REGRESSION_WORKFLOW),
    ):
        runs = [
            run for run in data.get("workflow_runs", [])
            if run.get("head_sha") == sha
            and run.get("workflow_id") == workflow_id
            and run.get("path") == workflow_path
            and run.get("event") == "push"
        ]
        if not runs:
            raise SyncError("Required GitHub Actions run missing: " + workflow_path)
        if not any(
            run.get("status") == "completed" and run.get("conclusion") == "success"
            for run in runs
        ):
            raise SyncError("Required GitHub Actions did not pass: " + workflow_path)
        print("GitHub Actions: SUCCESS for " + workflow_path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--check", action="store_true", help="Fetch/check only; do not update staging worktree")
    modes.add_argument("--sync", action="store_true", help="Update staging worktree after checks pass")
    args = parser.parse_args()

    if pwd.getpwuid(os.geteuid()).pw_name != "vpn4us-deploy":
        raise SyncError("Must run as vpn4us-deploy, not root")
    if not STAGING.is_dir() or not (STAGING / ".git").is_dir():
        raise SyncError("Staging checkout is missing")
    if STAGING.resolve() != STAGING:
        raise SyncError("Staging directory is a symlink; refusing")
    if git("config", "--get", "remote.origin.url").stdout.strip() != REMOTE_URL:
        raise SyncError("Unexpected Git remote")
    if git("symbolic-ref", "--short", "HEAD").stdout.strip() != BRANCH:
        raise SyncError("Unexpected local branch")
    if git("status", "--porcelain", "--untracked-files=all").stdout.strip():
        raise SyncError("Staging checkout has local changes; refusing")

    current = git("rev-parse", "HEAD").stdout.strip()
    git("fetch", "--no-tags", "--no-recurse-submodules", "origin", f"refs/heads/{BRANCH}")
    target = git("rev-parse", "FETCH_HEAD").stdout.strip()
    if not git("merge-base", "--is-ancestor", current, target, expect_success=False).returncode == 0:
        raise SyncError("Remote branch is not a fast-forward from staging; refusing")
    for workflow_path, expected_blob in (
        (WORKFLOW, WORKFLOW_BLOB),
        (REGRESSION_WORKFLOW, REGRESSION_WORKFLOW_BLOB),
    ):
        target_workflow_blob = git("rev-parse", f"{target}:{workflow_path}").stdout.strip()
        if target_workflow_blob != expected_blob:
            raise SyncError("Verification workflow changed; manual reapproval required: " + workflow_path)

    print("Current staging:", current[:12])
    print("Remote candidate:", target[:12])
    verified_ci(target)

    if target == current:
        print("Already current; no staging changes")
        return

    if args.check:
        print("CHECK OK: candidate eligible. Staging worktree NOT changed.")
        return

    git("-c", "core.hooksPath=/dev/null", "merge", "--ff-only", "--no-edit", target)
    if git("rev-parse", "HEAD").stdout.strip() != target:
        raise SyncError("Staging HEAD does not match expected target after fast-forward")
    print("SYNC OK: only isolated staging checkout updated.")
    print("Production /opt/vpn-bot was NOT modified.")


if __name__ == "__main__":
    try:
        main()
    except SyncError as exc:
        print("BLOCKED:", exc, file=sys.stderr)
        sys.exit(1)
