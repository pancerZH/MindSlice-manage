import contextlib
import io
import sys
import textwrap
import unittest
from pathlib import Path

from mindslice_manage import discover
from mindslice_manage.config import load_config, load_projects

from helpers import ENGINE, ENGINE_SPEC, Workspace

HERE = Path(__file__).resolve().parent

GOOD = """
schema = 1
name = "lib"
ignore = ["CHANGELOG.md"]
[version]
locations = [ { file = "pyproject.toml", regex = '(?m)^version = "([^"]+)"' },
              { file = "lib/__init__.py", regex = '__version__ = "([^"]+)"' } ]
[[deps]]
project = "engine"
kind = "min"
locations = [ { file = "pyproject.toml", regex = '"engine>=([^"]+)"' } ]
[commands]
test = [ ["python3", "-m", "unittest"] ]
"""


class Discover(Workspace):
    def setUp(self):
        super().setUp()
        self.repo("engine", ENGINE)
        self.spec("engine", ENGINE_SPEC)
        self.lib = self.repo("lib", {
            "pyproject.toml": '[project]\nname = "lib"\nversion = "0.4.0"\ndependencies = ["engine>=1.0"]\n',
            "lib/__init__.py": '__version__ = "0.4.0"\n', "CHANGELOG.md": "0.4.0: first\n", "README.md": "lib 0.4.0\n"})
        self.proposals = self.root / "proposals"
        self.proposals.mkdir()

    def propose(self, n: int, text: str) -> None:
        (self.proposals / f"proposal-{n}.toml").write_text(textwrap.dedent(text).lstrip("\n"), encoding="utf-8")

    def run_discover(self, yes=True, answer="y"):
        argv = f'["{sys.executable}", "{HERE / "fake_harness.py"}", "{self.proposals}", "{{prompt}}"]'
        cfg = load_config(self.config(f"[harness]\ncmd = {argv}\nresume = {argv}\nrepairs = 2\n"))
        p = next(p for p in load_projects(cfg) if p.name == "lib")
        with contextlib.redirect_stdout(io.StringIO()):
            ok = discover.discover_one(cfg, load_projects(cfg), p, yes, ask=lambda _: answer)
        return ok, cfg

    def prompts(self) -> list[str]:
        return [f.read_text() for f in sorted(self.proposals.glob("prompt-seen-*"))]

    def test_repair_rounds_then_write(self):
        self.propose(0, GOOD.replace("__version__", "__versio__"))  # a regex that matches nothing
        self.propose(1, GOOD.replace('ignore = ["CHANGELOG.md"]', "ignore = []"))  # leaves CHANGELOG.md and README.md
        self.propose(2, GOOD.replace('["CHANGELOG.md"]', '["CHANGELOG.md", "README.md"]'))
        ok, cfg = self.run_discover()
        self.assertTrue(ok)
        prompts = self.prompts()
        self.assertEqual(len(prompts), 3)
        self.assertIn("项目名：`lib`", prompts[0])
        self.assertIn("matches 0 times", prompts[1])
        self.assertIn("README.md:1: lib 0.4.0", prompts[2])
        written = (cfg.managed / "lib.toml").read_text()
        self.assertTrue(written.startswith("# mindslice-manage discover"))
        self.assertIn("README.md", written)
        self.assertFalse((self.lib / "LOCAL_EDIT").exists())  # the session only changed its snapshot
        run = next(cfg.runs.iterdir())
        self.assertFalse((run / "work").exists())
        self.assertTrue((run / "proposal.toml").is_file())

    def test_not_written_without_yes(self):
        self.propose(0, GOOD.replace('["CHANGELOG.md"]', '["CHANGELOG.md", "README.md"]'))
        ok, cfg = self.run_discover(yes=False, answer="n")
        self.assertFalse(ok)
        self.assertFalse((cfg.managed / "lib.toml").exists())

    def test_gives_up_after_repairs(self):
        self.propose(0, "not toml [")
        ok, cfg = self.run_discover()
        self.assertFalse(ok)
        self.assertEqual(len(self.prompts()), 3)
        self.assertIn("TOML 解析失败", self.prompts()[2])  # the round-0 file is still there and still broken
        self.assertFalse((cfg.managed / "lib.toml").exists())


class Verify(Workspace):
    def test_errors(self):
        self.repo("engine", ENGINE)
        lib = self.repo("lib", {"pyproject.toml": 'version = "0.1.0"\n', ".gitignore": "local.txt\n"})
        (lib / "local.txt").write_text('version = "0.1.0"\n')
        text = GOOD.replace("lib/__init__.py", "local.txt").replace('project = "engine"', 'project = "other"')
        text = text.replace('"python3"', '"no-such-tool-xyz"')
        result = discover.verify(lib, text, "lib", {"engine"})
        errors = "\n".join(result["errors"])
        self.assertIn("local.txt: 不在仓库里", errors)
        self.assertIn("'other' 不是受管项目", errors)
        self.assertIn("no-such-tool-xyz", errors)

    def test_jobs_and_mail(self):
        self.repo("engine", ENGINE)
        lib = self.repo("lib", {"pyproject.toml": '[project]\nversion = "0.4.0"\ndependencies = ["engine>=1.0"]\n',
                                "lib/__init__.py": '__version__ = "0.4.0"\n', "CHANGELOG.md": "",
                                "systemd/lib.timer": "[Timer]\nOnCalendar=daily\n", "systemd/notes.txt": "hello",
                                "config.toml": '[mail]\nsmtp = "/nowhere/smtp.toml"\n'})
        text = GOOD + '[[jobs]]\ntimer = "lib.timer"\ntemplates = ["systemd/lib.timer", "systemd/notes.txt"]\n' \
            '[mail]\naccount = { file = "config.toml", key = "mail.account" }\n'
        errors = "\n".join(discover.verify(lib, text, "lib", {"engine"})["errors"])
        self.assertIn("systemd/notes.txt: 不是 systemd unit", errors)
        self.assertIn("没有 mail.account", errors)
        fixed = text.replace(', "systemd/notes.txt"', "").replace("mail.account", "mail.smtp")
        self.assertEqual(discover.verify(lib, fixed, "lib", {"engine"})["errors"], [])  # the account file need not exist

    def test_inconsistency_is_reported_not_rejected(self):
        self.repo("engine", ENGINE)
        lib = self.repo("lib", {"pyproject.toml": '[project]\nversion = "0.4.1"\ndependencies = ["engine>=1.0"]\n',
                                "lib/__init__.py": '__version__ = "0.4.0"\n', "CHANGELOG.md": ""})
        result = discover.verify(lib, GOOD, "lib", {"engine"})
        self.assertEqual(result["errors"], [])
        self.assertTrue(result["inconsistent"])


if __name__ == "__main__":
    unittest.main()
