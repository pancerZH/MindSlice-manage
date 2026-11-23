"""release: publish one project and bring along everything that depends on it, unattended.

Unlike the other commands, release commits, pushes, tags, publishes and installs (user decision 2026-11-22).
Each step looks at the state first and skips what is already done, so after a failure the same command
continues where it stopped.
"""

import json
import shutil
import subprocess
import time
from pathlib import Path

from . import actions, check, sources
from .actions import ActionError, say
from .check import Context
from .config import Project
from .locations import parse_version

POLL = 15  # seconds between looks at CI and PyPI
CI_MINUTES = 40
CI_APPEARS_MINUTES = 3  # a repo whose workflows do not run on this push shows no run at all
PYPI_MINUTES = 30
RETRIES = 6  # PyPI's index lags behind an upload for a few minutes, for uv lock and pip alike
GOOD = {"success", "skipped", "neutral"}


def release(ctx: Context, name: str, version: str, dry_run: bool = False) -> bool:
    p = actions.managed(ctx, name)
    try:
        parse_version(version)
    except ValueError as e:
        raise ActionError(str(e)) from None
    if not p.spec.tag:
        raise ActionError(f"{name} 的 spec 没有 tag 格式（[release] tag），不知道怎么发布")
    if p.spec.follows:
        raise ActionError(f"{name} 的版本号跟随 {p.spec.follows}，由 {p.spec.follows} 的 release 一起处理")
    down = [q for q in actions.ordered(ctx) if kinds(q, name)]  # a min needs nothing from a release
    if dry_run:
        plan(p, version, down)
        return True
    for q in [p, *down]:
        preflight(q)
    publish(p, version)
    install_own(ctx, p, version)
    ctx.prefetch()  # the new tag and the PyPI release
    for q in down:
        follow(ctx, q, p, version)
    say("\n== check")
    states, found = check.run_all(ctx)
    say(check.format_findings(found))
    return not any(f.level == "error" for f in found)


def plan(p: Project, version: str, down: list[Project]) -> None:
    current, _ = check.own_version(p.repo, p.spec)
    say(f"会发布 {p.name} {version}（现在 {current or '不一致'}，tag {p.spec.tag.format(version=version)}）：")
    say(f"  1. 改版本号、跑测试，提交「update version number to {version}」并推送，等 CI 通过")
    say("  2. 打 tag 并推送" + ("，建 GitHub release，等 PyPI 上出现 wheel" if p.spec.pypi else ""))
    if p.spec.pypi:
        say(f"  3. pipx 里的 {p.spec.pypi} 装 {version}（装过才装）")
    for q in down:
        say(f"  · {q.name}（{'、'.join(kinds(q, p.name))}）：跟进、跑 build/verify/test、提交推送、等 CI、更新本机安装")
    say("  最后跑 check")


def kinds(q: Project, name: str) -> list[str]:
    """The pin and lock kinds by which q depends on name."""
    return sorted({d.kind for d in q.spec.deps if d.project == name and d.kind in ("pin", "lock")})


# git and GitHub

def preflight(q: Project) -> None:
    if lines := sources.dirty(q.repo):
        raise ActionError(f"{q.name} 有未提交的改动，先提交或还原：\n  " + "\n  ".join(lines))
    if not sources.git(q.repo, "branch", "--show-current", check=False):
        raise ActionError(f"{q.name} 不在任何分支上")
    try:
        sources.git(q.repo, "fetch", "--quiet", "origin")  # remote tags are read with ls-remote
    except RuntimeError as e:
        raise ActionError(f"{q.name}: {e}") from None
    if not sources.git(q.repo, "rev-parse", "--abbrev-ref", "@{upstream}", check=False):
        raise ActionError(f"{q.name} 的分支没有上游分支（git push -u origin <分支>）")
    if int(sources.git(q.repo, "rev-list", "--count", "HEAD..@{upstream}")):
        raise ActionError(f"{q.name} 落后于 origin，先 git pull")


def commit(q: Project, message: str) -> None:
    sources.git(q.repo, "commit", "-qam", message)
    say(f"  提交：{message}")


def scan(q: Project) -> None:
    """Refuse to push added lines that hold the home directory's path or the git user's email."""
    secrets = [str(Path.home()), sources.git(q.repo, "config", "user.email", check=False)]
    secrets = [s for s in secrets if s]
    hits, file = [], ""
    for line in sources.git(q.repo, "diff", "@{upstream}..HEAD").splitlines():
        if line.startswith("+++ "):
            file = line[6:]
        elif line.startswith("+") and any(s in line for s in secrets):
            hits.append(f"{file}: {line[1:].strip()[:120]}")
    if hits:
        raise ActionError(f"{q.name} 要推送的改动里有本机路径或邮箱，没有推送：\n  " + "\n  ".join(hits))


def push(q: Project) -> str:
    """Push the branch if it is ahead; the commit pushed."""
    head = sources.git(q.repo, "rev-parse", "HEAD")
    if int(sources.git(q.repo, "rev-list", "--count", "@{upstream}..HEAD")):
        scan(q)
        sources.git(q.repo, "push", "--quiet")
        say(f"  已推送 {head[:7]}")
    return head


def gh(q: Project, *args: str) -> subprocess.CompletedProcess:
    if not shutil.which("gh"):
        raise ActionError("找不到 gh（GitHub CLI），装好并 gh auth login 之后再跑")
    return subprocess.run(["gh", *args], cwd=q.repo, capture_output=True, text=True)


def runs(q: Project, sha: str) -> list[dict]:
    done = gh(q, "run", "list", "--commit", sha, "--limit", "50", "--json", "name,status,conclusion,event,url")
    if done.returncode:
        raise ActionError(f"{q.name}: gh run list 失败：{done.stderr.strip()}")
    return json.loads(done.stdout or "[]")


def wait_ci(q: Project, sha: str) -> None:
    if not (q.repo / ".github" / "workflows").is_dir():
        return
    say(f"  等 {q.name} 的 CI（{sha[:7]}）……")
    start = time.monotonic()
    while True:
        found = [r for r in runs(q, sha) if r["event"] != "release"]
        if found and all(r["status"] == "completed" for r in found):
            if bad := [r for r in found if r["conclusion"] not in GOOD]:
                raise ActionError(f"{q.name} 的 CI 失败：\n  " + "\n  ".join(f"{r['name']}: {r['url']}" for r in bad))
            say("  CI 通过")
            return
        waited = time.monotonic() - start
        if not found and waited > CI_APPEARS_MINUTES * 60:
            say("  这次推送没有触发 CI")
            return
        if waited > CI_MINUTES * 60:
            raise ActionError(f"{q.name} 的 CI 超过 {CI_MINUTES} 分钟还没跑完")
        time.sleep(POLL)


def publish(p: Project, version: str) -> None:
    say(f"\n== {p.name} {version}")
    tag = p.spec.tag.format(version=version)
    current, _ = check.own_version(p.repo, p.spec)
    if current != version:
        if tag in sources.tags(p.repo, remote=False):
            raise ActionError(f"tag {tag} 已经存在，但版本号是 {current}")
        say(f"  {current} → {version}")
        files = actions.set_version(p, version)
        if not actions.run_all(p.spec.test, p.repo, "test"):
            sources.git(p.repo, "checkout", "--", *files)
            raise ActionError(f"{p.name} 的测试失败，版本号已还原，没有提交")
        commit(p, f"update version number to {version}")
    head = push(p)
    wait_ci(p, head)
    local = sources.tags(p.repo, remote=False)
    if tag not in local:
        sources.git(p.repo, "tag", tag)
    elif subprocess.run(["git", "-C", str(p.repo), "merge-base", "--is-ancestor", local[tag], head]).returncode:
        raise ActionError(f"tag {tag} 指向 {local[tag][:7]}，不在现在的 HEAD {head[:7]} 之前")
    if tag not in sources.tags(p.repo, remote=True):
        sources.git(p.repo, "push", "--quiet", "origin", tag)
        say(f"  已推送 tag {tag}")
    if not p.spec.pypi:
        return
    if gh(p, "release", "view", tag).returncode:
        done = gh(p, "release", "create", tag, "--title", tag, "--generate-notes")
        if done.returncode:
            raise ActionError(f"gh release create 失败：{done.stderr.strip()}")
        say(f"  已建 release {tag}")
    say(f"  等 PyPI 上的 {p.spec.pypi} {version}……")
    start = time.monotonic()
    while not wheel(p.spec.pypi, version):
        if bad := [r for r in runs(p, head) if r["event"] == "release" and r["status"] == "completed"
                   and r["conclusion"] not in GOOD]:
            raise ActionError("发布的 workflow 失败：\n  " + "\n  ".join(f"{r['name']}: {r['url']}" for r in bad))
        if time.monotonic() - start > PYPI_MINUTES * 60:
            raise ActionError(f"{PYPI_MINUTES} 分钟后 PyPI 上还没有 {p.spec.pypi} {version}")
        time.sleep(POLL)
    say("  PyPI 已有 wheel")


def wheel(package: str, version: str):
    try:
        return sources.pypi_wheel(package, version)
    except OSError:
        return None


# local copies and the projects that follow

def retry(cmd: list[str], repo: Path) -> None:
    for attempt in range(RETRIES):
        if actions.run(cmd, repo):
            return
        if attempt < RETRIES - 1:
            say(f"  PyPI 的索引可能还没更新，{POLL * 2} 秒后重试")
            time.sleep(POLL * 2)
    raise ActionError(f"重试 {RETRIES} 次仍然失败：{' '.join(cmd)}")


def install_own(ctx: Context, p: Project, version: str) -> None:
    if p.spec.pypi and (ctx.cfg.hosts["pipx_venvs"] / p.spec.pypi).is_dir():
        retry(["pipx", "runpip", p.spec.pypi, "install", "--no-cache-dir", f"{p.spec.pypi}=={version}"], p.repo)


def follow(ctx: Context, q: Project, up: Project, version: str) -> None:
    say(f"\n== {q.name}")
    before, _ = check.own_version(q.repo, q.spec)
    for attempt in range(RETRIES):
        try:
            ok = actions.sync(ctx, [q.name], version, False, hints=False)
            break
        except ActionError as e:
            if "update 命令失败" not in str(e) or attempt == RETRIES - 1:
                raise
            say(f"  PyPI 的索引可能还没更新，{POLL * 2} 秒后重试")
            time.sleep(POLL * 2)
    if not ok:
        raise ActionError(f"{q.name} 的 build、verify 或 test 失败；改动留在工作区里，没有提交")
    if sources.dirty(q.repo):
        after, _ = check.own_version(q.repo, q.spec)
        pinned = "pin" in kinds(q, up.name)
        commit(q, f"update {up.name} to {version}; {q.name} {after}" if after != before
               else f"update {up.name} to {version}" if pinned else f"lock {up.name} {version}")
    wait_ci(q, push(q))
    install(ctx, q, up, version)


def install(ctx: Context, q: Project, up: Project, version: str) -> None:
    package = up.spec.pypi or up.name
    if q.spec.pypi:
        venv = ctx.cfg.hosts["pipx_venvs"] / q.spec.pypi
        if venv.is_dir() and sources.normalize(package) in sources.venv_packages(venv):
            retry(["pipx", "runpip", q.spec.pypi, "install", "--no-cache-dir", f"{package}=={version}"], q.repo)
    if q.spec.venv and (q.repo / q.spec.venv).is_dir() and (q.repo / "uv.lock").is_file():
        if not actions.run(["uv", "sync"], q.repo):
            raise ActionError(f"{q.name}: uv sync 失败")
    own, _ = check.own_version(q.repo, q.spec)
    for plugin in q.spec.plugins:
        if plugin.host not in sources.READABLE_HOSTS or not plugin.hint:
            say(f"  {plugin.host} 的插件要手动更新（spec 里没有可执行的更新命令，或读不到安装的版本）")
            continue
        if not sources.plugin_version(plugin.host, plugin.id, ctx.cfg.hosts):
            continue  # not installed on this machine
        if not actions.run(["sh", "-c", plugin.hint], q.repo):
            raise ActionError(f"{q.name}: 更新 {plugin.host} 插件失败：{plugin.hint}")
        if (have := sources.plugin_version(plugin.host, plugin.id, ctx.cfg.hosts)) != own:
            raise ActionError(f"{q.name}: 更新后 {plugin.host} 装的是 {have}，不是 {own}")
