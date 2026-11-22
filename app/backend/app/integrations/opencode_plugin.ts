/**
 * ContextCore plugin for OpenCode.
 *
 * Reports prompt capture and a session.idle lifecycle boundary. It exposes
 * static tool guidance to the model without adding memories or chat messages.
 * Capture never logs credentials or waits for a network request in the host turn.
 */
import { readFileSync, statSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";

const BASE_URL = "http://127.0.0.1:8420";
const TIMEOUT_MS = 500;
const WORKING_MEMORY_GUIDANCE = `ContextCore (续境) tools are available for everyday work across Agent sessions.
- Before starting a new project task or producing a project deliverable, your first tool call must be contextcore_bootstrap_working_memory with the current working directory and a focused query. This includes short project reports, drafts, edits, and tasks where the user wants only the finished output. Set new_task=true when the user is starting distinct work, so temporary constraints from another task do not transfer. Skip only unrelated small talk and simple factual questions. Use only relevant, current memories. Honor must_not_execute, task selection, and pending conflicts.
- During ordinary chat, save a clearly stated lasting decision, preference, or correction with contextcore_save_memory even if the user does not ask you to remember it. Use project scope for project rules and global scope only for explicitly cross-project preferences. When the user gives both a standing rule and a one-time exception, save only the standing rule. Leave the one-time exception out of the memory body, title, and tags, even as an example.
- Use contextcore_upsert_working_task for real progress, blockers, and temporary task constraints. Keep guesses, unapproved suggestions, transient requirements, and raw logs out of long-term memory.
- Before sending the result, cite only memory retrieved from an earlier turn that actually changed the output, using contextcore_cite_memory or the task's cited_memory_ids. Saving a rule from the current message is not a citation. Do not cite a standing rule that the current user request overrides. When the user wants only the deliverable, keep the citation in the tool record. Otherwise mention briefly which constraint was used. Do not claim a save or citation until the tool succeeds.
- Reserve the ContextCore relay tools and transcript retrieval for explicit continuation requests. If a ContextCore tool is unavailable, continue the user's conversation without claiming memory was loaded or saved.`;

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
  let hasContextTools = false;
  function report(body: Record<string, unknown>) {
    if (reporting) return;
    reporting = true;
    void post(body).catch(() => {}).finally(() => { reporting = false; });
  }
  return {
    config: async (input: { mcp?: Record<string, { type?: string; url?: string; enabled?: boolean }> }) => {
      const server = input?.mcp?.contextcore;
      hasContextTools = server?.type === "remote" &&
        server.url?.replace(/\/$/, "") === `${BASE_URL}/mcp` && server.enabled !== false;
    },

    "experimental.chat.system.transform": async (_input: unknown, output: { system: string[] }) => {
      if (hasContextTools && readToken()) output.system.push(WORKING_MEMORY_GUIDANCE);
    },

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
