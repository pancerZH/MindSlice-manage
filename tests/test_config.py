import tomllib
import unittest

from mindslice_manage.config import load_config, load_projects, parse_spec, url_dirname

from helpers import Workspace

GOOD = """
schema = 1
name = "x"
[version]
locations = [ { file = "pyproject.toml", regex = '^version = "([^"]+)"' } ]
"""


def errors(text: str, name: str = "x") -> list[str]:
    return parse_spec(tomllib.loads(text), name)[1]


class Schema(unittest.TestCase):
    def test_good(self):
        self.assertEqual(errors(GOOD), [])

    def test_name_and_schema(self):
        self.assertTrue(any("name" in e for e in errors(GOOD, "y")))
        self.assertTrue(any("schema" in e for e in errors(GOOD.replace("schema = 1", "schema = 2"))))

    def test_unknown_field(self):
        self.assertTrue(any("未知字段 'extra'" in e for e in errors(GOOD + 'extra = 1\n')))

    def test_regex_needs_one_group(self):
        bad = GOOD.replace('([^"]+)', '[^"]+')
        self.assertTrue(any("恰好有一个捕获组" in e for e in errors(bad)))

    def test_dep_rules(self):
        lock = GOOD + '[[deps]]\nproject = "e"\nkind = "lock"\nlocations = [ { file = "uv.lock", regex = "(1)" } ]\n'
        self.assertTrue(any("update" in e for e in errors(lock)))
        commit = GOOD + '[[deps]]\nproject = "e"\nkind = "min"\nlocations = [ { file = "a", regex = "(1)", value = "commit" } ]\n'
        self.assertTrue(any("只有 pin" in e for e in errors(commit)))
        pin = GOOD + '[[deps]]\nproject = "e"\nkind = "pin"\nlocations = [ { file = "a", regex = "(1)", value = "commit" } ]\n'
        self.assertTrue(any('value = "version"' in e for e in errors(pin)))

    def test_follows_needs_a_pin(self):
        bad = GOOD.replace("[version]", '[version]\nfollows = "e"')
        self.assertTrue(any("pin" in e for e in errors(bad)))

    def test_commands(self):
        ok = GOOD + '[commands]\ntest = [ ["make", "test"], { cmd = ["t"], env = { A = "1" } } ]\n'
        self.assertEqual(errors(ok), [])
        self.assertTrue(errors(GOOD + '[commands]\ntest = [ "make test" ]\n'))

    def test_jobs_and_mail(self):
        ok = GOOD + '[[jobs]]\ntimer = "a.timer"\ntemplates = ["systemd/a.timer", "systemd/a.service"]\n' \
            '[mail]\naccount = { file = "config.toml", key = "mail.smtp" }\nto = { file = "@account", key = "to" }\n'
        self.assertEqual(errors(ok), [])
        self.assertTrue(any(".timer 结尾" in e for e in errors(GOOD + '[[jobs]]\ntimer = "a.service"\n')))
        self.assertTrue(any("没有 a.timer 的模板" in e for e in errors(GOOD + '[[jobs]]\ntimer = "a.timer"\ntemplates = ["x.service"]\n')))
        self.assertTrue(any("缺少 account" in e for e in errors(GOOD + '[mail]\nto = { file = "c", key = "k" }\n')))

    def test_host(self):
        bad = GOOD + '[release]\nplugins = [ { host = "vim", id = "x" } ]\n'
        self.assertTrue(any("host" in e for e in errors(bad)))


class Naming(Workspace):
    def test_project_names(self):
        self.repo("a", {"pyproject.toml": '[project]\nname = "pkg-a"\n'})
        b = self.repo("b", {"README.md": "x"})
        from helpers import git
        git(b, "remote", "add", "origin", "git@github.com:someone/Repo-B.git")
        self.repo("c", {"README.md": "x"})
        cfg = load_config(self.config())
        self.assertEqual([p.name for p in load_projects(cfg)], ["pkg-a", "Repo-B", "c"])

    def test_url_dirname(self):
        self.assertEqual(url_dirname("git@github.com:o/My-Repo.git"), "My-Repo")
        self.assertEqual(url_dirname("https://github.com/o/r/"), "r")


if __name__ == "__main__":
    unittest.main()
