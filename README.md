# deepseek-harness-operator-skill

把「用户继续使用主屏、agent 在后台操作 DeepSeek Harness 官方 Web 界面」这套流程，封装成一个可自动发现的 Codex skill。

- 仓库名：`deepseek-harness-operator-skill`
- 作者与维护者：summi
- GitHub 所有者：SummerMi
- 许可证：MIT（见 [LICENSE](LICENSE)）

> 这不是 DeepSeek 官方项目，与 DeepSeek 公司没有隶属或背书关系。它只是把公开的官方安装包与官方 Web 界面组织成一个本地操作流程。

## 角色分工

| 角色 | 职责 |
| --- | --- |
| 用户 | 继续使用主屏；给出目标与约束；决定是否继续某个任务空间；随时可以接管浏览器。 |
| Agent（Codex 等） | 派单、读取轨迹、检查产物、追问纠偏；不代替 Harness 执行主体工作。 |
| DeepSeek Harness | 主要执行端：检索、分析、编写及其他已授权操作。 |

后台宿主进程是独立进程，用独立 profile 隐藏运行、只监听 `127.0.0.1`。它与桌面版 Harness 共用同一套 Harness 数据和用户存储，但**已经在桌面里跑的会话仍归原宿主所有**，本 skill 不会接管它；同一会话也不要跨宿主并发驱动。

## 前置条件

1. 已用官方安装包装好 DeepSeek Harness（默认在 `%LOCALAPPDATA%\Programs\DeepSeek Harness*`）。
2. 已有一个可用的默认 profile，且已经登录 DeepSeek 账号。本工具不会创建、导出或伪造凭据。
3. 已安装 `ego-browser`（开发与实测基准为 v2.1.0），并可用其 `nodejs` 模式运行 `.mjs` 模块。连接脚本跑在 ego-browser 里，不是跑在系统浏览器里。
4. Windows 是启动路径的主要支持平台。`discover`、`status`、日志解析和 `connect-script` 在其他平台也能用，但不在非 Windows 上声称完整启动兼容。

## 安装

把仓库里的 skill 目录放进 Codex 的 skills 目录即可：

```powershell
# 方式一：直接复制
Copy-Item -Recurse skills\deepseek-harness-operator "$env:USERPROFILE\.codex\skills\"

# 方式二：从克隆的仓库复制
git clone https://github.com/SummerMi/deepseek-harness-operator-skill.git
Copy-Item -Recurse deepseek-harness-operator-skill\skills\deepseek-harness-operator "$env:USERPROFILE\.codex\skills\"
```

安装后无需额外配置：skill 默认允许自动发现，也会在显式写 `$deepseek-harness-operator` 时生效。

## 使用

`scripts/harness_operator.py` 只依赖 Python 标准库，四个子命令都输出单行 JSON。

```powershell
cd skills\deepseek-harness-operator

# 1. 找到真实安装
python scripts\harness_operator.py discover

# 2. 启动（或复用一个已验证健康的实例）
python scripts\harness_operator.py start --profile codex-operator

# 3. 检查进程身份与端口可达性
python scripts\harness_operator.py status

# 4. 生成私有连接模块
python scripts\harness_operator.py connect-script

# 5. 通过脱敏入口运行连接模块（必须）
powershell -File scripts\run_ego.ps1 -Script "$env:USERPROFILE\.dsh\operator-private\codex-operator\connect-codex-operator.mjs"
```

**不要**直接把 `ego-browser nodejs -e ...` 当默认范例：ego 自己的诊断输出可能回显带凭据的导航地址。`run_ego.ps1` 会遮蔽 URL 凭据、可疑查询参数和 userinfo；模块内部的 `catch` 脱敏不能替代这一层。

`start` 的行为要点：

- 首次启动时若目标 profile 不存在，才带 `--from-default-profile web` 做一次初始化；已存在配置就复用，不重置。
- 目录存在但没有 `cordis.yml` 时直接停止并说明，不猜测、不覆盖。
- 运行状态与启动日志放在 profile 目录**之外**的私有状态目录（默认 `~/.dsh/operator-private/<profile>`）；`--state-dir` 指到 profile 内部会被拒绝，避免初始化重命名 profile 时出现 `EPERM`。
- 端口默认 `0`（由系统分配），只监听 `127.0.0.1`，始终 `--no-open`，子进程加 `CREATE_NO_WINDOW`，并且 `ELECTRON_RUN_AS_NODE=1` 只传给子进程。
- 启动时用状态目录里的独占锁串行化；已有锁就拒绝，不删别人的锁、不杀别的进程。
- 已有记录且 PID 与进程创建标识都吻合时复用；身份无法确认时拒绝再起第二个。
- `ready: true` 只在“进程仍存活且端口已接受连接”之后返回；URL 出现得晚时，`status`（或再次 `start`）会读取同一记录的私有日志补上 origin/port，而不是新开进程。

退出码：`0` 成功，`1` 运行失败，`2` 拒绝执行（状态不明确或可能造成覆盖/重复）。

### 关于 ASAR

安装包把宿主 JavaScript 打在单个 `resources\app.asar` 里。`harness_operator.py` 会读取 asar 头部的 JSON 索引，核对
`dsh/node_modules/@deepseek-ai/dsh-desktop-host/lib/cli.js` 这个成员确实存在，然后把 asar 虚拟路径交给 Electron；它不会解包整份安装包。`discover` 输出里：

- `cli_kind: "asar"`：成员在归档内，路径是虚拟路径，Windows `Path.is_file` 为 false 属正常现象；
- `cli_kind: "unpacked-sidecar"`：成员标记为 unpacked，使用 `app.asar.unpacked` 下的实体文件；
- `cli_kind: "on-disk"`：解包目录形式的安装。

**真实安装的手动验收**（只读，不启动任何东西）：

```powershell
python scripts\harness_operator.py discover
```

确认三点：输出 `"ok": true`；`cli_kind` 为 `"asar"`；`candidates` 里的安装目录与 `%LOCALAPPDATA%\Programs` 下的实际目录一致。整个过程不写用户配置、不解包归档；若同一台机器上同时存在多个 `DeepSeek Harness*` 目录，取自然排序最后一个作为默认，必要时用 `--executable` / `--cli-script` 显式指定。

### 私有文件

下面这些文件由 `harness_operator.py` 在私有状态目录生成，可能含认证 URL 或本机路径，**不要提交、不要贴到聊天里、不要上传**：

- `operator-state.json`：进程记录（PID、进程创建标识、无凭据 origin、日志路径），读取时会重新校验 profile、版本、字段类型和 loopback origin；
- `startup-*.stdout.log` / `startup-*.stderr.log`：官方启动输出，可能含一次性认证 URL；
- `connect-*.mjs`：生成的连接模块，内嵌绝对状态路径，运行时才从私有日志读取 URL。

`.gitignore` 已默认忽略以上全部内容，也忽略 `tmp/`、缓存与截图。

## 真实适用边界

- 采用官方 Web 表层，用页面事件操作，避免抢占用户的全局键盘和鼠标；但模型请求与工具执行**仍然消耗本机与账号资源**。
- 服务进程存活期间入口可以继续使用；重启后必须重新核实进程、端口和会话，不能把上一次的端口或模型名当作永久配置。
- 浏览器被用户接管时停手；正在进行的任务用同一个 space，不为失败另开 space；已完成 space 再次进入时先列举并重新认领，不假定原来的第一个页面还在。
- 本工具不支持：守护服务、公网远程访问、任务自动重试、任意进程杀死、认证绕过。它也不提供无人值守的永久运行。
- 授权给 Harness 的任务不会扩大用户的批准范围；执行时遵守当前有效授权以及工具自身的要求，不擅自扩大范围。

## 目录结构

```text
.
├── README.md
├── LICENSE
├── .gitignore
├── .github/workflows/ci.yml
├── skills/deepseek-harness-operator/
│   ├── SKILL.md
│   ├── agents/openai.yaml
│   ├── references/workflow.md
│   └── scripts/
│       ├── harness_operator.py
│       └── run_ego.ps1
└── tests/
    ├── test_operator.py
    └── stub_driver.mjs
```

## 测试

纯单元测试，不需要账号、网络或真实的 Harness 安装：

```powershell
python -m unittest discover -s tests -v
```

86 个用例，覆盖：

- 非 loopback / 带 userinfo / 非 http / 缺端口 / 端口越界的 URL 拒绝，ANSI 日志解析，带 fragment 的官方 URL 仅内部保留，stdout 与错误信息脱敏；
- CLI 入口回归：以子进程直接执行 `--help` 与 `discover`（防止模块名遮蔽标准库 `operator` 造成的循环导入）；
- asar 索引解析：现代双层头、旧式 8 字节头、成员缺失、unpacked 边车、解包目录；以及只读、不写入安装目录；
- profile 名与 `--state-dir` 越界拒绝、state 的 profile/版本/字段类型校验、被编辑 state 中的非 loopback 或带凭据 origin 一律拒绝；
- 进程身份（PID + 创建标识）复用/不匹配、旧进程存活但未就绪时不重复启动、启动锁互斥；
- 生成的连接模块在 Node 下对真实 ego API 的 stub 执行：首次连接复用空白 p1、复用当前空间、显式 claim 后 adopt、不接管无关标签页、多命中拒绝、名字被用户占用时拒绝、`--out` 放到其他目录仍可运行。

CI 在 Windows 与 Ubuntu 上运行这些单元测试（含 `setup-node`），不安装技能、不启动真实 Harness。

## 实机验证与兼容范围

2026-10-29 在 Windows 和 DeepSeek Harness rc3 上验证：安装发现能够识别 ASAR 内的 CLI；脚本实际启动隐藏的 loopback Web 宿主；重复启动复用同一进程；生成的连接模块通过真实 ego-browser 进入已认证界面，并完成一轮模型回复。另有实际文件任务验证了后台界面的 read/write 调用与磁盘产物。

连接模块采用现有空间的显式认领，保留了另一宿主的已打开页面。完成后再次认领未管理标签页的分支，另有 Node 行为测试覆盖。单元测试不需要账号或真实服务；实机验证不在公共 CI 内执行，也不包含真实启动日志。

验证范围仍是上述安装版本和浏览器接口。安装包或 ego-browser 升级后，应重新核对启动参数、页面状态和连接行为；不同账号或企业策略可能需要额外配置。Python 需要 3.10 或更新版本。
