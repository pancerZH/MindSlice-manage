"""bump, sync and test: the commands that change files in a repo or run its commands. None of them commits
(release, in release.py, is the one command that does)."""

import os
import shlex
import subprocess
import sys
from pathlib import Path

from . import sources
from .check import Context, dep_values, own_version, pinned_version, release_steps
from .config import Command, Project
from .locations import Location, bumped, parse_version, read_value, rewrite, version_key


class ActionError(Exception):
    pass


def say(msg: str = "") -> None:
    print(msg, flush=True)


def run(cmd: list[str] | Command, repo: Path) -> bool:
    """Run one command in the repo; its output goes to stderr, so --json output stays clean."""
    c = cmd if isinstance(cmd, Command) else Command(cmd)
    say(f"$ {c}")
    try:
        try:
            err = sys.stderr.fileno()
        except (AttributeError, OSError):  # sys.stderr replaced by something without a file descriptor
            err = None
        if err is not None:
            done = subprocess.run(c.cmd, cwd=repo, env=os.environ | c.env, stdout=err, stderr=err)
        else:
            done = subprocess.run(c.cmd, cwd=repo, env=os.environ | c.env, capture_output=True, text=True)
            sys.stderr.write(done.stdout + done.stderr)
    except FileNotFoundError as e:
        say(f"  找不到命令：{e}")
        return False
    if done.returncode:
        say(f"  失败，退出码 {done.returncode}")
    return done.returncode == 0


def run_all(cmds: list[Command], repo: Path, label: str) -> bool:
    if not cmds:
        return True
    say(f"\n{label}：")
    return all([run(c, repo) for c in cmds])  # a list, so every test command runs even after one fails


def refuse_dirty(repo: Path, files: list[str]) -> None:
    if lines := sources.dirty(repo, sorted(set(files))):
        raise ActionError("这些文件有未提交的改动，先提交或还原：\n  " + "\n  ".join(lines))


def files_of(locs: list[Location]) -> list[str]:
    return [loc.file for loc in locs]


def updates_of(locs: list[Location]) -> list[list[str]]:
    seen, out = set(), []
    for loc in locs:
        if loc.update and tuple(loc.update) not in seen:
            seen.add(tuple(loc.update))
            out.append(loc.update)
    return out


def set_version(p: Project, version: str) -> list[str]:
    """Write version into every own-version location (generated ones by their update command); the files changed."""
    changed = [loc.file for loc in p.spec.version if not loc.update and rewrite(p.repo, loc, version)]
    for cmd in updates_of(p.spec.version):
        if not run(cmd, p.repo):
            raise ActionError(f"update 命令失败：{shlex.join(cmd)}")
    current, rows = own_version(p.repo, p.spec)
    if current != version:
        detail = ", ".join(f"{r['file']}={r.get('value', r.get('error'))}" for r in rows)
        raise ActionError(f"改完后版本号对不上 {version}：{detail}")
    return changed


def next_steps(p: Project, lines: list[str]) -> None:
    repo = shlex.quote(str(p.repo))
    say("\n改动（未提交）：")
    say(sources.git(p.repo, "diff", "--stat", check=False) or "  （无）")
    say("\n下一步（工具不会执行）：")
    for line in [f"git -C {repo} diff", f"git -C {repo} commit -am '<说明>'", f"git -C {repo} push", *lines]:
        say(f"  {line}")


# bump

def bump(ctx: Context, name: str, target: str) -> None:
    p = managed(ctx, name)
    current, rows = own_version(p.repo, p.spec)
    if errors := [r["error"] for r in rows if "error" in r]:
        raise ActionError("位置不再匹配，先重新 discover：\n  " + "\n  ".join(errors))
    if target in ("major", "minor", "patch", "build"):
        if current is None:
            raise ActionError("现在的版本号各处不一致，请给出明确的版本号：" + ", ".join(f"{r['file']}={r['value']}" for r in rows))
        if p.spec.follows and target != "build":
            raise ActionError(f"{name} 的版本号跟随 {p.spec.follows}，只能 bump build（或用 sync 跟进上游）")
        version = bumped(current, target)
    else:
        version = target
        try:
            parse_version(version)
        except ValueError as e:
            raise ActionError(str(e)) from None
    if version == current:
        say(f"{name} 已经是 {version}")
        return
    refuse_dirty(p.repo, files_of(p.spec.version))
    say(f"{name}: {current or '（不一致）'} → {version}")
    set_version(p, version)
    next_steps(p, release_steps(p, version))


# sync

def pin_values(ctx: Context, up: Project, version: str, kinds: set[str]) -> dict[str, str]:
    """The values a pin on up at version needs: version, commit (the tag's), wheel_url and wheel_sha256 (PyPI)."""
    values = {"version": version}
    if "commit" in kinds:
        commit = ctx.tag_commit(up.name, version)
        if not commit:
            raise ActionError(f"找不到 {up.name} {version} 的 tag（{up.spec.tag or '没有 tag 格式'}）")
        values["commit"] = commit
    if kinds & {"wheel_url", "wheel_sha256"}:
        if not ctx.online:
            raise ActionError("锁定 wheel 要联网读 PyPI，去掉 --offline")
        wheel = sources.pypi_wheel(up.spec.pypi, version) if up.spec.pypi else None
        if not wheel:
            raise ActionError(f"PyPI 上没有 {up.spec.pypi or up.name} {version} 的 wheel")
        values["wheel_url"], values["wheel_sha256"] = wheel
    return values


def plan_sync(ctx: Context, p: Project, to: str | None) -> list[tuple]:
    """What sync would do: ("pin", dep, values) and ("lock", dep, target) steps, for deps that are behind or wrong."""
    deps = [d for d in p.spec.deps if d.kind in ("pin", "lock")]
    if to and len({d.project for d in deps}) > 1:
        raise ActionError("--to 只能用于只依赖一个上游的项目")
    steps = []
    for dep in deps:
        up = ctx.by_name.get(dep.project)
        if not up or not up.spec:
            raise ActionError(f"依赖的项目 {dep.project} 不在 config.toml 里，或还没有 spec")
        target = to or ctx.latest_release(dep.project)
        if not target:
            raise ActionError(f"不知道 {dep.project} 最新发布的版本（离线时只看本地 tag）")
        rows = dep_values(p.repo, dep)
        if errors := [r["error"] for r in rows if "error" in r]:
            raise ActionError("位置不再匹配，先重新 discover：\n  " + "\n  ".join(errors))
        if dep.kind == "pin":
            values = pin_values(ctx, up, target, {loc.value for loc in dep.locations})
            if any(r["value"] != values[r["kind"]] for r in rows):
                steps.append(("pin", dep, values, pinned_version(rows)))
        else:
            current = {r["value"] for r in rows}
            if any(version_key(v) < version_key(target) for v in current) or to and current != {target}:
                if to:
                    commands = updates_of(dep.locations)
                    if not commands or any(len(cmd) != 4 or cmd[:3] != ["uv", "lock", "--upgrade-package"]
                                           for cmd in commands):
                        raise ActionError(f"{p.name}: lock 更新命令不支持 --to 指定版本")
                steps.append(("lock", dep, target, ", ".join(sorted(current))))
    return steps


def sync(ctx: Context, names: list[str], to: str | None, dry_run: bool, hints: bool = True) -> bool:
    """Bring pins and locks up to the upstream release; True if every build, verify and test passed.

    hints=False leaves out the printed next steps (release carries them out itself).
    """
    projects = [managed(ctx, n) for n in names] if names else ordered(ctx)
    ok, did = True, False
    for p in projects:
        steps = plan_sync(ctx, p, to)
        if not steps:
            if names:
                say(f"{p.name}: 已经是最新")
            continue
        did = True
        say(f"\n== {p.name}")
        new_version = None
        for kind, dep, target, current in steps:
            shown = target["version"] if kind == "pin" else target
            say(f"  {kind} {dep.project}: {current or '?'} → {shown}")
            if kind == "pin" and p.spec.follows == dep.project:
                new_version = f"{shown}-1"
        if new_version:
            say(f"  版本号 → {new_version}（跟随 {p.spec.follows}）")
        if dry_run:
            continue
        touched = [loc.file for _, dep, _, _ in steps for loc in dep.locations]
        refuse_dirty(p.repo, touched + (files_of(p.spec.version) if new_version else []))
        for kind, dep, target, _ in steps:
            if kind == "pin":
                for loc in dep.locations:
                    rewrite(p.repo, loc, target[loc.value])
                    if read_value(p.repo, loc) != target[loc.value]:
                        raise ActionError(f"{loc.file}: 改完后读回的值不对")
            else:
                for cmd in updates_of(dep.locations):
                    if to:
                        cmd = [*cmd[:-1], f"{cmd[-1]}=={target}"]
                    if not run(cmd, p.repo):
                        raise ActionError(f"update 命令失败：{shlex.join(cmd)}")
                after = {r.get("value", r.get("error")) for r in dep_values(p.repo, dep)}
                if after != {target}:
                    raise ActionError(f"跑完 update 后 {dep.project} 是 {', '.join(sorted(after))}，不是 {target}")
        if new_version:
            set_version(p, new_version)
        passed = (run_all(p.spec.build, p.repo, "build") and run_all(p.spec.verify, p.repo, "verify")
                  and run_all(p.spec.test, p.repo, "test"))
        ok &= passed
        say(f"\n{p.name}: " + ("build、verify、test 都通过" if passed else "有命令失败，见上面的输出"))
        if hints:
            next_steps(p, [plugin.hint for plugin in p.spec.plugins if plugin.hint])
    if not did and not names:
        say("没有落后的 pin 或 lock。")
    return ok


def ordered(ctx: Context) -> list[Project]:
    """Managed projects, each after the projects it depends on."""
    done: list[Project] = []

    def visit(p: Project, path: tuple) -> None:
        if p in done or p.name in path:
            return
        for dep in p.spec.deps:
            up = ctx.by_name.get(dep.project)
            if up and up.spec:
                visit(up, path + (p.name,))
        done.append(p)

    for p in ctx.projects:
        if p.spec and p.repo.is_dir():
            visit(p, ())
    return done


# test

def test(ctx: Context, names: list[str]) -> bool:
    projects = [managed(ctx, n) for n in names] if names else [p for p in ctx.projects if p.spec and p.repo.is_dir()]
    results = {}
    for p in projects:
        say(f"\n== {p.name}")
        if not p.spec.test:
            say("  没有 test 命令")
            continue
        results[p.name] = run_all(p.spec.test, p.repo, "test")
    say("\n" + "\n".join(f"  {'通过' if ok else '失败'}  {name}" for name, ok in results.items()))
    return all(results.values())


def managed(ctx: Context, name: str) -> Project:
    p = ctx.by_name.get(name)
    if not p:
        raise ActionError(f"没有叫 {name} 的项目（config.toml 里有：{', '.join(ctx.by_name)}）")
    if not p.spec:
        raise ActionError(f"{name} 还没有 spec：先 mindslice-manage discover {name}")
    if not p.repo.is_dir():
        raise ActionError(f"{p.repo} 不存在")
    return p
