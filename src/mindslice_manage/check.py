"""What status shows and what check reports: one pass over every project builds both."""

import fnmatch
import re
import shlex
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from pathlib import Path

from . import sources
from .config import Config, Project, Spec
from .locations import LocationError, read_text, read_value, release_part, build_part, spans, version_key

LEVELS = ("error", "warning", "info")
LEVEL_NAMES = {"error": "错误", "warning": "警告", "info": "提示"}


@dataclass
class Finding:
    level: str
    project: str
    message: str
    fix: str = ""


class Context:
    """One run's view of the projects, with the remote facts (PyPI, tags) fetched once."""

    def __init__(self, cfg: Config, projects: list[Project], online: bool = True, fetch: bool = False):
        self.cfg, self.projects, self.online, self.fetch = cfg, projects, online, fetch
        self.by_name = {p.name: p for p in projects}
        self.remote: dict[str, dict] = {}

    def prefetch(self) -> None:
        todo = [p for p in self.projects if p.spec and p.repo.is_dir()]
        with ThreadPoolExecutor(8) as ex:
            self.remote = dict(zip([p.name for p in todo], ex.map(self._fetch, todo)))

    def _fetch(self, p: Project) -> dict:
        facts = {"pypi": None, "tags": sources.tags(p.repo, remote=False), "remote_tags": None, "errors": []}
        if self.fetch:
            sources.git(p.repo, "fetch", "--quiet", "--tags", check=False)
            facts["tags"] = sources.tags(p.repo, remote=False)
        if not self.online:
            return facts
        if p.spec.tag and sources.git(p.repo, "remote", "get-url", "origin", check=False):
            try:
                facts["remote_tags"] = sources.tags(p.repo, remote=True)
            except RuntimeError as e:
                facts["errors"].append(f"读不到 origin 的 tag：{e}")
        if p.spec.pypi:
            try:
                facts["pypi"] = sources.pypi_latest(p.spec.pypi)
            except OSError as e:
                facts["errors"].append(f"读不到 PyPI：{e}")
        return facts

    def tag_map(self, name: str) -> dict[str, str]:
        facts = self.remote.get(name, {})
        return facts.get("remote_tags") if facts.get("remote_tags") is not None else facts.get("tags", {})

    def latest_release(self, name: str) -> str | None:
        """The newest release of a project: PyPI, else its newest version tag."""
        p = self.by_name.get(name)
        if not p or not p.spec:
            return None
        if pypi := self.remote.get(name, {}).get("pypi"):
            return pypi
        versions = sources.tagged_versions(self.tag_map(name), p.spec.tag) if p.spec.tag else []
        return versions[-1] if versions else None

    def tag_commit(self, name: str, version: str) -> str | None:
        p = self.by_name[name]
        return self.tag_map(name).get(p.spec.tag.format(version=version)) if p.spec.tag else None


def own_version(repo: Path, spec: Spec) -> tuple[str | None, list[dict]]:
    """The project's version if every location agrees, and each location's value or error."""
    rows = []
    for loc in spec.version:
        try:
            rows.append({"file": loc.file, "value": read_value(repo, loc)})
        except LocationError as e:
            rows.append({"file": loc.file, "error": str(e)})
    values = {r["value"] for r in rows if "value" in r}
    ok = len(values) == 1 and all("value" in r for r in rows)
    return (values.pop() if ok else None), rows


def dep_values(repo: Path, dep) -> list[dict]:
    rows = []
    for loc in dep.locations:
        try:
            rows.append({"file": loc.file, "kind": loc.value, "value": read_value(repo, loc)})
        except LocationError as e:
            rows.append({"file": loc.file, "kind": loc.value, "error": str(e)})
    return rows


def pinned_version(rows: list[dict]) -> str | None:
    values = {r["value"] for r in rows if r.get("kind") == "version" and "value" in r}
    return values.pop() if len(values) == 1 else None


# versions written where no location covers them

def version_pattern(version: str) -> re.Pattern:
    return re.compile(rf"(?<![\d.]){re.escape(version)}(?!\d|\.\d)")


def uncovered(repo: Path, spec: Spec, versions: list[str], files: list[str] | None = None) -> list[dict]:
    """Occurrences of the versions in tracked files that no location captures and no ignore pattern skips."""
    if not versions:
        return []
    covered: dict[str, list[tuple[int, int]]] = {}
    for loc in spec.all_locations():
        covered.setdefault(loc.file, []).extend(spans(repo, loc))
    patterns = [version_pattern(v) for v in sorted(set(versions))]
    out = []
    for f in files if files is not None else sources.tracked_files(repo):
        if any(fnmatch.fnmatch(f, pat) for pat in spec.ignore):
            continue
        path = repo / f
        try:
            if not path.is_file() or path.stat().st_size > 2_000_000 or b"\0" in path.read_bytes()[:8192]:
                continue
            text = read_text(path)
        except (OSError, UnicodeDecodeError):
            continue
        hits = sorted((m for rx in patterns for m in rx.finditer(text)), key=lambda m: (m.start(), -m.end()))
        last_end = -1
        for m in hits:
            if m.end() <= last_end:  # inside a longer hit ("0.2.3" in "0.2.3-1")
                continue
            last_end = m.end()
            if any(a <= m.start() and m.end() <= b for a, b in covered.get(f, [])):
                continue
            line = text.count("\n", 0, m.start()) + 1
            out.append({"file": f, "line": line, "version": m.group(0),
                        "text": text.splitlines()[line - 1].strip()[:160]})
    return out


# one project

def inspect(ctx: Context, p: Project) -> tuple[dict, list[Finding]]:
    """The project's state (for status) and its findings (for check)."""
    found: list[Finding] = []

    def add(level: str, message: str, fix: str = "") -> None:
        found.append(Finding(level, p.name, message, fix))

    state: dict = {"name": p.name, "repo": str(p.repo), "spec": bool(p.spec)}
    if not p.repo.is_dir():
        add("info", "仓库还没有 clone", f"mindslice-manage discover {p.name}")
        return state, found
    if not p.spec:
        add("info", "还没有 spec", f"mindslice-manage discover {p.name}")
        return state, found
    spec, repo, facts = p.spec, p.repo, ctx.remote.get(p.name, {})
    for e in facts.get("errors", []):
        add("warning", e)

    # own version
    version, rows = own_version(repo, spec)
    state["version"], state["version_locations"] = version, rows
    for r in rows:
        if "error" in r:
            add("error", f"位置不再匹配：{r['error']}", f"mindslice-manage discover {p.name}")
    values = sorted({r["value"] for r in rows if "value" in r})
    if len(values) > 1:
        detail = ", ".join(f"{r['file']}={r['value']}" for r in rows if "value" in r)
        add("error", f"自己的版本号不一致：{detail}", f"mindslice-manage bump {p.name} <版本>")

    # git
    g = sources.git_state(repo)
    state["git"] = g
    if g["dirty"]:
        add("info", f"有 {g['dirty']} 个未提交的改动")

    # release
    rel = {"pypi": facts.get("pypi"), "tag": None}
    state["release"] = rel
    if version and spec.tag:
        tag = spec.tag.format(version=version)
        local, remote = facts.get("tags", {}), facts.get("remote_tags")
        commit = (remote or {}).get(tag) or local.get(tag)
        rel["tag"] = tag if commit else None
        released = rel["pypi"] == version
        if remote is not None and tag in remote and tag not in local:
            add("info", f"origin 有 tag {tag}，本地没有", f"git -C {shlex.quote(str(repo))} fetch --tags")
        if released and not commit:
            add("warning", f"{version} 已发布到 PyPI，但没有 tag {tag}",
                f"git -C {shlex.quote(str(repo))} tag {tag} <发布时的提交> && git -C {shlex.quote(str(repo))} push origin {tag}")
        if commit and (n := sources.commits_since(repo, commit)):
            add("info", f"{tag} 之后有 {n} 个新提交")
        if ctx.online and not commit and not released:
            add("info", f"{version} 还没发布" + (f"（PyPI 最新 {rel['pypi']}）" if rel["pypi"] else ""),
                "；".join(release_steps(p, version)))
        tagged = sources.tagged_versions(ctx.tag_map(p.name), spec.tag)
        if tagged and version_key(tagged[-1]) > version_key(version):
            add("warning", f"最新的 tag 是 {spec.tag.format(version=tagged[-1])}，比本地版本 {version} 新",
                f"mindslice-manage bump {p.name} {tagged[-1]}")
    if version and rel["pypi"] and version_key(rel["pypi"]) > version_key(version):
        add("warning", f"PyPI 上已有 {rel['pypi']}，比本地版本 {version} 新", f"mindslice-manage bump {p.name} {rel['pypi']}")

    # deps
    state["deps"] = []
    for dep in spec.deps:
        rows = dep_values(repo, dep)
        up = ctx.by_name.get(dep.project)
        latest = ctx.latest_release(dep.project) if up else None
        d = {"project": dep.project, "kind": dep.kind, "locations": rows, "latest": latest}
        state["deps"].append(d)
        for r in rows:
            if "error" in r:
                add("error", f"位置不再匹配：{r['error']}", f"mindslice-manage discover {p.name}")
        if not up:
            add("error", f"依赖的项目 {dep.project} 不在 config.toml 里")
            continue
        vals = sorted({r["value"] for r in rows if r["kind"] == "version" and "value" in r}, key=version_key)
        if dep.kind == "pin" and len(vals) > 1:
            add("error", f"对 {dep.project} 的锁定不一致：{', '.join(vals)}", f"mindslice-manage sync {p.name}")
        value = vals[0] if len(vals) == 1 else None
        d["value"] = value
        if not value:
            continue
        if dep.kind == "min":
            if latest and version_key(value) > version_key(latest):
                add("error", f"要求 {dep.project}>={value}，但它最新发布的是 {latest}", f"改下限，或先发布 {dep.project}")
        elif latest and version_key(value) < version_key(latest):
            add("warning", f"{dep.kind} {dep.project} {value}，上游最新 {latest}", f"mindslice-manage sync {p.name}")
        elif latest and dep.kind == "pin" and version_key(value) > version_key(latest):
            add("error", f"锁定的 {dep.project} {value} 还没有发布（最新 {latest}）")
        if dep.kind == "pin":
            pin_consistency(ctx, p, dep, rows, value, add)
        if spec.follows == dep.project and version and (release_part(version) != value or build_part(version) is None):
            add("error", f"版本号 {version} 应为 {value}-<n>（跟随 {dep.project}）", f"mindslice-manage sync {p.name}")

    # installed copies
    state["installs"] = installs(ctx, p, version, add)

    # versions nobody manages
    search = [v for v in [version] + [d.get("value") for d in state["deps"] if d["kind"] == "pin"] if v]
    state["uncovered"] = uncovered(repo, spec, search)
    for u in state["uncovered"]:
        add("warning", f"未登记的版本号 {u['version']}：{u['file']}:{u['line']}  {u['text']}",
            f"mindslice-manage discover {p.name}，或把文件加进 ignore")
    return state, found


def release_steps(p: Project, version: str) -> list[str]:
    """The commands that release version (printed, never run)."""
    if not p.spec.tag:
        return []
    tag = p.spec.tag.format(version=version)
    repo = shlex.quote(str(p.repo))
    steps = [f"git -C {repo} tag {tag} && git -C {repo} push origin {tag}"]
    if p.spec.pypi:
        steps.append(f"gh release create {tag} --generate-notes（若 PyPI 发布由 GitHub release 触发）")
    return steps


def pin_consistency(ctx: Context, p: Project, dep, rows: list[dict], version: str, add) -> None:
    """A pin's commit and wheel must be the upstream release's."""
    up = ctx.by_name[dep.project]
    want: dict[str, str | None] = {}
    kinds = {r["kind"] for r in rows if "value" in r}
    if "commit" in kinds:
        want["commit"] = ctx.tag_commit(dep.project, version)
    if kinds & {"wheel_url", "wheel_sha256"} and ctx.online and up.spec.pypi:
        try:
            wheel = sources.pypi_wheel(up.spec.pypi, version)
        except OSError as e:
            add("warning", f"读不到 PyPI：{e}")
            wheel = None
        if wheel:
            want["wheel_url"], want["wheel_sha256"] = wheel
    for r in rows:
        expected = want.get(r["kind"])
        if expected and "value" in r and r["value"] != expected:
            add("error", f"{r['file']} 的 {r['kind']} 与 {dep.project} {version} 的发布不符：{r['value'][:16]}… ≠ {expected[:16]}…",
                f"mindslice-manage sync {p.name} --to {version}")


def installs(ctx: Context, p: Project, version: str | None, add) -> list[dict]:
    spec, out = p.spec, []
    latest = ctx.latest_release(p.name)
    deps = {dep.project: ctx.by_name[dep.project] for dep in spec.deps if dep.project in ctx.by_name}

    def venv_deps(where: str, pkgs: dict, fix) -> None:
        for name, up in deps.items():
            have = pkgs.get(sources.normalize(up.spec.pypi or name)) if up.spec else None
            want = ctx.latest_release(name)
            if have:
                out.append({"where": f"{where} 里的 {name}", "version": have})
                if want and version_key(have) < version_key(want):
                    add("warning", f"{where} 里装的 {name} 是 {have}，最新 {want}", fix(up.spec.pypi or name))

    if spec.pypi:
        venv = ctx.cfg.hosts["pipx_venvs"] / spec.pypi
        pkgs = sources.venv_packages(venv) if venv.is_dir() else {}
        have = pkgs.get(sources.normalize(spec.pypi))
        out.append({"where": "pipx", "version": have})
        if have and latest and version_key(have) < version_key(latest):
            add("warning", f"pipx 装的是 {have}，最新 {latest}", f"pipx upgrade {spec.pypi}")
        if pkgs:
            venv_deps("pipx venv", pkgs, lambda dep: f"pipx runpip {spec.pypi} install -U {dep}")
    if spec.venv:
        pkgs = sources.venv_packages(p.repo / spec.venv)
        uv = (p.repo / "uv.lock").is_file()
        venv_deps(spec.venv, pkgs, lambda dep: f"cd {shlex.quote(str(p.repo))} && uv sync" if uv
                  else f"{shlex.quote(str(p.repo / spec.venv / 'bin' / 'pip'))} install -U {dep}")
    for plugin in spec.plugins:
        if plugin.host not in sources.READABLE_HOSTS:
            out.append({"where": plugin.host, "version": "?"})
            continue
        have = sources.plugin_version(plugin.host, plugin.id, ctx.cfg.hosts)
        out.append({"where": plugin.host, "version": have})
        if have and version and have != version:
            add("warning", f"{plugin.host} 装的插件是 {have}，仓库里是 {version}", plugin.hint or f"在 {plugin.host} 里更新 {plugin.id}")
    return out


def run_all(ctx: Context, names: list[str] | None = None) -> tuple[list[dict], list[Finding]]:
    ctx.prefetch()
    states, found = [], []
    for p in ctx.projects:
        if names and p.name not in names:
            continue
        s, f = inspect(ctx, p)
        states.append(s)
        found += f
    found.sort(key=lambda f: LEVELS.index(f.level))
    return states, found


# output

def width(s: str) -> int:
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in s)


def table(rows: list[list[str]]) -> str:
    widths = [max(width(r[i]) for r in rows) for i in range(len(rows[0]))]
    return "\n".join("  ".join(c + " " * (w - width(c)) for c, w in zip(r, widths)).rstrip() for r in rows)


def status_rows(states: list[dict]) -> list[list[str]]:
    rows = [["项目", "版本", "发布", "git", "依赖", "本机安装"]]
    for s in states:
        if not s.get("spec"):
            rows.append([s["name"], "—", "", "", "", "（还没有 spec）"])
            continue
        version = s.get("version") or "不一致"
        rel = s.get("release", {})
        release = " · ".join(x for x in [f"PyPI {rel['pypi']}" if rel.get("pypi") else "", rel.get("tag") or ""] if x)
        g = s["git"]
        git = g["branch"] + (f" ↑{g['ahead']}" if g["ahead"] else "") + (f" ↓{g['behind']}" if g["behind"] else "") \
            + (f" 改动{g['dirty']}" if g["dirty"] else "")
        deps = "; ".join(f"{d['kind']} {d['project']} {d.get('value') or '?'}" for d in s["deps"])
        inst = ", ".join(f"{i['where']} {i['version'] or '—'}" for i in s["installs"])
        rows.append([s["name"], version, release or "—", git, deps or "—", inst or "—"])
    return rows


def format_findings(found: list[Finding]) -> str:
    if not found:
        return "全部一致。"
    lines = []
    for f in found:
        lines.append(f"[{LEVEL_NAMES[f.level]}] {f.project}: {f.message}")
        if f.fix:
            lines.append(f"    → {f.fix}")
    counts = {lv: sum(f.level == lv for f in found) for lv in LEVELS}
    lines.append("")
    lines.append(f"{counts['error']} 个错误，{counts['warning']} 个警告，{counts['info']} 个提示")
    return "\n".join(lines)


def findings_json(found: list[Finding]) -> list[dict]:
    return [asdict(f) for f in found]
