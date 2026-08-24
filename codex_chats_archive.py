#!/usr/bin/env python3
"""Preview and archive old Codex tasks. Never deletes anything.

    python3 codex_task_cleaner.py --days 7          # dry run
    python3 codex_task_cleaner.py --days 7 --apply  # review, confirm, archive
"""

from __future__ import annotations

import argparse
import json
import os
import re
import select
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any


LOG_PATH = Path.home() / ".codex-task-cleaner.jsonl"
CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f-\x9f]")


class CleanerError(RuntimeError):
    pass


def clean_text(value: object, limit: int = 100) -> str:
    text = " ".join(CONTROL_CHARS.sub(" ", str(value or "")).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


class CodexClient:
    """Tiny client for Codex's newline-delimited app-server protocol."""

    def __init__(self) -> None:
        if shutil.which("codex") is None:
            raise CleanerError("The `codex` command is not installed or is not on PATH.")
        self.process = subprocess.Popen(
            ["codex", "app-server", "--listen", "stdio://"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        self.request_id = 0
        self.call(
            "initialize",
            {
                "clientInfo": {"name": "codex-task-cleaner", "version": "1.0"},
                "capabilities": {"experimentalApi": False},
            },
        )
        self.send({"method": "initialized"})

    def send(self, message: dict[str, Any]) -> None:
        if self.process.stdin is None:
            raise CleanerError("Codex app-server input is unavailable.")
        self.process.stdin.write(json.dumps(message) + "\n")
        self.process.stdin.flush()

    def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self.request_id += 1
        request_id = self.request_id
        self.send({"id": request_id, "method": method, "params": params})
        deadline = time.monotonic() + 20

        while True:
            if self.process.stdout is None:
                raise CleanerError("Codex app-server output is unavailable.")
            timeout = deadline - time.monotonic()
            if timeout <= 0 or not select.select([self.process.stdout], [], [], timeout)[0]:
                raise CleanerError("Timed out while waiting for Codex.")
            line = self.process.stdout.readline()
            if not line:
                raise CleanerError("Codex app-server closed unexpectedly.")
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            if message.get("id") != request_id:
                continue
            if "error" in message:
                raise CleanerError(f"Codex returned an error: {message['error']}")
            result = message.get("result")
            if not isinstance(result, dict):
                raise CleanerError("Codex returned an invalid response.")
            return result

    def close(self) -> None:
        if self.process.stdin is not None:
            self.process.stdin.close()
        try:
            self.process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self.process.terminate()
            self.process.wait(timeout=2)


def fetch_threads() -> list[dict[str, Any]]:
    """Ask Codex for non-archived tasks without reading its data files."""
    client = CodexClient()
    tasks: dict[str, dict[str, Any]] = {}
    cursor: str | None = None
    try:
        while True:
            result = client.call(
                "thread/list",
                {
                    "archived": False,
                    "cursor": cursor,
                    "limit": 100,
                    "sortKey": "recency_at",
                    "sortDirection": "asc",
                    "useStateDbOnly": True,
                },
            )
            data = result.get("data")
            if not isinstance(data, list):
                raise CleanerError("Codex returned an invalid task list.")
            for task in data:
                if isinstance(task, dict) and isinstance(task.get("id"), str):
                    tasks[task["id"]] = task
            cursor = result.get("nextCursor")
            if not isinstance(cursor, str) or not cursor:
                return list(tasks.values())
    finally:
        client.close()


def last_activity(task: dict[str, Any]) -> int:
    values = [
        value
        for key in ("createdAt", "updatedAt", "recencyAt")
        if isinstance((value := task.get(key)), int)
    ]
    return max(values, default=0)


def is_candidate(task: dict[str, Any], cutoff: int) -> bool:
    """Protect recent, active, pinned/organized, ephemeral, and subagent tasks."""
    status = task.get("status")
    status_type = status.get("type") if isinstance(status, dict) else None
    return (
        task.get("ephemeral") is not True
        and task.get("parentThreadId") is None
        and task.get("section") is None
        and status_type in {"idle", "notLoaded"}
        and last_activity(task) <= cutoff
    )


def title(task: dict[str, Any]) -> str:
    return clean_text(task.get("name") or task.get("preview") or "Untitled task")


def show(candidates: list[dict[str, Any]], days: int) -> None:
    print(f"Found {len(candidates)} unprotected task(s) inactive for {days}+ day(s).")
    for number, task in enumerate(candidates, 1):
        updated = datetime.fromtimestamp(last_activity(task)).astimezone()
        print(f"\n{number}. {title(task)}")
        print(f"   ID: {task['id']}")
        print(f"   Last active: {updated:%Y-%m-%d %H:%M %Z}")
        print(f"   Folder: {clean_text(task.get('cwd'), 140)}")


def prepare_log() -> None:
    try:
        descriptor = os.open(LOG_PATH, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
        os.close(descriptor)
        os.chmod(LOG_PATH, 0o600)
    except OSError as exc:
        raise CleanerError(f"Cannot write audit log {LOG_PATH}: {exc}") from exc


def log_archive(task: dict[str, Any]) -> None:
    record = {
        "action": "archive",
        "archived_at": datetime.now().astimezone().isoformat(),
        "thread_id": task["id"],
        "title": title(task),
        "last_activity": last_activity(task),
    }
    with LOG_PATH.open("a", encoding="utf-8") as log:
        log.write(json.dumps(record, ensure_ascii=False) + "\n")


def archive(candidates: list[dict[str, Any]]) -> int:
    if not sys.stdin.isatty():
        raise CleanerError("`--apply` requires an interactive terminal confirmation.")

    expected = f"ARCHIVE {len(candidates)}"
    if input(f"\nType {expected} to archive this batch: ").strip() != expected:
        print("Cancelled. No tasks were archived.")
        return 0

    prepare_log()
    failures = 0
    for task in candidates:
        try:
            result = subprocess.run(
                ["codex", "archive", task["id"]],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
        except subprocess.TimeoutExpired:
            failures += 1
            print(f"Failed: {title(task)} (timed out)", file=sys.stderr)
            continue
        if result.returncode:
            failures += 1
            detail = clean_text(result.stderr or result.stdout, 180)
            print(f"Failed: {title(task)} ({detail})", file=sys.stderr)
            continue
        log_archive(task)
        print(f"Archived: {title(task)}")

    print(f"\nAudit log: {LOG_PATH}")
    print("Restore with: codex unarchive <task-id>")
    return 1 if failures else 0


def positive_days(value: str) -> int:
    days = int(value)
    if days < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return days


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Preview old Codex tasks and archive a reviewed batch without deleting it."
    )
    parser.add_argument("--days", type=positive_days, default=7)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    try:
        cutoff = int(time.time()) - args.days * 86_400
        candidates = sorted(
            (task for task in fetch_threads() if is_candidate(task, cutoff)),
            key=last_activity,
        )
        show(candidates, args.days)
        if not candidates:
            return 0
        if not args.apply:
            print(f"\nDry run only. Review, then rerun with --days {args.days} --apply.")
            return 0
        return archive(candidates)
    except CleanerError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
