"""release: local bare repos as origin, a fake gh and uv on PATH, and the fake PyPI."""

import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

from mindslice_manage import actions, release

from helpers import ENGINE, ENGINE_SPEC, GIT, Workspace, git, pypi
from test_actions import quiet

FAKE_GH = '''
import json, os, sys
state_file = os.environ["FAKE_GH_STATE"]
state = json.load(open(state_file))
args = sys.argv[1:]
if args[:2] == ["run", "list"]:
    sha = args[args.index("--commit") + 1]
    runs = [{"name": "tests", "status": "completed", "conclusion": state["ci"], "event": "push", "url": "u/" + sha[:7]}]
    if any(state["releases"].get(t) == sha for t in state["releases"]):
        runs.append({"name": "publish", "status": "completed", "conclusion": "success", "event": "release", "url": "p"})
    print(json.dumps(runs))
elif args[:2] == ["release", "view"]:
    sys.exit(0 if args[2] in state["releases"] else 1)
elif args[:2] == ["release", "create"]:
    import subprocess
    state["releases"][args[2]] = subprocess.run(["git", "rev-parse", args[2] + "^{}"], capture_output=True,
                                                text=True).stdout.strip()
    json.dump(state, open(state_file, "w"))
else:
    sys.exit(f"fake gh: {args}")
'''

FAKE_UV = '''
import pathlib, sys
args = sys.argv[1:]
if args[:2] == ["lock", "--upgrade-package"]:
    version = args[2].split("==")[1]
    lock = pathlib.Path("uv.lock")
    lines = lock.read_text().splitlines()
    lock.write_text("\\n".join(l if not l.startswith("version") else f'version = "{version}"' for l in lines) + "\\n")
elif args != ["sync"]:
    sys.exit(f"fake uv: {args}")
'''

PLUGIN_SPEC = """
schema = 1
name = "plugin"
[version]
locations = [ { file = "plugin.json", regex = '"version": "([^"]+)"' } ]
follows = "engine"
[[deps]]
project = "engine"
kind = "pin"
locations = [
  { file = "upstream.lock", regex = '"version": "([^"]+)"' },
  { file = "upstream.lock", regex = '"commit": "([0-9a-f]{40})"', value = "commit" },
  { file = "upstream.lock", regex = '"sha256": "([0-9a-f]{64})"', value = "wheel_sha256" },
]
"""

APP_SPEC = """
schema = 1
name = "app"
ignore = ["uv.lock"]
[version]
locations = [ { file = "pyproject.toml", regex = '(?m)^version = "([^"]+)"' } ]
[[deps]]
project = "engine"
kind = "lock"
locations = [ { file = "uv.lock", regex = '(?m)^version = "([^"]+)"', update = ["uv", "lock", "--upgrade-package", "engine"] } ]
"""


class Release(Workspace):
    def setUp(self):
        super().setUp()
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        for name, code in (("gh", FAKE_GH), ("uv", FAKE_UV)):
            (bin_dir / name).write_text(f"#!{sys.executable}\n{code}", encoding="utf-8")
            (bin_dir / name).chmod(0o755)
        self.gh_state = self.root / "gh.json"
        self.set_ci("success")
        env = {"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}", "FAKE_GH_STATE": str(self.gh_state)}
        self.enterContext(patch.dict(os.environ, env))
        self.enterContext(patch.multiple(release, POLL=0, CI_APPEARS_MINUTES=0))
        workflow = {".github/workflows/tests.yml": "on: push\n"}
        self.engine = self.published("engine", {**ENGINE, **workflow}, tag="v1.2.0")
        self.spec("engine", ENGINE_SPEC + f'\n[commands]\ntest = [ ["{sys.executable}", "-c", "pass"] ]\n')
        self.plugin = self.published("plugin", {
            **workflow, "plugin.json": '{"version": "1.2.0-1"}\n',
            "upstream.lock": '{"version": "1.2.0", "commit": "' + "b" * 40 + '", "sha256": "' + "a" * 64 + '"}\n'})
        self.spec("plugin", PLUGIN_SPEC)
        self.app = self.published("app", {"pyproject.toml": '[project]\nname = "app"\nversion = "0.3.0"\n',
                                          "uv.lock": '[[package]]\nname = "engine"\nversion = "1.2.0"\n'})
        self.spec("app", APP_SPEC)
        self.enterContext(pypi({"engine": {"1.2.0": ["a" * 64], "1.3.0": ["c" * 64]}}))

    def published(self, name: str, files: dict[str, str], tag: str | None = None) -> Path:
        """A repo with a bare origin it is in sync with, and a git identity of its own (release commits)."""
        repo = self.repo(name, files, tag)
        origin = self.root / "origins" / f"{name}.git"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
        git(repo, "remote", "add", "origin", str(origin))
        git(repo, "push", "-q", "-u", "origin", "main", *(["--tags"] if tag else []))
        git(repo, "config", "user.name", "test")
        git(repo, "config", "user.email", "test@example.invalid")
        return repo

    def set_ci(self, conclusion: str) -> None:
        state = json.loads(self.gh_state.read_text()) if self.gh_state.exists() else {"releases": {}}
        self.gh_state.write_text(json.dumps(state | {"ci": conclusion}))

    def origin_log(self, name: str) -> list[str]:
        return git(self.root / "origins" / f"{name}.git", "log", "--format=%s", "main").splitlines()

    def run_release(self, version: str = "1.3.0"):
        return quiet(release.release, self.ctx(), "engine", version)

    def test_one_call_releases_and_brings_the_dependents_along(self):
        ok, out = self.run_release()
        self.assertTrue(ok, out)
        self.assertIn('__version__ = "1.3.0"', (self.engine / "engine/__init__.py").read_text())
        self.assertEqual(self.origin_log("engine")[0], "update version number to 1.3.0")
        head = git(self.engine, "rev-parse", "HEAD")
        self.assertIn(f"{head}\trefs/tags/v1.3.0", git(self.engine, "ls-remote", "--tags", "origin"))
        self.assertEqual(json.loads(self.gh_state.read_text())["releases"], {"v1.3.0": head})
        self.assertEqual(self.origin_log("plugin")[0], "update engine to 1.3.0; plugin 1.3.0-1")
        self.assertEqual((self.plugin / "upstream.lock").read_text(),
                         '{"version": "1.3.0", "commit": "' + head + '", "sha256": "' + "c" * 64 + '"}\n')
        self.assertEqual(self.origin_log("app")[0], "lock engine 1.3.0")
        self.assertIn('version = "1.3.0"', (self.app / "uv.lock").read_text())
        self.assertNotIn("工具不会执行", out)
        # a second call finds everything done, also after a later commit on top of the tag
        git(self.engine, "commit", "-q", "--allow-empty", "-m", "later")
        git(self.engine, "push", "-q")
        ok, out = self.run_release()
        self.assertTrue(ok, out)
        self.assertEqual([len(self.origin_log(n)) for n in ("engine", "plugin", "app")], [3, 2, 2])
        self.assertIn(f"{head}\trefs/tags/v1.3.0", git(self.engine, "ls-remote", "--tags", "origin"))  # not moved
        git(self.engine, "tag", "-f", "v1.3.0", git(self.engine, "rev-parse", "HEAD~2"))  # a tag off the branch line
        git(self.engine, "checkout", "-q", "-b", "side", "HEAD~2")
        git(self.engine, "commit", "-q", "--allow-empty", "-m", "side")
        git(self.engine, "tag", "-f", "v1.3.0")
        git(self.engine, "checkout", "-q", "main")
        with self.assertRaises(actions.ActionError) as caught:
            self.run_release()
        self.assertIn("不在现在的 HEAD", str(caught.exception))

    def test_failed_ci_stops_before_the_tag_and_a_rerun_continues(self):
        self.set_ci("failure")
        with self.assertRaises(actions.ActionError) as caught:
            self.run_release()
        self.assertIn("engine 的 CI 失败", str(caught.exception))
        self.assertEqual(self.origin_log("engine")[0], "update version number to 1.3.0")  # pushed, then CI ran
        self.assertNotIn("v1.3.0", git(self.engine, "tag"))
        self.assertEqual(self.origin_log("plugin"), ["init"])
        self.set_ci("success")
        ok, out = self.run_release()
        self.assertTrue(ok, out)
        self.assertEqual(self.origin_log("engine"), ["update version number to 1.3.0", "init"])
        self.assertEqual(self.origin_log("plugin")[0], "update engine to 1.3.0; plugin 1.3.0-1")

    def test_failed_tests_restore_the_version(self):
        self.spec("engine", ENGINE_SPEC + f'\n[commands]\ntest = [ ["{sys.executable}", "-c", "raise SystemExit(1)"] ]\n')
        with self.assertRaises(actions.ActionError) as caught:
            self.run_release()
        self.assertIn("测试失败，版本号已还原", str(caught.exception))
        self.assertEqual(git(self.engine, "status", "--porcelain"), "")
        self.assertEqual(self.origin_log("engine"), ["init"])

    def test_preflight_and_the_privacy_scan(self):
        (self.app / "notes.txt").write_text("draft\n")
        with self.assertRaises(actions.ActionError) as caught:
            self.run_release()
        self.assertIn("app 有未提交的改动", str(caught.exception))
        (self.app / "notes.txt").unlink()

        other = self.root / "other-clone"
        subprocess.run([*GIT, "clone", "-q", str(self.root / "origins" / "plugin.git"), str(other)], check=True)
        subprocess.run([*GIT, "-C", str(other), "commit", "-q", "--allow-empty", "-m", "elsewhere"], check=True)
        subprocess.run([*GIT, "-C", str(other), "push", "-q"], check=True)
        with self.assertRaises(actions.ActionError) as caught:
            self.run_release()
        self.assertIn("plugin 落后于 origin", str(caught.exception))
        git(self.plugin, "pull", "-q")

        (self.engine / "NOTES.md").write_text(f"built in {Path.home()}/somewhere\n")
        git(self.engine, "add", "NOTES.md")
        git(self.engine, "commit", "-q", "-m", "notes")
        with self.assertRaises(actions.ActionError) as caught:
            self.run_release()
        self.assertIn("本机路径或邮箱", str(caught.exception))
        self.assertIn("NOTES.md", str(caught.exception))
        self.assertEqual(self.origin_log("engine"), ["init"])

    def test_dry_run_changes_nothing(self):
        ok, out = quiet(release.release, self.ctx(), "engine", "1.3.0", True)
        self.assertTrue(ok)
        self.assertIn("会发布 engine 1.3.0（现在 1.2.0，tag v1.3.0）", out)
        self.assertIn("plugin（pin）", out)
        self.assertIn("app（lock）", out)
        self.assertEqual(git(self.engine, "status", "--porcelain"), "")
        self.assertEqual(self.origin_log("engine"), ["init"])
