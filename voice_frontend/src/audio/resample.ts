// Linear resampler + PCM16 packer (little-endian)
export function resampleAndEncodePCM16(float32: Float32Array, srcRate: number, dstRate: number): Uint8Array {
  if (srcRate === dstRate) {
    return floatToPCM16(float32)
  }
  const ratio = srcRate / dstRate
  const outLen = Math.floor(float32.length / ratio)
  const out = new Float32Array(outLen)
  for (let i = 0; i < outLen; i++) {
    const idx = i * ratio
    const i0 = Math.floor(idx)
    const i1 = Math.min(i0 + 1, float32.length - 1)
    const frac = idx - i0
    out[i] = float32[i0] * (1 - frac) + float32[i1] * frac
  }
  return floatToPCM16(out)
}

function floatToPCM16(floats: Float32Array): Uint8Array {
  const out = new Uint8Array(floats.length * 2)
  const view = new DataView(out.buffer)
  for (let i = 0; i < floats.length; i++) {
    let s = Math.max(-1, Math.min(1, floats[i]))
    const val = s < 0 ? s * 0x8000 : s * 0x7FFF
    view.setInt16(i * 2, val, true) // little endian
  }
  return out
}
