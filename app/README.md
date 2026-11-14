# Senrelay 桌面 App、工作台与本机服务

产品要求见 [公开规范](../docs/SPECIFICATION.md)，全范围实现/缺口见 [发布就绪清单](../docs/release-readiness.md)，历史观察见 [证据摘要](../docs/evidence.md)。

## 架构与能力边界

- 原生 macOS App：AppKit/WKWebView 窗口，内含运行环境、后台与前端；[构建、语言与分发状态](macos/README.md)。
- React/Vite 工作台：直观呈现项目工作台、Agent 管理、用量与性能、待确认分歧，以及侧栏底部设置（含数据库备份、增强检索与变更记录）。
- FastAPI/SQLite 本机服务：REST、SSE、Streamable HTTP MCP；macOS 为产品支持平台。关网页不退出后台，显式停止与自启偏好不同。
- 六宿主上下文：Codex 桌面/CLI、OMP、Cursor、Antigravity、Claude Code、OpenCode，按界面和能力独立报告。配置存在不等于真实加载，随包参考验收不等于本机安装已通过。
- D060 管理：供应商/模型/账号、MCP、Skills、提示词、项目组合、路由/用量、迁移导出。支持一键切换/同步并自动留存快照，检测到外部手动修改冲突或主动预览时弹出 Diff 确认并回读；宿主重载/真实请求另记。Skills 管理默认采用独立副本模式（copy），高级模式支持版本化托管软链（symlink，已实现并在后端通过本地测试，并非未决特性）；安装技能绝不执行其脚本。ZIP/自定义注册表支持有边界，更广宿主存在格式/操作差异，不称完整替代。
- D062 首版可选语义检索与来源关系不是后置范围；默认不下载、不装运行环境、不启动关系抽取。词法、证据分页、来源版本与 Context Pack 由现有服务提供。
- 记忆与聊天独立永久删除，无回收站，不承诺法证磁盘擦除。原始资料可保留凭据，审计/导出不得复制秘密。

## 日常使用指南

### 导航结构

- **项目工作台**（`projects`）：顶栏跨项目检索，以及项目内的 `概览`、`聊天`、`记忆`（含任务进度与 📌 置顶规则）、`资料` 四标签。
- **Agent 管理**（`agents`）：常驻顶栏切换各助手，分别管理每位助手的一键接入、模型、账号、MCP、Skills、使用规则与权限。
- **用量与性能**（`usage`）：有记录的请求、Token、耗时与费用估算；未观察到的指标标为未知。
- **待确认**（`conflicts`）：存在记忆分歧时按需显示在主导航并提示待处理数量。

设置（`settings`，内含外观、语言、开机自启、本地数据库备份、可选增强检索与内嵌 **变更记录**）、语言和主题保留在底部的紧凑控制区。

### 1. 创建项目（可选/无目录）

项目是工作的核心组织话题。创建项目时可以输入一个或多个本地工作区目录路径，也可以**完全不填写目录**（以纯话题模式组织记忆与资料）。系统也能从历史导入会话中自动识别并重构出历史项目。

### 2. 沉淀与管理记忆、资料与来源

- 对话中由伴生 MCP 与 Skills 采集带原文引用的结构化记忆（附 `💡` 简短回执），同时支持直接新建笔记或导入常用文档格式（MD、TXT、JSON、PDF、Word）。
- 记忆自动归纳或手动整理为六大分类：**习惯偏好、背景信息、约束规则、待办事项、经验教训、参考资料**。
- 任何沉淀记忆均可追溯其原始会话轮次或关联文档；支持 📌 核心规则置顶、`AGENTS.md` 规则手册预览与历史版本回滚。
- 聊天与记忆独立永久删除：删除原始对话不影响已沉淀的记忆；删除记忆也绝不级联删除原始对话；没有回收站。

### 3. 跨 Agent 对话接力

当需要切换到另一个 Agent 继续开发时，直接在目标 Agent 对话框中输入 `/contextcore`（或说“接着做” / “接力” / “continue” / “relay”），系统自动列出近期跨宿主对话候选：

- **(a) 仅选择候选序号**：列表只给短简介用于认出对话；点中某一场后不调用后台模型写交接卡，而是交出完整对话原文与分页指引，由接棒 Agent 通过 `read_conversation_messages`（或 `/api/conversations/<id>/messages`）连续读完全部消息并在当前上下文压缩全量对话后汇报（`must_not_execute=true`），等待你的下一句指令再改代码。
- **(b) 选择候选时附带具体任务指令**：在 `/contextcore` 接力点选的同时给出明确任务要求，接棒 Agent 结合上下文直接开始续做。

候选同时显示续境内部编号与宿主原始会话编号；可以直接提供反重力的原始 UUID。不同会话不会因为标题或开场白相同而合并；缺少工作目录的会话会明确标为未知项目，而不是从接力候选中消失。列表支持 `limit` / `offset` 分页，并返回总数与下一页位置。

列表按最近聊天时间分页，不为列一页候选读取整库聊天正文。旧记录暂时没有摘要索引时仍展示标题和原始编号，选择后再读取正文。请求刷新只唤醒已运行的后台采集；更新未完成或后台不可用会明确提示，空列表不被解释为宿主历史为空。

仅选择会话时，只刷新该会话已授权的原生来源并读取上下文，不要求先选择项目中的业务任务，也不创建或确认交接卡。来源正在同步、发生变化或无法读取时会明确报告未确认最新，不把库内旧记录冒充刚刚结束的对话。

序号只对应同一接棒助手最近展示且未过期的列表。列表过期、未展示该序号或目标已删除时，返回重新选择提示，不根据当前排序改接另一条聊天；原始会话编号不受列表有效期限制。

### 快捷切换、冲突保护与多宿主同步

`quick-switch`、`quick-toggle-asset` 与 `sync-hosts` 支持 `auto_apply=true` 快捷写入模式并自动留存原件备份快照；若检测到宿主配置文件存在外部手动修改冲突（`conflict_preview`）或用户显式请求差异预览，则返回计划供用户核对 Diff 并确认。没有明确模型选择时返回错误，不从预设或服务商名称猜模型；部分宿主失败时逐项报告，绝不伪装为整批成功。

启动续境、导入本机配置与读取聊天不会顺带升级宿主中的续境技能。安装、更新或移除 MCP、技能、Hook，以及切换模型，仍通过用户主动发起的配置操作完成；这些管理功能不因只读接力而被取消。

### 接力响应与预算

`relay_project` / `POST /api/handoffs/relay` 准备接力上下文；明确选择任务/会话时，不允许用另一任务的旧记录代替。缺少可识别的跨 Agent 来源时返回 `selection_required`，不会生成可执行的虚拟卡片。

`max_tokens` 接受 1–32000 的整数。对外上下文响应按完整 JSON（含重复字段）的 UTF-8 字节上界保守估算，`token_estimate=utf8_byte_upper_bound`；不是供应商实测 Token，宿主已有上下文及协议封装开销仍未知。`insufficient_budget` 不携带源正文或 `resume_from`，不得确认或执行。增加预算或显式缩小来源范围后重新装配；不能把拒绝当作完整上下文。

接力成功时，结构化正文在 `context`，可复制提示在 `context_text`。完整模式读取项目当前会话及活跃记忆快照，超预算整体拒绝；压缩模式明确标记部分覆盖。

会话级读取的长对话按实际序列化大小装配：优先保留最新用户问题和最新答复，必要时明确标记答复尾部节选、较早消息省略量及原始消息位置/时间。原文仍可通过已有聊天消息分页接口读取；受保护的约束无法装入时仍拒绝，不会静默截掉。该响应的 `handoff` 仅含编号和状态，完整协议卡仍由 `read_handoff` 提供；`context.project` 与 `context.six_elements` 使用 JSON Pointer 引用顶层 `project` 与 `card`，客户端应读取这两个顶层规范字段。`coverage` 明确说明消息、过程摘要和工具字段的覆盖范围。

伴生自动记忆遵循统一决定治理：助手建议不自动成为用户决定，同标题差异产生待确认分歧；明确改口需带来源用户引文、旧记录版本及替代关系。普通可信记忆自动积累。

`bootstrap_working_memory` 同样保护完整安全规则与任务临时限制；若放不下，返回 `insufficient_budget` 和 `must_not_execute=true`，不会为了通过预算删去“不要做什么”。任务经验提炼需先关联同项目原始聊天，再提供 `evidence_quote`，该引文必须实际出现在聊天正文且与结论相关。Agent 提炼标为 `source_backed`（有来源支持），不等于独立验证。工作任务列表按状态分页，支持上一页、下一页和指定任务定位，超过 200 条也可继续访问。

### 4. “Agent 管理”统一管理

- **常驻顶栏宿主切换与一键接入**：通过应用控制的 URL（`page=agents`、`agent=<id>`、`section=<tab>`）直达单个助手，支持「一键接入续境」自动配置路径授权、MCP、`/contextcore` 技能与静默 Hook。
- **清晰专属表单**：提供模型与账号（`providers`）、工具（`mcp`）、技能（`skill`）、使用规则（`prompt`）、账号授权（`accounts`）、项目配置组合（`bundles`）、更改记录（`plans`）、连接权限（`connections`）及高级管理（`advanced`）等分类表单。
- **只读预设元数据**：已知服务商预设仅提供配置模板元数据，目录中列出模型不代表账号拥有调用权限。
- **保存共享定义 vs 应用配置**：保存共享定义仅存入本机共享资料库，不会写入宿主配置文件；应用到宿主自动留存原件快照并执行回读校验（外部修改冲突或显式预览时弹出 Diff 确认）；文件写入成功不等于宿主进程已真实加载。
- **切换范围立即失效**：在助手或分类之间切换时，立即清空正在编辑的草稿、未应用的计划与执行回执，杜绝跨宿主配置串扰。
- **瞬态非秘密草稿**：应用生命周期内的编辑草稿支持继续编辑或放弃；草稿仅存于内存中，页面刷新不持久化，且密码、密钥与原始导入数据绝不缓存于草稿。
- **技能管理**：默认采用独立副本模式（`copy`）；托管版本软链（`symlink`）为高级选项，已实现并在后端通过本地测试（并非未决特性）；安装技能绝不授权执行其脚本；通用目录软链保持安全拦截。
- **高级操作**：共享路由与迁移保留在显式高级范围。模型目录刷新为显式手动操作且不触发推理；Gemini 目录与检测为不支持的手动录入，宿主兼容性仍保留差异预览；保留高级 JSON、协议、实体 ID 与回执。
- **独立权限边界**：读取历史与新增聊天、修改宿主配置文件、下载可选语义模型/运行环境各自独立确认，绝不捆绑。

## 统一开发安装

以下命令**都从仓库根目录**执行，而不是从 `app/` 执行。推荐 Python 3.12（最低 3.11，需要 SQLite `blobopen`）与 Node 24 LTS（>=24.15.0；支持引擎 `^22.22.2 || ^24.15.0 || >=26`，因 dev 依赖 `jsdom@30.1.3`；CI 使用 Node 24）。现有正在运行的项目虚拟环境不得在常规验证中重建；需要隔离或全新测试时指向独立虚拟环境。


```bash
python3.12 -m venv app/backend/.venv
app/backend/.venv/bin/python -m pip install -e 'app/backend[test]'
npm --prefix app/frontend ci --ignore-scripts
```

这会安装产品包和 test extra，注册项目内 `contextcore`。仅安装 requirements 不会注册命令。uv 替代命令为 `uv sync --project app/backend --frozen --extra test`，使用同一项目环境。后端不会自动加载 `.env`；CLI 优先用 `--data-dir`，详见 [环境说明](backend/.env.example)。不用配置 Cookie 签名密钥，网页会话是内存中的随机 session ID。

两个终端分别执行：

```bash
app/backend/.venv/bin/contextcore serve --host 127.0.0.1 --port 18420 --data-dir "$HOME/.contextcore/dev-data" --no-static
```

```bash
npm --prefix app/frontend run dev:isolated
```

打开 `http://127.0.0.1:15173`，代理隔离后端 18420。普通 `dev` 在 5173 并代理 8420，不应误连正式服务；Vite 不使用 `VITE_API_URL`。

### 构建静态工作台

先停止同一开发数据目录的旧进程，再执行：

```bash
npm --prefix app/frontend run build
app/backend/.venv/bin/contextcore serve --host 127.0.0.1 --port 18420 --data-dir "$HOME/.contextcore/dev-data" --frontend-dir app/frontend/dist-staging
```

打开 `http://127.0.0.1:18420`。源码的默认静态发现目录为 `app/frontend/dist-staging`；**不自动回退 `dist`**。显式 `--frontend-dir` 缺失/无效会失败；没有可发现的构建时，默认源码服务可能仅有 API，不能把 health 200 当页面已挂载。`--no-static` 明确运行 API/MCP。wheel 不内嵌已构建前端，必须提供静态目录或匹配发布。

CLI 启动时将选定前端固定到 `<data-dir>/frontend-releases/<sha256>`，后续工作区构建不应改变这份运行站点。实际效果须经当前验证，不能仅凭源码说明宣称已经部署。

## 匹配发布与停机激活

以下为发布准备与停机激活命令说明（使用隔离目录示例，均从仓库根目录运行）：

```bash
app/backend/.venv/bin/python app/scripts/prepare_release.py --backend app/backend --frontend app/frontend/dist-staging --data-dir "$HOME/.contextcore/release-test"
```

实际参数只有 `--backend`、`--frontend`、`--data-dir`、`--activate-release`。脚本输出 JSON，含 `release_id`、后端/前端摘要及路径、`api_contract`、文件清单、外部 `launcher_path`/摘要。候选写入 `releases/`、`frontend-releases/` 和 `launchers/`；默认**只准备**，不重启服务、不迁移数据库、不写宿主配置。

用户授权切换且该目录的服务已安全停止后，可准备并激活：

```bash
app/backend/.venv/bin/python app/scripts/prepare_release.py --backend app/backend --frontend app/frontend/dist-staging --data-dir "$HOME/.contextcore/release-test" --activate-release
app/backend/.venv/bin/contextcore serve --port 18420 --data-dir "$HOME/.contextcore/release-test"
```

激活更新 `current-release.json`，有存活进程或不能确认停止时失败；没有强制绕过。它不等于注册/重启 LaunchAgent。仓库辅助命令分别是 `./app/scripts/contextcore-service prepare-release ...` 与 `activate-release ...`，**不是 `upgrade`**。辅助脚本优先选择已激活环境，需固定环境时用上面的明确解释器。

macOS 默认 CLI 数据目录为 `~/Library/Application Support/ContextCore`；以上显式目录不触及它。真实正式切换、停止、LaunchAgent 安装/卸载或恢复材料操作均须另行授权。本机整库 SQLite 在线备份已通过 `contextcore backup` 与 `POST /api/settings/backup` 支持，而自动云备份与一键快照恢复不在范围内（恢复备份需在停止服务后手动替换 SQLite 文件，D040）；配置事务原件回滚与整库备份相互独立。发布信任检查使用外部启动器，`/api/release` 用于比对运行 release/API 合同/前端摘要，不靠相同的包版本号判断一致。

## 服务控制

```bash
app/backend/.venv/bin/contextcore status --json --data-dir "$HOME/.contextcore/dev-data"
app/backend/.venv/bin/contextcore stop --data-dir "$HOME/.contextcore/dev-data"
app/backend/.venv/bin/contextcore generate-plist --data-dir "$HOME/.contextcore/dev-data"
```

`generate-plist` 输出模板，不自动注册。`stop` 默认不升级 SIGKILL；显式 `--force` 仍需确认进程身份，无法证明归属就拒绝信号。`install/uninstall` 改 macOS LaunchAgent，不能混入日常验收。自启默认期望开启不证明已安装，需分别看 `installed/registered/running`、归属及实际运行；运行正常不证明 Mac 重启后正常。

## 数据接入与协议边界

- 历史格式随宿主版本变化；未知格式明确失败。Codex rollout、OMP JSONL、Cursor 桌面 SQLite、Antigravity transcript、Claude Code JSONL、OpenCode SQLite/旧 JSON 的解析存在不等于宿主实际消费。
- 用户主动输入 `/contextcore`（或“接着做”/“接力”）后 Agent 调用 MCP 获取接力上下文：仅选候选序号加载上下文并汇报摘要待命（`must_not_execute=true`），附带具体任务指令直接续做。Hook 仅静默上报事件，不自动向对话灌入上下文。
- 后端协议层保留受控算力治理字段（默认关闭且不自动回退外部模型）；未知用量/成本保持未知，绝不填 0。历史参考记录中 3 笔合成输入的 OMP 验证完成后已关闭持续算力。
- 可选语义准备分依赖计划、独立 venv、固定模型下载/校验、索引/检索；源资源在 `backend/app/services/semantic-runtime-requirements.txt` 和 `semantic-runtime-wheels.json`。未授权不执行安装/下载；源码和 wheel 需要保留这些资源。SQLite 关系表名为 `retrieval_relations`。
- OAuth 只允许供应商已注册的官方 public client；不借用他人客户端/登录缓存。用量在 `/api/usage`、路由在 `/api/routing`，路由默认关闭并使用独立 Bearer，网页 CSRF 不授予推理权限。

## 分页与并发

`/api/content`、`/api/conversations` 默认摘要分页，默认 50/上限 200；正文显式请求并分页。工作台使用有界索引预览和懒取正文，不以截断冒充完整内容。

REST 版本化变更需要当前 `expected_version`；批量内容删除/合并提供完整 `expected_versions`。冲突返回 409 后重读，不能静默覆盖。MCP 永久删除需要 `confirmed=True` 和正整数 `expected_version`。初始创建与不同接口的版本要求按真实 API 契约，不将“所有操作都有 CAS”泛化到未实现接口。

## 验证状态
验证命令统一见 [CONTRIBUTING](../CONTRIBUTING.md)。当前自动化门禁覆盖后端测试（1,484 项）、前端类型检查 / lint / Vitest（188 项）与双语覆盖率（1,334 条文本）、安装包与内置资源完整性（`check_release_readiness.py`）、原生 macOS 应用包与六宿主 Hook 沙箱校验，以及全仓安全扫描。历史参考记录包含 4 家 `live_host` 接棒、2 家 `live_protocol` 与 3 笔合成输入测试记录；随包参考记录不代表所有界面、所有记忆写入、语义质量或更广宿主配置管理完整等价，也不替代用户本机环境、真实外部账号、付费模型调用或全新 Mac 设备的独立验收。

## macOS 桌面 App

2026-10-25 新增桌面交付：原生窗口、内嵌当前工作台与随包后台服务。构建、使用和签名边界见 [macOS 说明](macos/README.md)。复用本机正式数据，不创建演示项目；关闭窗口不停止后台服务。

### Private runtime storage and candidate versions

On service startup, the designated product data directory is migrated to mode
0700; its database and existing WAL/SHM/journal files are limited to mode 0600.
The migration changes permissions, not stored records. Linked data directories,
linked/multiply-linked database files and files not owned by the current user
are rejected. User-selected ancestor directories are not recursively changed.
An explicit standalone database path does not grant ownership of its parent.
These controls protect other OS accounts; they do not hide data from the same
OS user, privileged processes, or copies already made elsewhere.

Release identities use an explicit mapping: frontend/tag `1.0.0-beta.1` /
`v1.0.0-beta.1`, Python/wheel `1.0.0b1`, and native short version `1.0.0` plus
a numeric build number. `BUILD.json` retains all mapped versions. Stable
versions retain `MAJOR.MINOR.PATCH` throughout. Unknown suffixes, mismatched
tags and wrong wheel metadata block packaging. This mapping does not select
or publish a new release by itself.
