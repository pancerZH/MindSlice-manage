"""mindslice-manage: one place to see and align the versions of a group of related repos."""

import argparse
import json
import os
import sys
from pathlib import Path

from . import actions, check, discover, jobs
from .config import ConfigError, load_config, load_projects
from .locations import LocationError

CHECKOUT_CONFIG = Path(__file__).resolve().parents[2] / "config.toml"


def config_path(arg: str | None) -> Path:
    """-c, else $MINDSLICE_MANAGE_CONFIG, else ./config.toml, else the config.toml of this checkout."""
    if arg:
        return Path(arg)
    if env := os.environ.get("MINDSLICE_MANAGE_CONFIG"):
        return Path(env)
    return Path("config.toml") if Path("config.toml").is_file() else CHECKOUT_CONFIG


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="mindslice-manage", description=__doc__)
    ap.add_argument("-c", "--config", help="config.toml 的路径")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p, network=True):
        p.add_argument("--json", action="store_true", help="输出 JSON")
        if network:
            p.add_argument("--offline", action="store_true", help="不访问网络（PyPI、origin 的 tag）")
            p.add_argument("--fetch", action="store_true", help="先 git fetch 每个仓库")

    p = sub.add_parser("status", help="一张表：版本、发布、git、依赖、本机安装")
    p.add_argument("names", nargs="*")
    common(p)
    p = sub.add_parser("check", help="列出所有不一致；有错误时退出码为 1")
    p.add_argument("names", nargs="*")
    p.add_argument("--strict", action="store_true", help="警告也返回 1")
    p.add_argument("--verify", action="store_true", help="另外跑每个项目的 verify 命令")
    common(p)
    p = sub.add_parser("discover", help="让 agent 读仓库、写出 spec，核验后由你确认")
    p.add_argument("names", nargs="*", help="不给就处理所有还没有 spec 的仓库")
    p.add_argument("--yes", action="store_true", help="核验通过就写入，不再询问")
    p = sub.add_parser("bump", help="改自己的版本号（所有位置）")
    p.add_argument("name")
    p.add_argument("version", help="版本号，或 patch / minor / major / build")
    p = sub.add_parser("sync", help="pin 和 lock 跟进上游最新发布版，再跑 build、verify、test")
    p.add_argument("names", nargs="*", help="不给就处理所有落后的项目")
    p.add_argument("--to", help="跟进到这个版本，而不是最新发布版")
    p.add_argument("--dry-run", action="store_true", help="只说会改什么")
    p.add_argument("--offline", action="store_true", help="不访问网络（只看本地 tag；锁定 wheel 时不可用）")
    p = sub.add_parser("test", help="跑测试命令")
    p.add_argument("names", nargs="*", help="不给就跑全部")
    args = ap.parse_args(argv)

    try:
        cfg = load_config(config_path(args.config))
        projects = load_projects(cfg)
    except ConfigError as e:
        print(e, file=sys.stderr)
        return 2
    ctx = check.Context(cfg, projects, online=not getattr(args, "offline", False), fetch=getattr(args, "fetch", False))

    try:
        if args.cmd in ("status", "check"):
            unknown = [n for n in args.names if n not in ctx.by_name]
            if unknown:
                print(f"config.toml 里没有：{', '.join(unknown)}", file=sys.stderr)
                return 2
            states, found = check.run_all(ctx, args.names)
            if args.cmd == "status":
                if args.json:
                    print(json.dumps(states, ensure_ascii=False, indent=2))
                else:
                    print(check.table(check.status_rows(states)))
                    if job_rows := jobs.rows(states):
                        print("\n" + check.table(job_rows))
                    counts = {lv: sum(f.level == lv for f in found) for lv in check.LEVELS}
                    if counts["error"] or counts["warning"]:
                        print(f"\n{counts['error']} 个错误，{counts['warning']} 个警告：mindslice-manage check 查看")
                return 0
            if args.verify:
                for p in ctx.projects:
                    if p.spec and p.repo.is_dir() and (not args.names or p.name in args.names) and p.spec.verify:
                        if not actions.run_all(p.spec.verify, p.repo, f"{p.name} verify"):
                            found.insert(0, check.Finding("error", p.name, "verify 命令失败", "看上面的输出"))
            print(json.dumps(check.findings_json(found), ensure_ascii=False, indent=2) if args.json
                  else check.format_findings(found))
            levels = ("error", "warning") if args.strict else ("error",)
            return 1 if any(f.level in levels for f in found) else 0
        if args.cmd == "discover":
            return 0 if discover.discover(cfg, projects, args.names, args.yes) else 1
        ctx.prefetch()
        if args.cmd == "bump":
            actions.bump(ctx, args.name, args.version)
            return 0
        if args.cmd == "sync":
            return 0 if actions.sync(ctx, args.names, args.to, args.dry_run) else 1
        if args.cmd == "test":
            return 0 if actions.test(ctx, args.names) else 1
    except (actions.ActionError, LocationError) as e:
        print(f"\n{e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
