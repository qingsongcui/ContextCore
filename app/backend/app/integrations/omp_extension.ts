/** OMP 18.x runtime: https://github.com/can1357/oh-my-pi/blob/v18.12.6/docs/extensions.md */
import { readFileSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";
import type { ExtensionAPI, ExtensionContext } from "@oh-my-pi/pi-coding-agent";

interface HookResponse { injection?: string; card?: string; status?: string; handoff_id?: string; }
const BASE_URL = "http://127.0.0.1:8420";

export default function contextcore(pi: ExtensionAPI) {
  // Factories are rebound for each session; never share identity at module scope.
  async function request(endpoint: string, body?: Record<string, unknown>, timeout = 10_000): Promise<HookResponse> {
    const token = readFileSync(join(homedir(), ".contextcore", "credentials", "omp.token"), "utf8").trim();
    if (!token) throw new Error("ContextCore OMP credential is empty");
    const response = await fetch(`${BASE_URL}${endpoint}`, {
      method: body ? "POST" : "GET",
      headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}` },
      body: body ? JSON.stringify(body) : undefined,
      signal: AbortSignal.timeout(timeout), redirect: "error",
    });
    if (!response.ok) throw new Error(`ContextCore request failed (HTTP ${response.status})`);
    return response.json() as Promise<HookResponse>;
  }
  function metadata(ctx: ExtensionContext) {
    return { cwd: ctx.cwd, sessionId: ctx.sessionManager.getSessionId(), timestamp: new Date().toISOString() };
  }
  function unavailable(ctx: ExtensionContext) {
    // Never log credentials, headers, source bodies, or raw provider errors.
    ctx.ui.notify("ContextCore unavailable: shared context was not retrieved. Do not claim a successful handoff.", "warning");
  }
  pi.registerCommand("contextcore", {
    description: "ContextCore status or cross-agent handoff (read back, then wait for confirmation)",
    handler: async (args, ctx) => {
      const [action = "status", ...query] = args.trim().split(/\s+/).filter(Boolean);
      try {
        if (action === "status") {
          await request("/api/status");
          ctx.ui.notify("ContextCore server online; this does not prove handoff completion.", "info");
        } else if (action === "handoff") {
          const result = await request("/api/connections/omp/hook", {
            ...metadata(ctx), event: "intent_detected", prompt: query.join(" ") || "接着做",
          });
          const content = result.injection || result.card;
          if (content) pi.sendMessage({ customType: "contextcore.handoff", content, display: true }, { triggerTurn: false });
          else ctx.ui.notify("ContextCore returned no predecessor context; specify a project or task.", "warning");
        } else ctx.ui.notify("Usage: /contextcore [status|handoff <task query>]", "info");
      } catch { unavailable(ctx); }
    },
  });
  // Never return a network promise to the host lifecycle dispatcher. Keep at
  // most one request alive; authorized history polling fills any missed events.
  let reporting = false;
  function report(body: Record<string, unknown>) {
    if (reporting) return;
    reporting = true;
    void request("/api/connections/omp/hook", body, 500)
      .catch(() => {})
      .finally(() => { reporting = false; });
  }
  pi.on("session_start", (_event, ctx) => {
    report({ ...metadata(ctx), event: "session_start" });
  });
  pi.on("input", (event, ctx) => {
    if (/^\/contextcore(?:\s|$)/.test(event.text.trim())) return;
    report({ ...metadata(ctx), event: "user_message", text: event.text, content: event.text });
  });
  pi.on("session_stop", (_event, ctx) => {
    report({ ...metadata(ctx), event: "session_stop" });
  });
}
