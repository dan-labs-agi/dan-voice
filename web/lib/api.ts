const API_BASE_URL = process.env.NEXT_PUBLIC_API_URL;

export type PairErrorCode =
  | "pin_not_found"
  | "pin_expired"
  | "pin_already_used"
  | "pin_mismatch"
  | "malformed_pin"
  | "rate_limited"
  | "network_error"
  | "unknown_error";

export interface ErrorDetail {
  code: string;
  message: string;
}

export interface PairResponse {
  access_token: string;
  token_type: string;
  expires_at: string;
}

export interface SessionVerifyResponse {
  session_id: string;
  issued_at: string;
  expires_at: string;
}

export class PairError extends Error {
  constructor(
    public readonly status: number,
    public readonly code: PairErrorCode,
    message: string,
  ) {
    super(message);
  }
}

export async function pair(pin: string): Promise<PairResponse> {
  let res: Response;
  try {
    res = await fetch(`${API_BASE_URL}/pair`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ pin }),
    });
  } catch {
    throw new PairError(0, "network_error", "Could not reach the backend.");
  }

  if (res.status === 200) {
    return (await res.json()) as PairResponse;
  }
  if (res.status === 422) {
    throw new PairError(422, "malformed_pin", "PIN must be 6 digits.");
  }
  if (res.status === 429) {
    throw new PairError(429, "rate_limited", "Too many attempts. Try again shortly.");
  }
  if (res.status === 401) {
    const body = (await res.json()) as { detail: ErrorDetail };
    throw new PairError(401, body.detail.code as PairErrorCode, body.detail.message);
  }
  throw new PairError(res.status, "unknown_error", "Unexpected error.");
}

export interface DevicePairResponse {
  access_token: string;
  token_type: string;
  device_id: string;
  expires_at: string;
}

export async function devicePair(pin: string): Promise<DevicePairResponse> {
  let res: Response;
  try {
    res = await fetch(`${API_BASE_URL}/device/pair`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ pin }),
    });
  } catch {
    throw new PairError(0, "network_error", "Could not reach the backend.");
  }

  if (res.status === 200) {
    return (await res.json()) as DevicePairResponse;
  }
  if (res.status === 422) {
    throw new PairError(422, "malformed_pin", "PIN must be 6 digits.");
  }
  if (res.status === 429) {
    throw new PairError(429, "rate_limited", "Too many attempts. Try again shortly.");
  }
  if (res.status === 401) {
    const body = (await res.json()) as { detail: ErrorDetail };
    throw new PairError(401, body.detail.code as PairErrorCode, body.detail.message);
  }
  throw new PairError(res.status, "unknown_error", "Unexpected error.");
}

export async function verifySession(token: string): Promise<SessionVerifyResponse> {
  const res = await fetch(`${API_BASE_URL}/session/verify`, {
    headers: { Authorization: `Bearer ${token}` },
  });
  if (!res.ok) {
    throw new PairError(res.status, "unknown_error", "Session invalid or expired.");
  }
  return (await res.json()) as SessionVerifyResponse;
}

export interface RefreshResponse {
  access_token: string;
  expires_at: string;
}

export async function refreshSession(token: string): Promise<RefreshResponse> {
  const res = await fetch(`${API_BASE_URL}/session/refresh`, {
    method: "POST",
    headers: { Authorization: `Bearer ${token}` },
  });
  if (!res.ok) {
    throw new PairError(res.status, "unknown_error", "Session refresh failed.");
  }
  return (await res.json()) as RefreshResponse;
}

export class OpencodeApiError extends Error {}

// Shared by streamOpencodeCommand's inline audio_chunk frames and
// streamSpeakText's legacy chunk frames — both carry base64 audio bytes
// over SSE (text-only) and decode to a playable object URL the same way.
function base64ToObjectUrl(base64: string, contentType: string): string {
  const bytes = Uint8Array.from(atob(base64), (c) => c.charCodeAt(0));
  const blob = new Blob([bytes], { type: contentType });
  return URL.createObjectURL(blob);
}

export interface OpencodeCommandResponse {
  text: string;
  parts: unknown[];
}

export async function sendOpencodeCommand(
  token: string,
  text: string,
): Promise<OpencodeCommandResponse> {
  let res: Response;
  try {
    res = await fetch(`${API_BASE_URL}/opencode/command`, {
      method: "POST",
      headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
      body: JSON.stringify({ text }),
    });
  } catch {
    throw new OpencodeApiError("Could not reach the backend.");
  }
  if (!res.ok) {
    throw new OpencodeApiError(`Command failed (HTTP ${res.status}).`);
  }
  return (await res.json()) as OpencodeCommandResponse;
}

// Same underlying opencode call as sendOpencodeCommand, but consumes
// /opencode/command/stream's SSE response so the caller can render the
// assistant's text as it's actually generated — the backend still blocks
// on opencode's own call for the whole turn, but relays live
// `message.part.delta` text chunks it observes meanwhile (confirmed
// against a live opencode instance, not assumed). `onDelta` fires once per
// chunk in arrival order; the returned promise resolves with the final,
// authoritative complete response once the stream's `done` event arrives.
//
// `onToolSummary`, if given, fires whenever the backend's Groq-based
// tool-call summarizer publishes a one-sentence status line for a
// completed/errored tool call — this is best-effort and can arrive at any
// point during the turn, or not at all for a given turn if the Groq call
// was still in flight when the turn finished (see PROGRESS.md — this is
// deliberate: the summarizer never delays the turn itself).
export async function streamOpencodeCommand(
  token: string,
  text: string,
  onDelta: (chunk: string) => void,
  onToolSummary?: (tool: string, text: string, success: boolean) => void,
  onAudioChunk?: (text: string, url: string, source: "answer" | "tool") => void,
): Promise<OpencodeCommandResponse> {
  let res: Response;
  try {
    res = await fetch(`${API_BASE_URL}/opencode/command/stream`, {
      method: "POST",
      headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
      body: JSON.stringify({ text }),
    });
  } catch {
    throw new OpencodeApiError("Could not reach the backend.");
  }
  if (!res.ok || !res.body) {
    // 409 means a prior command is still in flight (most commonly stuck
    // on an unanswered permission) — the backend's detail message is
    // specific and actionable, worth surfacing as-is rather than a
    // generic "Command failed" wrapper.
    let detail = `Command failed (HTTP ${res.status}).`;
    try {
      const body = (await res.json()) as { detail?: string };
      if (body.detail) detail = body.detail;
    } catch {
      // ignore — fall back to the generic message
    }
    throw new OpencodeApiError(detail);
  }

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let final: OpencodeCommandResponse | null = null;

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const frames = buffer.split("\n\n");
    buffer = frames.pop() ?? "";
    for (const frame of frames) {
      const line = frame.split("\n").find((l) => l.startsWith("data:"));
      if (!line) continue;
      const event = JSON.parse(line.slice("data:".length).trim()) as
        | { type: "delta"; text: string }
        | { type: "tool_summary"; tool: string; text: string; success: boolean }
        | {
            type: "audio_chunk";
            text: string;
            content_type: string;
            audio_base64: string;
            source: "answer" | "tool";
          }
        | { type: "done"; text: string; parts: unknown[] }
        | { type: "error"; message: string };
      if (event.type === "delta") {
        onDelta(event.text);
      } else if (event.type === "tool_summary") {
        onToolSummary?.(event.tool, event.text, event.success);
      } else if (event.type === "audio_chunk") {
        const url = base64ToObjectUrl(event.audio_base64, event.content_type);
        onAudioChunk?.(event.text, url, event.source);
      } else if (event.type === "error") {
        // The backend hit a real failure mid-stream (e.g. opencode timed
        // out, most often because a tool permission is still waiting on
        // approval) and said so explicitly — surface that message
        // directly rather than falling through to the generic
        // "stream ended without a final response" below.
        throw new OpencodeApiError(event.message);
      } else {
        final = { text: event.text, parts: event.parts };
      }
    }
  }

  if (!final) {
    throw new OpencodeApiError("Stream ended without a final response.");
  }
  return final;
}

export interface PendingPermission {
  id: string;
  session_id: string;
  permission: string;
  patterns: string[];
}

export async function listPendingPermissions(token: string): Promise<PendingPermission[]> {
  let res: Response;
  try {
    res = await fetch(`${API_BASE_URL}/opencode/permissions`, {
      headers: { Authorization: `Bearer ${token}` },
    });
  } catch {
    throw new OpencodeApiError("Could not reach the backend.");
  }
  if (!res.ok) {
    throw new OpencodeApiError(`Could not list pending permissions (HTTP ${res.status}).`);
  }
  const body = (await res.json()) as { permissions: PendingPermission[] };
  return body.permissions;
}

export async function replyPermission(
  token: string,
  permissionId: string,
  response: "once" | "always" | "reject",
): Promise<void> {
  let res: Response;
  try {
    res = await fetch(`${API_BASE_URL}/opencode/permissions/${permissionId}/reply`, {
      method: "POST",
      headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
      body: JSON.stringify({ response }),
    });
  } catch {
    throw new OpencodeApiError("Could not reach the backend.");
  }
  // A 404 means the permission was already resolved (e.g. a double-click,
  // or the poller's next tick beating this reply) — benign, not an error;
  // the next poll will already reflect that it's gone.
  if (!res.ok && res.status !== 404) {
    throw new OpencodeApiError(`Could not reply to permission (HTTP ${res.status}).`);
  }
}

export type AiTool = "opencode" | "dani-cli" | "mimocode";

export interface AiToolInfo {
  ai_tool: AiTool;
  label: string;
}

export async function getAiToolInfo(token: string): Promise<AiToolInfo> {
  let res: Response;
  try {
    res = await fetch(`${API_BASE_URL}/opencode/info`, {
      headers: { Authorization: `Bearer ${token}` },
    });
  } catch {
    throw new OpencodeApiError("Could not reach the backend.");
  }
  if (!res.ok) {
    throw new OpencodeApiError(`Could not fetch AI tool info (HTTP ${res.status}).`);
  }
  return (await res.json()) as AiToolInfo;
}

// Kills the current opencode-compatible process and starts the requested
// one on the same port — this clears the current conversation (the new
// process has no memory of the old one's session), so the caller should
// have already confirmed that with the user before calling this. A 409
// means a command is currently in flight (switching isn't allowed mid-
// turn); other failures mean the target binary wasn't found or didn't
// start, in which case the previous tool keeps running unaffected.
export async function switchAiTool(token: string, aiTool: AiTool): Promise<AiToolInfo> {
  let res: Response;
  try {
    res = await fetch(`${API_BASE_URL}/opencode/switch-tool`, {
      method: "POST",
      headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
      body: JSON.stringify({ ai_tool: aiTool }),
    });
  } catch {
    throw new OpencodeApiError("Could not reach the backend.");
  }
  if (!res.ok) {
    let detail = `HTTP ${res.status}`;
    try {
      const body = (await res.json()) as { detail?: string };
      if (body.detail) detail = body.detail;
    } catch {
      // ignore — fall back to the plain status
    }
    throw new OpencodeApiError(`Could not switch AI tool: ${detail}`);
  }
  return (await res.json()) as AiToolInfo;
}

export class AudioApiError extends Error {}

// Sends a recorded clip (whatever container MediaRecorder produced —
// typically audio/webm;codecs=opus in Chrome/Edge) to the backend, which
// transcodes it to 16kHz mono WAV via ffmpeg and runs it through a local
// whisper.cpp model. Returns the complete transcript once the whole clip
// has been processed — no partial/live transcription.
export async function transcribeAudio(token: string, blob: Blob): Promise<string> {
  const form = new FormData();
  form.append("file", blob, "recording.webm");

  let res: Response;
  try {
    res = await fetch(`${API_BASE_URL}/audio/transcribe`, {
      method: "POST",
      headers: { Authorization: `Bearer ${token}` },
      body: form,
    });
  } catch {
    throw new AudioApiError("Could not reach the backend.");
  }
  if (!res.ok) {
    throw new AudioApiError(`Transcription failed (HTTP ${res.status}).`);
  }
  const body = (await res.json()) as { text: string };
  return body.text;
}

// Requests spoken audio for a complete piece of text (typically an
// assistant message's final, authoritative text — not fired per streamed
// text delta, see PROGRESS.md), delivered as a sequence of sentence-level
// audio chunks over SSE rather than one blocking call for the whole
// response — the backend synthesizes and sends each chunk as soon as
// it's ready, so playback can start on the first sentence without
// waiting for the rest. `onChunk` fires once per chunk, in order, with a
// playable object URL the caller is responsible for revoking eventually
// (URL.revokeObjectURL). Resolves once the stream reports done.
export async function streamSpeakText(
  token: string,
  text: string,
  onChunk: (index: number, total: number, url: string) => void,
): Promise<void> {
  let res: Response;
  try {
    res = await fetch(`${API_BASE_URL}/audio/speak/stream`, {
      method: "POST",
      headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
      body: JSON.stringify({ text }),
    });
  } catch {
    throw new AudioApiError("Could not reach the backend.");
  }
  if (!res.ok || !res.body) {
    throw new AudioApiError(`Speech synthesis failed (HTTP ${res.status}).`);
  }

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let sawDone = false;

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const frames = buffer.split("\n\n");
    buffer = frames.pop() ?? "";
    for (const frame of frames) {
      const line = frame.split("\n").find((l) => l.startsWith("data:"));
      if (!line) continue;
      const event = JSON.parse(line.slice("data:".length).trim()) as
        | { type: "start"; total: number }
        | { type: "chunk"; index: number; total: number; content_type: string; audio_base64: string }
        | { type: "done" }
        | { type: "error"; message: string };
      if (event.type === "chunk") {
        onChunk(event.index, event.total, base64ToObjectUrl(event.audio_base64, event.content_type));
      } else if (event.type === "error") {
        throw new AudioApiError(event.message);
      } else if (event.type === "done") {
        sawDone = true;
      }
    }
  }

  if (!sawDone) {
    throw new AudioApiError("Stream ended without completing.");
  }
}
