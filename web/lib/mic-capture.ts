// Continuous raw-PCM mic capture via an AudioWorklet, replacing the old
// click-to-start/click-to-stop MediaRecorder approach in
// audio-recorder.ts. Captures at whatever sample rate the AudioContext
// actually runs at (not guaranteed to be 16kHz or even 44.1kHz across
// browsers — read back and resample from the real value, never assume
// one), and hands the caller back mono Float32 frames resampled to
// TARGET_SAMPLE_RATE. No network wiring here — see stt-client.ts for
// what consumes these frames.
const TARGET_SAMPLE_RATE = 16000;
const WORKLET_URL = "/worklets/pcm-capture-worklet.js";
const WORKLET_NAME = "pcm-capture-processor";

// Simple linear-interpolation resample — adequate for speech, not
// intended to be artifact-free. Runs per-frame (no cross-frame carry of
// fractional sample position), so there's a small, inaudible-in-practice
// discontinuity at each frame boundary rather than a perfectly continuous
// resample — acceptable for this use case.
function resampleLinear(input: Float32Array, fromRate: number, toRate: number): Float32Array {
  if (fromRate === toRate) return input;
  const outputLength = Math.round((input.length * toRate) / fromRate);
  const output = new Float32Array(outputLength);
  const ratio = (input.length - 1) / Math.max(outputLength - 1, 1);
  for (let i = 0; i < outputLength; i++) {
    const srcPos = i * ratio;
    const srcIndex = Math.floor(srcPos);
    const frac = srcPos - srcIndex;
    const a = input[srcIndex] ?? 0;
    const b = input[srcIndex + 1] ?? a;
    output[i] = a + (b - a) * frac;
  }
  return output;
}

export class MicCapture {
  private audioContext: AudioContext | null = null;
  private sourceNode: MediaStreamAudioSourceNode | null = null;
  private workletNode: AudioWorkletNode | null = null;
  private stream: MediaStream | null = null;

  async start(onFrame: (frame: Float32Array, sampleRate: number) => void): Promise<void> {
    this.stream = await navigator.mediaDevices.getUserMedia({
      audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true },
    });

    this.audioContext = new AudioContext();
    const actualSampleRate = this.audioContext.sampleRate;

    await this.audioContext.audioWorklet.addModule(WORKLET_URL);

    this.sourceNode = this.audioContext.createMediaStreamSource(this.stream);
    this.workletNode = new AudioWorkletNode(this.audioContext, WORKLET_NAME);
    this.workletNode.port.onmessage = (event: MessageEvent<Float32Array>) => {
      const resampled = resampleLinear(event.data, actualSampleRate, TARGET_SAMPLE_RATE);
      onFrame(resampled, TARGET_SAMPLE_RATE);
    };

    this.sourceNode.connect(this.workletNode);
    // Worklet output is never actually used for playback, but Chrome
    // requires an AudioWorkletNode be connected to the graph's
    // destination (even silently) for process() to keep being called
    // reliably in some versions — connecting through a zero-gain node
    // avoids audibly looping the mic back to the speakers.
    const silentGain = this.audioContext.createGain();
    silentGain.gain.value = 0;
    this.workletNode.connect(silentGain);
    silentGain.connect(this.audioContext.destination);
  }

  stop(): void {
    this.workletNode?.port.close();
    this.workletNode?.disconnect();
    this.sourceNode?.disconnect();
    this.stream?.getTracks().forEach((track) => track.stop());
    this.audioContext?.close();
    this.workletNode = null;
    this.sourceNode = null;
    this.stream = null;
    this.audioContext = null;
  }

  get isCapturing(): boolean {
    return this.audioContext !== null && this.audioContext.state !== "closed";
  }
}

export function isMicCaptureAvailable(): boolean {
  return (
    typeof navigator !== "undefined" &&
    !!navigator.mediaDevices?.getUserMedia &&
    typeof AudioContext !== "undefined" &&
    typeof AudioWorkletNode !== "undefined"
  );
}
