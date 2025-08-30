# compat_audio.py
# Minimal polyfill for parts of the 'audioop' module that were removed in Python 3.13.
# Implements:
#   - lin2ulaw(pcm16_bytes, width=2) -> ulaw_bytes
#   - ulaw2lin(ulaw_bytes, width=2) -> pcm16_bytes
#   - ratecv(pcm_bytes, width, channels, inrate, outrate, state) -> (pcm_bytes, None)
#
# Notes:
# - Supports width=2 (16-bit PCM) and channels=1 only (mono), which is what your code uses.
# - μ-law implementation follows ITU-T G.711 (bias=0x84). This produces proper G.711 μ-law bytes.
# - ratecv() uses simple linear interpolation; adequate for 8k<->24k voice bandwidth.

from typing import Optional, Tuple

_BIAS = 0x84       # 132
_CLIP = 32635

def _linear2ulaw_sample(sample: int) -> int:
    """Convert a single 16-bit PCM sample (-32768..32767) to an 8-bit μ-law code (0..255)."""
    # Get sign and magnitude
    if sample < 0:
        sign = 0x80
        sample = -sample
        if sample > 32767:
            sample = 32767
    else:
        sign = 0

    # Clip and add bias
    if sample > _CLIP:
        sample = _CLIP
    sample = sample + _BIAS  # 132

    # Determine segment (exponent)
    # Segment boundaries at: 0x84<<0 (132), <<1 (264), <<2 (528), <<3 (1056), <<4 (2112), <<5 (4224), <<6 (8448), <<7 (16896)
    segment = 7
    if sample < 0x100: segment = 0
    elif sample < 0x200: segment = 1
    elif sample < 0x400: segment = 2
    elif sample < 0x800: segment = 3
    elif sample < 0x1000: segment = 4
    elif sample < 0x2000: segment = 5
    elif sample < 0x4000: segment = 6

    # Mantissa is the 4 bits right below the segment’s top 1, shifted by (segment+3)
    mantissa = (sample >> (segment + 3)) & 0x0F
    ulaw = ~(sign | (segment << 4) | mantissa) & 0xFF
    return ulaw

def _ulaw2linear_sample(ulaw: int) -> int:
    """Convert a single μ-law code (0..255) to a 16-bit PCM sample (-32768..32767)."""
    ulaw = ~ulaw & 0xFF
    sign = ulaw & 0x80
    segment = (ulaw >> 4) & 0x07
    mantissa = ulaw & 0x0F

    # Reconstruct magnitude (add the implicit leading 1 and the bias)
    sample = ((mantissa | 0x10) << (segment + 3)) - _BIAS
    if sign != 0:
        sample = -sample
    # Clamp to 16-bit signed range
    if sample > 32767:
        sample = 32767
    elif sample < -32768:
        sample = -32768
    return sample

def lin2ulaw(fragment: bytes, width: int) -> bytes:
    """PCM16 (mono) -> G.711 μ-law bytes."""
    if width != 2:
        raise ValueError("compat_audio.lin2ulaw only supports width=2 (16-bit PCM).")
    if len(fragment) % 2 != 0:
        raise ValueError("PCM fragment length must be even (16-bit samples).")
    out = bytearray(len(fragment) // 2)
    # Little-endian int16
    for i in range(0, len(fragment), 2):
        sample = int.from_bytes(fragment[i:i+2], "little", signed=True)
        out[i // 2] = _linear2ulaw_sample(sample)
    return bytes(out)

def ulaw2lin(fragment: bytes, width: int) -> bytes:
    """G.711 μ-law bytes -> PCM16 (mono)."""
    if width != 2:
        raise ValueError("compat_audio.ulaw2lin only supports width=2 (16-bit PCM).")
    out = bytearray(len(fragment) * 2)
    for i, u in enumerate(fragment):
        sample = _ulaw2linear_sample(u)
        out[2*i:2*i+2] = int(sample).to_bytes(2, "little", signed=True)
    return bytes(out)

def ratecv(fragment: bytes, width: int, channels: int, inrate: int, outrate: int, state: Optional[object]) -> Tuple[bytes, None]:
    """Resample mono PCM with simple linear interpolation. Returns (new_bytes, None)."""
    if width != 2:
        raise ValueError("compat_audio.ratecv only supports width=2 (16-bit PCM).")
    if channels != 1:
        raise ValueError("compat_audio.ratecv only supports mono audio (channels=1).")
    if inrate <= 0 or outrate <= 0:
        raise ValueError("Sample rates must be positive.")

    if inrate == outrate:
        return fragment, None

    # Convert to int16 array (little-endian)
    n = len(fragment) // 2
    src = [int.from_bytes(fragment[2*i:2*i+2], "little", signed=True) for i in range(n)]

    if n == 0:
        return b"", None
    if n == 1:
        # Just repeat/trim a single sample
        out_len = int(round(n * outrate / inrate))
        out = bytes(int(src[0]).to_bytes(2, "little", signed=True) * out_len)
        return out, None

    # Linear interpolation
    ratio = outrate / inrate
    out_len = int(round(n * ratio))
    out = bytearray(out_len * 2)

    for j in range(out_len):
        # Position in source
        pos = j / ratio
        i0 = int(pos)
        if i0 >= n - 1:
            s = src[-1]
        else:
            frac = pos - i0
            s = int(round(src[i0] * (1.0 - frac) + src[i0 + 1] * frac))
        out[2*j:2*j+2] = int(s).to_bytes(2, "little", signed=True)

    return bytes(out), None
