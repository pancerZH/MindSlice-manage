"""Where the facts come from: git, PyPI and the copies installed on this machine."""

import json
import re
import subprocess
import urllib.error
import urllib.request
from importlib import metadata
from pathlib import Path

from .locations import VERSION_RE, version_key

PYPI = "https://pypi.org/pypi"


# git

def git(repo: Path, *args: str, check: bool = True) -> str:
    done = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    if check and done.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {done.stderr.strip()}")
    return done.stdout.strip() if done.returncode == 0 else ""


def tracked_files(repo: Path) -> list[str]:
    out = subprocess.run(["git", "-C", str(repo), "ls-files", "-z"], capture_output=True, check=True).stdout
    return [f for f in out.decode("utf-8", "surrogateescape").split("\0") if f]


def dirty(repo: Path, files: list[str] | None = None) -> list[str]:
    """`git status --porcelain` lines, for the whole repo or only for the given files."""
    out = git(repo, "status", "--porcelain", *(["--", *files] if files else []))
    return [line for line in out.splitlines() if line]


def git_state(repo: Path, fetch: bool = False) -> dict:
    if fetch:
        git(repo, "fetch", "--quiet", "--tags", check=False)
    state = {"branch": git(repo, "branch", "--show-current", check=False) or "(detached)",
             "head": git(repo, "rev-parse", "--short", "HEAD", check=False),
             "dirty": len(dirty(repo)), "upstream": git(repo, "rev-parse", "--abbrev-ref", "@{upstream}", check=False),
             "ahead": 0, "behind": 0}
    if state["upstream"]:
        counts = git(repo, "rev-list", "--left-right", "--count", "HEAD...@{upstream}", check=False).split()
        if len(counts) == 2:
            state["ahead"], state["behind"] = int(counts[0]), int(counts[1])
    return state


def tags(repo: Path, remote: bool) -> dict[str, str]:
    """{tag: commit} on origin (remote) or in the local repo; annotated tags give the commit they point at."""
    out = git(repo, "ls-remote", "--tags", "origin") if remote else git(repo, "show-ref", "--tags", "-d", check=False)
    refs: dict[str, str] = {}
    for line in out.splitlines():
        sha, _, ref = line.replace("\t", " ").partition(" ")
        name = ref.strip().removeprefix("refs/tags/")
        if name.endswith("^{}"):
            refs[name[:-3]] = sha  # the peeled line comes after the tag's own line and wins
        else:
            refs.setdefault(name, sha)
    return refs


def tagged_versions(tag_names, pattern: str) -> list[str]:
    """The versions among tag_names of the form pattern (e.g. v{version}), oldest first."""
    rx = re.compile(re.escape(pattern).replace(re.escape("{version}"), f"({VERSION_RE.pattern})"))
    return sorted((m.group(1) for t in tag_names if (m := rx.fullmatch(t))), key=version_key)


def commits_since(repo: Path, commit: str) -> int | None:
    out = git(repo, "rev-list", "--count", f"{commit}..HEAD", check=False)
    return int(out) if out.isdigit() else None


# PyPI

def fetch_json(url: str) -> dict | None:
    """The JSON at url, or None if it is not there (404). Tests replace this function."""
    try:
        with urllib.request.urlopen(url, timeout=20) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise


def pypi_latest(package: str) -> str | None:
    """The newest released version that has files and is not yanked.

    Taken from `releases`, not `info.version`: right after an upload the latter still names the previous release."""
    data = fetch_json(f"{PYPI}/{package}/json")
    if not data:
        return None
    versions = [v for v, files in data.get("releases", {}).items()
                if files and not all(f.get("yanked") for f in files) and VERSION_RE.fullmatch(v)]
    return max(versions, key=version_key) if versions else data.get("info", {}).get("version")


def pypi_wheel(package: str, version: str) -> tuple[str, str] | None:
    """(url, sha256) of the release's wheel, from the version's own endpoint; a pure-Python wheel is preferred."""
    data = fetch_json(f"{PYPI}/{package}/{version}/json")
    wheels = [f for f in (data or {}).get("urls", []) if f.get("packagetype") == "bdist_wheel"]
    if not wheels:
        return None
    wheels.sort(key=lambda f: not f["filename"].endswith("-py3-none-any.whl"))
    return wheels[0]["url"], wheels[0]["digests"]["sha256"]


# installed copies

def normalize(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def venv_packages(venv: Path) -> dict[str, str]:
    """{normalized name: version} of the distributions installed in a virtual environment."""
    sites = [*venv.glob("lib/python*/site-packages"), venv / "Lib" / "site-packages"]
    paths = [str(p) for p in sites if p.is_dir()]
    if not paths:
        return {}
    return {normalize(d.metadata["Name"]): d.version for d in metadata.distributions(path=paths) if d.metadata["Name"]}


def _json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def plugin_version(host: str, plugin_id: str, hosts: dict[str, Path]) -> str | None:
    """The installed version of a plugin, or None when it is not installed (or the host cannot be read)."""
    if host == "claude":
        entries = ((_json(hosts["claude"]) or {}).get("plugins") or {}).get(plugin_id) or []
        return next((e.get("version") for e in entries if e.get("version")), None)
    if host == "agy":
        return (_json(hosts["agy"] / plugin_id / "plugin.json") or {}).get("version")
    if host == "codex":  # <cache>/<marketplace>/<plugin>/<version>/
        name, _, market = plugin_id.partition("@")
        dirs = hosts["codex"].glob(f"{market or '*'}/{name}/*")
        versions = [d.name for d in dirs if d.is_dir() and VERSION_RE.fullmatch(d.name)]
        return max(versions, key=version_key) if versions else None
    return None


READABLE_HOSTS = ("claude", "agy", "codex")
