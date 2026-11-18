# MindSlice-manage：找出仓库里要管理的版本号

MindSlice-manage 集中管理几个相互依赖的仓库的版本号：项目自己的版本号常写在好几个文件里，下游对上游的要求有最低版本、精确锁定、lockfile 几种写法，改一处就要记得改其他几处。你要读一个仓库，写出它的 spec：版本号写在哪里、依赖哪些受管项目、怎么发布、改完要跑哪些命令。之后程序只按这份 spec 读写文件，不再问你，所以要准确、完整。

## 这次运行

- 项目名：`{{name}}`
- 仓库快照：当前目录下的 `repo/`（{{files}} 个文件）。快照里只有被 git 跟踪的文件，没有 `.git`，被 ignore 的本地文件也不在里面。
- 输入：当前目录下的 `input.json`。`projects` 列出全部受管项目：`name` 是项目名，`package` 是 Python 包名（pyproject 里的 name，可能为空；不代表发布到了 PyPI），`remote` 是 git 地址。依赖只登记对这些项目的依赖。
- 输出：把 spec 写到当前目录下的 `proposal.toml`。这是你唯一要写的文件。

只读：不要修改 `repo/` 里的任何文件，不要联网，不要运行仓库里的代码、构建或测试。读文件、搜索都可以。

写完后程序会对照真实仓库核验每一条：文件要存在，正则要按 `count` 恰好匹配那么多次，捕获的值要是合法的版本号，命令要找得到。核验不过，会把错误交回给你修改。它还会在仓库里搜索本项目的版本号，没有被任何位置覆盖、也不在 `ignore` 里的出现都会交回给你。

## spec 的格式

TOML。下面是全部字段；没有用到的节和字段可以省略，不要加别的字段。

```toml
schema = 1
name = "{{name}}"          # 必须是这个值
ignore = []                # 搜索"未登记的版本号"时跳过的文件（glob，相对仓库根；* 可跨目录）

[version]                  # 本项目自己的版本号：所有位置的值必须相同
locations = [ { file = "pyproject.toml", regex = '(?m)^version = "([^"]+)"' } ]
follows = ""               # 版本号形如 "<某个 pin 依赖的版本>-<n>" 时，填那个依赖的项目名

[release]                  # 不发布的项目省略这一节
pypi = ""                  # PyPI 包名
tag = ""                   # git tag 的格式，如 "v{version}"
plugins = []               # 作为宿主插件分发时：{ host = "...", id = "...", hint = "..." }

[[deps]]                   # 对受管项目的依赖，一个项目的一种写法一节
project = ""               # input.json 里的 name
kind = ""                  # min | pin | lock
locations = []

[installs]
venv = ""                  # 项目在仓库里的 venv 中运行时（如 uv run），填 ".venv"

[commands]                 # 在仓库根目录执行；每条是 argv 列表，或 { cmd = [...], env = { ... } }
build = []                 # 版本号改动后重新生成生成物
verify = []                # 检查生成物是否最新
test = []                  # 测试

notes = []                 # 拿不准、需要人看的地方，一句一条
```

**位置**的写法：`{ file, regex, count = 1, format = "version", value = "version", update = [] }`

- `file`：相对仓库根的路径。
- `regex`：Python `re` 语法，恰好一个捕获组，只捕获值本身（不含引号）。`^`、`$` 要加 `(?m)`；带上足够的上下文，让它只匹配想要的地方。用 TOML 单引号字符串写，免得转义。
- `count`：这个正则在文件里匹配几次（默认 1）。同一个值在一个文件里写了几处，就用一个位置加 `count`，不要写几个一样的位置。
- `format`：`version`（`0.2.3`、`0.2.3-1` 这种）或 `tuple`（代码里的 `(0, 2, 3)`，捕获括号里面的 `0, 2, 3`）。
- `value`：只在 pin 里用，说明这个位置存的是什么：`version`、`commit`（上游这个版本的 tag 指向的 40 位提交）、`wheel_url`、`wheel_sha256`（上游这个版本在 PyPI 上的 wheel）。
- `update`：生成文件（如 `uv.lock`）不直接改写，填刷新它的命令（argv），程序改完别处后执行它，再读回核对。

## 要找什么

1. **自己的版本号**：每个作为来源写着本项目版本号的地方，例如 `pyproject.toml` 的 `version`、`__version__`、`package.json` / `plugin.json` 的 `version`。
   - 有单一来源就只登记来源：`pyproject.toml` 用 `dynamic = ["version"]` 从 `__version__` 读时，只登记 `__version__`。
   - `uv.lock` 里本项目自己的条目也写着版本号：登记成位置，`update = ["uv", "lock"]`。
   - 构建生成的目录里的副本不登记：把目录加进 `ignore`，并在 `commands.build` / `commands.verify` 里给出生成和检查它的命令。
2. **对受管项目的依赖**，三种：
   - `min`：下限。`pyproject.toml` 里的 `pkg>=X`，或代码里检查最低版本的常量（如 `MINIMUM = (0, 1, 0)`，用 `format = "tuple"`）。
   - `pin`：精确锁定。如锁定文件里的版本、commit、wheel 地址和 sha256（各写一个位置，用 `value` 区分），以及文档里跟着锁定版本走的 `pkg==X`。改锁定时这些都要一起改。
   - `lock`：lockfile 解析出的版本，如 `uv.lock` 里 `name = "pkg"` 下一行的 `version`。要有 `update`，如 `["uv", "lock", "--upgrade-package", "pkg"]`。这时整个 lockfile 也加进 `ignore`（里面全是别的包的版本号）。
3. **发布**：PyPI 包名（看 publish 工作流、README 里的 `pip install` / `pipx install`）；tag 格式（看工作流、README、CHANGELOG；由 GitHub release 触发发布时通常是 `v{version}`）。作为宿主插件分发时，每个宿主一条 `plugins`：
   - `claude`：`id` 为 `<插件名>@<marketplace 名>`，见 `.claude-plugin/marketplace.json`；
   - `codex`：`id` 为 `<插件名>@<marketplace 名>`，见 `.agents/plugins/marketplace.json`；
   - `agy`：`id` 为插件名（`plugin.json` 的 `name`）；
   - `deepseek-harness`：`id` 为包名。
   - `hint`：README 里更新已安装插件的命令（只给人看，不会被执行）；没有就留空。
4. **本机运行方式**：项目用仓库里的 venv 运行（systemd 服务或脚本里用 `uv run`，README 让人 `uv sync`）时，`installs.venv = ".venv"`。
5. **命令**：从 CI 工作流和 README 里找。测试按 CI 矩阵展开，每个组合一条，矩阵变量放进 `env`；去掉只在 CI 里需要的准备步骤；用本机能直接跑的写法（uv 项目用 `uv run ...`）。

`ignore` 还应该包括：CHANGELOG、历史记录类文档、测试数据里与本项目无关的版本号。只是别的包碰巧同号的地方，也加进 `ignore`。

不要编造：看不出来的就不填，拿不准的写进 `notes`。

## 例子（虚构的仓库）

```toml
schema = 1
name = "example-cli"
ignore = ["uv.lock", "CHANGELOG.md"]

[version]
locations = [
  { file = "pyproject.toml", regex = '(?m)^version = "([^"]+)"' },
  { file = "example_cli/__init__.py", regex = '__version__ = "([^"]+)"' },
  { file = "uv.lock", regex = 'name = "example-cli"\nversion = "([^"]+)"', update = ["uv", "lock"] },
]

[release]
pypi = "example-cli"
tag = "v{version}"

[[deps]]
project = "example-core"
kind = "min"
locations = [ { file = "pyproject.toml", regex = '"example-core>=([^"]+)"' } ]

[[deps]]
project = "example-core"
kind = "lock"
locations = [ { file = "uv.lock", regex = 'name = "example-core"\nversion = "([^"]+)"', update = ["uv", "lock", "--upgrade-package", "example-core"] } ]

[commands]
test = [ ["uv", "run", "python", "-m", "unittest", "discover", "-s", "tests", "-t", "."] ]
```

一个锁定上游 wheel 的仓库，其中 pin 的位置是这样的：

```toml
[[deps]]
project = "example-core"
kind = "pin"
locations = [
  { file = "build/upstream.lock", regex = '"version": "([^"]+)"' },
  { file = "build/upstream.lock", regex = '"commit": "([0-9a-f]{40})"', value = "commit" },
  { file = "build/upstream.lock", regex = '"url": "([^"]+)"', value = "wheel_url" },
  { file = "build/upstream.lock", regex = '"sha256": "([0-9a-f]{64})"', value = "wheel_sha256" },
  { file = "docs/install.md", regex = 'example-core==([\d.]+)', count = 2 },
]
```

## 完成

写好 `proposal.toml` 后，用一两句话说明你登记了什么、哪些地方拿不准。
