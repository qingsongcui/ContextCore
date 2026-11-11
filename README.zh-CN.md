# ContextCore · Senrelay / 续境

macOS 原生桌面 App、浏览器工作台与本机常驻服务，让多个 AI 编程助手围绕同一项目沉淀带来源记忆、一句话跨助手接力对话，并在一处统管模型服务商、MCP、Skills 与使用规则。

[English](README.md) · [中文](README.zh-CN.md)

## 产品与核心范围

主产品位于 `app/`。[公开产品规范](docs/SPECIFICATION.md) 汇总核心产品边界与设计约束。

- **原生桌面 App + 浏览器工作台 + 独立 macOS 后台**：采用原生 AppKit / WKWebView 桌面壳与 React/Vite 工作台，后端由 FastAPI + SQLite 在本机环回地址常驻运行；关闭窗口或网页不会停止后台采集服务。
- **六大主力上下文宿主适配**：Codex（Desktop 与 CLI）、Oh My Pi（OMP）、Cursor、Antigravity、Claude Code、OpenCode。按宿主与具体界面分别核验能力（Codex Desktop ≠ Codex CLI，Cursor IDE ≠ Cursor CLI，Antigravity Desktop ≠ agy，OMP ≠ Pi / OpenCode），详见 [docs/support-matrix.md](docs/support-matrix.md)。配置文件写入成功不等于运行中的宿主进程已加载，本地连通性测速也不等同于真实模型推理调用。
- **对话内跨 Agent 无缝接力**：切换到任意新 Agent 输入 `/contextcore`（或说“接着做” / “接力” / “continue” / “relay”），自动盘点并列出近期跨宿主对话候选（含序号、来源 Agent、所属项目、标题与摘要）。**仅回复候选序号**时加载接力上下文（`must_not_execute=true`）、先汇报前序进展摘要并等待下一句指令；**选择序号的同时附带具体任务指令**时，结合上下文直接开始续做。生命周期 Hook 保持静默上报，绝不向对话强行注入未请求的上下文。
- **伴生隐式自动记忆与版本治理**：日常对话中由当前活跃 Agent 通过伴生 MCP 工具与 Skills 实时捕获带原话引用的结构化记忆（附简短 `💡` 回执），普通记忆自动生效；明确改口自动归档旧决定，模糊分歧进入「待确认」人工裁决；支持 📌 核心规则置顶、`AGENTS.md` 规则手册预览及聊天/记忆独立永久删除。
- **多宿主配置一处统管**：常驻顶栏秒切宿主，支持「一键接入续境」、服务商预设填入与端点测速、模型与账号绑定、第三方 MCP、Skills、系统提示词、项目配置组合、多模路由与本机已有配置导入导出。配置切换自动留存原件快照并支持随时回滚，检测到外部手动修改冲突时自动弹出差异（Diff）核对。
- **本机词法检索与可选增强检索**：工作台顶栏提供跨项目全文词法检索，默认毫秒级本地响应；设置页「高级」折叠区内提供可选本地向量语义检索与带来源关系图谱（默认关闭，搜索绝不自动联网下载模型或安装依赖）。
- **数据备份与范围边界**：支持通过设置页按钮或 `contextcore backup` 创建 SQLite 在线一致性快照（`0600` 权限保存在 `<data_dir>/backups/`）；自动云同步、Skill 生成工坊与主动打扰式洞察不在产品范围内。

## 桌面 App 与当前分发状态

原生 macOS App 内置独立 Python 运行环境、后端服务与前端工作台资源，构建与校验流程见 [桌面指南](app/macos/README.md)。当前公开候选包面向 **macOS 14.0+ Apple Silicon（`arm64`）**，采用 ad-hoc 签名（`Senrelay-macOS.zip` 仅含 `Senrelay.app` 本体）。首次启动只需两步：

1. 将 `Senrelay.app` 移入 `/Applications`；
2. 在终端执行 `xattr -cr /Applications/Senrelay.app`（或在「系统设置 → 隐私与安全性 → 仍要打开」中放行；源码检出目录亦提供 `app/macos/install-macos.sh`）。

Developer ID 签名与 Apple 公证、全新 Mac 物理设备验收及跨版本升级验收仍作为公开前置条件保留说明，详见 [发布就绪清单](docs/release-readiness.md)。

## 日常使用流程

### 导航与布局结构

- **左侧主导航（精简三项 + 条件分歧项）**：
  - **项目工作台**（`projects`）：顶栏全局检索，以及单个项目的 `概览`、`聊天`、`记忆`（含任务进度与 📌 置顶规则）、`资料` 四个工作区标签。
  - **Agent 管理**（`agents`）：常驻顶栏切换各 AI 助手，一站式管理一键接入、模型服务商、MCP、Skills、使用规则与路由模式。
  - **用量与性能**（`usage`）：展示已记录的请求次数、Token 消耗、耗时与费用估算；未观察到的指标诚实标为未知，绝不伪造为 `0`。
  - **待确认**（`conflicts`，按需出现）：仅当存在记忆分歧时在主导航显示数量徽标，平时隐藏不打扰。
- **侧栏底部控制区**：展示本机服务实时连接状态，提供 **设置**（`settings`，内含外观、语言、开机自启、本地数据库备份、可选增强检索设置及内嵌 **变更记录** 查看）、中英双语切换（`中 / EN`）与明暗主题切换。

### 1. 创建与组织项目

项目代表具体的工作主题。新建项目时可以关联一个或多个本地代码仓库目录，也可以**完全不绑定目录**（纯主题项目）；同时支持项目重命名、合并、拆分与可检索归档，或从历史导入会话中自动还原项目。

### 2. 沉淀记忆、资料与来源追溯

- **伴生自动沉淀 + 手工维护**：对话中由伴生组件自动提取带原文引用的记忆并给出 `💡` 简短回执；工作台内可直接新建笔记或导入文档（支持 `MD`、`TXT`、`JSON`、`PDF`、`DOCX`、`DOC`）。
- **六大分类与分层注入**：涵盖 **使用习惯、项目背景、决定与约束、任务与进度、踩坑经验、参考资料** 六类记忆，支持将核心约束一键 📌 置顶、去重整理以及预览导出 `AGENTS.md` 规则手册。
- **独立永久删除**：每条记忆均可溯源至原始对话轮次或关联文档并支持历史版本回滚；删除原始对话绝不级联删除已沉淀记忆，删除记忆也绝不影响原始对话记录。

### 3. 跨 Agent 对话接力

当某个 Agent 额度耗尽、上下文窗口写满，或需要换用更擅长当前任务的助手时，无需在工作台手动填表，直接在目标 Agent 对话框内完成接力：

1. 在新打开的目标 Agent（支持 Claude Code、Codex、Cursor、Antigravity、OMP、OpenCode）中输入 `/contextcore`，或直接说“接着做” / “接力” / “continue” / “relay”。
2. 系统自动同步并列出近期跨宿主对话候选清单（包含序号、来源 Agent、所属项目、会话标题与关键摘要）。
3. 按需选择续做节奏：
   - **只回复序号**（如 `1`）：目标 Agent 加载接力上下文（`must_not_execute=true`），先简要汇报上一段会话的目标、已完成进度、关键约束与下一步建议，停下等待你的下一句指令，绝不擅自改代码；
   - **回复序号并附带具体任务**（如 `选 1，把剩余的接口边界测试补齐`）：目标 Agent 加载上下文后结合你的明确指令直接继续执行。

### 4. Agent 管理

- **常驻顶栏宿主切换与「一键接入续境」**：在 `Agent 管理` 顶栏一键切换主力宿主与扩展工具；点击「一键接入续境」即可自动配齐历史会话路径授权、MCP 服务、`/contextcore` 斜杠命令技能与静默生命周期 Hook。
- **一键切换与安全快照保护**：内置常用供应商只读预设模板与端点延迟测速；API Key 独立存入 macOS 系统钥匙串。启用服务商、切换 MCP / Skills / 提示词默认自动留存原件快照并校验回读，若检测到宿主配置文件被外部手动修改则自动弹出 Diff 差异确认框防止误覆盖，且支持随时一键回滚。
- **多宿主同步与多模路由**：支持勾选多个目标宿主批量同步共享 MCP、Skills 与提示词（冲突宿主逐项弹出差异核对）；支持为单个助手切换官方直连、本机代理转发（含优先级故障回退链）或模型聚合模式。
- **技能安装边界**：默认采用独立文件副本（`copy`），高级选项支持受管单文件版本软链（`symlink`）；安装技能绝不自动执行其内部脚本，通用配置目录软链保持安全拦截。
- **权限独立确认**：读取聊天历史记录、修改宿主配置文件、下载可选本地语义模型三类权限各自独立确认，绝不捆绑授权。

## 本机开发快速开始

推荐 **Python 3.12**（最低 3.11，需 SQLite `blobopen` 支持）与 **Node 24 LTS**（`>=24.15.0`；支持引擎 `^22.22.2 || ^24.15.0 || >=26`）。**以下命令全部从仓库根目录执行**，使用独立数据目录调试，不会读取个人日常历史或改动正式服务：

```bash
python3.12 -m venv app/backend/.venv
app/backend/.venv/bin/python -m pip install -e 'app/backend[test]'
app/backend/.venv/bin/contextcore serve --host 127.0.0.1 --port 18420 --data-dir "$HOME/.contextcore/dev-data" --no-static
```

另开终端，从仓库根目录启动隔离前端开发服（端口 `15173` 自动代理至 `18420`）：

```bash
npm --prefix app/frontend ci --ignore-scripts
npm --prefix app/frontend run dev:isolated
```

### 构建并托管完整工作台

先在开发后台终端按 `Ctrl+C` 停止使用同一数据目录的进程，再执行：

```bash
npm --prefix app/frontend run build
app/backend/.venv/bin/contextcore serve --host 127.0.0.1 --port 18420 --data-dir "$HOME/.contextcore/dev-data" --frontend-dir app/frontend/dist-staging
```

在浏览器打开 `http://127.0.0.1:18420`。源码默认静态目录发现固定指向 `app/frontend/dist-staging`（不会回退旧 `dist`）；`--no-static` 仅暴露 API 与 MCP。发布包准备与停机激活流程详见 [应用运维指南](app/README.md)。

## 服务控制与配置

```bash
app/backend/.venv/bin/contextcore status --json --data-dir "$HOME/.contextcore/dev-data"
app/backend/.venv/bin/contextcore backup --data-dir "$HOME/.contextcore/dev-data"
app/backend/.venv/bin/contextcore stop --data-dir "$HOME/.contextcore/dev-data"
app/backend/.venv/bin/contextcore generate-plist --data-dir "$HOME/.contextcore/dev-data"
```

`./app/scripts/contextcore-service` 支持同等 CLI 子命令及 `prepare-release` / `activate-release`。`generate-plist` 仅输出模板，`install` / `uninstall` 才会改动 macOS LaunchAgent。后端**不会自动加载 `.env`**（详见 [.env.example](.env.example)）；浏览器会话使用进程内存中的高熵随机 Session ID，无需配置额外签名密钥。

## 验证门禁与已知边界

当前自动化发布门禁覆盖：

- **后端测试（`pytest`）**：1,484 项通过；
- **前端工作台（`typecheck` / `lint` / `vitest` / `check:i18n`）**：188 项组件与回归测试通过，1,334 条中英双语文案 100% 对齐且零未包裹中文；
- **官网构建与交互校验**：预览与部署双模式静态校验、112 条首页双语字典 + 双语指南页、交互测试与球体动效测试全部通过；
- **打包、原生桌面与安全扫描**：独立 wheel 11 项内置资源完整性校验（`check_release_readiness.py`）、原生 `Senrelay.app`（`arm64`，`minos <= 14.0`）包体与双语 WKWebView 渲染校验、六宿主隔离沙箱 Hook 校验，以及全仓与 Git 历史安全扫描（`verify_repository_security.py`）。

详细实现映射、历史参考证据与公开边界（如私有 GitHub 仓库鉴权、外部账号真实计费/OAuth 资质、全新 Mac 公证与跨版本升级验收）见 [发布就绪清单](docs/release-readiness.md) 与 [公开证据摘要](docs/evidence.md)。

## 安全

服务严格仅绑定环回地址（`127.0.0.1`），请勿通过局域网、公网端口、反向代理或隧道暴露。网页写操作强制校验会话绑定 CSRF 与同源策略；Agent MCP/Hook 与本机模型路由使用各自独立的授权凭据。原始对话记录按设计原样保留，审计日志与配置导出默认剔除托管密钥；应用层永久删除不等同于底层法证磁盘擦除。详见 [安全策略](SECURITY.md)。

## 许可证

[MIT](LICENSE)
