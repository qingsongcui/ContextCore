# Support matrix

Public package version `1.0.0-beta.6` (Python `1.0.0b6` / native short version `1.0.0`). This page is a capability limit statement and does not certify an untested host, account, or model call. For daily end-user installation and cross-Agent workflow instructions, see the [Setup Guide (English)](https://senrelay.com/guide.html) and [Setup Guide (中文)](https://senrelay.com/zh/guide.html), and see [5-Way Comparison](https://senrelay.com/compare.html) / [五方横评](https://senrelay.com/zh/compare.html) for architecture tradeoffs.

Three states stay separate:

| State | What it means | What it does not mean |
|---|---|---|
| Configuration write | A definition was saved and can be read back from ContextCore. | The assistant process loaded that file. |
| Local or synthetic test | An isolated test, fixture, or disabled-connection drill passed. | A real host, real account, or real model call succeeded. |
| Host load or live call | The named surface loaded the component, or a consented model call returned. | Any other surface, version, or later tree. |

Senrelay has six Agents. Some ship more than one app (Codex desktop app and CLI, Cursor IDE and CLI, Antigravity desktop app and `agy`); each such pair is one Agent with shared configuration, but evidence is recorded per app: a result on one does not move the other.

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
| Antigravity | CLI (`agy`) | not_run |
| Claude Code | CLI | not_run |
| OpenCode | CLI | not_run |

Historical grades recorded on older trees stay in [evidence](evidence.md). They include four live-host continuation loops and two live-protocol loops. They are not six live-host passes, not memory-write proof, and not acceptance of this tree. D007 on this tree has a synthetic predecessor-exhaustion test only. A live exhausted predecessor was not run.

## Configuration surfaces

Provider, model, account, MCP, Skill, prompt, route, project bundle, migration, and secret operations have isolated tests. Those tests can show preview, failure, ownership, and "do not guess a missing model." They do not show that an installed assistant reloaded, that a provider was reachable, or that a key called a model. Official OAuth, Keychain lock and recovery, and paid calls remain unaccepted.

Semantic retrieval stays off by default. No model download or inference is authorized by opening search.
