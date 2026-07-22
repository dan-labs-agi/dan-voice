// Replaces the old index-and-known-total playback model
// (playChunkIfReady/handleChunkArrived in chat/page.tsx), which assumed
// the total chunk count was known upfront — true for the legacy
// /audio/speak/stream endpoint, but not for the live inline audio_chunk
// stream (Stage 4), where the sentence count is only known once the
// turn finishes. push()/advance() decouple arrival from playback
// entirely: chunks get appended whenever they arrive, playback drains
// whatever's queued whenever it's free, with no "waiting for index N"
// state at all.
export class AudioPlaybackQueue {
  private queue: string[] = [];
  private playedThrough = -1;
  private current: HTMLAudioElement | null = null;
  private onIdle: (() => void) | null = null;

  push(url: string): void {
    this.queue.push(url);
    if (!this.current) this.advance();
  }

  // Bulk-replays a complete, already-known list of URLs from the start —
  // used for instant cached replay (see chat/page.tsx's Listen button:
  // clicking it again on a fully-generated message replays without
  // re-hitting the backend).
  replay(urls: string[]): void {
    this.current?.pause();
    this.current = null;
    this.queue = [...urls];
    this.playedThrough = -1;
    this.advance();
  }

  private advance(): void {
    const url = this.queue[this.playedThrough + 1];
    if (!url) {
      this.current = null;
      this.onIdle?.();
      return;
    }
    this.playedThrough += 1;
    const audio = new Audio(url);
    this.current = audio;
    audio.addEventListener("ended", () => this.advance());
    void audio.play();
  }

  pause(): void {
    this.current?.pause();
  }

  resume(): void {
    void this.current?.play();
  }

  get isActive(): boolean {
    return this.current !== null;
  }

  // Hard interrupt: halts playback immediately, drops everything still
  // queued, and revokes any object URLs that were queued but never
  // played. Only safe to call on playback with no separate cache
  // referencing those same URLs (the live per-turn queue) — a cached
  // manual-replay queue should be paused instead, never stopped, or its
  // cache breaks (revoking a blob URL invalidates it everywhere that
  // string is held, not just here). See chat/page.tsx for which is used
  // where.
  stop(): void {
    this.current?.pause();
    this.current = null;
    for (let i = this.playedThrough + 1; i < this.queue.length; i++) {
      URL.revokeObjectURL(this.queue[i]);
    }
    this.queue = [];
    this.playedThrough = -1;
  }

  setOnIdle(cb: (() => void) | null): void {
    this.onIdle = cb;
  }
}
