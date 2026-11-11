# Per-assistant connection modes — implementation contract

Status: implementation contract. Per-assistant native HTTP mode journeys and bounded account/configuration/bundle lifecycle paths have local acceptance, while remaining project, account and actual host/provider acceptance are tracked in [release readiness](release-readiness.md). The contract does not add new product decisions or certify the full product.

## Current problem

The starting service had one global target, fallback queue and model-route map behind advanced JSON, without a native host gateway workflow. Normal management now uses separate host profiles, readable forms and reviewed native transitions; the older independent-client API remains available. Provider definitions distinguish API surfaces. Valid configuration syntax and successful local forwarding still do not establish account entitlement or real host consumption.

## Data and ownership

- Keep provider definitions shared, with an explicit upstream API dialect and source metadata when known. Do not infer custom endpoint support from an OpenAI-compatible label. Preserve legacy definitions and distinguish unknown dialect from a failed endpoint.
- Add versioned per-host mode profiles: direct selection retained for restoration, current route target, allowed fallback queue, aggregation members/model aliases, default provider, timeout/circuit settings, and pending/applied/recovery state. No one profile changes another host.
- Keep gateway credentials separate from browser sessions, MCP/compute credentials and upstream provider keys. Native gateway requests must be independently authenticated and bound to their host profile. List/status responses expose presence, never token values.
- Reference provider/model versions in previews. Changes after preview invalidate application. Persist pending/recovery evidence around file/profile changes; filesystem plus SQLite is not an absolute atomic transaction.
- Read full definitions only for selected edits, previews or request dispatch. Reuse summary paging and selected-ID lookups for all mode forms and selectors.

## User journeys

1. Viewing Direct, Routing or Aggregation changes only the visible tab. The current applied mode remains explicitly identified. Leaving an unsaved form follows the existing per-assistant draft rules, without caching secrets.
2. Direct selection saves a definition, then separately previews the exact model/endpoint/configuration changes, requires confirmation, applies and reads back. Display host reload and real request status independently.
3. Starting routing selects a target and explicitly allowed fallback queue, previews the local gateway address/key consequences, then confirms the native configuration change. Upstream keys remain local to the service. The UI explains that the service must remain available.
4. Changing a route target affects new requests; in-flight requests retain their starting target. Only unchanged client configuration can be described as requiring no reload. Changed model/catalog/client state requires its own host-specific reload guidance.
5. Aggregation publishes selected model aliases and dispatches to their chosen provider. It does not silently fall back to another provider. The default provider cannot be removed before another default is selected.
6. Returning to direct previews the retained direct selection and current file preconditions, confirms, applies and reads back. External edits and partial failure retain explicit recovery rather than overwriting unknown state.
7. Circuit/fallback/model mapping use readable rows, ordered choices and numeric controls. Raw JSON remains an optional advanced representation of the same draft, not a prerequisite for normal operation.

## Protocol and consent boundaries

Use a host/dialect capability matrix backed by current primary documentation. Preserve streaming, tools, reasoning and image semantics within the supported path; refuse unsupported conversion rather than discard content. Native pass-through and conversion are separate capabilities. Do not claim full multi-host protocol parity from the current subset.

The current Codex reference accepts Responses as its native wire API. DeepSeek now documents Responses support; an older assumption that DeepSeek is Chat-only is incorrect. Actual account access, model availability and every parameter's support still need separate evidence. Broader host renderers must be checked against their actual native request format, not just config syntax.

Real account login/refresh, private host credential access, paid model requests, host reload and production activation remain distinct consequential operations. Implement and exercise local journeys with synthetic homes, fake vaults and fake upstreams first. Never borrow another application's client identity or private login cache. Upstream approval/subagent features are not authorization to change the user's host approval settings.

## Required acceptance

| Boundary | Proof required |
|---|---|
| Per-host isolation | Two active profiles use different targets/tokens; changes, fallback and restoration do not cross hosts |
| Native activation | Preview → confirmation → actual temporary file changes → readback → complete direct restoration |
| State changes | Tab navigation has no mutation; stale definitions/profile revisions reject apply; partial failures remain recoverable |
| Forwarding | Actual local HTTP fake-upstream calls for every supported dialect, streaming/tool/usage semantics, cancellation and no replay after partial or unknown output |
| Aggregation | Explicit aliases reach the intended provider/model, default/removal guards hold, no unintended fallback |
| Consumer forms | Search/page choices, queue ordering, mappings, validation and unsaved drafts work without JSON |
| Credential boundary | No secrets in lists/audits/exports; mismatched/revoked gateway token refuses requests; vault failures fail closed |
| Actual hosts/providers | Separately authorized live loading/calls and reload behavior; synthetic file writes are not accepted as this proof |

Primary sources read on 2026-10-26: [Codex configuration reference](https://learn.chatgpt.com/docs/config-file/config-reference) and [DeepSeek Responses guide](https://api-docs.deepseek.com/guides/responses_api/), alongside compatible local proxy/routing/aggregation specifications. These are documentation evidence, not account or host acceptance.
