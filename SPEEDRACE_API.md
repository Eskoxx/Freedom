# SpeedRace API — Complete Decryption Reference

> Fmovies.gd backend powering stream sources.
> Reverse-engineered from the live Next.js page (modules 84737 "BV", 83846 "c7", 93589 "Hashids", 50882 "endpoint registry").

---

## Table of Contents

1. [Architecture Overview](#1-architecture-overview)
2. [Endpoints](#2-endpoints)
3. [Seed Fetching](#3-seed-fetching)
4. [API Request](#4-api-request)
5. [Ciphertext Format](#5-ciphertext-format)
6. [Key Derivation](#6-key-derivation)
7. [State Initialization (61-element PRNG)](#7-state-initialization-61-element-prng)
8. [XOR Keystream Generation](#8-xor-keystream-generation)
9. [Decryption & Validation](#9-decryption--validation)
10. [Complete JS Source (Module 84737 / BV)](#10-complete-js-source-module-84737--bv)
11. [Working Python Implementation](#11-working-python-implementation)
12. [b35ebba4 / c7 / Hashids — Why Unused](#12-b35ebba4--c7--hashids--why-unused)
13. [Endpoint Registry (Module 50882)](#13-endpoint-registry-module-50882)
14. [Constants Reference](#14-constants-reference)
15. [Troubleshooting](#15-troubleshooting)

---

## 1. Architecture Overview

```
┌─────────────────┐    1. /seed?mediaId={id}    ┌────────────────────────┐
│  fmovies.gd     │ ──────────────────────────→  │  api.speedracelight.com│
│  (Next.js SPA)  │                               │                        │
│                 │ ←── {seed, ttlMs} ──────────  │                        │
│                 │                               │                        │
│                 │    2. /{ep}/sources-with-title │                        │
│                 │       ?title=...&enc=2&seed=  │                        │
│                 │ ──────────────────────────→  │                        │
│                 │ ←── base64url(ciphertext) ──  │                        │
│                 │                               │                        │
│   BV() decrypt  │  (client-side in webpack)     │                        │
│   ──────────►   │  seed + mediaId → XOR key     │                        │
│   magic: "mvm1" │  base64url → raw → XOR → JSON │                        │
│                 │                               │                        │
│    JSON result  │  {sources: [...],             │                        │
│                 │   subtitles: [...]}            │                        │
└─────────────────┘                               └────────────────────────┘
```

**Key insight**: The API returns a ciphertext (not JSON). The page decrypts it
using a custom XOR cipher driven by a 61-element PRNG initialized from the
seed + mediaId (TMDB ID). The 4th URL parameter `b35ebba4` is **computed by
the page but never used by the decryption function**.

---

## 2. Endpoints

All endpoints follow the same pattern: `/{type}/sources-with-title`.

Discovered in module 50882:

| Endpoint | URL | Notes |
|----------|-----|-------|
| **cdn** | `{BASE}/cdn/sources-with-title` | **Currently used by the page (2026)** |
| neon2 | `{BASE}/neon2/sources-with-title` | Original endpoint |
| meine | `{BASE}/meine/sources-with-title` | German (passes `language: "german"`) |
| hdmovie | `{BASE}/hdmovie/sources-with-title` | Hindi/English (filters by quality) |
| m4uhd | `{BASE}/m4uhd/sources-with-title` | Standard |
| mbx | `{BASE}/mbx/sources-with-title` | Standard |

**Base URL**: `https://api.speedracelight.com`

---

## 3. Seed Fetching

**Request**:
```
GET https://api.speedracelight.com/seed?mediaId={tmdbId}
```

**Response** (JSON):
```json
{
  "seed": "59485590.MS-ZDV2LUJ2ccKR3tEuxW2",
  "ttlMs": 30000
}
```

**Behavior** (from JS source, module 84737):
- The page caches seeds in a `Map` keyed by `"{origin}|{mediaId}"`
- Cache is valid for `ttlMs - 5000` ms (5-second safety margin)
- On 401 error, the cache entry is deleted and a retry is attempted

---

## 4. API Request

**Request**:
```
GET {BASE}/{endpoint}/sources-with-title
  ?title={encodedTitle}
  &mediaType=movie|tv
  &year={year}
  &episodeId={episodeId}
  &seasonId={seasonId}
  &tmdbId={tmdbId}
  &imdbId={imdbId}
  &enc=2
  &seed={seed}
```

**Parameters**:
| param | required | notes |
|-------|----------|-------|
| title | yes | URL-encoded movie/series title |
| mediaType | yes | `"movie"` or `"tv"` |
| year | no | Release year |
| episodeId | depends | `"1"` for movies |
| seasonId | depends | `"1"` for movies |
| tmdbId | yes | The Movie Database ID |
| imdbId | no | e.g. `"tt11378946"` |
| enc | no | **Must be `"2"`** (the page always passes this) |
| seed | yes | From `/seed` endpoint |

**Response**: A base64url-encoded ciphertext string (no JSON wrapper).

---

## 5. Ciphertext Format

The API response is a **raw base64url string**. Steps:

1. Replace URL-safe chars: `-` → `+`, `_` → `/`
2. Pad with `=` to multiple of 4:
   ```
   pad_length = (4 - len(s) % 4) % 4
   s += '=' * pad_length
   ```
3. Decode as standard base64 → `Uint8Array` of bytes
4. XOR-decrypt (see [§9](#9-decryption--validation))
5. First 4 bytes must be `"mvm1"` (hex: `6d 76 6d 31`)
6. Remaining bytes are UTF-8 encoded JSON:
   ```json
   {
     "sources": [
       {
         "url": "https://...",
         "quality": "Auto",
         "type": "dash"
       }
     ],
     "subtitles": [
       {
         "url": "https://...",
         "lang": "English"
       }
     ]
   }
   ```

---

## 6. Key Derivation

The XOR key starts from a 32-bit value derived from the seed and media ID.
Three fmix32 operations are applied in a specific nested pattern.

### 6a. fmix32 ("f" function)

```javascript
function u(e) {
    e >>>= 0;
    e ^= e >>> 16;
    e = Math.imul(e, 2246822507) >>> 0;
    e ^= e >>> 13;
    e = Math.imul(e, 3266489909) >>> 0;
    e ^= e >>> 16;
    return e >>> 0;
}
```

This is the MurmurHash3 finalizer (fmix32) — constant-folded with specific
imul constants instead of the standard `0x85EBCA6B` / `0xC2B2AE35`.

### 6b. FNV-1a 32-bit

```javascript
function fnv1a(str) {
    let h = 2166136261;
    for (let i = 0; i < str.length; i++) {
        h = Math.imul(h ^ str.charCodeAt(i), 16777619) >>> 0;
    }
    return h >>> 0;
}
```

Standard FNV-1a with an unsigned 32-bit wrap after each multiply.

### 6c. Three-Level fmix Nesting

```javascript
let s = u(
    u(fnv1a(seed)) ^
    u(mediaId >>> 0 ^ 2654435769)
) >>> 0;
```

**Crucially**: The `fnv1a(seed)` result is **wrapped in `u()` (fmix)** before
XOR. This is the most commonly misimplemented step. The expression has:
- **Level 1**: `fnv1a(seed)` — raw 32-bit hash
- **Level 2**: `u(fnv1a(seed))` — fmix applied to fnv1a result
- **Level 2b**: `u(mediaId ^ 2654435769)` — fmix applied to (mediaId XOR constant)
- **Level 3**: `u(level2 ^ level2b)` — fmix applied to the XOR of both

Full: `s = fmix(fmix(fnv1a(seed)) ^ fmix(mediaId ^ 2654435769))`

Where `2654435769` = `0x9E3779B9` = the golden ratio fractional part
(standard in many hash functions).

---

## 7. State Initialization (61-element PRNG)

The state is a 61-element array `S` plus an accumulator `acc`, initialized
in 8 iterations.

### 7a. Parity Check (Dead Code)

```javascript
function m(e) { return (e * (e + 1) & 1) === 1; }
function l(e) { return (e * (e + 1) & 1) === 0; }
```

`m(x)` is **always false** because `x * (x+1)` is always even.
`l(x)` is **always true** because `x * (x+1)` is always even, and `even & 1 = 0`.

A 256-byte RC4 S-box branch exists in the minified code but is **unreachable**
(routed through `m()`). The actual code always uses the 61-element branch.

### 7b. 8-Iteration Initialization

```javascript
let S = Array(61);
let s_val = u(u(fnv1a(seed)) ^ u(mediaId ^ 2654435769)) >>> 0;

for (let i = 0; i < 8; i++) {
    let t = s_val % 61;                         // index
    s_val = rotate(s_val + 2654435769, 7 + (7 & i)) >>> 0;
    S[t] = (s_val ^ u(s_val)) >>> 0;            // S[index] = s ^ fmix(s)
    s_val = u(s_val + t >>> 0);                  // s = fmix(s + index)
}
```

Rotate amounts per iteration:
| i | 7+(7&i) | meaning |
|---|---------|---------|
| 0 | 7       | 7       |
| 1 | 8       | 7+1     |
| 2 | 9       | 7+2     |
| 3 | 10      | 7+3     |
| 4 | 11      | 7+4     |
| 5 | 12      | 7+5     |
| 6 | 13      | 7+6     |
| 7 | 14      | 7+7     |

### 7c. Final Accumulator

```javascript
acc = u(2779096485 ^ s_val) >>> 0;
```

Where `2779096485` = `0xA5ABF2A5` (a hash constant).

### 7d. Python init function

```python
def init_state(seed: str, media_id: int):
    s_val = f(u32(f(fnv1a(seed)) ^ f(u32(media_id ^ 2654435769))))
    S = [None] * 61
    for i in range(8):
        t_idx = s_val % 61
        s_val = d(u32(s_val + 2654435769), 7 + (7 & i))
        S[t_idx] = u32(s_val ^ f(s_val))
        s_val = f(u32(s_val + t_idx))
    return S, f(u32(2779096485 ^ s_val))
```

---

## 8. XOR Keystream Generation

A counter-mode PRNG that generates either 3 or 4 bytes per iteration
(the full 32-bit value is XORed byte by byte).

### 8a. Per-Iteration Details

```
Inputs:  state = { S: array[61], acc: uint32 }
         counter = iteration number (0, 1, 2, ...)

Output:  one uint32 of keystream (up to 4 bytes)

Step 1 — Index and mask:
    o = acc % 61                         // index into S
    in_s = (o in S) ? true : false
    i_mask = in_s ? 0xFFFFFFFF : 0       // -1 if in S, 0 if not

Step 2 — Current S value:
    r = S[o] ?? 0                        // 0 if undefined

Step 3 — Compute c_val:
    n_s = r ^ (2654435769 * (counter + 1)) & 0xFFFFFFFF  // S[o] XOR const
    a = acc                              // NOTE: acc, not counter!
    c_val = ((a ^ n_s) | (a & n_s & i_mask)) & 0xFFFFFFFF

    When S[o] IS defined (i_mask = -1):
        c_val = acc | (S[o] ^ const)     // bitwise OR
    When S[o] is NOT defined (i_mask = 0):
        c_val = acc ^ (S[o] ^ const)     // bitwise XOR
                                       // (S[o]=0 so: c_val = acc ^ const)

Step 4 — Rotate and XOR:
    rot1 = rotate(c_val + acc, 31 & o)
    rot2 = rotate(acc, 31 & (o * 7))
    xor_val = (rot1 ^ rot2) & 0xFFFFFFFF

Step 5 — Update acc:
    new_acc = fmix((xor_val + 2654435769) & 0xFFFFFFFF)

Step 6 — Store back:
    S[o] = new_acc
    acc = new_acc

Step 7 — Output (4 bytes, LE):
    byte[0] = acc & 0xFF
    byte[1] = (acc >> 8) & 0xFF    (if needed)
    byte[2] = (acc >> 16) & 0xFF   (if needed)
    byte[3] = (acc >> 24) & 0xFF   (if needed)
```

### 8b. Python Generation

```python
def generate_xor_key(S, acc, length):
    S = list(S)
    out = bytearray()
    ctr = 0
    while len(out) < length:
        o = acc % 61
        in_s = o < 61 and S[o] is not None
        i_mask = 0xFFFFFFFF if in_s else 0
        r = u32(S[o]) if in_s else 0

        n_s = u32(r ^ u32(2654435769 * (ctr + 1)))
        a_val = acc
        c_val = u32(u32(a_val ^ n_s) | u32(a_val & n_s & i_mask))

        xor_temp = u32(d(u32(c_val + acc), 31 & o) ^ d(acc, 31 & ((o * 7) & 0xFFFFFFFF)))
        acc = f(u32(xor_temp + 2654435769))

        if o < 61:
            S[o] = acc

        out.append(acc & 0xFF)
        if len(out) < length: out.append((acc >> 8) & 0xFF)
        if len(out) < length: out.append((acc >> 16) & 0xFF)
        if len(out) < length: out.append((acc >> 24) & 0xFF)
        ctr += 1

    return bytes(out)
```

---

## 9. Decryption & Validation

```python
def decrypt(seed: str, media_id: int, ct_b64: str):
    # 1. Decode base64url → raw bytes
    raw = decode_base64url(ct_b64)

    # 2. Initialize PRNG state from seed + media_id
    S, acc = init_state(seed, media_id)

    # 3. Generate XOR keystream (same length as ciphertext)
    xor_key = generate_xor_key(S, acc, len(raw))

    # 4. XOR to decrypt
    decrypted = bytes(a ^ b for a, b in zip(raw, xor_key))

    # 5. Verify magic bytes "mvm1"
    if decrypted[:4] != b"mvm1":
        raise ValueError("Decryption failed: magic mismatch")

    # 6. Parse JSON (skip 4-byte magic header)
    return json.loads(decrypted[4:].decode("utf-8"))
```

### Magic Bytes

| Type | Value |
|------|-------|
| ASCII | `mvm1` |
| Hex | `6d 76 6d 31` |
| Decimal | `109, 118, 109, 49` |

This 4-byte prefix is verified in JS as:
```javascript
let r = [109, 118, 109, 49];  // "mvm1"
for (let e = 0; e < r.length; e++) {
    if (n[e] !== r[e]) throw Error("decrypt failed: bad seed or tampered payload");
}
```

---

## 10. Complete JS Source (Module 84737 / BV)

The full module as extracted from the live page webpack runtime.
Module 84737 is loaded in chunk 4035-9c8ed421868df6b9.js.

```javascript
// Module 84737 — BV function
a.d(t, { BV: function() { return c; } });

var s = a(35225);  // HTTP client (axios-like)

// Seed cache: Map<"origin|mediaId", {seed, expiresAt}>
let n = new Map();

function d(e) { return new URL(e).origin; }

// Fetch seed with caching
async function o(e, t) {
    let a = d(e),
        o = `${a}|${t}`,
        i = Date.now(),
        r = n.get(o);
    if (r && r.expiresAt - 5000 > i) return r.seed;
    let l = await (0, s.Wg)(`${a}/seed`, { params: { mediaId: t }, retry: 1 }),
        m = l.ttlMs ?? 30000;
    n.set(o, { seed: l.seed, expiresAt: i + m });
    return l.seed;
}

// Hash constants (unused in decryption, from SHA-256 init vector)
let i = [
    1116352408, 1899447441, 3049323471, 3921009573,
    961987163, 1508970993, 2453635748, 2870763221,
    3624381080, 310598401, 607225278, 1426881987,
    1925078388, 2162078206, 2614888103, 3248222580
];

let r = [109, 118, 109, 49];  // "mvm1"
let l = e => (e * (e + 1) & 1) == 0;  // always true (dead parity check)
let m = e => (e * (e + 1) & 1) == 1;  // always false

// fmix32
function u(e) {
    e >>>= 0;
    e ^= e >>> 16;
    e = Math.imul(e, 2246822507) >>> 0;
    e ^= e >>> 13;
    e = Math.imul(e, 3266489909) >>> 0;
    e ^= e >>> 16;
    return e >>> 0;
}

// Rotate left (cyclic shift)
function p(e, t) {
    e >>>= 0;
    t &= 31;
    if (t == 0) return e >>> 0;
    return (e << t | e >>> 32 - t) >>> 0;
}

// Main decrypt function
async function c(e, t, a, c) {
    // e = URL, t = params, a = mediaId, c = b35ebba4 (UNUSED)
    if (void 0 === a) throw Error("mediaId is required to decode sources");

    let b = async () => {
        let n = await o(e, a);  // fetch seed

        // Inner function: decode, decrypt, verify
        return function(e, t, a) {
            // e = response text (base64url)
            // t = seed
            // a = mediaId (actually ciphertext length in the call)

            // Base64url decode
            let n = function(e) {
                let t = e.replace(/-/g, "+").replace(/_/g, "/")
                         .padEnd(4 * Math.ceil(e.length / 4), "=");
                if ("function" == typeof atob) {
                    let e = atob(t),
                        a = new Uint8Array(e.length);
                    for (let t = 0; t < e.length; t++) a[t] = e.charCodeAt(t);
                    return a;
                }
                return new Uint8Array(globalThis.Buffer.from(t, "base64"));
            }(e);

            // Generate XOR key
            let d = function(e, t, a) {
                // inner init — see sections 6-7
                // ... (full algorithm as documented above)
            }(t, a, n.length);

            // XOR ciphertext with generated key
            for (let e = 0; e < n.length; e++) n[e] ^= d[e];

            // Verify "mvm1" magic bytes
            for (let e = 0; e < r.length; e++)
                if (n[e] !== r[e])
                    throw Error("decrypt failed: bad seed or tampered payload");

            // Parse JSON from remaining bytes
            let s = n.subarray(r.length);
            return typeof TextDecoder !== "undefined"
                ? new TextDecoder("utf-8").decode(s)
                : globalThis.Buffer.from(s).toString("utf8");

        }(
            await (0, s.Wg)(e, {
                params: { ...t, enc: "2", seed: n },
                retry: 0
            }),
            n,
            a
        );
    };

    try {
        return await b();
    } catch (t) {
        // On 401: retry after clearing seed cache
        if (401 === (t?.response?.status ?? t?.status)) {
            n.delete(`${d(e)}|${a}`);
            return await b();
        }
        throw t;
    }
}
```

---

## 11. Working Python Implementation

Located at `/tmp/speedrace_decrypt.py` — fully functional.

```python
#!/usr/bin/env python3
import base64, json, requests

BASE = "https://api.speedracelight.com"
MVM1 = b"mvm1"

def u32(x): return x & 0xFFFFFFFF

def f(e):
    """fmix32 — MurmurHash3 finalizer"""
    e = u32(e); e ^= e >> 16; e = u32(e * 2246822507)
    e ^= e >> 13; e = u32(e * 3266489909); e ^= e >> 16; return u32(e)

def d(e, t):
    """Rotate left (cyclic shift, t masked to 5 bits)"""
    e = u32(e); t &= 31
    return e if t == 0 else u32((e << t) | (e >> (32 - t)))

def fnv1a(s):
    """FNV-1a 32-bit hash"""
    h = 2166136261
    for c in s:
        h = u32((h ^ ord(c)) * 16777619)
    return h

def decode_base64url(s):
    s = s.replace("-", "+").replace("_", "/")
    pad = (4 - len(s) % 4) % 4
    return bytearray(base64.b64decode(s + "=" * pad))

def init_state(seed: str, media_id: int):
    """Init 61-element PRNG from seed + media_id.
    
    JS: s = u(u(fnv1a(seed)) ^ u(mediaId ^ 2654435769))
    """
    s_val = f(u32(f(fnv1a(seed)) ^ f(u32(media_id ^ 2654435769))))
    S = [None] * 61
    for i in range(8):
        t_idx = s_val % 61
        s_val = d(u32(s_val + 2654435769), 7 + (7 & i))
        S[t_idx] = u32(s_val ^ f(s_val))
        s_val = f(u32(s_val + t_idx))
    return S, f(u32(2779096485 ^ s_val))

def generate_xor_key(S, acc, length):
    """Generate XOR keystream.
    
    KEY RULE: a_val = acc (NOT the counter!). This is the #1 bug source.
    """
    S = list(S)
    out = bytearray()
    ctr = 0
    while len(out) < length:
        o = acc % 61
        in_s = o < 61 and S[o] is not None
        i_mask = 0xFFFFFFFF if in_s else 0
        r = u32(S[o]) if in_s else 0

        n_s = u32(r ^ u32(2654435769 * (ctr + 1)))
        c_val = u32(u32(acc ^ n_s) | u32(acc & n_s & i_mask))

        xor_temp = u32(d(u32(c_val + acc), 31 & o) ^ d(acc, 31 & ((o * 7) & 0xFFFFFFFF)))
        acc = f(u32(xor_temp + 2654435769))

        if o < 61:
            S[o] = acc

        out.append(acc & 0xFF)
        if len(out) < length: out.append((acc >> 8) & 0xFF)
        if len(out) < length: out.append((acc >> 16) & 0xFF)
        if len(out) < length: out.append((acc >> 24) & 0xFF)
        ctr += 1
    return bytes(out)

def decrypt(seed: str, media_id: int, ct_b64: str):
    raw = decode_base64url(ct_b64)
    S, acc = init_state(seed, media_id)
    xor_key = generate_xor_key(S, acc, len(raw))
    decrypted = bytes(a ^ b for a, b in zip(raw, xor_key))
    if decrypted[:4] != MVM1:
        raise ValueError(f"Bad magic: {decrypted[:4].hex()} != mvm1")
    return json.loads(decrypted[4:].decode("utf-8"))
```

### Common Implementation Mistakes

| # | Mistake | Correct |
|---|---------|---------|
| 1 | `r_val = counter` instead of `acc` | `a_val = acc` — the variable `a` in JS is assigned `d` (the accumulator), not the iteration counter `t` |
| 2 | `f(fnv1a(seed))` not applied | fnv1a result MUST be wrapped in fmix: `f(fnv1a(seed))` |
| 3 | `+ CONST` outside fmix | The `+ 2654435769` addition is INSIDE `u()` (fmix): `u(u32(xor_temp + CONST))` |
| 4 | Using `|` only | The `c_val` computation is `(acc ^ n_s) | (acc & n_s & mask)` — XOR first, then OR with masked AND |
| 5 | JS `>>> 0` | The `>>> 0` operator is a 32-bit unsigned wrap. In JS, bitwise ops already truncate to 32-bit, but `>>> 0` forces unsigned interpretation. Must be reproduced as `& 0xFFFFFFFF` in Python. |
| 6 | `Math.imul(a, b)` | JS `Math.imul()` returns the low 32 bits of the product as a signed integer. Python `*` on unsigned values is correct as long as you apply `& 0xFFFFFFFF` after. |
| 7 | `Math.imul(o, 7)` for rotate | In `31 & Math.imul(o, 7)` — `o` is the index, `Math.imul` here is just integer multiply (no overflow since `o < 61`, `61*7 = 427` fits in 32 bits). Safe to use `(o * 7) & 31`. |
| 8 | `31 & o` vs `o % 32` | `31 & o` is the same as `o % 32` because `o >= 0`. The rotate function masks `t &= 31` internally, but the caller still computes `31 & o` separately for the rotate call. |

---

## 12. b35ebba4 / c7 / Hashids — Why Unused

### 12a. What It Is

The page computes `b35ebba4` in the movie page component:

```javascript
import M from(93589);  // Hashids
import D from(83846);  // c7 hash

let x = D.c7(tmdbId_string + "d486ae1ce6fdbe63b60bd1704541fcf0");
let w = new M.Z();
let b35ebba4 = w.encode(x);
```

### 12b. c7 Function (Module 83846)

The `c7` function takes a single string argument and returns a hex string.
Called as: `c7("936075" + "d486ae1ce6fdbe63b60bd1704541fcf0")`.

We did not fully decompile this function, but it produces a hex-encoded hash
that is then passed to Hashids for encoding.

### 12c. Hashids (Module 93589)

Standard Hashids encoding. The hex string from `c7()` is encoded into a
short hashid string.

### 12d. Why It's Not Used

Looking at the BV function signature `function c(e, t, a, c)` — the 4th
parameter `c` (which receives `b35ebba4`) is **never referenced anywhere
in the function body**. The BV function only uses:
- `e` = URL for the API request
- `t` = URL parameters
- `a` = mediaId (for seed fetch + key init)

**Conclusion**: `b35ebba4` is a vestige from an older API version or used
by a different path. It can be safely ignored for the current API.

---

## 13. Endpoint Registry (Module 50882, Chunk 4035)

The module that wires up all source endpoints:

```javascript
// Module 50882
let p = async e => {
    // neon2 — calls BV with 4 params
    let {sources: n=[], subtitles: o=[]} = JSON.parse(
        await (0, l.BV)(`${c.S}/neon2/sources-with-title`, r, a.tmdbId, a.b35ebba4)
    );
    // Filters for dash+mpd
    return {sources: n.filter(e => e.type === "dash" || e.url.includes(".mpd"))};
};

// mein — German
let g = async e => {
    // Calls BV with same pattern, adds language: "german"
};

// hdmovie — Hindi / English
let h = async e => { /* Hindi quality filter */ };
let I = async e => { /* English quality filter */ };

// m4uhd — standard
let y = async e => { /* standard */ };

// cdn — currently active (2026)
let v = async e => {
    let n = await (0, l.BV)(`${c.S}/cdn/sources-with-title`, r, a.tmdbId, a.b35ebba4);
    return {sources: JSON.parse(n).sources, subtitles: JSON.parse(n).subtitles};
};
```

All endpoints use the same `BV()` function with identical parameter structure.

---

## 14. Constants Reference

| Constant | Value | Usage |
|----------|-------|-------|
| `2654435769` | `0x9E3779B9` | Golden ratio fractional — used in key init (+= & XOR), c_val computation, and fmix update |
| `2779096485` | `0xA5ABF2A5` | Final acc seed: `acc = fmix(2779096485 ^ s_val)` |
| `2166136261` | `0x811C9DC5` | FNV-1a offset basis |
| `16777619` | FNV-1a prime | FNV-1a multiply constant |
| `2246822507` | fmix constant 1 | `e *= 2246822507` |
| `3266489909` | fmix constant 2 | `e *= 3266489909` |
| `109, 118, 109, 49` | `"mvm1"` | Magic bytes (4-byte verification prefix) |
| 61 | array size | PRNG state array length (prime) |
| 8 | iteration count | Number of state initialization rounds |
| 7 | rotate min | Base rotate amount: `7 + (7 & i)` |

---

## 15. Troubleshooting

### "Bad magic: ... != mvm1"

The decryption produced wrong output. Common causes:

**1. Seed expired** — Seed TTL is 30s. Fetch a fresh seed immediately before
   making the API request.

**2. Wrong mediaId** — The mediaId passed to `/seed` and the one used in
   key derivation must be the same. The page uses the TMDB ID (integer).

**3. FNV-1a not wrapped in fmix** — The fnv1a result must be passed through
   `f()` before XOR: `f(u32(f(fnv1a(seed)) ^ f(u32(media_id ^ C))))`.

**4. `a_val` set to counter** — In the XOR generation loop, the `a` variable
   in JS is assigned from `d` (the **accumulator**), NOT from `t` (the
   iteration counter). This is the most common bug.

**5. Wrong rotate argument** — The second rotate in the fmix update uses
   `31 & Math.imul(o, 7)` as the shift amount, where `o = acc % 61`.

**6. Missing `>>> 0` wraps** — Every intermediate value in JS that goes
   through `>>> 0` or `| 0` must be wrapped with `& 0xFFFFFFFF` in Python.

### "Seed fetch returned 404"

- The `mediaId` must be a valid TMDB ID (integer, not string "tt...")
- The `mediaId` from the `/movie/:id` page route might differ from the
  `tmdbId` used in the API call — the page uses the TMDB numeric ID

### "Ciphertext is empty"

- The API might return `{"error":"STREAMCRYPTO_SEED_INVALID"}` as 37 bytes
  of text (not base64url). This happens when the seed is expired or invalid.
- Fix: use the exact same seed from the `/seed` call (don't generate your own)

### "Different endpoint returns different format"

- The `cdn`, `neon2`, `mbx`, `meine`, `hdmovie`, `m4uhd` endpoints all use
  the same ciphertext format (base64url + "mvm1" + JSON)
- The `db.speedracelight.com` endpoints are for metadata (images, trailers)
  and don't use this encryption

---

## Revision History

- **2026-07-20**: Initial documented — full reverse-engineering from live page
  JS chunks. Algorithm verified working with Python decryption against live API.
