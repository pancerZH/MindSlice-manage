"""discover: an agent reads a snapshot of a repo and proposes its spec; the program verifies it; the user accepts it."""

import difflib
import json
import os
import shutil
import signal
import subprocess
import time
import tomllib
from datetime import datetime
from importlib import resources
from pathlib import Path

from . import sources
from .check import own_version, uncovered, dep_values, pinned_version
from .config import Config, Project, Spec, parse_spec
from .locations import LocationError, read_value

# variables a parent harness leaks into children; without them the child starts a standalone session (as in MindSlice-dream)
STRIP_PREFIXES = ("ANTIGRAVITY_", "CLAUDE_CODE_", "CODEX_", "MINDSLICE_PLUGIN_", "PLUGIN_")
STRIP_NAMES = {"CLAUDECODE", "CLAUDE_PID", "CLAUDE_EFFORT", "AI_AGENT"}
SESSION_KEYS = ("session_id", "conversation_id", "thread_id", "sessionId", "conversationId")
RUN_VAR = "MINDSLICE_MANAGE_RUN"


# the harness session

def clean_env(tag: str) -> dict:
    env = {k: v for k, v in os.environ.items() if k not in STRIP_NAMES and not k.startswith(STRIP_PREFIXES)}
    # ISOLATE: the harness's own MindSlice plugin loads no memory; RUN tags every process of the session
    return env | {"MINDSLICE_PLUGIN_ISOLATE": "1", RUN_VAR: tag, "PYTHONDONTWRITEBYTECODE": "1"}


def tagged(tag: str) -> list[int]:
    needle = f"{RUN_VAR}={tag}".encode()
    pids = []
    for proc in Path("/proc").glob("[0-9]*"):
        try:
            if needle in (proc / "environ").read_bytes().split(b"\0"):
                pids.append(int(proc.name))
        except OSError:
            continue
    return pids


def stop_leftovers(tag: str) -> None:
    """Stop every process the session started and left running: SIGTERM, then SIGKILL."""
    for sig in (signal.SIGTERM, signal.SIGKILL):
        for pid in tagged(tag):
            try:
                os.kill(pid, sig)
            except ProcessLookupError:
                pass
        for _ in range(20):
            if not tagged(tag):
                return
            time.sleep(0.1)


def json_values(text: str):
    try:
        yield json.loads(text)
        return
    except json.JSONDecodeError:
        pass
    for line in text.splitlines():
        if line.strip().startswith(("{", "[")):
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def find_key(value, keys):
    if isinstance(value, dict):
        for k in keys:
            if isinstance(value.get(k), str) and value[k]:
                return value[k]
        value = list(value.values())
    if isinstance(value, list):
        for v in value:
            if found := find_key(v, keys):
                return found
    return None


def launch(argv: list[str], work: Path, run_dir: Path, n: int, timeout_minutes: float) -> dict:
    """One non-interactive session in work/; its output is kept as stdout-n.log and stderr-n.log."""
    tag = f"{run_dir}#{n}"
    status, proc = "ok", None
    with open(run_dir / f"stdout-{n}.log", "w") as out, open(run_dir / f"stderr-{n}.log", "w") as err:
        try:
            proc = subprocess.Popen(argv, cwd=work, env=clean_env(tag), stdin=subprocess.DEVNULL, stdout=out, stderr=err)
            code = proc.wait(timeout=timeout_minutes * 60)
            if code:  # agy can exit 0 with an error and non-zero after doing the work: the proposal decides
                status = f"退出码 {code}"
        except subprocess.TimeoutExpired:
            status = "超时"
        except FileNotFoundError as e:
            status = f"找不到 harness：{e}"
        finally:
            stop_leftovers(tag)
            if proc:
                proc.wait()
    output = (run_dir / f"stdout-{n}.log").read_text(errors="replace")
    return {"status": status, "session_id": next((s for v in json_values(output) if (s := find_key(v, SESSION_KEYS))), None)}


def fill(argv: list[str], **values: str) -> list[str]:
    out = []
    for a in argv:
        for k, v in values.items():
            a = a.replace("{" + k + "}", v)
        out.append(a)
    return out


# inputs

def snapshot(repo: Path, dest: Path) -> int:
    """Copy the tracked files (as they are in the working tree) to dest; the number copied."""
    n = 0
    for f in sources.tracked_files(repo):
        src = repo / f
        if not src.exists() and not src.is_symlink():
            continue  # deleted in the working tree
        target = dest / f
        target.parent.mkdir(parents=True, exist_ok=True)
        if src.is_symlink():
            target.symlink_to(os.readlink(src))
        elif src.is_file():
            shutil.copy2(src, target)
        n += 1
    return n


def known_projects(projects: list[Project]) -> list[dict]:
    out = []
    for p in projects:
        package = p.spec.pypi if p.spec else (p.name if (p.repo / "pyproject.toml").is_file() else "")
        remote = sources.git(p.repo, "remote", "get-url", "origin", check=False) if p.repo.is_dir() else p.url
        out.append({"name": p.name, "package": package, "remote": remote})
    return out


def render(template: str, **values) -> str:
    for k, v in values.items():
        template = template.replace("{{" + k + "}}", str(v))
    return template


# verification

def verify(repo: Path, text: str, name: str, known: set[str]) -> dict:
    """Check a proposal against the real repo: errors (must be fixed), inconsistencies and uncovered versions (shown)."""
    result = {"spec": None, "errors": [], "inconsistent": [], "uncovered": []}
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        result["errors"].append(f"TOML 解析失败：{e}")
        return result
    spec, errors = parse_spec(data, name)
    result["errors"] += errors
    tracked = set(sources.tracked_files(repo))
    for loc in spec.all_locations():
        if loc.file not in tracked:
            result["errors"].append(f"{loc.file}: 不是被 git 跟踪的文件")
            continue
        try:
            read_value(repo, loc)
        except LocationError as e:
            result["errors"].append(str(e))
    for dep in spec.deps:
        if dep.project not in known:
            result["errors"].append(f"deps: {dep.project!r} 不是受管项目（可选：{', '.join(sorted(known))}）")
    commands = [c.cmd for c in spec.build + spec.verify + spec.test] + [loc.update for loc in spec.all_locations() if loc.update]
    for cmd in commands:
        if cmd and not shutil.which(cmd[0]) and not (repo / cmd[0]).exists():
            result["errors"].append(f"命令 {cmd[0]!r} 不在 PATH 上，也不在仓库里")
    if result["errors"]:
        return result
    version, rows = own_version(repo, spec)
    if version is None:
        result["inconsistent"].append("自己的版本号不一致：" + ", ".join(f"{r['file']}={r['value']}" for r in rows))
    pins = []
    for dep in spec.deps:
        drows = dep_values(repo, dep)
        if dep.kind == "pin":
            if v := pinned_version(drows):
                pins.append(v)
            else:
                result["inconsistent"].append(f"对 {dep.project} 的锁定不一致：" +
                                              ", ".join(f"{r['file']}={r['value']}" for r in drows if r["kind"] == "version"))
    result["uncovered"] = uncovered(repo, spec, ([version] if version else []) + pins, sorted(tracked))
    result["spec"] = spec
    return result


def repair_prompt(result: dict, proposal: str | None) -> str:
    lines = ["MindSlice-manage 核验了你写的 proposal.toml，有下面的问题。请改好后覆盖 proposal.toml，规则和前面一样。", ""]
    if proposal is None:
        lines.append("- 没有找到 proposal.toml：请把 spec 写到当前目录的 proposal.toml。")
    for e in result.get("errors", []):
        lines.append(f"- {e}")
    if result.get("uncovered"):
        lines += ["", "下面这些地方写着本项目的版本号（或 pin 的上游版本号），但没有被任何位置覆盖。"
                  "是要管理的就登记成位置；是生成物、lockfile、历史记录，或者只是别的东西碰巧同号，就把文件加进 ignore：", ""]
        lines += [f"- {u['file']}:{u['line']}: {u['text']}" for u in result["uncovered"][:50]]
    return "\n".join(lines)


def summary(spec: Spec, repo: Path) -> str:
    version, rows = own_version(repo, spec)
    lines = [f"自己的版本号 {version or '（不一致）'}" + (f"，跟随 {spec.follows}" if spec.follows else "") + "："]
    lines += [f"  {r['file']} = {r.get('value', r.get('error'))}" + ("（update）" if loc.update else "")
              for r, loc in zip(rows, spec.version)]
    for dep in spec.deps:
        lines.append(f"依赖 {dep.kind} {dep.project}：")
        lines += [f"  {r['file']} {r['kind']} = {r.get('value', r.get('error'))}" for r in dep_values(repo, dep)]
    rel = [x for x in [f"PyPI {spec.pypi}" if spec.pypi else "", f"tag {spec.tag}" if spec.tag else ""] if x]
    rel += [f"{p.host} {p.id}" for p in spec.plugins]
    lines.append("发布：" + ("，".join(rel) or "—"))
    if spec.venv:
        lines.append(f"仓库 venv：{spec.venv}")
    for label, cmds in (("build", spec.build), ("verify", spec.verify), ("test", spec.test)):
        lines += [f"{label}：{c}" for c in cmds]
    if spec.ignore:
        lines.append("ignore：" + ", ".join(spec.ignore))
    lines += [f"note：{n}" for n in spec.notes]
    return "\n".join(lines)


# the whole flow for one repo

def discover_one(cfg: Config, projects: list[Project], p: Project, yes: bool, ask=input) -> bool:
    if not p.repo.is_dir():
        if not p.url:
            print(f"{p.name}: {p.repo} 不存在")
            return False
        print(f"clone {p.url} → {p.repo}")
        p.repo.parent.mkdir(parents=True, exist_ok=True)
        if subprocess.run(["git", "clone", p.url, str(p.repo)]).returncode:
            return False
    run_dir = cfg.runs / f"{datetime.now():%Y%m%d-%H%M%S}-discover-{p.name}"
    work = run_dir / "work"
    (work / "repo").mkdir(parents=True)
    try:
        return _discover(cfg, projects, p, run_dir, work, yes, ask)
    finally:
        shutil.rmtree(work, ignore_errors=True)  # the snapshot and anything the session left; the record stays


def _discover(cfg: Config, projects: list[Project], p: Project, run_dir: Path, work: Path, yes: bool, ask) -> bool:
    n_files = snapshot(p.repo, work / "repo")
    head = sources.git(p.repo, "rev-parse", "--short", "HEAD", check=False)
    known = known_projects(projects)
    (work / "input.json").write_text(json.dumps({"name": p.name, "repo": "repo", "head": head, "projects": known},
                                                ensure_ascii=False, indent=2), encoding="utf-8")
    template = resources.files("mindslice_manage").joinpath("prompts/discover.md").read_text(encoding="utf-8")
    prompt = render(template, name=p.name, files=n_files)
    h = cfg.harness
    print(f"{p.name}: 快照 {n_files} 个文件，启动 agent（{h['cmd'][0]}）…", flush=True)

    session_id, result, proposal = None, {}, None
    for n in range(int(h["repairs"]) + 1):
        if n == 0:
            text = prompt
            argv = fill(h["cmd"], prompt=text)
        else:
            text = repair_prompt(result, proposal)
            if session_id and h.get("resume"):
                argv = fill(h["resume"], prompt=text, session_id=session_id)
            else:  # no session to resume: start over with everything
                text = prompt + "\n\n---\n\n上一轮写出的 proposal.toml：\n\n```toml\n" + (proposal or "") + "\n```\n\n" + text
                argv = fill(h["cmd"], prompt=text)
        (run_dir / f"prompt-{n}.md").write_text(text, encoding="utf-8")
        session = launch(argv, work, run_dir, n, float(h["timeout_minutes"]))
        session_id = session["session_id"] or session_id
        f = work / "proposal.toml"
        proposal = f.read_text(encoding="utf-8") if f.is_file() else None
        if proposal is None:
            result = {"errors": [f"会话没有写出 proposal.toml（{session['status']}）"]}
        else:
            result = verify(p.repo, proposal, p.name, {q.name for q in projects if q.name != p.name})
        print(f"  第 {n + 1} 轮：{session['status']}，{len(result['errors'])} 个错误，"
              f"{len(result.get('uncovered', []))} 处未登记", flush=True)
        if not result["errors"] and not result.get("uncovered"):
            break

    if proposal is not None:
        (run_dir / "proposal.toml").write_text(proposal, encoding="utf-8")
    (run_dir / "verify.json").write_text(json.dumps({k: v for k, v in result.items() if k != "spec"},
                                                    ensure_ascii=False, indent=2), encoding="utf-8")
    if result["errors"]:
        print(f"\n{p.name}: 核验没通过，没有写入。记录在 {run_dir}")
        print("\n".join(f"  - {e}" for e in result["errors"]))
        return False

    print(f"\n== {p.name} 的 spec（{run_dir / 'proposal.toml'}）\n")
    print(summary(result["spec"], p.repo))
    for item in result["inconsistent"]:
        print(f"\n[不一致] {item}")
    if result["uncovered"]:
        print("\n[未登记] agent 修了几轮仍有这些地方：")
        print("\n".join(f"  {u['file']}:{u['line']}: {u['text']}" for u in result["uncovered"]))
    target = cfg.managed / f"{p.name}.toml"
    header = f"# mindslice-manage discover 生成于 {datetime.now():%Y-%m-%d %H:%M}（{p.name} HEAD {head}），已核验\n"
    content = header + proposal
    if target.is_file():
        old = target.read_text(encoding="utf-8")
        diff = list(difflib.unified_diff(old.splitlines()[1:], content.splitlines()[1:], "旧 spec", "新 spec", lineterm=""))
        print("\n与旧 spec 的差异：\n" + ("\n".join(diff) if diff else "  （没有变化）"))
    if not yes:
        try:
            answer = ask(f"\n写入 {target}？[y/N] ").strip().lower()
        except EOFError:
            answer = ""
        if answer not in ("y", "yes"):
            print(f"没有写入；proposal 留在 {run_dir}")
            return False
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    print(f"已写入 {target}")
    return True


def discover(cfg: Config, projects: list[Project], names: list[str], yes: bool) -> bool:
    if names:
        by = {p.name: p for p in projects} | {p.repo.name: p for p in projects}
        if missing := [n for n in names if n not in by]:
            print(f"config.toml 里没有：{', '.join(missing)}")
            return False
        todo = [by[n] for n in names]
    else:
        todo = [p for p in projects if not p.spec]
        if not todo:
            print("每个仓库都已经有 spec；要重新生成就给出名字：discover <name>")
            return True
    return all([discover_one(cfg, projects, p, yes) for p in todo])
