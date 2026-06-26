/**
 * AudioWorklet：采集麦克风 PCM，重采样至 16 kHz 后回传主线程。
 * 通过零增益节点保持处理链活跃，避免麦克风回放到扬声器。
 */
class PCMProcessor extends AudioWorkletProcessor {
  constructor(options) {
    super();
    const opts = options.processorOptions || {};
    this._frameSize = opts.frameSize || 4096;
    this._targetRate = opts.targetSampleRate || 16000;
    this._inputRate = sampleRate;
    this._pending = new Float32Array(0);
  }

  _append(input) {
    const merged = new Float32Array(this._pending.length + input.length);
    merged.set(this._pending);
    merged.set(input, this._pending.length);
    this._pending = merged;
  }

  _resample(float32) {
    if (this._inputRate === this._targetRate) {
      return float32;
    }
    const ratio = this._inputRate / this._targetRate;
    const outLen = Math.max(1, Math.round(float32.length / ratio));
    const out = new Float32Array(outLen);
    for (let i = 0; i < outLen; i++) {
      const src = i * ratio;
      const idx = Math.floor(src);
      const frac = src - idx;
      const s0 = float32[idx] ?? 0;
      const s1 = float32[idx + 1] ?? s0;
      out[i] = s0 + frac * (s1 - s0);
    }
    return out;
  }

  process(inputs) {
    const input = inputs[0] && inputs[0][0];
    if (!input || input.length === 0) {
      return true;
    }

    this._append(input);

    while (this._pending.length >= this._frameSize) {
      const chunk = this._pending.slice(0, this._frameSize);
      this._pending = this._pending.slice(this._frameSize);
      const resampled = this._resample(chunk);
      const out = new Float32Array(resampled);
      this.port.postMessage(out);
    }

    return true;
  }
}

registerProcessor('pcm-processor', PCMProcessor);
