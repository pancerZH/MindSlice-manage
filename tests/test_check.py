import unittest

from mindslice_manage.check import run_all

from helpers import ENGINE, ENGINE_SPEC, Workspace, git, pypi, write_json

PLUGIN_SPEC = """
schema = 1
name = "plugin"
ignore = ["dist/*"]

[version]
locations = [ { file = "plugin.json", regex = '"version": "([^"]+)"' } ]
follows = "engine"

[release]
plugins = [ { host = "claude", id = "p@m", hint = "update it" }, { host = "agy", id = "p" } ]

[[deps]]
project = "engine"
kind = "pin"
locations = [
  { file = "upstream.lock", regex = '"version": "([^"]+)"' },
  { file = "upstream.lock", regex = '"commit": "([0-9a-f]{40})"', value = "commit" },
  { file = "upstream.lock", regex = '"sha256": "([0-9a-f]{64})"', value = "wheel_sha256" },
]
"""


def lock(version: str, commit: str, sha: str) -> str:
    return f'{{"version": "{version}", "commit": "{commit}", "sha256": "{sha}"}}\n'


class Rules(Workspace):
    def setUp(self):
        super().setUp()
        self.engine = self.repo("engine", ENGINE, tag="v1.2.0")
        self.commit = git(self.engine, "rev-parse", "HEAD")
        self.spec("engine", ENGINE_SPEC)

    def findings(self, releases=None, online=True):
        with pypi(releases or {"engine": {"1.2.0": ["a" * 64]}}):
            return run_all(self.ctx(online=online))[1]

    def messages(self, found, level=None):
        return [f.message for f in found if level is None or f.level == level]

    def add_plugin(self, version="1.2.0-1", pinned="1.2.0", commit=None, sha="a" * 64, extra=None):
        files = {"plugin.json": f'{{"version": "{version}"}}\n', "upstream.lock": lock(pinned, commit or self.commit, sha)}
        self.plugin = self.repo("plugin", files | (extra or {}))
        self.spec("plugin", PLUGIN_SPEC)

    def test_all_consistent(self):
        self.add_plugin()
        self.assertEqual(self.messages(self.findings(), "error") + self.messages(self.findings(), "warning"), [])

    def test_own_version_disagrees(self):
        self.repo("lib", {"pyproject.toml": '[project]\nname = "lib"\nversion = "0.1.1"\n',
                          "lib/__init__.py": '__version__ = "0.1.0"\n'})
        self.spec("lib", """
            schema = 1
            name = "lib"
            [version]
            locations = [ { file = "pyproject.toml", regex = '(?m)^version = "([^"]+)"' },
                          { file = "lib/__init__.py", regex = '__version__ = "([^"]+)"' } ]
        """)
        self.assertTrue(any("不一致" in m for m in self.messages(self.findings(), "error")))

    def test_stale_location(self):
        self.add_plugin()
        (self.plugin / "plugin.json").write_text('{"name": "p"}\n')
        self.assertTrue(any("位置不再匹配" in m for m in self.messages(self.findings(), "error")))

    def test_pin_behind_and_commit_and_follows(self):
        self.add_plugin(version="1.1.0-1", pinned="1.1.0", commit="b" * 40)
        git(self.engine, "tag", "v1.1.0")
        found = self.findings({"engine": {"1.1.0": ["c" * 64], "1.2.0": ["a" * 64]}})
        warnings, errors = self.messages(found, "warning"), self.messages(found, "error")
        self.assertTrue(any("pin engine 1.1.0，上游最新 1.2.0" in m for m in warnings))
        self.assertTrue(any("commit" in m for m in errors))
        self.assertTrue(any("wheel_sha256" in m for m in errors))
        self.assertFalse(any("跟随" in m for m in errors))  # 1.1.0-1 follows the pinned 1.1.0

    def test_follows_mismatch(self):
        self.add_plugin(version="1.1.0-3")
        self.assertTrue(any("跟随 engine" in m for m in self.messages(self.findings(), "error")))

    def test_min_above_release(self):
        self.repo("cli", {"pyproject.toml": '[project]\nname = "cli"\nversion = "1.0"\ndependencies = ["engine>=1.3"]\n'})
        self.spec("cli", """
            schema = 1
            name = "cli"
            [version]
            locations = [ { file = "pyproject.toml", regex = '(?m)^version = "([^"]+)"' } ]
            [[deps]]
            project = "engine"
            kind = "min"
            locations = [ { file = "pyproject.toml", regex = '"engine>=([^"]+)"' } ]
        """)
        self.assertTrue(any("engine>=1.3" in m for m in self.messages(self.findings(), "error")))

    def test_uncovered_version(self):
        self.add_plugin(extra={"docs.md": "install engine==1.2.0 or run v1.2.0-1\n", "dist/x.json": '"1.2.0-1"'})
        found = self.messages(self.findings(), "warning")
        self.assertEqual(len([m for m in found if "未登记" in m]), 2)  # both strings in docs.md; dist/ is ignored

    def test_release_without_tag(self):
        git(self.engine, "tag", "-d", "v1.2.0")
        self.assertTrue(any("没有 tag v1.2.0" in m for m in self.messages(self.findings(), "warning")))

    def test_commits_after_tag(self):
        (self.engine / "x").write_text("x")
        git(self.engine, "add", "x")
        git(self.engine, "commit", "-q", "-m", "more")
        self.assertTrue(any("1 个新提交" in m for m in self.messages(self.findings(), "info")))

    def test_unreleased_version(self):
        (self.engine / "engine/__init__.py").write_text('__version__ = "1.3.0"\n')
        git(self.engine, "commit", "-qam", "bump")
        found = self.findings()
        self.assertIn("1.3.0 还没发布（PyPI 最新 1.2.0）", self.messages(found, "info"))
        self.assertTrue(any("tag v1.3.0" in f.fix for f in found))
        self.assertNotIn("1.3.0 还没发布", " ".join(self.messages(self.findings(online=False))))

    def test_offline_uses_tags(self):
        self.add_plugin(version="1.1.0-1", pinned="1.1.0")
        found = self.findings(online=False)
        self.assertTrue(any("上游最新 1.2.0" in m for m in self.messages(found, "warning")))

    def test_installed_copies(self):
        self.add_plugin()
        write_json(self.hosts / "claude.json", {"plugins": {"p@m": [{"version": "1.1.0-1"}]}})
        write_json(self.hosts / "agy" / "p" / "plugin.json", {"version": "1.2.0-1"})
        site = self.hosts / "pipx" / "engine" / "lib" / "python3.12" / "site-packages" / "engine-1.1.0.dist-info"
        site.mkdir(parents=True)
        (site / "METADATA").write_text("Metadata-Version: 2.1\nName: engine\nVersion: 1.1.0\n")
        warnings = self.messages(self.findings(), "warning")
        self.assertIn("claude 装的插件是 1.1.0-1，仓库里是 1.2.0-1", warnings)
        self.assertIn("pipx 装的是 1.1.0，最新 1.2.0", warnings)
        self.assertFalse(any(m.startswith("agy") for m in warnings))


if __name__ == "__main__":
    unittest.main()
