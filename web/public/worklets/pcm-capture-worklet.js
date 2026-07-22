// Runs in the AudioWorkletGlobalScope, loaded via
// audioContext.audioWorklet.addModule("/worklets/pcm-capture-worklet.js")
// — plain script, not bundled through Next's module graph, hence living
// under public/ rather than lib/. Accumulates the 128-sample render
// quanta the Web Audio API hands `process()` into ~20ms frames (at
// whatever the AudioContext's real sample rate turns out to be — not
// assumed) and posts each accumulated frame to the main thread as a
// transferable Float32Array buffer (zero-copy).
class PCMCaptureProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this._frameSamples = Math.round(sampleRate * 0.02);
    this._buffer = new Float32Array(this._frameSamples);
    this._writeIndex = 0;
  }

  process(inputs) {
    const input = inputs[0];
    if (!input || input.length === 0) return true;
    const channelData = input[0];
    if (!channelData || channelData.length === 0) return true;

    let readIndex = 0;
    while (readIndex < channelData.length) {
      const remainingInFrame = this._frameSamples - this._writeIndex;
      const remainingInInput = channelData.length - readIndex;
      const toCopy = Math.min(remainingInFrame, remainingInInput);
      this._buffer.set(channelData.subarray(readIndex, readIndex + toCopy), this._writeIndex);
      this._writeIndex += toCopy;
      readIndex += toCopy;

      if (this._writeIndex >= this._frameSamples) {
        this.port.postMessage(this._buffer, [this._buffer.buffer]);
        this._buffer = new Float32Array(this._frameSamples);
        this._writeIndex = 0;
      }
    }
    return true;
  }
}

registerProcessor("pcm-capture-processor", PCMCaptureProcessor);
