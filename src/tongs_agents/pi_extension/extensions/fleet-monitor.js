import { spawn } from "node:child_process";
import { randomUUID } from "node:crypto";
import {
  closeSync, fstatSync, lstatSync, openSync, readSync, realpathSync,
} from "node:fs";
import { homedir } from "node:os";
import { dirname, isAbsolute, join } from "node:path";
import { fileURLToPath } from "node:url";

const RUNTIME_KEY = Symbol.for("tongs-agents.pi.runtime.v1");
const CONFIG_OWNER = "tongs-agents";
const CONFIG_NAME = "tongs-agents.json";
const MAX_QUEUE = 128;
const SEND_TIMEOUT_MS = 1500;
const MAX_CONFIG_BYTES = 16 * 1024;
// Agent chat (#32): the hook prints a digest for a granted launched session.
// The event and return shapes below are unvalidated against a live Pi.
const CHAT_CUSTOM_TYPE = "tongs-agents-chat";
const CHAT_DIGEST_TIMEOUT_MS = 500;
const CHAT_MAX_DIGEST_BYTES = 4096;
const CHAT_TOOL_SPACING_MS = 10000;
const LAUNCH_ID_PATTERN = /^[0-9a-f]{32}$/;
const CURRENT_EXTENSION_PATH = realpathSync(
  dirname(dirname(fileURLToPath(import.meta.url))),
);

function createRuntime() {
  return {
    instanceId: randomUUID(),
    sequenceBySession: new Map(),
    stateBySession: new Map(),
    activeSessionId: null,
    queue: [],
    sending: false,
    shutdownDrainDeadline: null,
    shutdownDeadline: null,
  };
}

function validAbsolutePath(value) {
  return typeof value === "string"
    && value.length > 0
    && !value.includes("\0")
    && Buffer.byteLength(value, "utf8") <= 4096
    && isAbsolute(value);
}

const runtime = globalThis[RUNTIME_KEY] ?? createRuntime();
globalThis[RUNTIME_KEY] = runtime;

function loadConfig() {
  let descriptor;
  try {
    const configDir = process.env.PI_CODING_AGENT_DIR
      || join(homedir(), ".pi", "agent");
    const configPath = join(configDir, CONFIG_NAME);
    const entry = lstatSync(configPath);
    if (entry.isSymbolicLink() || !entry.isFile() || entry.size > MAX_CONFIG_BYTES) {
      return null;
    }
    descriptor = openSync(configPath, "r");
    const stat = fstatSync(descriptor);
    if (!stat.isFile() || stat.size > MAX_CONFIG_BYTES) return null;
    const buffer = Buffer.alloc(stat.size);
    if (readSync(descriptor, buffer, 0, stat.size, 0) !== stat.size) return null;
    const config = JSON.parse(buffer.toString("utf8"));
    if (
      config?.owner !== CONFIG_OWNER
      || config?.schema_version !== 1
      || !validAbsolutePath(config?.hook_path)
      || !validAbsolutePath(config?.extension_path)
      || !Array.isArray(config?.managed_paths)
      || config.managed_paths.length > 64
      || config.managed_paths.some((value) => !validAbsolutePath(value))
      || !config.managed_paths.includes(config.extension_path)
      || realpathSync(config.extension_path) !== CURRENT_EXTENSION_PATH
    ) {
      return null;
    }
    return config;
  } catch {
    return null;
  } finally {
    if (descriptor !== undefined) {
      try { closeSync(descriptor); } catch {}
    }
  }
}

const config = loadConfig();

function sessionId(ctx) {
  const value = ctx?.sessionManager?.getSessionId?.();
  return typeof value === "string" && value.length > 0 ? value : null;
}

function safeToolName(value) {
  if (typeof value !== "string") return "";
  return [...value.replace(/[\u0000-\u001f\u007f]/g, "")].slice(0, 64).join("");
}

function nextSequence(id) {
  const sequence = (runtime.sequenceBySession.get(id) ?? 0) + 1;
  runtime.sequenceBySession.set(id, sequence);
  return sequence;
}

function send(payload, final) {
  if (!config) return Promise.resolve(false);
  return new Promise((resolve) => {
    let settled = false;
    let child;
    let timer;
    const finish = (success) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      resolve(success);
    };
    try {
      child = spawn(
        config.hook_path,
        ["fleet-event", "--agent", "pi"],
        { stdio: ["pipe", "ignore", "ignore"], env: process.env },
      );
    } catch {
      resolve(false);
      return;
    }
    const deadline = final ? runtime.shutdownDeadline : runtime.shutdownDrainDeadline;
    const timeout = deadline === null
      ? SEND_TIMEOUT_MS
      : Math.max(1, Math.min(SEND_TIMEOUT_MS, deadline - Date.now()));
    timer = setTimeout(() => {
      try { child.kill(); } catch {}
      finish(false);
    }, timeout);
    child.once("error", () => finish(false));
    child.once("close", (code) => finish(code === 0));
    child.stdin.once("error", () => {
      try { child.kill(); } catch { finish(false); }
    });
    try {
      child.stdin.end(JSON.stringify(payload));
    } catch {
      try { child.kill(); } catch { finish(false); }
    }
  });
}

function drain() {
  if (runtime.sending) return;
  if (
    runtime.shutdownDrainDeadline !== null
    && Date.now() >= runtime.shutdownDrainDeadline
  ) {
    const final = runtime.queue.find((item) => item.final);
    for (const queued of runtime.queue.splice(0)) {
      if (queued !== final) queued.resolve(false);
    }
    if (final) runtime.queue.push(final);
  }
  const item = runtime.queue.shift();
  if (!item) return;
  runtime.sending = true;
  send(item.payload, item.final)
    .catch(() => false)
    .then(item.resolve)
    .finally(() => {
      runtime.sending = false;
      if (item.final) {
        runtime.shutdownDrainDeadline = null;
        runtime.shutdownDeadline = null;
      }
      drain();
    });
}

function enqueue(payload, final = false) {
  return new Promise((resolve) => {
    if (runtime.queue.length >= MAX_QUEUE) {
      runtime.queue.shift()?.resolve(false);
    }
    runtime.queue.push({ payload, resolve, final });
    drain();
  });
}

// Only a pane tongs-agents launched on Linux can be granted chat; any other
// session never spawns the digest.
const chatPossible = Boolean(config)
  && process.platform === "linux"
  && LAUNCH_ID_PATTERN.test(process.env.TONGS_AGENTS_LAUNCH_ID ?? "");
const chatState = { toolInFlight: false, lastToolRequest: 0 };

function chatDigest(point) {
  return new Promise((resolve) => {
    let settled = false;
    let child;
    let timer;
    const chunks = [];
    let size = 0;
    const finish = (text) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      resolve(text);
    };
    try {
      child = spawn(
        config.hook_path,
        ["chat-digest", "--agent", "pi"],
        { stdio: ["pipe", "pipe", "ignore"], env: process.env },
      );
    } catch {
      resolve("");
      return;
    }
    timer = setTimeout(() => {
      try { child.kill(); } catch {}
      finish("");
    }, CHAT_DIGEST_TIMEOUT_MS);
    child.once("error", () => finish(""));
    child.stdout.on("data", (chunk) => {
      size += chunk.length;
      if (size > CHAT_MAX_DIGEST_BYTES) {
        try { child.kill(); } catch {}
        finish("");
        return;
      }
      chunks.push(chunk);
    });
    child.once("close", (code) => {
      finish(code === 0 ? Buffer.concat(chunks).toString("utf8") : "");
    });
    child.stdin.once("error", () => {
      try { child.kill(); } catch { finish(""); }
    });
    try {
      child.stdin.end(JSON.stringify({ point }));
    } catch {
      try { child.kill(); } catch { finish(""); }
    }
  });
}

function chatMessage(content) {
  return { customType: CHAT_CUSTOM_TYPE, content, display: true };
}

function chatOnPrompt() {
  return chatDigest("before_agent_start")
    .catch(() => "")
    .then((content) => (content ? { message: chatMessage(content) } : undefined));
}

function chatOnToolEnd(pi) {
  const now = Date.now();
  if (
    chatState.toolInFlight
    || now - chatState.lastToolRequest < CHAT_TOOL_SPACING_MS
    || typeof pi.sendMessage !== "function"
  ) {
    return;
  }
  chatState.toolInFlight = true;
  chatState.lastToolRequest = now;
  chatDigest("tool_execution_end")
    .catch(() => "")
    .then((content) => {
      if (!content) return;
      try {
        pi.sendMessage(chatMessage(content), { deliverAs: "steer" });
      } catch {}
    })
    .finally(() => {
      chatState.toolInFlight = false;
    });
}

function stateFor(id) {
  let state = runtime.stateBySession.get(id);
  if (!state) {
    state = {
      status: "started",
      detail: "session started",
      tool: "",
      waitingDepth: 0,
      pendingOutcome: null,
    };
    runtime.stateBySession.set(id, state);
  }
  return state;
}

function capture(ctx, eventId, status, detail, tool = "", final = false) {
  const id = sessionId(ctx);
  if (!id) return Promise.resolve(false);
  const cwd = typeof ctx.cwd === "string" && ctx.cwd.length > 0
    ? ctx.cwd
    : process.cwd();
  const payload = {
    schema_version: 1,
    event_id: `pi.${eventId}`,
    session_id: id,
    instance_id: runtime.instanceId,
    sequence: nextSequence(id),
    pid: process.pid,
    cwd,
    status,
    detail,
    tool,
  };
  return enqueue(payload, final);
}

function transition(ctx, eventId, status, detail, tool = "", final = false) {
  const id = sessionId(ctx);
  if (!id) return Promise.resolve(false);
  const state = stateFor(id);
  state.status = status;
  state.detail = detail;
  state.tool = tool;
  if (state.waitingDepth > 0 && status !== "ended") {
    return Promise.resolve(true);
  }
  return capture(ctx, eventId, status, detail, tool, final);
}

function outcomeFromMessages(messages) {
  if (!Array.isArray(messages)) return { status: "idle", detail: "finished" };
  for (let index = messages.length - 1; index >= 0; index -= 1) {
    const message = messages[index];
    if (message?.role !== "assistant") continue;
    if (message.stopReason === "error") {
      return { status: "error", detail: "turn failed" };
    }
    if (message.stopReason === "aborted") {
      return { status: "idle", detail: "turn aborted" };
    }
    break;
  }
  return { status: "idle", detail: "finished" };
}

export default function tongsAgents(pi) {
  if (!config) return;
  pi.on("session_start", (event, ctx) => {
    const id = sessionId(ctx);
    if (!id) return;
    if (runtime.activeSessionId === id) return;
    runtime.activeSessionId = id;
    const state = stateFor(id);
    state.waitingDepth = 0;
    state.pendingOutcome = null;
    transition(ctx, "session-start", "started", "session started");
  });

  pi.on("before_agent_start", (_event, ctx) => {
    const id = sessionId(ctx);
    if (!id) return;
    stateFor(id).pendingOutcome = null;
    transition(ctx, "before-agent-start", "running", "processing prompt");
    if (chatPossible) return chatOnPrompt();
  });

  pi.on("tool_execution_start", (event, ctx) => {
    const tool = safeToolName(event?.toolName);
    transition(ctx, "tool-execution-start", "running", `using ${tool || "tool"}`, tool);
  });

  if (chatPossible) {
    pi.on("tool_execution_end", () => {
      chatOnToolEnd(pi);
    });
  }

  pi.on("agent_end", (event, ctx) => {
    const id = sessionId(ctx);
    if (!id) return;
    stateFor(id).pendingOutcome = outcomeFromMessages(event?.messages);
  });

  pi.on("agent_settled", (_event, ctx) => {
    const id = sessionId(ctx);
    if (!id) return;
    const state = stateFor(id);
    const outcome = state.pendingOutcome ?? { status: "idle", detail: "finished" };
    state.pendingOutcome = null;
    transition(ctx, "agent-settled", outcome.status, outcome.detail);
  });

  pi.on("ui_prompt_start", (_event, ctx) => {
    const id = sessionId(ctx);
    if (!id) return;
    const state = stateFor(id);
    state.waitingDepth += 1;
    if (state.waitingDepth !== 1) return;
    capture(ctx, "ui-prompt-start", "waiting", "waiting for user input");
  });

  pi.on("ui_prompt_end", (_event, ctx) => {
    const id = sessionId(ctx);
    if (!id) return;
    const state = stateFor(id);
    if (state.waitingDepth === 0) return;
    state.waitingDepth -= 1;
    if (state.waitingDepth !== 0) return;
    capture(ctx, "ui-prompt-end", state.status, state.detail, state.tool);
  });

  pi.on("session_shutdown", (_event, ctx) => {
    const id = sessionId(ctx);
    if (!id) return;
    if (runtime.activeSessionId === id) runtime.activeSessionId = null;
    const state = stateFor(id);
    state.waitingDepth = 0;
    state.pendingOutcome = null;
    runtime.shutdownDrainDeadline = Date.now() + SEND_TIMEOUT_MS;
    runtime.shutdownDeadline = Date.now() + (SEND_TIMEOUT_MS * 2);
    return transition(ctx, "session-shutdown", "ended", "session closed", "", true);
  });
}
