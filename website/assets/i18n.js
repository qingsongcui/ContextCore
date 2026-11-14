export const metaEn = {
  title: 'Senrelay — macOS Multi-Agent Relay, Memory & MCP Workbench',
  description: 'Free ($0 MIT) macOS Apple Silicon workbench for cross-agent session relay (/contextcore), source-linked shared memory, and multi-host MCP sync.',
  ogTitle: 'Senrelay — macOS Multi-Agent Relay, Memory & MCP Workbench',
  ogDescription: 'Free ($0 MIT, no signup) local macOS workbench: type /contextcore to relay recent conversations across AI agents, keep shared memory, and sync MCP.',
  twitterDescription: 'Free ($0 MIT) macOS workbench for cross-agent conversation relay (/contextcore), source-linked shared memory, and multi-host MCP & Skills management.',
  locale: 'en_US',
  siteDescription: 'Free ($0, MIT) multi-agent context relay, companion auto-memory, and multi-host configuration workbench for macOS.',
  jsonDescription: 'Free ($0, MIT License) macOS 14.0+ Apple Silicon local service and browser workbench supporting cross-agent session discovery and conversation relay, companion source-linked auto-memory capture, and multi-host provider, MCP, Skills, and prompt management.',
};

export const htmlEn = {
  'hero.title': 'Switch agents,<br><span class="serif">keep the thread</span><span class="period">.</span>',
  'hero.description': '<strong>Senrelay</strong> is a local multi-agent context workbench for macOS 14.0+ (Apple Silicon), free and open source under the MIT License ($0, no account required).<br>Type <code>/contextcore</code> or say "Relay" across Claude Code, Codex, Cursor, Antigravity, OMP, and OpenCode to continue recent conversations, capture source-linked memories in chat, and manage providers, MCP, and Skills in one place.',
  'intro.lead': 'Architecture settled, code halfway done.<br>New agent, another briefing from scratch?',
  'intro.body': 'Senrelay brings <strong><a class="inline-link" href="#handoff">full-transcript cross-agent relay</a>, <a class="inline-link" href="#workspace">source-linked companion auto-memory</a>, and <a class="inline-link" href="#assistants">multi-host MCP/Skills management</a></strong> into one local Mac workbench. Companion tools track working task state and save source-linked memories as you work; in a new assistant, type <code>/contextcore</code> or say "Relay" to pick from a short brief list, read the full transcript, and compact in context to continue (see the <a class="inline-link" href="./guide.html#everyday-use">workflows guide</a>).',
  'closing.title': 'Next time,<br>start from <span class="serif">here</span>.',
  jobs: `<p class="jobs-caption">Built for everyday multi-agent workflows</p><ul><li><strong>Cross-Host Coding Relay</strong><span>Finish backend APIs in Claude Code, then switch to Cursor or Codex with exact diff boundaries and test state.</span></li><li><strong>Research &amp; Architecture</strong><span>Carry multi-turn sources, ruled-out options, and constraints into another model to keep reasoning.</span></li><li><strong>Implicit Auto-Memory</strong><span>Capture coding conventions, tone preferences, and lessons learned with source links and a brief receipt during chats.</span></li><li><strong>Unified Configuration Control</strong><span>Switch and sync providers, MCP, Skills, and system prompts in one click with automatic rollback snapshots and external-edit conflict diff prompts.</span></li></ul>`,
  retrieval: `<div><span class="feature-number">04 — FIND WITH SOURCES</span><h3>Ask in your own words.<br>Check the original.</h3></div><div><p>Use a question or keyword to search chats, memories, and documents in the current project or across all projects. Open any match to inspect its exact source and original transcript before handing it to an assistant.</p><details><summary>Optional enhancement: local semantic search &amp; knowledge graph <span aria-hidden="true">+</span></summary><p>Local vector search finds conceptually related discussions, while source-backed entity graphs connect clues across sessions. Both run locally on your Mac and can be enabled on demand while keeping keyword search instant.</p></details></div>`,
  assistants: `<div class="section-heading"><h2 id="assistants-title">Your agents.<br><span class="serif">Unified here.</span></h2><p>Manage providers, models, tools, and prompts in one place. Switch Claude Code, Codex, Cursor, Antigravity, OMP, OpenCode, and extended hosts from the top bar (see the <a class="inline-link" href="#connections">support scope &amp; boundaries</a>), connect or sync configs in one click with automatic rollback snapshots and conflict diff checks, following <a class="inline-link" href="./guide.html#security-boundaries">local data &amp; Keychain isolation</a>.</p></div>
<ol class="assistant-path"><li><span>01 / SWITCH HOST</span>Select target host in top bar</li><li><span>02 / PRESET &amp; SPEEDTEST</span>Apply presets &amp; test latency</li><li><span>03 / SYNC RESOURCES</span>Sync MCP, Skills &amp; instructions</li><li><span>04 / GUARD &amp; APPLY</span>Conflict check &amp; safe rollback</li></ol>
<div class="agent-workflow-preview" role="group" aria-label="Illustrated provider switching workflow">
  <div class="agent-preview-heading">
    <div class="preview-host-pills"><strong>Multi-Host Console</strong><span class="host-pill active">Claude Code</span><span class="host-pill">Codex</span><span class="host-pill">Cursor</span><span class="host-pill">Antigravity</span><span class="host-pill">OMP</span><span class="host-pill">OpenCode</span></div>
    <span class="preview-sync-badge">Local Config Sync · Diff Preview</span>
  </div>
  <div class="agent-preview-grid">
    <div><span>One-Click Switch &amp; Presets</span><strong>Official / OpenRouter / DeepSeek</strong><small>Built-in provider presets · 142ms illustrative latency · Keychain isolation</small></div>
    <div><span>Cross-Host Resource Sync</span><strong>MCP Tools · Skills · Global Prompts</strong><small>Configure once and sync to selected Agent hosts in one click</small></div>
    <div><span>Conflict Guard &amp; Recovery</span><strong>Write Verification + Env Alerts</strong><small>Detects shell environment overrides with snapshot diff and rollback</small></div>
  </div>
</div>
<div class="assistant-features">
<article><h3>Provider Presets &amp; Local Config Sync</h3><p>Switch target hosts from the top bar, fill built-in presets in one click, run live endpoint latency tests, and preview-import existing local configurations on demand.</p></article>
<article><h3>Multi-Mode Routing &amp; Keychain</h3><p>API keys stay isolated in macOS Keychain. Switch each assistant between Direct, Local Proxy Routing, or Model Aggregation with ordered automatic fallback chains.</p></article>
<article><h3>MCP Tools &amp; Companion Skills</h3><p>Configure stdio or HTTP tool servers and import Skills from local folders, ZIPs, or GitHub. Connects hosts to local <code>http://127.0.0.1:8420/mcp/</code> under the server key <code>contextcore</code> alongside the companion <code>/contextcore</code> relay skill (never run unrelated PyPI <code>contextcore</code>).</p></article>
<article><h3>System Prompts &amp; Project Bundles</h3><p>Maintain coding conventions and global rules in one place, or bundle project-specific providers, models, MCP tools, Skills, and prompts to preview diffs and switch in one click.</p></article>
<article><h3>Request Traces, Usage &amp; Latency</h3><p>Track request counts, token consumption, first-token latency, and cost estimates in real time, clearly separating primary calls from retry chains across long-term trends.</p></article>
<article><h3>External Edit Guard &amp; Safe Snapshots</h3><p>Clearly distinguishes applied state, pending updates, and external manual edits. Every write saves a recovery snapshot with diff inspection, conflict alerts, and instant rollback—and startup or config import never silently overwrites installed host Skills.</p></article>
</div><p class="assistant-boundary">Each host uses its own native configuration format and capability scope; the workbench provides live write verification, latency checks, and environment conflict alerts to keep every change predictable.</p><a class="text-link" href="#connections">See assistant support <span aria-hidden="true">↘</span></a>`,
  workflow: `<div class="section-heading"><h2 id="workflow-title">Ready in four steps.<br>Start relaying.</h2><p>Launch the local service and workbench to unify multi-host configurations and companion components; capture source-linked project memories during daily chats and relay across agents with a single command.</p></div><ol class="workflow"><li><span>01</span><h3>Start Local Workbench</h3><p>Open the desktop app or follow the <a class="inline-link" href="./guide.html#download-requirements">local setup guide</a> to start the local service, and optionally preview and sync existing local configurations.</p></li><li><span>02</span><h3>One-Click Connect &amp; Sync</h3><p>Switch hosts in Agent Management, click One-Click Connect to wire history paths, MCP, and /contextcore Skills, and enable providers with live latency checks.</p></li><li><span>03</span><h3>Capture Auto-Memories</h3><p>Work normally in your favorite agent while companion MCP tools and Skills save source-linked structured memories with a concise receipt.</p></li><li><span>04</span><h3>Type "Relay" in New Agent</h3><p>In a new agent, type /contextcore or say "Relay", pick a session from the short-brief list, then page through and compact the full transcript in context to stand by or continue.</p></li></ol>`,
  hostMatrix: `<table class="host-matrix-table"><caption>Host Support &amp; Local Runtime Boundaries</caption><thead><tr><th scope="col">Tier</th><th scope="col">Covered Hosts / Runtime</th><th scope="col">Actual Capabilities</th></tr></thead><tbody><tr><th scope="row">6 Core Context Hosts</th><td>Claude Code, Codex, Cursor, Antigravity, OMP, OpenCode</td><td>Native session capture with worktree disambiguation, working tasks and versioned auto-memory, <code>/contextcore</code> short-brief selection and paged full-transcript compaction (number-only sets <code>must_not_execute=true</code>), plus <code>AGENTS.md</code> preview and config sync (<code>~/.claude.json</code>, <code>~/.codex/config.toml</code>).</td></tr><tr><th scope="row">9 Extended Config Hosts</th><td>Claude Desktop, Gemini CLI, Grok Build, Kimi Code, MiniMax Code, Pi, Hermes, OpenClaw, agy</td><td>Configuration diff preview, writes, and rollback snapshots only (JSON / JSON5 / TOML / YAML); no session capture or conversation relay.</td></tr><tr><th scope="row">Local Architecture</th><td><code>macOS 14.0+</code> · <code>Apple Silicon</code> (<code>arm64</code>)</td><td>Loopback only on <code>127.0.0.1:8420</code> (dev <code>18420</code>) with MCP server key <code>contextcore</code> (<code>/mcp/</code>); local <code>SQLite</code> + <code>FTS5</code> (<code>0600</code>) with <code>contextcore backup</code> online backups and API keys in macOS Keychain.</td></tr></tbody></table><table class="host-matrix-table fact-sheet-table" id="fact-sheet"><caption>Release, Pricing &amp; Known Limitations (Verified 2026-11-15)</caption><thead><tr><th scope="col">Item</th><th scope="col">Verified Fact</th><th scope="col">Notes &amp; Constraints</th></tr></thead><tbody><tr><th scope="row">Pricing &amp; License</th><td><strong>$0 · MIT Free &amp; Open Source</strong> · No Signup</td><td>Permanently free with no paid tiers or cloud paywalls; model API keys stay with your chosen providers and macOS Keychain.</td></tr><tr><th scope="row">Release &amp; Install</th><td><code>v1.0.0-beta.2</code> · <code>brew install --cask qingsongcui/tap/senrelay</code></td><td>Install via Homebrew Cask, download <code>Senrelay-macOS.zip</code> from Releases, or <a class="inline-link" href="./guide.html#download-requirements">build from source</a>. Do not run unrelated <code>pip install contextcore</code>.</td></tr><tr><th scope="row">Where Not to Use</th><td>macOS 14.0+ arm64 only · Single-Mac · First-launch clear</td><td>Not supported on Windows, Linux, or Intel (<code>x86_64</code>) Macs, and has no multi-device cloud sync; Beta builds are not Apple-notarized and require a one-time <code>xattr -cr /Applications/Senrelay.app</code> (<a class="inline-link" href="./compare.html">5-option comparison</a>).</td></tr></tbody></table>`,
  support: `<summary>Multi-host integration &amp; capability coordination <span aria-hidden="true">+</span></summary><p>The six core hosts—Codex, Claude Code, Cursor, Antigravity, OMP, and OpenCode—support full local session capture, companion implicit auto-memory, and cross-agent conversation relay. Additional extended hosts support previewed configuration file management only.</p><p>Configuration file writes, environment variable conflict checks, endpoint latency tests, and live process reload verification are tracked independently so status stays transparent.</p>`,
  faq: `<details><summary>What is Senrelay (ContextCore), and how does it differ from CLAUDE.md or cloud memory MCPs? <span aria-hidden="true">+</span></summary><p>Senrelay (open-source repository <code>ContextCore</code>, registered in host MCP configs as <code>contextcore</code>) is a free ($0 MIT, no signup) macOS 14.0+ Apple Silicon local workbench. Unlike static <code>CLAUDE.md</code> or <code>AGENTS.md</code> repo instructions or hosted cloud memory MCPs, it keeps everything on your Mac while combining <code>/contextcore</code> full-transcript cross-agent relay, versioned shared memory, and multi-host MCP/Skills sync.</p></details>
<details><summary>How does Cross-Agent Conversation Relay work in practice? <span aria-hidden="true">+</span></summary><p>Type <code>/contextcore</code> or say "Relay" in your new agent to list numbered recent cross-host sessions with short recognizable briefs—zero model calls during discovery. Once you select a conversation, the receiving agent pages through the full transcript via <code>read_conversation_messages</code> (capped per MCP page to protect context budgets) and compacts the entire thread directly in context: replying with just a number locks <code>must_not_execute=true</code> to report first and wait; attaching a task continues immediately with full context.</p></details>
<details><summary>How is implicit auto-memory triggered automatically? <span aria-hidden="true">+</span></summary><p>Companion MCP tools and Skills automatically capture structured shared memories with a concise receipt during everyday chats—no manual note-saving required. Senrelay maintains dual-tier working memory combining working tasks (<code>working_tasks</code>) and version-governed long-term memories: superseding a decision marks the prior record outdated instead of overwriting history, ambiguous changes route to Pending Conflicts, and lesson promotions require source-dialogue provenance alongside rule pinning and document imports.</p></details>
<details><summary>Which local configurations can I sync or import? <span aria-hidden="true">+</span></summary><p>You can switch and sync providers, models, MCP servers, Skills, and prompts across 6 core hosts and 9 extended config hosts, or import existing local config files. Senrelay includes top-bar host switching, built-in provider presets, one-click activation with automatic rollback snapshots, endpoint latency testing, model mapping, and shell environment conflict detection.</p></details>
<details><summary>Will changing one assistant overwrite my other assistants? <span aria-hidden="true">+</span></summary><p>No—changing one assistant never silently overwrites another assistant's configuration. You can switch providers or rules for a single host, or explicitly check multiple hosts to sync shared MCP tools, Skills, and prompts with full diff preview and automatic rollback snapshots.</p></details>
<details><summary>Do my transcripts and API keys stay safe on my Mac? <span aria-hidden="true">+</span></summary><p>Yes—100% of your transcripts, memories, and config snapshots remain on your local Mac and are never uploaded to the cloud. All data lives in your local SQLite database (<code>0600</code> file permissions), and API keys are isolated in macOS Keychain and excluded from config exports by default.</p></details>
<details><summary>How much does Senrelay cost? Do I need an account or subscription? <span aria-hidden="true">+</span></summary><p>Senrelay is 100% free ($0) under the open-source MIT License, with no account registration, no subscription tiers, and no cloud paywall. Every cross-agent relay, auto-memory, and multi-host configuration feature runs locally; you only provide your own model provider API keys in your AI coding assistants.</p></details>
<details><summary>What are Senrelay's real limitations, and when should I NOT use it? <span aria-hidden="true">+</span></summary><p>Do not use Senrelay if you work on Windows, Linux, or Intel Macs, or if you require multi-device cloud sync and hosted team collaboration. Current release <code>v1.0.0-beta.2</code> runs exclusively on single-machine macOS 14.0+ (Apple Silicon <code>arm64</code>); the 9 extended hosts support configuration file management only; and Public Beta builds require a one-time <code>xattr -cr /Applications/Senrelay.app</code> command before Apple notarization is added. Compare tradeoffs in our <a class="inline-link" href="./compare.html#picker">5-option comparison &amp; stack picker</a>.</p></details>
<details><summary>How do I install and launch the workbench right now? <span aria-hidden="true">+</span></summary><p>Run <code>brew install --cask qingsongcui/tap/senrelay</code> via Homebrew or download the free MIT-licensed <code>Senrelay-macOS.zip</code> from GitHub Releases. You can also follow the <a class="inline-link" href="./guide.html#launch-workbench">local setup guide</a> to run from source on loopback port <code>127.0.0.1:8420</code> / <code>18420</code>. Note: never run <code>pip install contextcore</code>—the PyPI package of that name is an unrelated third-party project.</p><a class="text-link" href="#start">See the start guide <span aria-hidden="true">↘</span></a></details>`,
  start: `<p>Launch your local multi-agent workbench in minutes.<br>Manage multi-host configurations, capture source-linked memories as you chat, and relay conversations across agents anytime.</p><div class="start-links"><a class="button primary" href="./guide.html">Read the local setup guide <span aria-hidden="true">→</span></a><a class="text-link" href="./compare.html">Compare 5 approaches <span aria-hidden="true">→</span></a><a class="text-link" href="https://github.com/qingsongcui/ContextCore" target="_blank" rel="noopener noreferrer">Visit source repository <span aria-hidden="true">↗</span><span class="sr-only">(opens in a new window)</span></a></div><div class="brew-snippet"><span>Homebrew Cask:</span><code>brew install --cask qingsongcui/tap/senrelay</code></div>`,
  trustBox: `<div class="trust-col"><strong>Author &amp; Open-Source Repository</strong><p>Senrelay (open-source repository <a class="inline-link" href="https://github.com/qingsongcui/ContextCore" target="_blank" rel="noopener noreferrer">ContextCore</a>) is created and maintained by independent developer <strong>Qingsong Cui (George O'Nair)</strong> (<a class="inline-link" href="https://github.com/qingsongcui" target="_blank" rel="noopener noreferrer">GitHub @qingsongcui</a> · <a class="inline-link" href="https://x.com/George_ONair" target="_blank" rel="noopener noreferrer">X @George_ONair</a>) for macOS developers switching between AI coding agents.</p></div><div class="trust-col"><strong>Local Privacy &amp; License</strong><p>Strictly binds to local loopback <code>127.0.0.1:8420</code> with zero telemetry, zero uploads, and no signup; SQLite databases (<code>0600</code>) and Keychain credentials stay on your Mac. Provided "AS IS" under the <a class="inline-link" href="https://github.com/qingsongcui/ContextCore/blob/main/LICENSE" target="_blank" rel="noopener noreferrer">MIT License</a>.</p></div>`,
  versionNote: `macOS 14.0+ (Apple Silicon) · <code>v1.0.0-beta.2</code> · <strong>$0 MIT open source</strong> · Updated <time datetime="2026-11-15">2026-11-15</time>`,
};

export const attrEn = {
  '服务商切换流程示意': 'Illustrated provider switching workflow',
  '语言': 'Language',
  'Senrelay 续境首页': 'Senrelay home',
  '主导航': 'Main navigation',
  '展开导航': 'Open navigation',
  '收起导航': 'Close navigation',
  '六个 Agent 围绕共享上下文连接的动效示意，非实时运行状态': 'Diagram of six agents connected through shared context; illustrative, not live status',
  '向下了解 Senrelay': 'Learn more about Senrelay',
  '项目工作台结构示意': 'Diagram of the project workspace',
  '六类记忆示意': 'Diagram of the six memory types',
  '六类文件汇入可检索知识': 'Six file types feeding searchable knowledge',
};

export const textEn = {
  'Agent 管理': 'Agent management',
  '了解 Agent 管理': 'Explore Agent management',
  '多宿主配置管理': 'Multi-host configuration management',
  '隐式自动记忆': 'Implicit auto-memory',
  '跨 Agent 项目接力': 'Cross-agent relay',
  '模型、工具、习惯，各归其位': 'Models, tools, and preferences in their own place',
  'Senrelay · 续境 — 换个 Agent，思路接着走。': 'Senrelay — New agent. Same thread.',
  'Senrelay · 续境 — macOS 多 Agent 接力、记忆与 MCP 工作台': 'Senrelay — macOS Multi-Agent Relay, Memory & MCP Workbench',
  '跳到主要内容': 'Skip to main content',
  '· 续境': '',
  '为什么 Senrelay': 'Why Senrelay',
  '接棒方式': 'Handoff',
  '共享记忆': 'Shared memory',
  '如何开始': 'Get started',
  '方案对比': 'Compare',
  '运行指南': 'Setup Guide',
  '（新窗口）': '(opens in a new window)',
  '工具可以切换。': 'Switch tools.',
  '你的思路，不必重来。': 'Keep your thinking.',
  '了解如何接棒': 'See how relay works',
  'MIT 开源': 'MIT open source',
  '流转示意 · 非实时状态': 'Illustration · not live status',
  '为多 Agent 工作流而生': 'Built for multi-agent work',
  '把上下文，留在项目里': 'Keep context with the project',
  '把时间留给推进，': 'Spend time moving forward,',
  '不是重讲背景。': 'not re-explaining.',
  '看看它如何工作': 'See how it works',
  '服务商与模型切换': 'Provider and model switching',
  '顶栏秒切宿主，一键填入预设服务商、测试端点延迟，一键启用并跨宿主同步 MCP 与 Skills，支持按需导入本机已有配置。': 'Switch hosts from the top bar, fill provider presets in one click, test latency, switch or sync MCP and Skills across hosts, and import existing local configurations on demand.',
  '伴生隐式自动记忆': 'Companion Implicit Auto-Memory',
  '对话过程中由伴生 MCP 与 Skills 采集带原话出处的结构化记忆，同时支持手工整理与多格式知识文档导入。': 'Companion MCP tools and Skills capture structured memories with exact source links during chats, alongside manual editing and multi-format document imports.',
  '一句话跨 Agent 项目接力': 'One-phrase cross-agent relay',
  '在新 Agent 输入 /contextcore 或说「接力」，先用短简介秒认哪一场（不跑模型）；选中后分页读完完整原文并原地压缩全部上下文，只选序号先待命，附带任务则直接续做。': 'Type /contextcore or say "Relay" in a new agent to pick from a short-brief list without calling a model; once selected, it pages through the full transcript and compacts everything in context—brief first on number-only selection, or continue immediately with a task.',
  '上下文到位，思路就位': 'Context in place, thinking in place',
  '换个 Agent，': 'Switch agents.',
  '先接上完整来龙去脉。': 'Pick up the entire thread.',
  '打开任意新 Agent 输入 /contextcore 或说一句「接力」，先列出带短简介的近期会话候选让你秒认是哪一场（不调用模型）；点中某一场后不拿外置交接卡凑合，而是按页读完完整对话原文并由新 Agent 原地压缩全部上下文——只选序号先汇报待命，附带具体任务则直接续做。': 'Type /contextcore or say "Relay" in any new agent to list recent cross-host conversations with short recognizable briefs without calling a model; after you pick one, instead of truncating into an external handoff card, the receiving agent pages through the full transcript and compacts everything in context—reply with just a number to report and wait, or include a task to continue right away.',
  '选定之后，接手方真正拿到的全部上下文。': 'What full-transcript relay preserves.',
  '列表只展示短简介用于快速识别 → 选中后从第 0 条按页读完完整对话（每页字节封顶防溢出）→ 在接手 Agent 上下文中直接全量压缩，完整守住以下六项工程保障：': 'Identify the session from short briefs → read every message from offset 0 within safe page budgets → compact the entire conversation directly in the receiving agent with these six guarantees:',
  'STEP 01 · 呼叫接力': 'STEP 01 · TRIGGER RELAY',
  '在新 Agent 输入 /contextcore 或「接力」': 'Type /contextcore or "Relay" in your new agent',
  '直接读取本机原生会话索引列出候选，不触发多余模型调用': 'Reads local native session indexes to list candidates without extra model calls',
  'STEP 02 · 短简介点选': 'STEP 02 · PICK BY SHORT BRIEF',
  '展示序号、来源 Agent、时间、标题与短简介': 'View index, source agent, timestamp, title & short brief',
  '保留原生会话 ID 与目录/分支身份，同名或未绑定目录会话清晰区分': 'Preserves native session IDs and worktree/directory identity so same-title or unassigned sessions stay distinct',
  'STEP 03 · 读完原文压缩': 'STEP 03 · PAGE FULL TRANSCRIPT & COMPACT',
  '顺序分页读完完整原文并原地压缩上下文': 'Read full transcript pages sequentially and compact in context',
  '单页 48KB 封顶防爆上下文；只选序号只读待命，附带任务才直接动手': '48KB page cap guards tool budgets; number-only selection reports in read-only stand-by, while explicit tasks execute',
  '短简介秒选，不提前写卡': 'Short brief first, zero pre-summarization',
  '列表只呈现能认出对话的标题与短简介，不跑背景提炼模型。': 'Lists show only recognizable titles and short briefs without running a background summarizer.',
  '原生会话与工作目录消歧': 'Native session & worktree identity',
  '保留原始 UUID 与 Worktree/目录身份，同名会话或未归档对话各归其位。': 'Retains raw session UUIDs and worktree paths so same-title or unassigned chats never collide.',
  '顺序分页读完完整原文': 'Paged full-transcript reading',
  '从第 0 条到末页通过分页工具顺序读全，单页严格封顶防止响应溢出。': 'Pages from offset 0 through the final turn with strict byte bounds to avoid MCP response overflows.',
  '接手端上下文原地压缩': 'In-context compaction, no handoff cards',
  '由新 Agent 在自己上下文内压缩完整历史，告别易遗漏细节的交接卡。': 'The receiving agent compacts the full read transcript in its own context instead of relying on lossy handoff cards.',
  '工作任务与硬约束护栏': 'Working tasks & fail-closed constraints',
  '携带当前任务进度与已锁定规则；受保护约束若超预算则显式阻断而不静默截断。': 'Carries active working tasks and locked rules; protected constraints fail closed when over budget instead of silent clipping.',
  '只读待命与按需刷新': 'Read-only stand-by & on-demand refresh',
  '仅回复序号或 ID 时锁定 must_not_execute=true 先汇报待命；只按需刷新所选会话，不扫全库。': 'Number- or ID-only selection locks must_not_execute=true to report and wait; refreshes only the chosen source.',
  '只选序号先读完再待命，附带任务才动手做。': 'Pick a number to read & report first, or add a task to act.',
  '仅回复序号或会话 ID 时，新 Agent 会读完完整对话并压缩整理成前情汇报后等待你的下一句指令（绝不擅自改文件）；若选场时附带明确任务，则带着压缩后的完整上下文直接续做。': 'Replying with only a number or session ID reads the full transcript, compacts it into a clean briefing, and waits without editing files; adding an explicit task continues right away with full context.',
  '属于项目，而非某次对话': 'Belongs to the project, not one chat',
  '项目的来龙去脉，': 'The story of the project,',
  '随时查得到。': 'always within reach.',
  '散落的会话，': 'Scattered chats,',
  '回到同一个项目。': 'back in one project.',
  '不用在几个聊天窗口里翻找。支持无目录先建项目，来自不同 Agent 与 Worktree 的对话记录、工作任务进度、已定决定和参考资料自动归集到同一项目空间；想知道“为什么这样做”，随时一键溯源原话。': 'Stop digging through scattered chat windows. Create directory-free projects up front, and let chats across agents and worktrees, working task state, decisions, and references converge in one project with direct source links.',
  '按项目组织 · 无目录也可新建 · 跨来源检索': 'Organized by project · directory-free creation · cross-source search',
  '纸桥计划': 'Paper Bridge',
  '示意': 'Preview',
  '概览': 'Overview',
  '任务进度': 'Tasks',
  '相关聊天': 'Chats',
  '项目记忆': 'Memory',
  '知识资料': 'Materials',
  '已确认决定': 'Confirmed decision',
  '沿用现有设计系统，': 'Keep the existing design system,',
  '让迭代保持一致。': 'so iterations stay consistent.',
  '决定与约束 / 可查看来源': 'Decisions and constraints / sources available',
  '项目背景': 'Background',
  '踩坑经验': 'Lessons',
  '让经验积累，': 'Let experience accumulate,',
  '而不是聊天堆积。': 'not chats pile up.',
  '无需手动整理笔记：日常对话中由伴生 MCP 与 Skills 维护工作任务进度与带版本的六类长期记忆，新决定替代旧记录时留痕不覆盖，教训晋升须核对同项目原对话出处，存在分歧则交由你确认，支持核心规则置顶。': 'No manual note-taking required—companion MCP tools and Skills maintain working task progress alongside six categories of version-governed long-term memories with provenance-checked lesson promotions, supersession trails, pinned rules, and conflict review.',
  '工作任务 + 版本治理记忆 · 原文可溯源': 'Working tasks + versioned memory · Source-traceable',
  '使用习惯': 'Preferences',
  '决定与约束': 'Constraints',
  '任务与进度': 'Tasks',
  '参考资料': 'References',
  '文档不只是存着，': 'Keep your documents',
  '还能接着用。': 'working for you.',
  '需求、规范和参考资料，不必每次重新贴进对话。导入文档后，提取的内容可供授权 Agent 检索；你也能在工作台核对来源与正文。': 'Import requirements, guidelines, and references for authorized assistants to search. Review the source and extracted text in the workspace.',
  '每份资料，': 'Every document,',
  '都有下文。': 'has a next chapter.',
  '从一位助手、一件事开始': 'One assistant, one task to start',
  '本机优先，授权清晰。': 'Local first, clear permissions.',
  '了解数据与权限的边界': 'Understand data and permission boundaries',
  '所有会话索引、工作任务、项目记忆与配置快照均存储在你的 Mac 本机，API Key 独立存入系统钥匙串。读取聊天记录、修改宿主配置与下载可选语义模型均独立确认，受保护规则超出预算时显式拦截而不静默截断，默认搜索不联网。': 'All session indexes, working tasks, project memories, and config snapshots stay on your Mac, with API keys isolated in Keychain. Reading chat history, editing host configs, and downloading optional semantic models are confirmed separately; protected rules fail closed instead of silent clipping when over budget, and search never connects to the network.',
  '保留你的工作方式': 'Keep your way of working',
  '熟悉的 Agent。': 'Your familiar agents.',
  '接上同一个项目。': 'Connected to the same project.',
  '你继续用熟悉的工具，项目背景由 Senrelay 统一保存和提供。': 'Keep using familiar tools. Senrelay stores and provides shared project context.',
  '把项目进度带进下一段工作': 'Carry progress into the next stretch of work',
  '在编辑器里接上已有思路': 'Pick up existing thinking in the editor',
  '让上下文跟随你的工作流': 'Let context follow your workflow',
  '换个工作环境，保留项目背景': 'Change environments, keep the project background',
  '在终端里接着上一段工作': 'Continue the previous stretch in the terminal',
  '把项目背景带进终端工作流': 'Bring project context into terminal workflows',
  '以上六大主力 Agent 支持完整的会话采集、隐式自动记忆与跨 Agent 接力；其他扩展宿主仅支持按各自配置文件格式进行管理。': 'The six core agents above support full session capture, implicit auto-memory, and cross-agent relay; additional extended hosts support configuration file management only.',
  '你可能还想知道。': 'A few more questions.',
  '关于接棒、数据，': 'About handoffs, data,',
  '以及现在可以做什么。': 'and what you can do today.',
  'macOS 本机服务 + 浏览器工作台 · 支持多宿主配置同步、隐式自动记忆与跨 Agent 项目接力。': 'macOS local service + browser workbench · Multi-host config sync, implicit auto-memory, and cross-agent project relay.',
  '回到顶部 ↑': 'Back to top ↑',
};
