"use client";

import { useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import {
  ArrowUpIcon,
  BluetoothIcon,
  BotIcon,
  ChevronDownIcon,
  InfoIcon,
  Loader2Icon,
  LogOutIcon,
  MicIcon,
  MoreVerticalIcon,
  PauseIcon,
  PlusIcon,
  ShieldAlertIcon,
  SquareIcon,
  Volume2Icon,
  WrenchIcon,
} from "lucide-react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import {
  verifySession,
  refreshSession,
  streamOpencodeCommand,
  listPendingPermissions,
  replyPermission,
  streamSpeakText,
  getAiToolInfo,
  switchAiTool,
  OpencodeApiError,
  AudioApiError,
  type SessionVerifyResponse,
  type PendingPermission,
  type AiTool,
  type AiToolInfo,
} from "@/lib/api";
import { saveSession, loadSession, clearSession } from "@/lib/session";
import { cn } from "@/lib/utils";
import { MicCapture, isMicCaptureAvailable } from "@/lib/mic-capture";
import { SttClient, isSttClientAvailable } from "@/lib/stt-client";
import { AudioPlaybackQueue } from "@/lib/audio-playback-queue";
import { EspProvisioning } from "@/components/esp-provisioning";
import { Button } from "@/components/ui/button";
import {
  MessageScrollerProvider,
  MessageScroller,
  MessageScrollerViewport,
  MessageScrollerContent,
  MessageScrollerItem,
  MessageScrollerButton,
} from "@/components/ui/message-scroller";
import { Message, MessageContent } from "@/components/ui/message";
import { Bubble, BubbleContent } from "@/components/ui/bubble";
import { Marker, MarkerIcon, MarkerContent } from "@/components/ui/marker";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
} from "@/components/ui/dialog";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";

type ChatEntry = {
  id: string;
  role: "user" | "assistant" | "error" | "system" | "tool";
  text: string;
  tool?: string;
  toolSuccess?: boolean;
};

// "idle": nothing happening.
// "pending": pointerdown just fired, capture+STT are starting/started,
//   but a threshold timer is still running to decide tap vs hold.
// "hold": the timer fired while still pressed — a confirmed hold;
//   release now finalizes and auto-sends (today's original behavior).
// "open": the pointer released before the timer fired — a confirmed
//   tap; capture keeps running with no auto-send. A second tap (not a
//   release) stops it; Send/Enter also stops it first, then sends.
// "finalizing": between "stop capturing" and Deepgram's finalize
//   handshake completing.
type MicPhase = "idle" | "pending" | "hold" | "open" | "finalizing";

// opencode's response `parts` are intentionally left as `unknown[]` in the
// API layer (this driver doesn't render them beyond the flattened `text`
// today) — narrowed locally to just the shape needed here.
type OpencodePart = {
  type?: string;
  tool?: string;
  state?: { status?: string; error?: string };
};

// A turn can legitimately end with no text part at all — e.g. when a tool
// call is rejected, opencode's step ends right after the tool call with no
// further explanation from the model. `result.text` is then genuinely an
// empty string, not a bug; render something meaningful instead of a blank
// bubble.
function describeEmptyResponse(parts: unknown[]): string {
  const rejected = (parts as OpencodePart[]).find(
    (p) => p.type === "tool" && p.state?.status === "error" && /rejected/i.test(p.state?.error ?? ""),
  );
  if (rejected) return `Tool call rejected: ${rejected.tool ?? "unknown tool"}`;
  return "(no response)";
}

export default function ChatPage() {
  const router = useRouter();
  const [session, setSession] = useState<SessionVerifyResponse | null>(null);
  const [checkingSession, setCheckingSession] = useState(true);

  const [entries, setEntries] = useState<ChatEntry[]>([]);
  const [commandText, setCommandText] = useState("");
  const [commandPending, setCommandPending] = useState(false);
  // The assistant entry currently receiving stream deltas, if any — while
  // an entry is still streaming, its text is necessarily incomplete
  // (e.g. mid-way through a slow tool call like a web search), so Listen
  // must not be usable on it yet. See PROGRESS.md: clicking Listen before
  // a turn finished was captured as "only reads content before the web
  // search" — not a delta-relay bug, just a missing guard.
  const [streamingEntryId, setStreamingEntryId] = useState<string | null>(null);
  const [pendingPermissions, setPendingPermissions] = useState<PendingPermission[]>([]);
  const [permissionReplyInFlight, setPermissionReplyInFlight] = useState<Set<string>>(new Set());
  const [provisionOpen, setProvisionOpen] = useState(false);

  const [aiToolInfo, setAiToolInfo] = useState<AiToolInfo | null>(null);
  // The tool the user picked from the menu, pending confirmation — not
  // yet switched to. Switching destroys the current conversation (the
  // new process has no memory of the old one's session), so this is a
  // two-step confirm rather than an immediate action.
  const [switchTarget, setSwitchTarget] = useState<AiTool | null>(null);
  const [switching, setSwitching] = useState(false);
  const [switchError, setSwitchError] = useState<string | null>(null);

  const [speakingId, setSpeakingId] = useState<string | null>(null);
  // "__live__" while the live per-turn audio_chunk stream (Stage 4) is
  // the active playback; a real entry id while a manual Listen replay is
  // active; null when idle. Only one of these plays at a time — pressing
  // the hold-to-talk button or starting a new turn always interrupts
  // whichever is currently active (see handleMicPointerDown/sendCommand).
  const [playingId, setPlayingId] = useState<string | null>(null);
  // The single AudioPlaybackQueue currently producing sound, whichever
  // one it is (live turn or a specific message's replay) — only one
  // instance is ever "active" at a time, mirroring playingId above.
  const playbackQueueRef = useRef<AudioPlaybackQueue | null>(null);
  // Which id "owns" playbackQueueRef.current — "__live__", an entry id,
  // or null. Deliberately separate from playingId: playingId reflects
  // "audibly playing right now" (cleared on pause, for the UI), while
  // this ref keeps pointing at the same owner across a pause so a
  // second click resumes in place instead of restarting from scratch,
  // and so a still-in-flight generation the user has switched away from
  // can tell it's been abandoned and stop pushing audible chunks into a
  // queue nobody's listening to anymore (it still populates the cache
  // in the background either way).
  const activeQueueOwnerRef = useRef<string | null>(null);
  // Per-entry TTS chunk cache for the manual Listen button — object URLs
  // accumulate here as they stream in from streamSpeakText and double as
  // a replay cache: clicking Listen again on an already-fully-generated
  // message replays instantly via AudioPlaybackQueue.replay() instead of
  // re-hitting the backend. Deliberately separate from the live per-turn
  // queue's URLs (which are never cached — see stop() below) since
  // revoking a blob URL invalidates it everywhere that string is held.
  const ttsCacheRef = useRef<Record<string, string[]>>({});
  const ttsCacheCompleteRef = useRef<Record<string, boolean>>({});

  // Mic capture state machine (see MicPhase above). micStateRef is the
  // synchronous source of truth used for control-flow guards inside the
  // pointer handlers — React state updates are async/batched, and
  // pointerup can genuinely fire while pointerdown's own async setup
  // (STT connect + mic capture start) is still in flight, so control
  // flow can't safely depend on a re-render having happened yet.
  // micState mirrors it purely for rendering; setMicPhase keeps both in
  // sync in one place.
  const [micState, setMicState] = useState<MicPhase>("idle");
  const micStateRef = useRef<MicPhase>("idle");
  function setMicPhase(next: MicPhase) {
    micStateRef.current = next;
    setMicState(next);
  }

  // Live energy-VAD speech flag from the capture worklet — drives the mic
  // button's visual state (de-presses on silence) and the open-mode
  // auto-stop timer. Only meaningful while a capture is running.
  const [micSpeaking, setMicSpeaking] = useState(false);

  const micCaptureRef = useRef<MicCapture | null>(null);
  const sttClientRef = useRef<SttClient | null>(null);
  // The transcript actually sent/kept — kept in a ref, not just React
  // state, because release handling needs the value that was current
  // the instant Deepgram's finalize handshake resolved, not whatever
  // `commandText` closure was captured when the handler was attached (a
  // real stale-closure risk otherwise, since setCommandText updates land
  // asynchronously relative to the awaited finalize()).
  const finalTranscriptRef = useRef("");
  // performance.now() at press, and a one-shot guard so the
  // press -> first-PCM-frame-sent latency (an explicit Stage 5
  // verification requirement, not just an open risk to eyeball) is
  // logged exactly once per press, not once per captured frame.
  const holdStartRef = useRef(0);
  const firstFrameLoggedRef = useRef(false);
  // Set once handleMicPointerDown's own STT-connect + capture-start
  // setup has actually finished. If the pointer releases before that —
  // a real possibility for a fast tap, since the setup itself takes real
  // time (WebSocket handshake, AudioWorklet init) — handleMicPointerUp
  // can't yet safely touch capture/stt refs the setup hasn't finished
  // populating; it records the intended outcome here instead, and
  // pointerdown's own continuation applies it once setup completes.
  const setupCompleteRef = useRef(false);
  const deferredReleaseRef = useRef<"discard" | "open" | null>(null);
  const pendingTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  // Auto-stop timer for open (tap-to-toggle) mode — armed when the VAD
  // reports silence, fired when the user has stayed silent long enough
  // (VAD_AUTO_STOP_MS) to count as "done talking".
  const vadSilenceTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  // /chat requires an already-established session — this isn't a
  // provisioning path, just a guard for someone reaching this route
  // directly without one (or a session that expired since it was last
  // checked).
  useEffect(() => {
    let cancelled = false;
    const stored = loadSession();
    if (!stored) {
      router.push("/");
      return;
    }
    verifySession(stored.access_token)
      .then((result) => {
        if (!cancelled) {
          setSession(result);
          setCheckingSession(false);
        }
      })
      .catch(() => {
        clearSession();
        if (!cancelled) router.push("/");
      });
    return () => {
      cancelled = true;
    };
  }, [router]);

  // Fetch which AI tool is currently active once we have a session — no
  // polling needed, this only changes as a direct result of a switch
  // this same page initiates (see handleConfirmSwitch).
  useEffect(() => {
    if (!session) return;
    const stored = loadSession();
    if (!stored) return;
    let cancelled = false;
    getAiToolInfo(stored.access_token)
      .then((info) => {
        if (!cancelled) setAiToolInfo(info);
      })
      .catch(() => {
        // Non-critical — the switcher just won't show a current
        // selection until the next successful fetch.
      });
    return () => {
      cancelled = true;
    };
  }, [session]);

  // Schedule automatic token refresh at 50% of session TTL — moved here
  // from the root page since /chat is now where a session actually lives
  // for any length of time.
  useEffect(() => {
    if (!session) return;

    const expiresAt = new Date(session.expires_at).getTime();
    const now = Date.now();
    const ttl = expiresAt - now;
    const refreshIn = Math.max(ttl * 0.5, 10_000);

    const timer = setTimeout(async () => {
      const stored = loadSession();
      if (!stored) return;
      try {
        const result = await refreshSession(stored.access_token);
        saveSession({ access_token: result.access_token, expires_at: result.expires_at });
        setSession((prev) => (prev ? { ...prev, expires_at: result.expires_at } : prev));
      } catch {
        clearSession();
        router.push("/");
      }
    }, refreshIn);

    return () => clearTimeout(timer);
  }, [session, router]);

  // A pending permission can only exist as a side effect of an in-flight
  // /opencode/command call (that call blocks server-side until every
  // permission it raises is answered), so polling only while a command
  // is pending is sufficient — for permissions raised by a command *this
  // page instance* sent.
  useEffect(() => {
    if (!commandPending) return;
    const stored = loadSession();
    if (!stored) return;

    const poll = () => {
      listPendingPermissions(stored.access_token)
        .then(setPendingPermissions)
        .catch(() => {
          // Transient poll failure — leave the last known list as-is and
          // try again on the next tick.
        });
    };
    poll();
    const interval = setInterval(poll, 1500);
    return () => clearInterval(interval);
  }, [commandPending]);

  // One-time check on mount for a permission that's *already* pending
  // server-side — e.g. left over from before a page refresh.
  // `commandPending` is plain React state that resets to false on every
  // fresh mount, so without this, a genuinely-still-pending permission
  // from a prior page load became permanently invisible and
  // unanswerable: the effect above never started polling (gated on
  // commandPending), no Approve/Reject UI ever reappeared, yet the
  // backend's single global opencode session stayed blocked on it
  // indefinitely — every future command would queue up behind it and
  // receive no events at all (confirmed live; see PROGRESS.md's "Stream
  // ended without a final response" investigation). Setting
  // commandPending here both surfaces the permission immediately and
  // hands off to the effect above for continued polling.
  useEffect(() => {
    if (!session) return;
    const stored = loadSession();
    if (!stored) return;
    listPendingPermissions(stored.access_token)
      .then((perms) => {
        if (perms.length > 0) {
          setPendingPermissions(perms);
          setCommandPending(true);
        }
      })
      .catch(() => {
        // Non-critical — if this happens to fail, the situation it's
        // meant to catch (an orphaned permission) is already rare and a
        // manually-sent command will still surface the same 409 message.
      });
  }, [session]);

  // Stop any playing audio, close any in-progress mic capture, and
  // revoke cached TTS chunk object URLs on unmount.
  useEffect(() => {
    const cache = ttsCacheRef.current;
    return () => {
      playbackQueueRef.current?.pause();
      if (pendingTimerRef.current) clearTimeout(pendingTimerRef.current);
      if (vadSilenceTimerRef.current) clearTimeout(vadSilenceTimerRef.current);
      micCaptureRef.current?.stop();
      sttClientRef.current?.close();
      Object.values(cache).forEach((urls) => urls.forEach((url) => url && URL.revokeObjectURL(url)));
    };
  }, []);

  async function sendCommand(overrideText?: string) {
    const stored = loadSession();
    const text = overrideText ?? commandText;
    if (!stored || text.trim().length === 0) return;

    // A new turn always takes over playback — see interruptPlayback's
    // doc comment for why a live queue gets hard-stopped while a manual
    // Listen replay's cache is preserved instead.
    interruptPlayback();
    setSpeakingId(null);

    const userEntry: ChatEntry = { id: crypto.randomUUID(), role: "user", text };
    const assistantId = crypto.randomUUID();
    setEntries((prev) => [...prev, userEntry, { id: assistantId, role: "assistant", text: "" }]);
    setCommandText("");
    setCommandPending(true);
    setStreamingEntryId(assistantId);

    // Fresh queue for this turn's inline audio — both the assistant's
    // own answer (source: "answer") and conversational tool narration
    // (source: "tool", including failures — see groq_summarizer.py)
    // arrive as audio_chunk events interleaved with the text deltas
    // themselves, not a separate call kicked off after the turn ends.
    const liveQueue = new AudioPlaybackQueue();
    liveQueue.setOnIdle(() => setPlayingId((prev) => (prev === "__live__" ? null : prev)));
    playbackQueueRef.current = liveQueue;
    activeQueueOwnerRef.current = "__live__";

    try {
      const result = await streamOpencodeCommand(
        stored.access_token,
        userEntry.text,
        (chunk) => {
          setEntries((prev) =>
            prev.map((e) => (e.id === assistantId ? { ...e, text: e.text + chunk } : e)),
          );
        },
        (tool, summary, success) => {
          // Inserted just before the assistant's (still-growing) bubble,
          // not appended at the end — a tool summary describes something
          // that happened *during* generation, so it belongs above the
          // final answer, not visually implying it came after.
          setEntries((prev) => {
            const idx = prev.findIndex((e) => e.id === assistantId);
            const toolEntry: ChatEntry = {
              id: crypto.randomUUID(),
              role: "tool",
              tool,
              text: summary,
              toolSuccess: success,
            };
            if (idx === -1) return [...prev, toolEntry];
            return [...prev.slice(0, idx), toolEntry, ...prev.slice(idx)];
          });
        },
        (_text, url) => {
          // Guard against a stale callback outliving its turn (e.g. the
          // user pressed hold-to-talk mid-stream, interrupting this
          // queue) — never push audible chunks into a queue nobody's
          // listening to anymore.
          if (playbackQueueRef.current !== liveQueue) return;
          liveQueue.push(url);
          setPlayingId("__live__");
        },
      );
      // A turn can legitimately stream no text at all (e.g. a rejected
      // tool call ends the step with no further explanation from the
      // model) — replace the still-empty placeholder with a meaningful
      // marker instead of leaving a blank message. Any tool-narration
      // audio already played live regardless, via the onAudioChunk
      // callback above — nothing further to trigger here either way.
      if (result.text.trim().length === 0) {
        setEntries((prev) =>
          prev.map((e) =>
            e.id === assistantId
              ? { ...e, role: "system", text: describeEmptyResponse(result.parts) }
              : e,
          ),
        );
      }
    } catch (err) {
      const message =
        err instanceof OpencodeApiError ? err.message : "Something went wrong — try again.";
      // Keep whatever text streamed in before the error; only drop the
      // placeholder if nothing ever arrived.
      setEntries((prev) => [
        ...prev.filter((e) => e.id !== assistantId || e.text.length > 0),
        { id: crypto.randomUUID(), role: "error", text: message },
      ]);
    } finally {
      setCommandPending(false);
      setPendingPermissions([]);
      setStreamingEntryId(null);
    }
  }

  function handleSendCommand(e: React.FormEvent) {
    e.preventDefault();
    void sendWithOpenCaptureStopped();
  }

  async function handlePermissionReply(id: string, response: "once" | "reject") {
    const stored = loadSession();
    if (!stored) return;
    setPermissionReplyInFlight((prev) => new Set(prev).add(id));
    try {
      await replyPermission(stored.access_token, id, response);
      setPendingPermissions((prev) => prev.filter((p) => p.id !== id));
    } finally {
      setPermissionReplyInFlight((prev) => {
        const next = new Set(prev);
        next.delete(id);
        return next;
      });
    }
  }

  function handleDisconnect() {
    clearSession();
    router.push("/");
  }

  async function handleConfirmSwitch() {
    if (!switchTarget) return;
    const stored = loadSession();
    if (!stored) return;
    setSwitching(true);
    setSwitchError(null);
    try {
      const info = await switchAiTool(stored.access_token, switchTarget);
      setAiToolInfo(info);
      // The new process has no memory of the old one's conversation —
      // clear it rather than leave a chat history that no longer
      // corresponds to anything the backend actually remembers.
      setEntries([
        {
          id: crypto.randomUUID(),
          role: "system",
          text: `Switched AI tool to ${info.label}. The conversation was reset.`,
        },
      ]);
      setSwitchTarget(null);
    } catch (err) {
      setSwitchError(err instanceof OpencodeApiError ? err.message : "Could not switch AI tool.");
    } finally {
      setSwitching(false);
    }
  }

  // Below this, release is genuinely accidental (today's original
  // behavior) — discard silently. Between this and TAP_HOLD_THRESHOLD_MS,
  // a release is a deliberate tap that opens continuous recording. Past
  // the threshold while still pressed, it's a confirmed hold.
  const ACCIDENTAL_TAP_MS = 100;
  const TAP_HOLD_THRESHOLD_MS = 350;
  // Sustained silence (energy-VAD) in open mode before auto-stop. Mirrors
  // the ~700ms figure from VOICE_ARCHITECTURE.md's VAD section.
  const VAD_AUTO_STOP_MS = 700;

  // Deterministic, explicit interrupt: pressing the mic always stops
  // whatever's currently playing before capture starts, whether that's
  // the live per-turn audio or a manual Listen replay. This is what
  // replaces open-mic barge-in — since the mic is only ever active while
  // physically held or explicitly opened, and a press always silences
  // playback first, there's no window where capture and playback can be
  // active together, so the echo-feedback risk flagged in the original
  // plan can't occur at all.
  function interruptPlayback() {
    if (activeQueueOwnerRef.current === "__live__") playbackQueueRef.current?.stop();
    else playbackQueueRef.current?.pause();
    activeQueueOwnerRef.current = null;
    setPlayingId(null);
  }

  // VAD-driven behavior for the running capture. Speech going true
  // always cancels any pending auto-stop; speech going false only arms
  // one while in "open" mode — in "hold" mode the physical release is
  // what governs, so a mid-thought pause must never auto-send. On fire
  // the recording auto-finalizes (transcript stays in the composer, no
  // auto-send) — the tap-to-toggle stop action, hands-free.
  function handleSpeechChange(speech: boolean) {
    setMicSpeaking(speech);
    if (vadSilenceTimerRef.current) {
      clearTimeout(vadSilenceTimerRef.current);
      vadSilenceTimerRef.current = null;
    }
    if (speech) return;
    if (micStateRef.current !== "open") return;
    vadSilenceTimerRef.current = setTimeout(() => {
      vadSilenceTimerRef.current = null;
      if (micStateRef.current !== "open") return;
      setMicPhase("finalizing");
      void finalizeCapture().then(() => setMicPhase("idle"));
    }, VAD_AUTO_STOP_MS);
  }

  // Stops capture/STT without sending — used for a genuinely accidental
  // release (under ACCIDENTAL_TAP_MS) and doesn't need to wait for
  // Deepgram's finalize handshake, since nothing captured is going to be
  // used.
  function stopCaptureSilently(): void {
    if (vadSilenceTimerRef.current) {
      clearTimeout(vadSilenceTimerRef.current);
      vadSilenceTimerRef.current = null;
    }
    const capture = micCaptureRef.current;
    const stt = sttClientRef.current;
    micCaptureRef.current = null;
    sttClientRef.current = null;
    capture?.stop();
    stt?.close();
    finalTranscriptRef.current = "";
    setCommandText("");
    setMicSpeaking(false);
  }

  // Stops capture and waits for Deepgram's finalize handshake, returning
  // the transcript that was actually captured. Used by both the
  // confirmed-hold release path (auto-send) and the open-mode second-tap
  // stop path (no send).
  async function finalizeCapture(): Promise<string> {
    if (vadSilenceTimerRef.current) {
      clearTimeout(vadSilenceTimerRef.current);
      vadSilenceTimerRef.current = null;
    }
    const capture = micCaptureRef.current;
    const stt = sttClientRef.current;
    micCaptureRef.current = null;
    sttClientRef.current = null;
    capture?.stop();
    await stt?.finalize();
    setMicSpeaking(false);
    return finalTranscriptRef.current.trim();
  }

  async function handleMicPointerDown(e: React.PointerEvent) {
    e.preventDefault();

    if (micStateRef.current === "open") {
      // A second tap while a tap-opened recording is running stops it —
      // without sending, per spec. The composer keeps whatever text was
      // captured for manual review/edit/Send.
      setMicPhase("finalizing");
      await finalizeCapture();
      setMicPhase("idle");
      return;
    }
    if (micStateRef.current !== "idle" || commandPending) return;

    const stored = loadSession();
    if (!stored) return;

    interruptPlayback();

    setMicPhase("pending");
    setupCompleteRef.current = false;
    deferredReleaseRef.current = null;
    setMicSpeaking(false);
    // This runs only inside a real pointer-event callback, never during
    // render; the purity rule's static analysis can't trace that
    // through the async/Promise indirection, but there's no
    // render-phase call here.
    // eslint-disable-next-line react-hooks/purity
    holdStartRef.current = performance.now();
    firstFrameLoggedRef.current = false;
    finalTranscriptRef.current = "";

    // Starts ticking from the moment of physical press, not from
    // whenever the async setup below happens to finish — matches how a
    // real hold/tap gesture is judged. Guarded on deferredReleaseRef
    // too: if the pointer already released while setup was still in
    // flight (recorded as a deferred outcome, not yet applied to
    // micState), the physical gesture is already over — the timer
    // firing afterward must not flip the phase to "hold" out from under
    // that, or the deferred resolution below would apply a stale
    // decision to a state that's since moved on.
    pendingTimerRef.current = setTimeout(() => {
      pendingTimerRef.current = null;
      if (micStateRef.current === "pending" && deferredReleaseRef.current === null) {
        setMicPhase("hold");
      }
    }, TAP_HOLD_THRESHOLD_MS);

    const capture = new MicCapture();
    const stt = new SttClient();
    micCaptureRef.current = capture;
    sttClientRef.current = stt;

    // Two distinct failure modes were previously conflated into one
    // catch-all "microphone permission denied" message — misleading
    // whenever the real cause was the STT WebSocket (backend
    // unreachable, auth rejected, CORS/origin mismatch) rather than
    // getUserMedia itself. Split so the real error surfaces.
    try {
      await stt.connect(stored.access_token, (event) => {
        if (event.text.trim().length > 0) {
          finalTranscriptRef.current = event.text;
          setCommandText(event.text);
        }
      });
      await capture.start(
        (frame) => {
          if (!firstFrameLoggedRef.current) {
            firstFrameLoggedRef.current = true;
            const latencyMs = performance.now() - holdStartRef.current;
            console.log(`[mic] press -> first PCM frame sent: ${latencyMs.toFixed(1)}ms`);
          }
          sttClientRef.current?.sendFrame(frame);
        },
        handleSpeechChange,
      );
    } catch (err) {
      console.error("[mic] setup failed", err);
      if (pendingTimerRef.current) {
        clearTimeout(pendingTimerRef.current);
        pendingTimerRef.current = null;
      }
      deferredReleaseRef.current = null;
      capture.stop();
      stt.close();
      micCaptureRef.current = null;
      sttClientRef.current = null;
      setMicPhase("idle");
      const message =
        err instanceof Error && err.message.length > 0
          ? err.message
          : "Microphone permission denied or unavailable.";
      setEntries((prev) => [...prev, { id: crypto.randomUUID(), role: "error", text: message }]);
      return;
    }

    setupCompleteRef.current = true;
    // The pointer may have already released while the setup above was
    // still in flight — handleMicPointerUp couldn't safely act on
    // capture/stt refs that didn't exist yet, so it left its intended
    // outcome here instead. Apply it now.
    const deferred = deferredReleaseRef.current;
    if (deferred) {
      deferredReleaseRef.current = null;
      if (pendingTimerRef.current) {
        clearTimeout(pendingTimerRef.current);
        pendingTimerRef.current = null;
      }
      if (deferred === "discard") {
        stopCaptureSilently();
        setMicPhase("idle");
      } else {
        setMicPhase("open");
      }
    }
  }

  // Bound to onPointerUp AND onPointerLeave/onPointerCancel — a dragged-
  // off finger or a lost window focus must still release the mic, or it
  // stays stuck "on". Only meaningful during "pending"/"hold" — "open"
  // mode's stop action is a second tap (see handleMicPointerDown), not a
  // release, since the user needs to be able to move the pointer away to
  // edit the composer or hit Send without stopping the recording.
  async function handleMicPointerUp() {
    const phase = micStateRef.current;

    if (phase === "pending") {
      // eslint-disable-next-line react-hooks/purity
      const heldMs = performance.now() - holdStartRef.current;
      const outcome: "discard" | "open" = heldMs < ACCIDENTAL_TAP_MS ? "discard" : "open";

      if (!setupCompleteRef.current) {
        // Setup (STT connect + mic capture start) hasn't finished yet —
        // acting on capture/stt refs now would race with
        // handleMicPointerDown's own in-flight awaits. Defer; its
        // continuation applies this once setup actually completes.
        deferredReleaseRef.current = outcome;
        return;
      }

      if (pendingTimerRef.current) {
        clearTimeout(pendingTimerRef.current);
        pendingTimerRef.current = null;
      }
      if (outcome === "discard") {
        stopCaptureSilently();
        setMicPhase("idle");
      } else {
        setMicPhase("open");
      }
      return;
    }

    if (phase === "hold") {
      setMicPhase("finalizing");
      const text = await finalizeCapture();
      setMicPhase("idle");
      if (text.length === 0) return;
      if (commandPending) {
        setEntries((prev) => [
          ...prev,
          {
            id: crypto.randomUUID(),
            role: "error",
            text: "Still waiting on the previous command — try again once it finishes.",
          },
        ]);
        return;
      }
      void sendCommand(text);
      return;
    }

    // "idle" | "open" | "finalizing" — no-op.
  }

  // Send/Enter while a tap-opened recording is still running would
  // otherwise leave the mic capturing in the background for whatever the
  // user says *next*, overwriting the composer while the just-sent turn
  // is still processing — stop/finalize it first (silently, the
  // recording itself is never sent), then send whatever was in the
  // composer at that moment.
  async function sendWithOpenCaptureStopped(overrideText?: string) {
    if (micStateRef.current === "open") {
      setMicPhase("finalizing");
      await finalizeCapture();
      setMicPhase("idle");
    }
    void sendCommand(overrideText);
  }

  async function handleSpeak(entry: ChatEntry) {
    // Already the active queue's owner — toggle pause/resume in place
    // rather than restarting. Checked via activeQueueOwnerRef, not
    // playingId: pausing clears playingId (for the UI) but deliberately
    // leaves ownership alone, so a second click resumes rather than
    // treating the paused entry as "a different message."
    if (activeQueueOwnerRef.current === entry.id) {
      const queue = playbackQueueRef.current;
      if (playingId === entry.id) {
        queue?.pause();
        setPlayingId(null);
        return;
      }
      if (speakingId === entry.id) return; // still generating, nothing to toggle yet
      queue?.resume();
      setPlayingId(entry.id);
      return;
    }

    // A different message (or the live turn) is active — pause it
    // without destroying anything (see interruptPlayback's doc comment
    // on why hard-stop isn't safe for a cached manual replay).
    interruptPlayback();
    setSpeakingId(null);

    const cachedUrls = ttsCacheRef.current[entry.id];
    if (ttsCacheCompleteRef.current[entry.id] && cachedUrls) {
      // Fully cached from an earlier generation — replay from the start
      // without hitting the backend again.
      const replayQueue = new AudioPlaybackQueue();
      replayQueue.setOnIdle(() => setPlayingId((prev) => (prev === entry.id ? null : prev)));
      playbackQueueRef.current = replayQueue;
      activeQueueOwnerRef.current = entry.id;
      replayQueue.replay(cachedUrls);
      setPlayingId(entry.id);
      return;
    }

    const stored = loadSession();
    if (!stored) return;
    ttsCacheRef.current[entry.id] = [];
    ttsCacheCompleteRef.current[entry.id] = false;
    setSpeakingId(entry.id);
    const freshQueue = new AudioPlaybackQueue();
    freshQueue.setOnIdle(() => setPlayingId((prev) => (prev === entry.id ? null : prev)));
    playbackQueueRef.current = freshQueue;
    activeQueueOwnerRef.current = entry.id;
    try {
      await streamSpeakText(stored.access_token, entry.text, (index, _total, url) => {
        // Always cache, even if the user has since switched away — a
        // future click can still replay instantly. Only push into the
        // queue (and touch playingId) while it's still the one actually
        // being listened to, or an abandoned generation would start
        // audibly playing on top of whatever the user switched to.
        ttsCacheRef.current[entry.id]?.push(url);
        if (playbackQueueRef.current !== freshQueue) return;
        freshQueue.push(url);
        if (index === 0) {
          setSpeakingId((prev) => (prev === entry.id ? null : prev));
          setPlayingId(entry.id);
        }
      });
      ttsCacheCompleteRef.current[entry.id] = true;
    } catch (err) {
      const message = err instanceof AudioApiError ? err.message : "Could not generate speech.";
      setEntries((prev) => [...prev, { id: crypto.randomUUID(), role: "error", text: message }]);
      setSpeakingId((prev) => (prev === entry.id ? null : prev));
    }
  }

  // Shared between assistant messages and tool-summary Markers — both can
  // be read aloud via the same streamed-chunk pipeline, so this is the
  // one place that button is defined.
  function renderListenControl(entry: ChatEntry) {
    const stillStreaming = entry.id === streamingEntryId;
    const isSpeaking = speakingId === entry.id;
    const isPlaying = playingId === entry.id;
    return (
      <div className="mt-2 flex flex-col gap-1">
        <Button
          type="button"
          variant="outline"
          size="sm"
          className="h-9 w-fit rounded-full px-3 text-muted-foreground"
          disabled={isSpeaking || stillStreaming}
          onClick={() => handleSpeak(entry)}
          title={stillStreaming ? "Wait for the response to finish" : undefined}
        >
          {isSpeaking ? (
            <Loader2Icon className="size-4 animate-spin" />
          ) : isPlaying ? (
            <PauseIcon className="size-4" />
          ) : (
            <Volume2Icon className="size-4" />
          )}
          <span>
            {isSpeaking
              ? "Generating…"
              : isPlaying
                ? "Pause"
                : stillStreaming
                  ? "Listen (waiting…)"
                  : "Listen"}
          </span>
        </Button>
      </div>
    );
  }

  if (checkingSession || !session) {
    return (
      <div className="flex h-dvh flex-1 items-center justify-center bg-background">
        <p className="text-lg text-muted-foreground">Checking session…</p>
      </div>
    );
  }

  return (
    <div className="flex h-dvh flex-col bg-background font-sans">
      <header className="flex items-center justify-between gap-2 border-b border-border px-4 py-3 sm:px-6">
        <div className="flex min-w-0 flex-col">
          <p className="truncate text-lg font-bold text-foreground">Dani Voice</p>
          <p className="truncate text-xs text-muted-foreground">
            Expires {new Date(session.expires_at).toLocaleString()}
          </p>
        </div>
        {/* AI-tool switcher — always visible (not folded into the mobile
            overflow menu below) since it's meaningful status, not just an
            action. Picking a different tool doesn't switch immediately —
            see the confirmation dialog below, since switching destroys
            the current conversation. */}
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <Button variant="outline" size="sm" className="shrink-0" title="Switch AI tool">
              <BotIcon />
              <span className="hidden sm:inline">{aiToolInfo?.label ?? "AI tool"}</span>
              <ChevronDownIcon className="size-3.5" />
            </Button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end">
            {(["opencode", "dani-cli", "mimocode"] as const).map((tool) => (
              <DropdownMenuItem
                key={tool}
                disabled={aiToolInfo?.ai_tool === tool}
                onClick={() => setSwitchTarget(tool)}
              >
                <span className="flex-1">{tool}</span>
                {aiToolInfo?.ai_tool === tool && (
                  <span className="text-xs text-muted-foreground">current</span>
                )}
              </DropdownMenuItem>
            ))}
          </DropdownMenuContent>
        </DropdownMenu>
        {/* Full buttons on wider screens; collapsed into one overflow menu
            on mobile where two adjacent icon buttons were cramped and easy
            to mis-tap. */}
        <div className="hidden shrink-0 items-center gap-2 sm:flex">
          <Button variant="outline" size="sm" onClick={() => setProvisionOpen(true)}>
            <BluetoothIcon />
            <span>Provision ESP32</span>
          </Button>
          <Button variant="outline" size="sm" onClick={handleDisconnect}>
            <LogOutIcon />
            <span>Disconnect</span>
          </Button>
        </div>
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <Button
              variant="outline"
              size="icon"
              className="size-11 shrink-0 sm:hidden"
              title="Menu"
            >
              <MoreVerticalIcon />
            </Button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end">
            <DropdownMenuItem onClick={() => setProvisionOpen(true)}>
              <BluetoothIcon />
              Provision ESP32
            </DropdownMenuItem>
            <DropdownMenuItem onClick={handleDisconnect}>
              <LogOutIcon />
              Disconnect
            </DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>
      </header>

      <MessageScrollerProvider autoScroll>
      <MessageScroller className="min-h-0 flex-1">
        <MessageScrollerViewport>
          <MessageScrollerContent className="mx-auto w-full max-w-2xl justify-end px-4 py-6 sm:px-6">
            {entries.length === 0 && (
              <p className="text-center text-sm text-muted-foreground">
                Send a command to opencode to get started.
              </p>
            )}
            {entries.map((entry) => (
              <MessageScrollerItem
                key={entry.id}
                messageId={entry.id}
                scrollAnchor={entry.role === "user"}
              >
                {entry.role === "error" || entry.role === "system" ? (
                  <Marker variant="border">
                    <MarkerIcon>
                      {entry.role === "error" ? <ShieldAlertIcon /> : <InfoIcon />}
                    </MarkerIcon>
                    <MarkerContent
                      className={entry.role === "error" ? "text-red-600 dark:text-red-400" : undefined}
                    >
                      {entry.text}
                    </MarkerContent>
                  </Marker>
                ) : entry.role === "tool" ? (
                  // Live, one-sentence, first-person narration of a
                  // completed/errored tool call (see backend's
                  // groq_summarizer.py) — surfaced the same way permission
                  // requests already are, inline in the message stream at
                  // the point the tool call actually happened, not just
                  // retroactively. Auto-plays during generation for both
                  // successes and failures (see sendCommand's
                  // onAudioChunk); the manual Listen control below is
                  // just a replay of that same narration, so both
                  // outcomes get one.
                  <div className="flex flex-col gap-1">
                    <Marker variant="border">
                      <MarkerIcon>
                        <WrenchIcon />
                      </MarkerIcon>
                      <MarkerContent className="text-muted-foreground">{entry.text}</MarkerContent>
                    </Marker>
                    {renderListenControl(entry)}
                  </div>
                ) : entry.role === "user" ? (
                  <Message align="end">
                    <MessageContent>
                      <Bubble variant="default">
                        <BubbleContent className="whitespace-pre-wrap">{entry.text}</BubbleContent>
                      </Bubble>
                    </MessageContent>
                  </Message>
                ) : (
                  // Assistant responses render as plain flowing markdown,
                  // not inside a bounded bubble — they're often long/
                  // structured (lists, code, links) and don't need the
                  // width limit or background treatment a chat "bubble"
                  // implies.
                  <Message align="start">
                    <MessageContent className="max-w-none">
                      <div className="text-sm leading-relaxed text-foreground">
                        <ReactMarkdown
                          remarkPlugins={[remarkGfm]}
                          components={{
                            p: ({ children }) => <p className="mb-2 last:mb-0">{children}</p>,
                            ul: ({ children }) => (
                              <ul className="mb-2 list-disc space-y-1 pl-5 last:mb-0">{children}</ul>
                            ),
                            ol: ({ children }) => (
                              <ol className="mb-2 list-decimal space-y-1 pl-5 last:mb-0">{children}</ol>
                            ),
                            a: ({ children, href }) => (
                              <a
                                href={href}
                                target="_blank"
                                rel="noreferrer"
                                className="underline underline-offset-2 hover:text-muted-foreground"
                              >
                                {children}
                              </a>
                            ),
                            code: (props) => {
                              const { className, children } = props as {
                                className?: string;
                                children?: React.ReactNode;
                              };
                              const isBlock = className?.includes("language-");
                              return isBlock ? (
                                <code className={className}>{children}</code>
                              ) : (
                                <code className="rounded bg-muted px-1 py-0.5 font-mono text-[0.85em]">
                                  {children}
                                </code>
                              );
                            },
                            pre: ({ children }) => (
                              <pre className="mb-2 overflow-x-auto rounded-lg bg-muted p-3 text-xs last:mb-0">
                                {children}
                              </pre>
                            ),
                          }}
                        >
                          {entry.text}
                        </ReactMarkdown>
                      </div>
                      {entry.text.trim().length > 0 && renderListenControl(entry)}
                    </MessageContent>
                  </Message>
                )}
              </MessageScrollerItem>
            ))}

            {pendingPermissions.map((perm) => (
              <MessageScrollerItem key={perm.id} messageId={perm.id}>
                <Marker variant="border" className="flex-col items-stretch gap-3 rounded-xl border border-amber-500 bg-amber-50 p-3 text-amber-900 dark:border-amber-400 dark:bg-amber-950 dark:text-amber-200">
                  <div className="flex items-center gap-2">
                    <MarkerIcon>
                      <ShieldAlertIcon />
                    </MarkerIcon>
                    <MarkerContent className="font-semibold text-inherit">
                      opencode wants to use: {perm.permission}
                    </MarkerContent>
                  </div>
                  {perm.patterns.length > 0 && (
                    <p className="break-all font-mono text-xs text-inherit opacity-80">
                      {perm.patterns.join(", ")}
                    </p>
                  )}
                  <div className="flex gap-2">
                    <Button
                      size="sm"
                      variant="default"
                      className="h-10 flex-1"
                      disabled={permissionReplyInFlight.has(perm.id)}
                      onClick={() => handlePermissionReply(perm.id, "once")}
                    >
                      Approve
                    </Button>
                    <Button
                      size="sm"
                      variant="outline"
                      className="h-10 flex-1"
                      disabled={permissionReplyInFlight.has(perm.id)}
                      onClick={() => handlePermissionReply(perm.id, "reject")}
                    >
                      Reject
                    </Button>
                  </div>
                </Marker>
              </MessageScrollerItem>
            ))}

            {commandPending && (
              <p className="text-center text-sm text-muted-foreground">
                {pendingPermissions.length > 0 ? "Running… waiting on your approval above" : "Running…"}
              </p>
            )}
          </MessageScrollerContent>
        </MessageScrollerViewport>
        <MessageScrollerButton />
      </MessageScroller>
      </MessageScrollerProvider>

      <div className="border-t border-border p-3 sm:p-4">
        <form
          onSubmit={handleSendCommand}
          className="mx-auto flex w-full max-w-2xl items-end gap-1 rounded-3xl border border-border bg-muted/40 p-2 pl-4"
        >
          <textarea
            value={commandText}
            onChange={(e) => setCommandText(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                void sendWithOpenCaptureStopped();
              }
            }}
            placeholder="Message opencode…"
            rows={1}
            className="max-h-40 flex-1 resize-none self-center bg-transparent py-2 text-sm text-foreground outline-none placeholder:text-muted-foreground"
          />
          {/* Attachment placeholder — no backend support yet (no file/image
              handling exists), left present but inert per CLAUDE.md's
              scoping until there's a real use for it. */}
          <Button
            type="button"
            variant="ghost"
            size="icon"
            className="size-11 rounded-full sm:size-9"
            disabled
            title="Attachments (coming soon)"
          >
            <PlusIcon />
          </Button>
          {/* Tap vs. hold, two distinct modes (see MicPhase above):
              - Hold past the threshold, release: records only while
                held, auto-sends on release — no separate Send tap
                needed, holding and releasing is itself the confirm.
              - Quick tap (release before the threshold, past the
                genuinely-accidental floor): opens continuous recording
                that stays on for review/editing — tap the mic again to
                stop without sending, or hit Send/Enter as usual.
              onPointerLeave/onPointerCancel are bound to the same
              release handler as onPointerUp so a dragged-off finger or a
              lost window focus can't leave the mic stuck "on" — but only
              matter during pending/hold; "open" mode's stop is a second
              tap, not a release, so the pointer can move freely away to
              reach the composer/Send button. */}
          <Button
            type="button"
            variant="ghost"
            size="icon"
            className={cn(
              "size-11 touch-none rounded-full select-none sm:size-9",
              (micState === "pending" || micState === "hold") &&
                "text-red-600 dark:text-red-400 animate-pulse",
              micState === "open" && "text-red-600 dark:text-red-400",
              // In open mode the pulse tracks live VAD speech — the
              // button visually "de-presses" the moment the user stops
              // talking, even before the auto-stop timer fires.
              micState === "open" && micSpeaking && "animate-pulse",
            )}
            disabled={
              micState === "finalizing" ||
              commandPending ||
              !isMicCaptureAvailable() ||
              !isSttClientAvailable()
            }
            onPointerDown={handleMicPointerDown}
            onPointerUp={handleMicPointerUp}
            onPointerLeave={handleMicPointerUp}
            onPointerCancel={handleMicPointerUp}
            onContextMenu={(e) => e.preventDefault()}
            title={
              micState === "open"
                ? "Recording — tap to stop"
                : micState === "pending" || micState === "hold"
                  ? "Release to send"
                  : "Hold to talk, or tap to keep recording open"
            }
          >
            {micState === "finalizing" ? (
              <Loader2Icon className="animate-spin" />
            ) : micState === "open" ? (
              <SquareIcon />
            ) : (
              <MicIcon />
            )}
          </Button>
          <Button
            type="submit"
            size="icon"
            className="size-11 rounded-full sm:size-9"
            disabled={commandText.trim().length === 0 || commandPending}
            title="Send"
          >
            <ArrowUpIcon />
          </Button>
        </form>
      </div>

      <Dialog open={provisionOpen} onOpenChange={setProvisionOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Provision ESP32</DialogTitle>
            <DialogDescription>
              Pair or re-pair an ESP32 device over Bluetooth using your session PIN.
            </DialogDescription>
          </DialogHeader>
          <EspProvisioning onDone={() => setProvisionOpen(false)} />
        </DialogContent>
      </Dialog>

      <Dialog
        open={switchTarget !== null}
        onOpenChange={(open) => {
          if (!open && !switching) {
            setSwitchTarget(null);
            setSwitchError(null);
          }
        }}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Switch AI tool to {switchTarget}?</DialogTitle>
            <DialogDescription>
              This restarts the AI process and clears the current conversation — the new one has
              no memory of what you were just discussing. This can take a few seconds, during
              which the assistant is briefly unavailable.
            </DialogDescription>
          </DialogHeader>
          {switchError && <p className="text-sm text-destructive">{switchError}</p>}
          <div className="flex gap-2">
            <Button
              variant="outline"
              className="flex-1"
              disabled={switching}
              onClick={() => {
                setSwitchTarget(null);
                setSwitchError(null);
              }}
            >
              Cancel
            </Button>
            <Button className="flex-1" disabled={switching} onClick={() => void handleConfirmSwitch()}>
              {switching ? <Loader2Icon className="size-4 animate-spin" /> : null}
              {switching ? "Switching…" : "Switch"}
            </Button>
          </div>
        </DialogContent>
      </Dialog>
    </div>
  );
}
