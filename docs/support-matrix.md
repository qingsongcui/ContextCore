# Support matrix

Public package version `1.0.0-beta.2` (Python `1.0.0b2` / native short version `1.0.0`). This page is a capability limit statement and does not certify an untested host, account, or model call. For daily end-user installation and cross-Agent workflow instructions, see the [Setup Guide (中文)](https://senrelay.com/guide.html) and [Setup Guide (English)](https://senrelay.com/en/guide.html), and see [5-Way Comparison](https://senrelay.com/en/compare.html) / [五方横评](https://senrelay.com/compare.html) for architecture tradeoffs.

Three states stay separate:

| State | What it means | What it does not mean |
|---|---|---|
| Configuration write | A definition was saved and can be read back from ContextCore. | The assistant process loaded that file. |
| Local or synthetic test | An isolated test, fixture, or disabled-connection drill passed. | A real host, real account, or real model call succeeded. |
| Host load or live call | The named surface loaded the component, or a consented model call returned. | Any other surface, version, or later tree. |

Codex Desktop and Codex CLI, Cursor IDE and Cursor CLI, Antigravity Desktop and agy, and OMP and Pi are different surfaces. A result on one does not move the other.

## Context surfaces on this tree

Current real-host cells for history read, new capture, tool load, memory read, memory write, six-point wait, confirmed continuation, offline behavior, and revoked access are `not_run` on this tree for every row below. Synthetic suite evidence is not a substitute.

| Host | Surface | Current real-host acceptance |
|---|---|---|
| Codex | Desktop | not_run |
| Codex | CLI | not_run |
| OMP | CLI | not_run |
| Cursor | IDE | not_run |
| Cursor | CLI | not_run |
| Antigravity | Desktop | not_run |
| agy | CLI | not_run |
| Claude Code | CLI | not_run |
| OpenCode | CLI | not_run |

Historical grades recorded on older trees stay in [evidence](evidence.md). They include four live-host continuation loops and two live-protocol loops. They are not six live-host passes, not memory-write proof, and not acceptance of this tree. D007 on this tree has a synthetic predecessor-exhaustion test only. A live exhausted predecessor was not run.

## Configuration surfaces

Provider, model, account, MCP, Skill, prompt, route, project bundle, migration, and secret operations have isolated tests. Those tests can show preview, failure, ownership, and "do not guess a missing model." They do not show that an installed assistant reloaded, that a provider was reachable, or that a key called a model. Official OAuth, Keychain lock and recovery, and paid calls remain unaccepted.

Semantic retrieval stays off by default. No model download or inference is authorized by opening search.

## Wider configuration hosts

These rows are not context hosts. A format table, a parser, or an isolated file write is not history capture, tool loading, memory, relay, or a live host. Pi is not OMP. agy is not Antigravity Desktop. Real-host cells for the six context surfaces stay `not_run` above; nothing here fills them.

Context operations below are all gaps: history read, new capture, tool load by the host process, memory read, memory write, six-point wait, confirmed continuation, background disconnect does not block, and access stops after revoke. None of these hosts are in the history registry.

| Host | What isolated tests or code paths actually cover | Gap |
|---|---|---|
| Claude Desktop | MCP JSON path is declared. Direct remote MCP is rejected as unverified. Model activation is rejected as unverified. No skill or prompt path. | No isolated MCP write test. No history, capture, memory, relay, or live load. |
| Gemini CLI | MCP path `.gemini/settings.json` is declared, and the sample host bar can show the name. Model activation is rejected as unverified. No skill or prompt path. | No isolated MCP write test. No history, capture, memory, relay, or live load. |
| Grok Build | Isolated test renders a native TOML MCP entry. Changing the global default model is explicitly unsupported. Activation is rejected. | Skill and prompt paths are declared only. No history, capture, memory, relay, or live load. |
| Kimi Code | Isolated tests render MCP and append a provider without changing the existing default. Activation requires a declared context window. | Skill and prompt paths are declared only. No history, capture, memory, relay, or live load. |
| MiniMax Code | Isolated tests map an MCP entry and write a provider while keeping the native default. An over-long prompt is rejected. | Skill directory is declared only. No history, capture, memory, relay, or live load. |
| Pi | Isolated tests render MCP and a provider, and refuse to rewrite existing JSON5. Separate from OMP. | Skill and prompt paths are declared only. No history, capture, memory, relay, or live load. |
| Hermes | Isolated tests render YAML MCP and a named provider, and can preview activation. | Skill and prompt paths are declared only. No history, capture, memory, relay, or live load. |
| OpenClaw | Isolated test writes provider JSON5 and preserves comments. Native MCP sync is absent; asking for MCP is rejected. | No MCP. Skill and prompt paths are declared only. No history, capture, memory, relay, or live load. |
| agy | Isolated test renders MCP. Provider credentials stay in process environment or a private profile and are not written from settings. Separate from Antigravity Desktop. | Activation is rejected. No history, capture, memory, relay, or live load. |
