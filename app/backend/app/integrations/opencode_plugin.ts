/**
 * ContextCore plugin for OpenCode.
 *
 * Reports prompt capture and a session.idle lifecycle boundary. It does not inject
 * memory or handoff cards into the chat; agents must call MCP tools when the user
 * asks to continue. It never logs credentials, never waits for a network request in the host turn, and fails open when the local service is unavailable.
 */
import { readFileSync, statSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";

const BASE_URL = "http://127.0.0.1:8420";
const TIMEOUT_MS = 500;

function readToken(): string | null {
  try {
    const tokenPath = join(homedir(), ".contextcore", "credentials", "opencode.token");
    const stat = statSync(tokenPath);
    if ((stat.mode & 0o077) !== 0) return null;
    const token = readFileSync(tokenPath, "utf8").trim();
    return token || null;
  } catch {
    return null;
  }
}

async function post(body: Record<string, unknown>): Promise<Record<string, unknown> | null> {
  const token = readToken();
  if (!token) return null;
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), TIMEOUT_MS);
  timer.unref?.();
  try {
    const response = await fetch(`${BASE_URL}/api/connections/opencode/hook`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${token}`,
      },
      body: JSON.stringify(body),
      redirect: "error",
      signal: controller.signal,
    });
    if (!response.ok) return null;
    const data = await response.json();
    return data && typeof data === "object" ? data : null;
  } catch {
    return null;
  } finally {
    clearTimeout(timer);
  }
}

export default async function contextcore(ctx: { directory?: string }) {
  let reporting = false;
  function report(body: Record<string, unknown>) {
    if (reporting) return;
    reporting = true;
    void post(body).catch(() => {}).finally(() => { reporting = false; });
  }
  return {
    "chat.message": (input: any, output: any) => {
      const parts = Array.isArray(output?.parts) ? output.parts : [];
      const prompt = parts
        .filter((part: any) => part?.type === "text" && part.synthetic !== true && part.ignored !== true)
        .map((part: any) => String(part.text || ""))
        .join("\n")
        .trim();
      if (!prompt || prompt.startsWith("/")) return;
      report({
        event: "chat.message",
        session_id: input?.sessionID,
        message_id: output?.message?.id || input?.messageID,
        prompt,
        cwd: ctx?.directory,
        timestamp: new Date().toISOString(),
      });
    },

    event: ({ event }: { event?: any }) => {
      if (event?.type !== "session.idle") return;
      const sessionID = event?.properties?.sessionID;
      if (!sessionID) return;
      report({
        event: "session.idle",
        session_id: sessionID,
        cwd: ctx?.directory,
        timestamp: new Date().toISOString(),
      });
    },
  };
}
