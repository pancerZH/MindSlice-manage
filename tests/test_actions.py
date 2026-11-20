import contextlib
import io
import sys
import unittest
from unittest.mock import patch

from mindslice_manage import actions
from mindslice_manage.check import run_all

from helpers import ENGINE, ENGINE_SPEC, Workspace, git, pypi

# a stand-in for `uv lock --upgrade-package engine`
RELOCK = "import pathlib\np = pathlib.Path('uv.lock')\np.write_text(p.read_text().replace('1.1.0', '1.2.0'))\n"


def quiet(fn, *args):
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
        result = fn(*args)
    return result, out.getvalue()


class Bump(Workspace):
    def setUp(self):
        super().setUp()
        self.lib = self.repo("lib", {"pyproject.toml": '[project]\nname = "lib"\nversion = "0.1.1"\n',
                                     "lib/__init__.py": '__version__ = "0.1.1"\n'})
        self.spec("lib", """
            schema = 1
            name = "lib"
            [version]
            locations = [ { file = "pyproject.toml", regex = '(?m)^version = "([^"]+)"' },
                          { file = "lib/__init__.py", regex = '__version__ = "([^"]+)"' } ]
        """)

    def test_bump_every_location(self):
        quiet(actions.bump, self.ctx(online=False), "lib", "patch")
        self.assertIn('version = "0.1.2"', (self.lib / "pyproject.toml").read_text())
        self.assertIn('"0.1.2"', (self.lib / "lib/__init__.py").read_text())
        self.assertEqual(git(self.lib, "log", "--oneline").count("\n"), 0)  # nothing committed

    def test_refuses_dirty_files(self):
        (self.lib / "lib/__init__.py").write_text('__version__ = "0.1.1"  # edited\n')
        with self.assertRaisesRegex(actions.ActionError, "未提交"):
            quiet(actions.bump, self.ctx(online=False), "lib", "0.2.0")

    def test_explicit_version_fixes_disagreement(self):
        (self.lib / "lib/__init__.py").write_text('__version__ = "0.1.0"\n')
        git(self.lib, "commit", "-qam", "drift")
        with self.assertRaisesRegex(actions.ActionError, "不一致"):
            quiet(actions.bump, self.ctx(online=False), "lib", "patch")
        quiet(actions.bump, self.ctx(online=False), "lib", "0.1.1")
        self.assertIn('"0.1.1"', (self.lib / "lib/__init__.py").read_text())

    def test_unknown_project(self):
        with self.assertRaisesRegex(actions.ActionError, "没有叫 nope"):
            quiet(actions.bump, self.ctx(online=False), "nope", "patch")


class Sync(Workspace):
    def setUp(self):
        super().setUp()
        self.engine = self.repo("engine", ENGINE, tag="v1.2.0")
        git(self.engine, "tag", "v1.1.0")
        self.commit = git(self.engine, "rev-parse", "HEAD")
        self.spec("engine", ENGINE_SPEC)

    def test_pin_follows_and_commands(self):
        plugin = self.repo("plugin", {
            "plugin.json": '{"version": "1.1.0-2"}\n',
            "upstream.lock": '{"version": "1.1.0", "commit": "' + "b" * 40 + '", "sha256": "' + "c" * 64 + '"}\n',
            "README.md": "pip install engine==1.1.0\n"})
        self.spec("plugin", f"""
            schema = 1
            name = "plugin"
            [version]
            locations = [ {{ file = "plugin.json", regex = '"version": "([^"]+)"' }} ]
            follows = "engine"
            [[deps]]
            project = "engine"
            kind = "pin"
            locations = [
              {{ file = "upstream.lock", regex = '"version": "([^"]+)"' }},
              {{ file = "upstream.lock", regex = '"commit": "([0-9a-f]{{40}})"', value = "commit" }},
              {{ file = "upstream.lock", regex = '"sha256": "([0-9a-f]{{64}})"', value = "wheel_sha256" }},
              {{ file = "README.md", regex = 'engine==([\\d.]+)' }},
            ]
            [commands]
            build = [ ["{sys.executable}", "-c", "open('built', 'w').write('ok')"] ]
            test = [ ["{sys.executable}", "-c", "import os; assert os.environ['T'] == '1'"], {{ cmd = ["{sys.executable}", "-c", "pass"], env = {{ T = "1" }} }} ]
        """)
        with pypi({"engine": {"1.1.0": ["c" * 64], "1.2.0": ["a" * 64]}}):
            ctx = self.ctx()
            ok, out = quiet(actions.sync, ctx, ["plugin"], None, False)
            self.assertFalse(ok)  # the first test command has no T=1
            self.assertIn('"version": "1.2.0-1"', (plugin / "plugin.json").read_text())
            self.assertEqual((plugin / "upstream.lock").read_text(),
                             '{"version": "1.2.0", "commit": "' + self.commit + '", "sha256": "' + "a" * 64 + '"}\n')
            self.assertIn("engine==1.2.0", (plugin / "README.md").read_text())
            self.assertTrue((plugin / "built").exists())
            self.assertIn("下一步", out)
            # now it is up to date: nothing more to do, and check finds nothing wrong
            git(plugin, "commit", "-qam", "synced")
            _, out = quiet(actions.sync, self.ctx(), ["plugin"], None, False)
            self.assertIn("已经是最新", out)
            errors = [f for f in run_all(self.ctx())[1] if f.level == "error"]
            self.assertEqual(errors, [])

    def test_lock_runs_update(self):
        app = self.repo("app", {"pyproject.toml": '[project]\nname = "app"\nversion = "0.3.0"\n',
                                "uv.lock": '[[package]]\nname = "engine"\nversion = "1.1.0"\n', "relock.py": RELOCK})
        self.spec("app", f"""
            schema = 1
            name = "app"
            ignore = ["uv.lock"]
            [version]
            locations = [ {{ file = "pyproject.toml", regex = '(?m)^version = "([^"]+)"' }} ]
            [[deps]]
            project = "engine"
            kind = "lock"
            locations = [ {{ file = "uv.lock", regex = 'name = "engine"\\nversion = "([^"]+)"', update = ["{sys.executable}", "relock.py"] }} ]
        """)
        with pypi({"engine": {"1.1.0": ["c" * 64], "1.2.0": ["a" * 64]}}):
            _, out = quiet(actions.sync, self.ctx(), [], None, True)
            self.assertIn("lock engine: 1.1.0 → 1.2.0", out)
            self.assertIn('"1.1.0"', (app / "uv.lock").read_text())  # dry run
            with self.assertRaisesRegex(actions.ActionError, "不支持 --to"):
                quiet(actions.sync, self.ctx(), ["app"], "1.2.0", False)
            self.assertIn('"1.1.0"', (app / "uv.lock").read_text())
            quiet(actions.sync, self.ctx(), ["app"], None, False)
            self.assertIn('version = "1.2.0"', (app / "uv.lock").read_text())

    def test_exact_version_is_given_to_the_lock_resolver(self):
        app = self.repo("app", {"uv.lock": 'version = "1.1.0"\n', "VERSION": "0.1.0\n"})
        self.spec("app", """
            schema = 1
            name = "app"
            [version]
            locations = [{ file = "VERSION", regex = '^(.*)$' }]
            [[deps]]
            project = "engine"
            kind = "lock"
            locations = [{ file = "uv.lock", regex = 'version = "([^"]+)"', update = ["uv", "lock", "--upgrade-package", "engine"] }]
        """)

        def relock(argv, cwd):
            self.assertEqual(argv, ["uv", "lock", "--upgrade-package", "engine==1.0.0"])
            self.assertEqual(cwd, app)
            (cwd / "uv.lock").write_text('version = "1.0.0"\n')
            return True

        with patch.object(actions, "run", side_effect=relock) as command:
            quiet(actions.sync, self.ctx(online=False), ["app"], "1.0.0", False)
            command.assert_called_once()

    def test_bump_follows_only_build(self):
        plugin = self.repo("plugin", {"plugin.json": '{"version": "1.2.0-1"}\n', "upstream.lock": '{"version": "1.2.0"}\n'})
        self.spec("plugin", """
            schema = 1
            name = "plugin"
            [version]
            locations = [ { file = "plugin.json", regex = '"version": "([^"]+)"' } ]
            follows = "engine"
            [[deps]]
            project = "engine"
            kind = "pin"
            locations = [ { file = "upstream.lock", regex = '"version": "([^"]+)"' } ]
        """)
        with self.assertRaisesRegex(actions.ActionError, "只能 bump build"):
            quiet(actions.bump, self.ctx(online=False), "plugin", "minor")
        quiet(actions.bump, self.ctx(online=False), "plugin", "build")
        self.assertIn("1.2.0-2", (plugin / "plugin.json").read_text())

    def test_missing_tag(self):
        self.repo("plugin", {"upstream.lock": '{"version": "1.1.0", "commit": "' + "b" * 40 + '"}\n'})
        self.spec("plugin", """
            schema = 1
            name = "plugin"
            [version]
            locations = [ { file = "upstream.lock", regex = '"version": "([^"]+)"' } ]
            [[deps]]
            project = "engine"
            kind = "pin"
            locations = [ { file = "upstream.lock", regex = '"version": "([^"]+)"' },
                          { file = "upstream.lock", regex = '"commit": "([0-9a-f]{40})"', value = "commit" } ]
        """)
        with pypi({"engine": {"1.3.0": ["a" * 64]}}):
            with self.assertRaisesRegex(actions.ActionError, "tag"):
                quiet(actions.sync, self.ctx(), ["plugin"], None, False)


if __name__ == "__main__":
    unittest.main()
