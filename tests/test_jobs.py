import textwrap
import unittest
from unittest import mock

from mindslice_manage import jobs, sources
from mindslice_manage.check import run_all

from helpers import Workspace

SERVICE = """
[Unit]
Description=app run
# a comment
[Service]
Type=oneshot
WorkingDirectory=@REPO@
ExecStart=%h/.local/bin/uv run app
"""
TIMER = "[Timer]\nOnCalendar=*-*-* 07:30:00\nOnCalendar=*-*-* 18:00:00\nPersistent=true\n[Install]\nWantedBy=timers.target\n"
SPEC = """
schema = 1
name = "app"
[version]
locations = [ { file = "pyproject.toml", regex = '(?m)^version = "([^"]+)"' } ]
[[jobs]]
timer = "app.timer"
templates = ["systemd/app.timer", "systemd/app.service"]
[mail]
account = { file = "config.toml", key = "mail.smtp" }
to = { file = "@account", key = "to" }
"""


class Jobs(Workspace):
    def setUp(self):
        super().setUp()
        self.app = self.repo("app", {"pyproject.toml": '[project]\nname = "app"\nversion = "1.0"\n',
                                     "systemd/app.service": SERVICE, "systemd/app.timer": TIMER})
        self.spec("app", SPEC)
        self.installed = self.root / "units"
        self.installed.mkdir()
        # installed the way the READMEs say: @REPO@ filled in, %h left for systemd
        (self.installed / "app.service").write_text(textwrap.dedent(SERVICE).replace("@REPO@", str(self.app)))
        (self.installed / "app.timer").write_text(TIMER)
        self.account = self.root / "smtp.toml"
        self.account.write_text('host = "smtp.example"\nport = 465\nusername = "me"\npassword = "secret"\nto = ["me@example.invalid"]\n')
        (self.app / "config.toml").write_text(f'[mail]\nsmtp = "{self.account}"\n')
        self.timer = {"Id": "app.timer", "LoadState": "loaded", "UnitFileState": "enabled", "ActiveState": "active",
                      "NextElapseUSecRealtime": "Wed 2026-11-18 07:30:00 MST", "LastTriggerUSec": "Tue 2026-11-17 18:00:10 MST",
                      "FragmentPath": str(self.installed / "app.timer")}
        self.service = {"Id": "app.service", "LoadState": "loaded", "UnitFileState": "static", "ActiveState": "inactive",
                        "Result": "success", "ExecMainStartTimestamp": "Tue 2026-11-17 18:00:10 MST",
                        "ExecMainStartTimestampMonotonic": "1000000", "ExecMainExitTimestampMonotonic": str(1000000 + 396 * 10**6),
                        "FragmentPath": str(self.installed / "app.service")}
        self.linger = True

    def run_check(self, units="default"):
        fake = {"app.timer": self.timer, "app.service": self.service} if units == "default" else units
        with mock.patch.object(sources, "units", lambda names: fake), mock.patch.object(sources, "linger", lambda: self.linger):
            states, found = run_all(self.ctx(online=False))
        return states, found

    def messages(self, found, level):
        return [f.message for f in found if f.level == level]

    def test_all_good(self):
        states, found = self.run_check()
        self.assertEqual([f for f in found if f.level != "info"], [])
        row = jobs.rows(states)[1]
        self.assertEqual(row, ["app", "app", "07:30:00, 18:00:00", "启用", "11-17 18:00 成功，6.6 分钟", "11-18 07:30",
                               "me@example.invalid"])
        self.assertNotIn("secret", repr(states))
        self.service["ExecMainExitTimestampMonotonic"] = str(1000000 + 3 * 10**6)
        self.assertIn("成功，3 秒", jobs.rows(self.run_check()[0])[1][4])

    def test_not_installed(self):
        self.timer = {"Id": "app.timer", "LoadState": "not-found"}
        _, found = self.run_check()
        f = next(f for f in found if "没有安装" in f.message)
        self.assertIn('sed "s#@REPO@#', f.fix)
        self.assertIn("systemctl --user enable --now app.timer", f.fix)

    def test_disabled(self):
        self.timer["UnitFileState"], self.timer["ActiveState"] = "disabled", "inactive"
        _, found = self.run_check()
        self.assertTrue(any("没有启用" in m for m in self.messages(found, "warning")))

    def test_drift(self):
        (self.installed / "app.timer").write_text(TIMER.replace("07:30", "08:00"))
        _, found = self.run_check()
        drift = [m for m in self.messages(found, "warning") if "不一致" in m]
        self.assertEqual(len(drift), 1)
        self.assertIn("OnCalendar=*-*-* 08:00:00", drift[0])

    def test_failed_and_running_and_never(self):
        self.service["Result"] = "exit-code"
        _, found = self.run_check()
        self.assertTrue(any("上次运行失败（exit-code" in m for m in self.messages(found, "warning")))
        self.service["ActiveState"] = "activating"
        states, _ = self.run_check()
        self.assertEqual(states[0]["jobs"][0]["last"], "运行中")
        self.service = {"Id": "app.service", "LoadState": "loaded", "ActiveState": "inactive"}
        self.timer.pop("LastTriggerUSec")
        _, found = self.run_check()
        self.assertIn("app.service 还没运行过", self.messages(found, "info"))

    def test_linger(self):
        self.linger = False
        _, found = self.run_check()
        self.assertTrue(any(f.project == "本机" and "linger" in f.message for f in found))

    def test_mail_problems(self):
        self.account.write_text('host = "smtp.example"\nport = 465\nusername = "me"\nto = []\n')
        _, found = self.run_check()
        self.assertTrue(any("缺少 password" in m for m in self.messages(found, "warning")))
        self.assertIn("收件人为空，这个项目不发邮件", self.messages(found, "info"))
        self.account.unlink()
        _, found = self.run_check()
        self.assertTrue(any("发信账号文件" in m and "不存在" in m for m in self.messages(found, "warning")))

    def test_no_systemd(self):
        states, found = self.run_check(units=None)
        self.assertEqual(states[0]["jobs"][0]["state"], "读不到 systemd")
        self.assertFalse(any("app.timer" in f.message for f in found))


if __name__ == "__main__":
    unittest.main()
