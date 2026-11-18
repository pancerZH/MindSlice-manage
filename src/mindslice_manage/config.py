"""config.toml (written by the user) and the per-repo specs in managed/ (written by discover)."""

import re
import subprocess
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .locations import FORMATS, VALUE_RES, Location

HOSTS = ("claude", "agy", "codex", "deepseek-harness")
DEFAULT_HOSTS = {
    "pipx_venvs": "~/.local/share/pipx/venvs",
    "claude": "~/.claude/plugins/installed_plugins.json",
    "agy": "~/.gemini/config/plugins",
    "codex": "~/.codex/plugins/cache",
}
DEP_KINDS = ("min", "pin", "lock")


class ConfigError(Exception):
    pass


@dataclass
class Command:
    cmd: list[str]
    env: dict[str, str] = field(default_factory=dict)

    def __str__(self) -> str:
        return " ".join(f"{k}={v}" for k, v in self.env.items()) + (" " if self.env else "") + " ".join(self.cmd)


@dataclass
class Plugin:
    host: str
    id: str
    hint: str = ""


@dataclass
class Dep:
    project: str
    kind: str
    locations: list[Location]


@dataclass
class Spec:
    name: str
    version: list[Location]
    follows: str = ""
    ignore: list[str] = field(default_factory=list)
    pypi: str = ""
    tag: str = ""
    plugins: list[Plugin] = field(default_factory=list)
    deps: list[Dep] = field(default_factory=list)
    venv: str = ""
    build: list[Command] = field(default_factory=list)
    verify: list[Command] = field(default_factory=list)
    test: list[Command] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def all_locations(self) -> list[Location]:
        return self.version + [loc for d in self.deps for loc in d.locations]


@dataclass
class Repo:
    path: Path
    url: str = ""
    name: str = ""


@dataclass
class Config:
    file: Path
    repos: list[Repo]
    clones: Path
    harness: dict
    hosts: dict[str, Path]

    @property
    def root(self) -> Path:
        return self.file.parent

    @property
    def managed(self) -> Path:
        return self.root / "managed"

    @property
    def runs(self) -> Path:
        return self.root / "runs"


@dataclass
class Project:
    name: str
    repo: Path
    url: str = ""
    spec: Spec | None = None
    spec_file: Path | None = None


# config.toml

def load_config(path: Path) -> Config:
    if not path.is_file():
        raise ConfigError(f"{path} 不存在：把 config.example.toml 复制成 config.toml 再修改")
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path}: {e}") from None
    path = path.resolve()
    base = path.parent

    def resolve(p: str) -> Path:
        return (base / Path(p).expanduser()).resolve()

    clones = resolve(data.get("paths", {}).get("clones", "repos"))
    repos = []
    for i, r in enumerate(data.get("repos", [])):
        if r.get("path"):
            repos.append(Repo(resolve(r["path"]), r.get("url", ""), r.get("name", "")))
        elif r.get("url"):
            repos.append(Repo(clones / url_dirname(r["url"]), r["url"], r.get("name", "")))
        else:
            raise ConfigError(f"{path}: repos[{i}] 需要 path 或 url")
    hosts = {k: Path(v).expanduser() for k, v in (DEFAULT_HOSTS | data.get("hosts", {})).items()}
    harness = {"timeout_minutes": 20, "repairs": 2} | data.get("harness", {})
    return Config(path, repos, clones, harness, hosts)


def url_dirname(url: str) -> str:
    return re.sub(r"\.git$", "", url.rstrip("/").rsplit("/", 1)[-1].rsplit(":", 1)[-1])


def project_name(repo: Path) -> str:
    """The project's name: [project].name of its pyproject.toml, else the name of its origin repo, else its directory's."""
    py = repo / "pyproject.toml"
    if py.is_file():
        try:
            name = tomllib.loads(py.read_text(encoding="utf-8")).get("project", {}).get("name")
            if isinstance(name, str) and name:
                return name
        except tomllib.TOMLDecodeError:
            pass
    done = subprocess.run(["git", "-C", str(repo), "remote", "get-url", "origin"], capture_output=True, text=True)
    return url_dirname(done.stdout.strip()) if done.returncode == 0 and done.stdout.strip() else repo.name


def load_projects(cfg: Config) -> list[Project]:
    projects = []
    for r in cfg.repos:
        name = r.name or (project_name(r.path) if r.path.is_dir() else r.path.name)
        p = Project(name, r.path, r.url)
        f = cfg.managed / f"{name}.toml"
        if f.is_file():
            try:
                data = tomllib.loads(f.read_text(encoding="utf-8"))
            except tomllib.TOMLDecodeError as e:
                raise ConfigError(f"{f}: {e}") from None
            spec, errors = parse_spec(data, name)
            if errors:
                raise ConfigError(f"{f}:\n  " + "\n  ".join(errors))
            p.spec, p.spec_file = spec, f
        projects.append(p)
    names = [p.name for p in projects]
    if dup := {n for n in names if names.count(n) > 1}:
        raise ConfigError(f"项目重名：{', '.join(sorted(dup))}")
    return projects


# the spec schema; parse_spec collects every error instead of stopping at the first

LOC_KEYS = {"file", "regex", "count", "format", "value", "update"}


class _Checker:
    def __init__(self):
        self.errors: list[str] = []

    def err(self, where: str, msg: str) -> None:
        self.errors.append(f"{where}: {msg}")

    def table(self, d, where: str, keys: set[str]) -> dict | None:
        if not isinstance(d, dict):
            self.err(where, "应为表（table）")
            return None
        for k in sorted(set(d) - keys):
            self.err(where, f"未知字段 {k!r}")
        return d

    def str_(self, d: dict, key: str, where: str, default: str = "", required: bool = False) -> str:
        v = d.get(key, None)
        if v is None:
            if required:
                self.err(where, f"缺少 {key}")
            return default
        if not isinstance(v, str):
            self.err(where, f"{key} 应为字符串")
            return default
        return v

    def strs(self, d: dict, key: str, where: str) -> list[str]:
        v = d.get(key, [])
        if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
            self.err(where, f"{key} 应为字符串列表")
            return []
        return v

    def location(self, d, where: str) -> Location | None:
        if self.table(d, where, LOC_KEYS) is None:
            return None
        loc = Location(self.str_(d, "file", where, required=True), self.str_(d, "regex", where, required=True))
        count = d.get("count", 1)
        if not isinstance(count, int) or isinstance(count, bool) or count < 1:
            self.err(where, "count 应为正整数")
        else:
            loc.count = count
        loc.format = self.str_(d, "format", where, "version")
        if loc.format not in FORMATS:
            self.err(where, f"format 只能是 {' / '.join(FORMATS)}")
        loc.value = self.str_(d, "value", where, "version")
        if loc.value not in VALUE_RES:
            self.err(where, f"value 只能是 {' / '.join(VALUE_RES)}")
        loc.update = self.strs(d, "update", where)
        if loc.regex:
            try:
                if re.compile(loc.regex).groups != 1:
                    self.err(where, "regex 必须恰好有一个捕获组")
            except re.error as e:
                self.err(where, f"regex 无法编译：{e}")
        return loc

    def locations(self, v, where: str) -> list[Location]:
        if not isinstance(v, list) or not v:
            self.err(where, "应为非空的位置列表")
            return []
        return [loc for i, d in enumerate(v) if (loc := self.location(d, f"{where}[{i}]"))]

    def commands(self, v, where: str) -> list[Command]:
        if not isinstance(v, list):
            self.err(where, "应为命令列表")
            return []
        out = []
        for i, c in enumerate(v):
            w = f"{where}[{i}]"
            if isinstance(c, dict):
                if self.table(c, w, {"cmd", "env"}) is None:
                    continue
                cmd, env = c.get("cmd"), c.get("env", {})
            else:
                cmd, env = c, {}
            if not isinstance(cmd, list) or not cmd or not all(isinstance(x, str) for x in cmd):
                self.err(w, "命令应为非空的字符串列表（argv）")
            elif not isinstance(env, dict) or not all(isinstance(x, str) for x in env.values()):
                self.err(w, "env 应为字符串表")
            else:
                out.append(Command(cmd, dict(env)))
        return out


def parse_spec(data: dict, name: str) -> tuple[Spec, list[str]]:
    """The spec in data, and every schema error in it (empty when it is well-formed)."""
    c = _Checker()
    c.table(data, "spec", {"schema", "name", "ignore", "version", "release", "deps", "installs", "commands", "notes"})
    if data.get("schema") != 1:
        c.err("schema", "应为 1")
    if data.get("name") != name:
        c.err("name", f"应为 {name!r}")
    spec = Spec(name, [])
    spec.ignore = c.strs(data, "ignore", "spec")
    spec.notes = c.strs(data, "notes", "spec")

    if (v := c.table(data.get("version"), "version", {"locations", "follows"})) is not None:
        spec.version = c.locations(v.get("locations"), "version.locations")
        spec.follows = c.str_(v, "follows", "version")

    if (r := c.table(data.get("release", {}), "release", {"pypi", "tag", "plugins"})) is not None:
        spec.pypi = c.str_(r, "pypi", "release")
        spec.tag = c.str_(r, "tag", "release")
        if spec.tag and "{version}" not in spec.tag:
            c.err("release", "tag 里要有 {version}")
        plugins = r.get("plugins", [])
        if not isinstance(plugins, list):
            c.err("release", "plugins 应为列表")
            plugins = []
        for i, p in enumerate(plugins):
            w = f"release.plugins[{i}]"
            if c.table(p, w, {"host", "id", "hint"}) is not None:
                plugin = Plugin(c.str_(p, "host", w, required=True), c.str_(p, "id", w, required=True),
                                c.str_(p, "hint", w))
                if plugin.host and plugin.host not in HOSTS:
                    c.err(w, f"host 只能是 {' / '.join(HOSTS)}")
                spec.plugins.append(plugin)

    deps = data.get("deps", [])
    if not isinstance(deps, list):
        c.err("deps", "应为表数组（[[deps]]）")
        deps = []
    for i, d in enumerate(deps):
        w = f"deps[{i}]"
        if c.table(d, w, {"project", "kind", "locations"}) is None:
            continue
        dep = Dep(c.str_(d, "project", w, required=True), c.str_(d, "kind", w, required=True),
                  c.locations(d.get("locations"), f"{w}.locations"))
        if dep.kind and dep.kind not in DEP_KINDS:
            c.err(w, f"kind 只能是 {' / '.join(DEP_KINDS)}")
        if dep.project == name:
            c.err(w, "不能依赖自己")
        for j, loc in enumerate(dep.locations):
            lw = f"{w}.locations[{j}]"
            if dep.kind != "pin" and loc.value != "version":
                c.err(lw, "只有 pin 的位置能存 version 以外的值")
            if dep.kind == "lock" and not loc.update:
                c.err(lw, "lock 的位置要有 update 命令")
            if dep.kind in ("min", "pin") and loc.update:
                c.err(lw, f"{dep.kind} 的位置直接改写，不用 update")
        if dep.kind == "pin" and not any(loc.value == "version" for loc in dep.locations):
            c.err(w, "pin 至少要有一个 value = \"version\" 的位置")
        spec.deps.append(dep)

    for j, loc in enumerate(spec.version):
        if loc.value != "version":
            c.err(f"version.locations[{j}]", "自己的版本号位置只能存 version")
    if spec.version and all(loc.update for loc in spec.version):
        c.err("version.locations", "至少要有一个直接改写的位置（没有 update 的）")
    if spec.follows and not any(d.project == spec.follows and d.kind == "pin" for d in spec.deps):
        c.err("version.follows", f"要有一个对 {spec.follows!r} 的 pin 依赖")

    if (i := c.table(data.get("installs", {}), "installs", {"venv"})) is not None:
        spec.venv = c.str_(i, "venv", "installs")
    if (cm := c.table(data.get("commands", {}), "commands", {"build", "verify", "test"})) is not None:
        spec.build = c.commands(cm.get("build", []), "commands.build")
        spec.verify = c.commands(cm.get("verify", []), "commands.verify")
        spec.test = c.commands(cm.get("test", []), "commands.test")
    return spec, c.errors
