# MindSlice-manage

English | [中文](https://github.com/pancerZH/MindSlice-manage/blob/main/README-zh.md)

One place to see and align the versions of a group of related repos: where each project writes its own version, what downstream projects require of upstream ones (minimum version, exact pin, lockfile), and the copies installed on this machine (pipx, virtual environments, host plugins).

It only does local work: a dashboard, checks, file edits, builds and tests. It never commits, pushes, tags, releases or installs plugins; for those steps it prints the commands to run.

The command-line output and the prompt that discover gives the agent are in Chinese.

## Install

Python 3.12+, standard library only.

```sh
pipx install -e .
cp config.example.toml config.toml   # then list your repos
```

`config.toml` is looked up in the current directory, then in this checkout; `-c` or `MINDSLICE_MANAGE_CONFIG` names another one.

## Usage

```sh
mindslice-manage discover          # an agent reads each repo without a spec and writes managed/<name>.toml; verified, then you confirm
mindslice-manage status            # one table: versions, releases, git, dependencies, installed copies
mindslice-manage check             # every inconsistency with its fix; exit code 1 on errors (and on warnings with --strict)
mindslice-manage bump <name> patch # change the project's own version everywhere; also minor / major / build / an explicit version
mindslice-manage sync [<name>]     # move pins and locks to the latest upstream release, then run build, verify and test
mindslice-manage test [<name>]     # run the tests
```

`status` and `check` take `--offline` (no PyPI or origin, local tags only), `--fetch` (git fetch first) and `--json`. `sync` takes `--to <version>` and `--dry-run`.

- discover gives the agent (agy with `gemini-4-hydrogen-high` by default) a separate copy of tracked and non-ignored untracked regular files. Symbolic links are skipped. Edits to the copy stay there. The program checks every entry of the spec against the real repo and hands errors back to the agent, for at most `harness.repairs` rounds.
- bump and sync refuse to run when a file they would change has uncommitted changes, and read every location back after the change.

`config.toml`, `managed/` and `runs/` hold machine paths and agent-written content, and stay out of git.

## Scheduled jobs

For projects whose spec lists scheduled jobs (systemd user timers), `status` adds a table: schedule, enabled or not, last run (time, result, duration), next run, recipients. `check` reports:
- a timer that is not installed or not enabled;
- a last run that failed;
- an installed unit that differs from the repo's template (compared with `@REPO@` and `%h` filled in on both sides, blank lines and comments left out);
- linger being off;
- a mail account file that is missing or lacks keys, or no recipients;
- a job that has not run yet.

manage changes neither systemd nor the projects' configs; it gives the command or the place instead:
- change the schedule: edit `OnCalendar` in the repo's template, then run the reinstall command check prints;
- enable or disable: `systemctl --user enable --now <timer>` or `disable --now <timer>`;
- change the recipients: `mail.to_where` in `status --json` names the file and key.

## Tests

```sh
PYTHONPATH=src:tests python3 -m unittest discover -s tests -t tests
```
