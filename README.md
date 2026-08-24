# Codex chat archive

A small Python script for clearing old one-shot tasks from the Codex UI. It lists inactive tasks first, then archives the reviewed batch. It never deletes chats.

## Requirements

- Python 3.10+
- `codex` on `PATH` with the `app-server`, `archive`, and `unarchive` commands

## Use

Preview tasks inactive for seven days:

```bash
python3 codex_chats_archive.py --days 7
```

Archive the same selection:

```bash
python3 codex_chats_archive.py --days 7 --apply
```

The second command asks you to type `ARCHIVE <count>` before changing anything. The script skips active, pinned, organized, ephemeral, and subagent tasks.

Successful actions go to `~/.codex-task-cleaner.jsonl`. Restore a task with:

```bash
codex unarchive <task-id>
```

## Caveat

`codex app-server` is experimental. A future Codex update may require a script update.
