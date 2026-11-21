# MindSlice-manage

[English](README.md) | 中文

集中查看和对齐一组相互依赖的仓库的版本号：每个项目自己的版本号写在哪几处、下游对上游的最低版本 / 精确锁定 / lockfile、本机装的 pipx、venv 和宿主插件。

只做本地的事：看板、检查、改文件、跑 build 和测试。commit、push、打 tag、发版、装插件都不做，只打印要执行的命令。

## 安装

Python 3.12+，只用标准库。

```sh
pipx install -e .
cp config.example.toml config.toml   # 填上仓库地址
```

`config.toml` 默认从当前目录找，找不到就用本仓库里的；也可以用 `-c` 或 `MINDSLICE_MANAGE_CONFIG` 指定。

## 用法

```sh
mindslice-manage discover          # agent 读每个还没有 spec 的仓库，写出 managed/<name>.toml，核验后问你是否写入
mindslice-manage status            # 一张表：版本、发布、git、依赖、本机安装
mindslice-manage check             # 列出所有不一致和修法；有错误时退出码为 1（--strict 时警告也算）
mindslice-manage bump <name> patch # 改自己的版本号（所有位置）；也可以给 minor / major / build / 具体版本
mindslice-manage sync [<name>]     # pin 和 lock 跟进上游最新发布版，再跑 build、verify、test
mindslice-manage test [<name>]     # 跑测试
```

`status`、`check` 支持 `--offline`（不访问 PyPI 和 origin，只看本地 tag）、`--fetch`（先 git fetch）和 `--json`。`sync` 支持 `--to <版本>` 和 `--dry-run`。

- discover 让 agent（默认 agy + `gemini-4-neon-high`）读取单独的仓库副本，包含跟踪文件和未忽略的未跟踪普通文件，跳过符号链接。对副本的修改保留在副本中。spec 的每一条都由程序对照真实仓库核验，不过关就把错误交回 agent 修，最多 `harness.repairs` 轮。
- bump 和 sync 在要改的文件有未提交改动时拒绝执行，改完会把每个位置读回来核对。

`config.toml`、`managed/` 和 `runs/` 有本机路径和 agent 生成的内容，都不进 git。

## 定时任务

spec 里登记了定时任务（systemd 用户定时器）的项目，`status` 会多一张表：运行时间、是否启用、上次运行（时间、结果、耗时）、下次运行、收件人。`check` 会报告这些问题：
- timer 没安装或没启用；
- 上次运行失败；
- 装的 unit 和仓库模板不一致（两边的 `@REPO@` 和 `%h` 都展开后比较，忽略空行和注释）；
- linger 没开；
- 发信账号文件不存在或缺键，收件人为空；
- 还没运行过。

manage 不改 systemd，也不改各项目的配置，只给出命令或位置：
- 改运行时间：改仓库模板里的 `OnCalendar`，再执行 check 给出的重装命令；
- 启用或停用：`systemctl --user enable --now <timer>` 或 `disable --now <timer>`；
- 改收件人：`status --json` 里的 `mail.to_where` 指明写在哪个文件的哪个键。

## 测试

```sh
PYTHONPATH=src:tests python3 -m unittest discover -s tests -t tests
```
