"""Scan Git working trees and compare recorded snapshots."""

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys


def working_trees(root):
    """Find working trees, including linked worktrees with a .git file."""
    for directory, subdirs, files in os.walk(root):
        if ".git" in subdirs or ".git" in files:
            yield Path(directory)
        subdirs[:] = sorted(name for name in subdirs if not name.startswith("."))


def git(*args):
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    env.setdefault("GIT_SSH_COMMAND", "ssh -o BatchMode=yes")
    try:
        result = subprocess.run(
            ["git", "--no-optional-locks", *args],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=15,
            env=env,
        )
    except subprocess.TimeoutExpired:
        return "", "Git command timed out after 15 seconds"
    except FileNotFoundError:
        return "", "Git executable not found"
    if result.returncode:
        return "", result.stderr.strip() or f"Git exited with status {result.returncode}"
    return result.stdout, None


def inspect_tree(repo):
    output, error = git("-C", str(repo), "status", "--porcelain=v2", "--branch")
    if error:
        return {"error": error}
    headers = {}
    changes = []
    for line in output.splitlines():
        if line.startswith("# "):
            name, _, value = line[2:].partition(" ")
            headers[name] = value
        else:
            changes.append(line)

    ahead, behind = map(int, headers.get("branch.ab", "+0 -0").split())
    return {
        "branch": headers.get("branch.head"),
        "head": headers.get("branch.oid"),
        "upstream": headers.get("branch.upstream"),
        "ahead": ahead,
        "behind": abs(behind),
        "changes": changes,
    }


def inspect_remote(url):
    output, error = git("ls-remote", "--symref", "--", url, "HEAD")
    if error:
        return {"error": error}
    branch = None
    head = None
    for line in output.splitlines():
        ref, _, name = line.partition("\t")
        if name == "HEAD":
            if ref.startswith("ref: "):
                branch = ref.removeprefix("ref: refs/heads/")
            else:
                head = ref
    return {"branch": branch, "head": head}


def scan(root, remotes):
    tasks = [
        (f"local {repo.relative_to(root)}", inspect_tree, repo)
        for repo in working_trees(root)
    ]
    tasks.extend((f"remote {url}", inspect_remote, url) for url in dict.fromkeys(remotes))
    with ThreadPoolExecutor(max_workers=8) as pool:
        pending = [(label, pool.submit(inspect, value)) for label, inspect, value in tasks]
        return {label: future.result() for label, future in pending}


def summarize(state):
    if "error" in state:
        return f"error: {state['error']}"
    branch = state["branch"] or "no default branch"
    head = state["head"]
    summary = f"{branch} @ {head[:10]}" if head and head != "(initial)" else f"{branch} (no commits)"
    if "changes" in state:
        changes = state["changes"]
        summary += f" | {len(changes)} changed paths" if changes else " | clean"
        if state["upstream"]:
            summary += f" | {state['upstream']} +{state['ahead']}/-{state['behind']}"
    return summary


def report(snapshot, attention_only=False):
    for label, state in snapshot.items():
        if attention_only and not any(
            state.get(field) for field in ("error", "changes", "ahead", "behind")
        ):
            continue
        print(f"{label}: {summarize(state)}")


def compare(before, after):
    changed = False
    for label in sorted(before.keys() | after.keys()):
        old = before.get(label)
        new = after.get(label)
        if old == new:
            continue
        changed = True
        if old is None:
            print(f"+ {label}: {summarize(new)}")
        elif new is None:
            print(f"- {label}: {summarize(old)}")
        else:
            fields = ", ".join(
                field for field in sorted(old.keys() | new.keys())
                if old.get(field) != new.get(field)
            )
            print(f"~ {label} [{fields}]")
            print(f"  before: {summarize(old)}")
            print(f"  after:  {summarize(new)}")
    if not changed:
        print("No changes between the last two snapshots.")


def snapshot_history(path, root, snapshot=None):
    if snapshot is not None:
        path.parent.mkdir(parents=True, exist_ok=True)
    elif not path.is_file():
        return []
    with sqlite3.connect(path) as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS snapshots (
                id INTEGER PRIMARY KEY,
                root TEXT NOT NULL,
                recorded_at TEXT NOT NULL,
                data TEXT NOT NULL
            )
        """)
        db.execute("CREATE INDEX IF NOT EXISTS snapshots_root ON snapshots(root, id)")
        if snapshot is not None:
            recorded_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
            db.execute(
                "INSERT INTO snapshots (root, recorded_at, data) VALUES (?, ?, ?)",
                (str(root), recorded_at, json.dumps(snapshot)),
            )
            return []
        return db.execute(
            "SELECT id, recorded_at, data FROM snapshots WHERE root = ? ORDER BY id DESC LIMIT 20",
            (str(root),),
        ).fetchall()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", nargs="?", type=Path, default=Path.cwd())
    parser.add_argument("--db", type=Path, default=Path.home() / ".local/state/git-scout/history.sqlite3")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--record", action="store_true", help="save the current scan")
    mode.add_argument("--history", action="store_true", help="list the latest 20 saved scans")
    mode.add_argument("--compare", action="store_true", help="compare the latest two saved scans")
    parser.add_argument("--remote", action="append", default=[], metavar="URL", help="check a remote HEAD (repeatable)")
    parser.add_argument("--gitea-imac", action="store_true", help="check personal-site and python-project via SSH")
    parser.add_argument("--needs-attention", action="store_true", help="show changes, divergence, and errors")
    args = parser.parse_args()
    if (args.history or args.compare) and (args.remote or args.gitea_imac or args.needs_attention):
        parser.error("remote and attention options apply only to live scans")

    root = args.directory.expanduser().resolve()
    db_path = args.db.expanduser()
    try:
        if args.history or args.compare:
            rows = snapshot_history(db_path, root)
            if args.compare:
                if len(rows) < 2:
                    parser.error("record two snapshots of this directory first")
                compare(json.loads(rows[1][2]), json.loads(rows[0][2]))
            elif rows:
                for row_id, recorded_at, data in rows:
                    print(f"{row_id}  {recorded_at}  {len(json.loads(data))} targets")
            else:
                print(f"No snapshots recorded for {root}")
            return 0

        if not root.is_dir():
            parser.error(f"directory does not exist: {root}")
        remotes = args.remote
        if args.gitea_imac:
            remotes.extend(
                f"gitea-imac:jzzliu/{name}.git" for name in ("personal-site", "python-project")
            )
        snapshot = scan(root, remotes)
        if snapshot:
            report(snapshot, args.needs_attention)
        else:
            print(f"No Git working trees found under {root}")
        if args.record:
            snapshot_history(db_path, root, snapshot)
            print(f"Snapshot saved to {db_path}")
        return int(any("error" in state for state in snapshot.values()))
    except (OSError, sqlite3.Error) as error:
        print(f"Git Scout: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
