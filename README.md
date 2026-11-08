# Git Scout

Git Scout monitors local Git working trees and remote default branches. It uses
Python 3.9+ and Git, with no additional packages.

```sh
python3 git_scout.py ~/Projects
python3 git_scout.py ~/Projects --needs-attention
python3 git_scout.py ~/Projects --gitea-imac --record
```

Local scans include linked worktrees and skip hidden subdirectories. The
attention view shows modified or untracked paths, ahead or behind counts, and
errors. Ahead and behind counts use locally recorded upstream refs.

Use `--remote URL` repeatedly to include other remotes. Remote checks read the
default branch and commit without fetching objects. The `--gitea-imac` shortcut
uses the iMac's existing SSH alias to check `personal-site` and `python-project`.

## Compare scans

Run the same scan twice with `--record`, then use:

```sh
python3 git_scout.py ~/Projects --history
python3 git_scout.py ~/Projects --compare
```

Snapshots are grouped by absolute scan directory. Keep the same remote options
across recordings to compare the same set of targets. SQLite history is stored
at `~/.local/state/git-scout/history.sqlite3` by default. Use `--db PATH` to
choose a different location. Snapshots record Git status and commit IDs, not
file contents. A scan with an error returns status 1 and still saves the
results when `--record` is set.
