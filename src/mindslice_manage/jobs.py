"""Scheduled jobs (systemd user timers) and the mail settings of a project. Read only: changes are printed as commands."""

import shlex
from pathlib import Path

from . import sources
from .config import Job, Project, Ref

ACCOUNT_KEYS = ("host", "port", "username", "password")
UNIT_DIR = "~/.config/systemd/user"
TIMER_KEYS = ("OnCalendar", "OnBootSec", "OnStartupSec", "OnActiveSec", "OnUnitActiveSec", "OnUnitInactiveSec")


def unit_names(projects: list[Project]) -> list[str]:
    """Every unit the specs mention: the timers, their services and the units that have a template."""
    names = set()
    for p in projects:
        for job in (p.spec.jobs if p.spec else []):
            names |= {job.timer, job.service} | {Path(t).name for t in job.templates}
    return sorted(names)


def read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def unit_lines(text: str, repo: Path) -> list[str]:
    """A unit file as compared: @REPO@ and %h filled in, blank lines and comments left out."""
    text = text.replace("@REPO@", str(repo)).replace("%h", str(Path.home()))
    return [s for line in text.splitlines() if (s := line.strip()) and not s.startswith(("#", ";"))]


def first_difference(want: list[str], have: list[str]) -> str:
    for a, b in zip(want, have):
        if a != b:
            return f"模板是 `{a}`，装的是 `{b}`"
    return f"模板 {len(want)} 行，装的 {len(have)} 行"


def schedule(timer_text: str) -> str:
    out = []
    for line in timer_text.splitlines():
        key, _, value = line.strip().partition("=")
        if key in TIMER_KEYS and value:
            out.append(value.strip().removeprefix("*-*-* ") if key == "OnCalendar" else f"{key}={value.strip()}")
    return ", ".join(out)


def short_time(stamp: str | None) -> str:
    """'Tue 2026-11-17 18:00:10 MST' -> '11-17 18:00'."""
    parts = (stamp or "").split()
    return f"{parts[1][5:]} {parts[2][:5]}" if len(parts) >= 3 else ""


def minutes(service: dict) -> float | None:
    try:
        start, end = int(service.get("ExecMainStartTimestampMonotonic", 0)), int(service.get("ExecMainExitTimestampMonotonic", 0))
    except ValueError:
        return None
    return (end - start) / 60e6 if start and end > start else None


def install_command(p: Project, job: Job) -> str:
    if not job.templates:
        return f"仓库里没有 {job.timer} 的模板，按 {p.name} 的 README 安装"
    cmds = [f"mkdir -p {UNIT_DIR}"]
    cmds += [f'sed "s#@REPO@#{p.repo}#" {shlex.quote(str(p.repo / t))} > {UNIT_DIR}/{Path(t).name}' for t in job.templates]
    cmds.append(f"systemctl --user daemon-reload && systemctl --user enable --now {job.timer}")
    return " && ".join(cmds)


# mail settings

def ref_path(p: Project, ref: Ref, account: Path | None) -> Path | None:
    return account if ref.file == "@account" else p.repo / Path(ref.file).expanduser()


def mail_state(p: Project, add) -> dict:
    """The project's mail account file and recipients; warnings when they cannot be read."""
    m, out = p.spec.mail, {"account": None, "to": []}
    config = ref_path(p, m.account, None)
    try:
        value = sources.toml_key(config, m.account.key)
    except LookupError as e:
        add("warning", f"读不到发信账号的路径（{m.account}）：{e}", f"补上 {config} 里的 {m.account.key}")
        return out
    account = config.parent / Path(str(value)).expanduser()
    out["account"] = str(account)
    if not account.is_file():
        add("warning", f"发信账号文件 {account} 不存在", f"创建它，写上 {'、'.join(ACCOUNT_KEYS)}")
    else:
        missing = []
        for key in ACCOUNT_KEYS:  # only whether they are there: the values (a password among them) are never shown
            try:
                if sources.toml_key(account, key) in ("", None):
                    missing.append(key)
            except LookupError:
                missing.append(key)
        if missing:
            add("warning", f"发信账号文件 {account} 缺少 {'、'.join(missing)}", f"在 {account} 里补上")
    if m.to:
        where = ref_path(p, m.to, account)
        try:
            to = sources.toml_key(where, m.to.key)
        except LookupError as e:
            add("warning", f"读不到收件人（{m.to}）：{e}", f"补上 {where} 里的 {m.to.key}")
            return out
        out["to"] = [t for t in ([to] if isinstance(to, str) else to if isinstance(to, list) else []) if t]
        out["to_where"] = f"{where} 的 {m.to.key}"
        if not out["to"]:
            add("info", "收件人为空，这个项目不发邮件", f"要发就填 {where} 里的 {m.to.key}")
    return out


# jobs

def inspect_jobs(units: dict | None, p: Project, add, recipients: list[str]) -> list[dict]:
    rows = []
    for job in p.spec.jobs:
        row = {"timer": job.timer, "service": job.service, "project": p.name, "recipients": recipients}
        rows.append(row)
        if units is None:
            row["state"] = "读不到 systemd"
            continue
        timer, service = units.get(job.timer, {}), units.get(job.service, {})
        if timer.get("LoadState") != "loaded":
            row["state"] = "未安装"
            add("warning", f"{job.timer} 没有安装", install_command(p, job))
            continue
        row["schedule"] = schedule(read(Path(timer.get("FragmentPath", ""))))
        row["next"] = short_time(timer.get("NextElapseUSecRealtime"))
        enabled = timer.get("UnitFileState") == "enabled" and timer.get("ActiveState") == "active"
        row["state"] = "启用" if enabled else "未启用"
        if not enabled:
            add("warning", f"{job.timer} 没有启用（{timer.get('UnitFileState')}，{timer.get('ActiveState')}）",
                f"systemctl --user enable --now {job.timer}")
        for tpl in job.templates:
            name, installed = Path(tpl).name, units.get(Path(tpl).name, {})
            if installed.get("LoadState") != "loaded":
                add("warning", f"{name} 没有安装", install_command(p, job))
                continue
            want = unit_lines(read(p.repo / tpl), p.repo)
            have = unit_lines(read(Path(installed.get("FragmentPath", ""))), p.repo)
            if want != have:
                add("warning", f"装的 {name} 和仓库模板 {tpl} 不一致：{first_difference(want, have)}", install_command(p, job))
        row["last"] = last_run(service, timer, job, add)
    return rows


def last_run(service: dict, timer: dict, job: Job, add) -> str:
    if service.get("ActiveState") in ("activating", "deactivating", "reloading"):
        return "运行中"
    start = service.get("ExecMainStartTimestamp")
    if not start:
        if trigger := short_time(timer.get("LastTriggerUSec")):
            return f"{trigger}（重启前触发，结果不详）"  # systemd forgets a service's last run across reboots
        add("info", f"{job.service} 还没运行过")
        return "还没运行过"
    result = service.get("Result", "")
    took = minutes(service)
    took = "" if took is None else f"，{took:.1f} 分钟" if took >= 1 else f"，{took * 60:.0f} 秒" if took * 60 >= 1 else "，不到 1 秒"
    text = f"{short_time(start)} " + ("成功" if result == "success" else f"失败（{result}）") + took
    if result != "success":
        add("warning", f"{job.service} 上次运行失败（{result}，{short_time(start)}）",
            f"journalctl --user -u {job.service} -n 100；重跑：systemctl --user start {job.service}")
    return text


def rows(states: list[dict]) -> list[list[str]]:
    out = [["定时任务", "项目", "时间", "状态", "上次运行", "下次运行", "收件人"]]
    for s in states:
        for j in s.get("jobs", []):
            out.append([j["timer"].removesuffix(".timer"), j["project"], j.get("schedule", ""), j.get("state", ""),
                        j.get("last", ""), j.get("next", ""), ", ".join(j["recipients"]) or "—"])
    return out if len(out) > 1 else []
