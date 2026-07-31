# Anikoto Extraction Method — Complete Pattern Reference

> **Source:** `anime_watch/providers/anikoto.py` (477 lines)  
> **Target site:** `anikotv.to` (also mirrors: `anikoto.cz`, `anikoto.me`, `anikoto.net`, `anikoto.se`)  
> **Architecture:** Provider class extending `BaseProvider` with 3-phase extraction pipeline (Search → Episodes → Stream)

---

## Table of Contents

1. [Architecture Overview](#1-architecture-overview)
2. [Phase 1: Search / Discovery](#2-phase-1-search--discovery)
3. [Phase 2: Episode List](#3-phase-2-episode-list)
4. [Phase 3: Server Resolution](#4-phase-3-server-resolution)
5. [Phase 4: Stream Extraction](#5-phase-4-stream-extraction)
6. [HLS Variant Picking](#6-hls-variant-picking)
7. [PNG-Wrapped CDN Proxy](#7-png-wrapped-cdn-proxy)
8. [Fallback Extraction](#8-fallback-extraction)
9. [Reusable Pattern Template](#9-reusable-pattern-template)

---

## 1. Architecture Overview

```
AnikotoProvider (extends BaseProvider)
├── search(query)                     → list[SearchResult]
├── get_episodes(result)              → list[Episode]
├── get_servers(episode)              → list[dict{name, link_id, type}]
├── extract_stream(episode, audio, quality) → StreamSource
│   ├── _extract_megaclone(embed_url, domain, quality)
│   └── _extract_generic(embed_url, quality)
│
├── _pick_hls_variant(master_url, quality) → Optional[str]
├── _proxy_hls(playlist_url, referer, cdn_domain) → (proxy_url, server)
│
├── http server (threaded)
│   └── _SegmentProxyHandler (strips PNG wrappers from TS segments)
```

**Data Flow:**

```
Search (HTML) → Episode List (AJAX JSON) → Servers (AJAX JSON) → Embed (AJAX JSON) → Sources (AJAX JSON) → m3u8 URL → StreamSource
```

**Key patterns extracted from every result:**
- `media_id` (numeric, from `data-tip` on poster element)
- `data_ids` (base64-encoded composite, from episode `<a>` tags)
- `link_id` (base64, from server `<li>` elements)
- `data-mal`, `data-slug`, `data-timestamp` (for mapper API fallback)
- `file_id` (numeric, from embed page regex `File (\d+) -`)

---

## 2. Phase 1: Search / Discovery

### Endpoint

```
GET {site_url}/filter?keyword={query}
```

### Headers

```python
SESSION.headers = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 ...",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}
# Shared requests.Session() — persists cookies across requests
```

### CSS Selectors and Extraction

```python
soup.select(".main .item")
# Each item card contains:
#   .poster  →  data-tip attribute  →  media_id (numeric)
#   .name    →  text()              →  title
#   img      →  src attribute       →  thumbnail URL
```

### Mapping to Internal Model

```python
SearchResult(
    title=title,                        # from .name text
    url=f"{site_url}/mid/{media_id}",   # internal lookup URL
    site_name="Anikoto",                # provider identifier
    image=thumb_url or "",              # from img[src]
)
```

### Pattern for Other Sites

```python
# 1. Hit search/filter page with keyword param
# 2. Parse each result card using site-specific CSS selectors
# 3. Extract: title, unique ID, thumbnail
# 4. Store ID in URL for later phases (e.g. /mid/{id} or /anime/{slug})
```

---

## 3. Phase 2: Episode List

### Endpoint

```
GET {site_url}/ajax/episode/list/{media_id}
```

### Required Headers

```python
headers = {
    "X-Requested-With": "XMLHttpRequest",  # Tells server it's an AJAX call
    "Referer": result.url,                 # Referer = /mid/{media_id}
}
```

Note: The site returns `{status: 200, result: "<HTML string>"}` — the episode list is HTML **embedded in a JSON response**. Always check `status == 200` first.

### Parsing Logic

```python
body = resp.json()                           # Parse outer JSON
assert body["status"] == 200
soup = BeautifulSoup(body["result"], "lxml") # Parse inner HTML
for a in soup.find_all("a", href=True):
    data_ids = a.get("data-ids", "")         # ← CRITICAL: server lookup key
    if not data_ids:
        continue
    num    = a.get("data-num", "")            # episode number
    title  = a.get_text(strip=True)           # episode title
    mal_id = a.get("data-mal", "")            # MyAnimeList ID (for mapper)
    slug   = a.get("data-slug", "")           # URL slug (for mapper)
    ts     = a.get("data-timestamp", "")      # timestamp (for mapper)
```

### Data Attached to Episode

```python
episode.data = {
    "data_ids": data_ids,    # base64-encoded server group IDs
    "media_id": media_id,    # anime-level ID
    "mal_id": mal_id,        # MAL ID (for mapper fallback)
    "slug": slug,            # URL slug (for mapper fallback)
    "ts": ts,                # timestamp (for mapper fallback)
}
```

### Pattern for Other Sites

```python
# 1. Look for AJAX endpoints that return JSON-wrapped HTML
#    e.g. /ajax/episode/list/{id}
# 2. Parse inner HTML for <a> tags with data attributes
# 3. Episode <a> tags often carry: data-num (number), data-ids (server key)
# 4. Store all data-* attributes on the Episode model for later phases
```

---

## 4. Phase 3: Server Resolution

### Endpoint 1 — Page Servers

```
GET {site_url}/ajax/server/list?servers={data_ids}
```

```python
headers = {"X-Requested-With": "XMLHttpRequest", "Referer": episode.url}
body = resp.json()
if body["status"] == 200:
    soup = BeautifulSoup(body["result"], "lxml")  # HTML-lists in JSON
```

### Server List HTML Structure

```html
<div class="type" data-type="sub">        <!-- or "dub", "hsub" -->
  <ul>
    <li data-link-id="BASE64_STRING">HD-1</li>
    <li data-link-id="BASE64_STRING">Vidstream-2</li>
  </ul>
</div>
```

### Extraction Logic

```python
for li in soup.select("li[data-link-id]"):
    name = li.get_text(strip=True)           # Server display name
    link_id = li.get("data-link-id", "")     # Unique server identifier
    # Determine audio type from parent container
    parent = li.find_parent(class_="type")
    dtype = "sub"
    if parent:
        dt = parent.get("data-type", "")
        if dt == "dub":   dtype = "dub"
        elif dt == "hsub": dtype = "hsub"
```

### Endpoint 2 — Mapper API (Fallback Extra Servers)

When `mal_id`, `slug`, and `ts` are all present, a secondary API provides more servers:

```
GET https://mapper.nekostream.site/api/mal/{mal_id}/{slug}/{ts}
```

```python
r2 = SESSION.get(f"https://mapper.nekostream.site/api/mal/{mal_id}/{slug}/{ts}")
mapper_data = r2.json()   # Dict of server_name -> {sub: {}, dub: {}}
for sv_name, sv_data in mapper_data.items():
    if sv_name == "status": continue
    if "kiwi" in sv_name.lower(): continue   # skip kiwi servers
    for lang in ("sub", "dub"):
        entry = sv_data.get(lang)
        if entry and (entry.get("url") or entry.get("download")):
            servers.append({
                "name": sv_name,
                "link_id": entry.get("url", ""),
                "type": lang,
                "download_url": entry.get("download", {}).get(sv_name, ""),
            })
```

### Pattern for Other Sites

```python
# 1. Server list is often a separate AJAX call using IDs from episode data
# 2. Two-tier server resolution:
#    a. Primary: AJAX HTML with <li data-link-id>
#    b. Secondary (mapper): external API keyed by mal_id + slug + timestamp
# 3. Audio type (sub/dub/hsub) is encoded in parent container class/attribute
```

---

## 5. Phase 4: Stream Extraction

### Step 1 — Embed URL

```
GET {site_url}/ajax/server?get={link_id}
```

```python
headers = {"X-Requested-With": "XMLHttpRequest", "Referer": episode.url}
body = resp.json()
embed_url = body["result"]["url"]   # e.g. https://{domain}/stream/{id}
```

### Step 2 — MegaClone Embed (Primary)

If the embed URL contains `/stream/`, it's a MegaClone-type player.

```
GET {embed_url}
Referer: {site_url}
```

Parse the embed page for:
```python
# Regex to extract file_id
file_id_match = re.search(r"File\s+(\d+)\s+-", embed_resp.text)
file_id = file_id_match.group(1)
```

### Step 3 — Sources API

```
GET https://{domain}/stream/getSources?id={file_id}
Headers:
  Referer: {embed_url}
  X-Requested-With: XMLHttpRequest
```

```python
sources_data = resp.json()
sources = sources_data.get("sources", {})
m3u8_url = sources.get("file", "") if isinstance(sources, dict) else ""

# Subtitles (captions)
subtitles = [
    {"url": t["file"], "label": t.get("label", ""), "lang": t.get("label", "").lower()}
    for t in sources_data.get("tracks", [])
    if t.get("kind") == "captions" and t.get("file")
]
```

### StreamSource Construction

```python
StreamSource(
    url=m3u8_url,
    site_name=self.name,
    quality=quality_pref,
    is_direct=True,
    headers={"Referer": f"https://{domain}/"},
    subtitles=subtitles or None,
    proxy_server=proxy_server,  # For PNG-wrapped CDNs
)
```

### Pattern for Other Sites

```python
# 1. Server link_id → AJAX resolve to embed URL
# 2. Embed page → extract file_id via regex
# 3. Sources API → get m3u8 URL + subtitle tracks
# 4. Common API patterns: /stream/getSources, /getSources, /api/source
# 5. Embed often includes a "File {number} -" pattern
```

---

## 6. HLS Variant Picking

### Logic

```python
def _pick_hls_variant(master_url: str, quality_pref: str) -> Optional[str]:
    target_h = int(quality_pref.replace("p", ""))  # e.g. "720p" → 720

    resp = SESSION.get(master_url, headers={"Referer": "https://megaplay.buzz/"})
    body = resp.text

    for i, line in enumerate(body.splitlines()):
        if line.startswith("#EXT-X-STREAM-INF:"):
            m = re.search(r"RESOLUTION=(\d+)x(\d+)", line)
            h = int(m.group(2))
            variant = lines[i + 1].strip()
            # Option A: Best match ≤ target
            if h <= target_h and h > best_h:
                best_h, best_url = h, variant
            # Option B: Fallback = lowest resolution
            if h < fallback_h:
                fallback_h, fallback_url = h, variant

    return best_url or fallback_url
```

### Resolution Handling

- Primary strategy: pick the variant closest to but not exceeding the target (e.g. for "720p", picks 640x360 if 1280x720 is unavailable)
- Fallback: lowest available resolution
- If `quality_pref == "best"`, returns the raw master playlist (player chooses highest)

### Pattern for Other Sites

```python
# 1. Download the master .m3u8 playlist
# 2. Parse #EXT-X-STREAM-INF lines for RESOLUTION
# 3. Pick closest variant to target quality
# 4. Resolve relative URIs against master playlist base
```

---

## 7. PNG-Wrapped CDN Proxy

### Purpose

Some CDNs (identified by domain: `mt.nekostream.site`) wrap MPEG-TS segments inside PNG containers, presumably to bypass detection. A local HTTP proxy strips the PNG wrapper before passing data to the video player.

### Detection

```python
PNG_WRAP_CDNS = {"mt.nekostream.site"}

parsed_m3u8 = urlparse(m3u8_url)
if parsed_m3u8.netloc in PNG_WRAP_CDNS:
    proxy_url, proxy_server = _proxy_hls(m3u8_url, referer, cdn_domain)
```

### Implementation

```python
def _proxy_hls(playlist_url, referer, cdn_domain) -> tuple[str, HTTPServer]:
    # 1. Download the master playlist
    resp = SESSION.get(playlist_url, headers={"Referer": referer})

    # 2. Rewrite all segment URIs to localhost proxy paths
    seg_map = {}
    new_lines = []
    for line in body.splitlines():
        if line.startswith("#") or not line.strip():
            new_lines.append(line)
            continue
        # Resolve relative vs absolute
        orig = line if "://" in line else f"{base}/{line.lstrip('/')}"
        path = f"/seg/{idx}"
        seg_map[path] = orig
        new_lines.append(path)

    # 3. Start local HTTP server that:
    #    - Serves rewritten playlist
    #    - Fetches original segments, strips PNG IEND wrapper, returns raw TS
```

### Segment Proxy Handler

```python
class _SegmentProxyHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/playlist.m3u8":
            # Serve rewritten playlist
            self.wfile.write(server._playlist.encode())
            return

        orig_url = server._seg_map.get(self.path)
        resp = SESSION.get(orig_url, headers={"Referer": server._referer})
        data = resp.content

        if data[:1] == b"\x89":  # PNG signature
            # Strip PNG container: skip PNG headers, find IEND chunk
            offset = 8
            while offset < len(data) - 8:
                chunk_len = int.from_bytes(data[offset:offset+4], 'big')
                if data[offset+4:offset+8] == b"IEND":
                    data = data[offset + 12:]  # Raw TS data after IEND
                    break
                offset += 12 + chunk_len

        self.send_response(200)
        self.send_header("Content-Type", "video/MP2T")
        self.wfile.write(data)
```

### Pattern for Other Sites

```python
# 1. Some CDNs wrap video segments in non-video containers (PNG, etc.)
# 2. Detect by examining first bytes of segment (PNG = \x89)
# 3. Create local proxy that:
#    a. Rewrites playlist to point to localhost
#    b. Fetches original segments
#    c. Strips container wrapper
#    d. Returns raw MPEG-TS
# 4. Runs in a daemon thread, shutdown after playback
```

---

## 8. Fallback Extraction

If the MegaClone pattern fails (no `File {id} -` match), a generic regex-based extraction is used:

```python
def _extract_generic(self, embed_url, quality_pref):
    resp = SESSION.get(embed_url, headers={"Referer": self.site_url})
    text = resp.text

    # Try m3u8 first
    m3u8_match = re.search(r'https?://[^"\'<> ]+?\.m3u8[^"\'<> ]*', text)
    if m3u8_match:
        url = m3u8_match.group(0)
        # Optionally pick variant
        variant = _pick_hls_variant(url, quality_pref)
        return StreamSource(url=variant or url, ...)

    # Fallback to mp4
    mp4_match = re.search(r'https?://[^"\'<> ]+?\.mp4[^"\'<> ]*', text)
    if mp4_match:
        return StreamSource(url=mp4_match.group(0), ...)
```

### Regex Pattern

```
https?://[^"\'<> ]+?\.(m3u8|mp4)[^"\'<> ]*
```

This catches video URLs embedded in JavaScript variables, data attributes, or hidden elements.

---

## 9. Reusable Pattern Template

Apply this template to any new anime streaming site:

```python
from typing import Optional
import re, json
import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse
from .base import BaseProvider
from your_models import SearchResult, Episode, StreamSource
from your_core import SESSION, SCRAPE_TIMEOUT

class NewSiteProvider(BaseProvider):
    name = "NewSite"
    site_url = "https://example.to"

    # ─── SETUP ─────────────────────────────────────────────
    def get_supported_qualities(self) -> list[str]:
        return ["1080p", "720p", "480p", "best"]

    def get_supported_audio(self) -> list[str]:
        return ["sub", "dub"]

    # ─── PHASE 1: SEARCH ──────────────────────────────────
    def search(self, query: str) -> list[SearchResult]:
        results = []
        try:
            resp = SESSION.get(
                f"{self.site_url}/filter",
                params={"keyword": query},
                timeout=SCRAPE_TIMEOUT,
            )
            if resp.status_code != 200:
                return results
            soup = BeautifulSoup(resp.text, "lxml")

            for item in soup.select(".main .item"):  # ← ADAPT SELECTOR
                # Extract unique ID
                poster = item.select_one(".poster")
                media_id = poster.get("data-tip") if poster else None
                if not media_id:
                    continue

                # Extract title
                title_el = item.select_one(".name")
                title = title_el.get_text(strip=True) if title_el else ""
                if not title or query.lower() not in title.lower():
                    continue

                # Extract thumbnail
                img = item.select_one("img")
                thumb = img.get("src") if img else ""

                results.append(SearchResult(
                    title=title,
                    url=f"{self.site_url}/mid/{media_id}",  # ← ADAPT URL PATTERN
                    site_name=self.name,
                    image=thumb or "",
                ))
        except requests.RequestException:
            pass
        return results

    # ─── PHASE 2: EPISODES ────────────────────────────────
    def get_episodes(self, result: SearchResult) -> list[Episode]:
        episodes = []
        # Extract ID from URL
        m = re.search(r"/mid/(\d+)", result.url)  # ← ADAPT regex
        if not m:
            return episodes
        media_id = m.group(1)
        an = result.title.split(" (")[0].strip()

        try:
            resp = SESSION.get(
                f"{self.site_url}/ajax/episode/list/{media_id}",  # ← ADAPT
                headers={"X-Requested-With": "XMLHttpRequest", "Referer": result.url},
                timeout=SCRAPE_TIMEOUT,
            )
            if resp.status_code != 200:
                return episodes
            body = resp.json()

            # Some sites return HTML in a JSON field
            if body.get("status") != 200:
                return episodes
            soup = BeautifulSoup(body.get("result", ""), "lxml")  # ← ADAPT field name

            for a in soup.find_all("a", href=True):
                num = a.get("data-num", "")
                title = a.get_text(strip=True) or f"Episode {num}"
                data_ids = a.get("data-ids", "")
                if not data_ids:
                    continue  # ← REQUIRED for server phase

                # Collect ALL data attributes for later phases
                episodes.append(Episode(
                    title=title,
                    url=result.url,
                    number=str(num),
                    site_name=self.name,
                    anime_name=an,
                    data={
                        "data_ids": data_ids,
                        "media_id": media_id,
                        **{k: a.get(k) for k in ("data-mal", "data-slug", "data-timestamp") if a.get(k)},
                    },
                ))
        except (requests.RequestException, json.JSONDecodeError, KeyError):
            pass
        return episodes

    # ─── PHASE 3: SERVERS ─────────────────────────────────
    def get_servers(self, episode: Episode) -> list[dict]:
        data_ids = episode.data.get("data_ids", "")
        if not data_ids:
            return []
        servers = []
        seen = set()

        try:
            resp = SESSION.get(
                f"{self.site_url}/ajax/server/list?servers={data_ids}",  # ← ADAPT
                headers={"X-Requested-With": "XMLHttpRequest", "Referer": episode.url},
                timeout=SCRAPE_TIMEOUT,
            )
            if resp.status_code == 200:
                body = resp.json()
                if body.get("status") == 200:
                    soup = BeautifulSoup(body["result"], "lxml")

                    for li in soup.select("li[data-link-id]"):  # ← ADAPT SELECTOR
                        name = li.get_text(strip=True)
                        link_id = li.get("data-link-id", "")
                        # Determine sub/dub from parent container
                        dtype = "sub"
                        parent = li.find_parent(class_="type")  # ← ADAPT
                        if parent:
                            dt = parent.get("data-type", "")
                            if dt == "dub":   dtype = "dub"
                            elif dt == "hsub": dtype = "hsub"

                        display = f"{name} ({dtype.upper()})"
                        if display not in seen:
                            seen.add(display)
                            servers.append({
                                "name": name,
                                "display": display,
                                "link_id": link_id,
                                "type": dtype,
                            })
        except (requests.RequestException, json.JSONDecodeError, KeyError):
            pass

        # ── MAPPER FALLBACK (if available) ──
        # Some sites have a secondary server API keyed by MAL ID + slug + timestamp
        mal_id = episode.data.get("mal_id", "")
        slug = episode.data.get("slug", "")
        ts = episode.data.get("ts", "")
        if mal_id and slug and ts:
            try:
                r2 = SESSION.get(
                    f"https://mapper.nekostream.site/api/mal/{mal_id}/{slug}/{ts}",  # ← ADAPT
                    timeout=SCRAPE_TIMEOUT,
                )
                if r2.status_code == 200:
                    mapper_data = r2.json()
                    for sv_name, sv_data in mapper_data.items():
                        if sv_name == "status": continue
                        for lang in ("sub", "dub"):
                            entry = sv_data.get(lang)
                            if not entry or not entry.get("url"):
                                continue
                            display = f"{sv_name} ({lang.upper()})"
                            if display not in seen:
                                seen.add(display)
                                servers.append({
                                    "name": sv_name,
                                    "display": display,
                                    "link_id": entry["url"],
                                    "type": lang,
                                })
            except (requests.RequestException, json.JSONDecodeError):
                pass

        return servers

    # ─── PHASE 4: STREAM EXTRACTION ───────────────────────
    def extract_stream(
        self, episode: Episode,
        audio_pref: str = "sub",
        quality_pref: str = "best",
    ) -> Optional[StreamSource]:

        servers = self.get_servers(episode)
        if not servers:
            return None

        # Auto-pick first server matching audio preference
        link_id = None
        for sv in servers:
            if sv.get("type") == audio_pref and sv.get("link_id"):
                link_id = sv["link_id"]
                break
        if not link_id and servers:
            link_id = servers[0].get("link_id", "")
        if not link_id:
            return None

        # Resolve embed URL from link_id
        try:
            resp = SESSION.get(
                f"{self.site_url}/ajax/server?get={link_id}",  # ← ADAPT
                headers={"X-Requested-With": "XMLHttpRequest", "Referer": episode.url},
                timeout=SCRAPE_TIMEOUT,
            )
            if resp.status_code != 200:
                return None
            body = resp.json()
            embed_url = body.get("result", {}).get("url", "")
            if not embed_url:
                return None
        except (requests.RequestException, json.JSONDecodeError, KeyError):
            return None

        return self._extract_from_embed(embed_url, quality_pref)

    # ─── EMBED EXTRACTOR ──────────────────────────────────
    def _extract_from_embed(
        self, embed_url: str, quality_pref: str
    ) -> Optional[StreamSource]:

        domain = urlparse(embed_url).netloc

        try:
            resp = SESSION.get(
                embed_url,
                headers={"Referer": self.site_url},
                timeout=SCRAPE_TIMEOUT,
            )
            if resp.status_code != 200:
                return None

            # ── STRATEGY A: MegaClone-type (file_id via regex) ──
            # Look for pattern like "File 12345 -" in the embed page
            file_id_match = re.search(r"File\s+(\d+)\s+-", resp.text)
            if file_id_match:
                file_id = file_id_match.group(1)
                api_base = f"https://{domain}"

                sources_resp = SESSION.get(
                    f"{api_base}/stream/getSources?id={file_id}",  # ← ADAPT
                    headers={"Referer": embed_url, "X-Requested-With": "XMLHttpRequest"},
                    timeout=SCRAPE_TIMEOUT,
                )
                if sources_resp.status_code == 200:
                    sources_data = sources_resp.json()
                    sources = sources_data.get("sources", {})
                    m3u8_url = sources.get("file", "") if isinstance(sources, dict) else ""

                    if m3u8_url:
                        # Optional: pick specific HLS variant
                        if quality_pref != "best":
                            variant = self._pick_hls_variant(m3u8_url, quality_pref)
                            if variant:
                                m3u8_url = variant

                        # Optional: handle PNG-wrapped CDNs
                        proxy_server = None
                        if urlparse(m3u8_url).netloc in {"mt.nekostream.site"}:  # ← ADAPT
                            proxy_url, proxy_server = self._proxy_hls(
                                m3u8_url, f"{api_base}/",
                            )
                            if proxy_url:
                                m3u8_url = proxy_url

                        # Extract subtitle tracks
                        subtitles = [
                            {"url": t["file"], "label": t.get("label", ""),
                             "lang": t.get("label", "").lower()}
                            for t in sources_data.get("tracks", [])
                            if t.get("kind") == "captions" and t.get("file")
                        ]

                        return StreamSource(
                            url=m3u8_url,
                            site_name=self.name,
                            quality=quality_pref,
                            is_direct=True,
                            headers={"Referer": f"{api_base}/"},
                            subtitles=subtitles or None,
                            proxy_server=proxy_server,
                        )

            # ── STRATEGY B: Generic regex fallback ──
            # Look for .m3u8 or .mp4 URLs in the page
            m3u8_match = re.search(
                r'https?://[^"\'<> ]+?\.m3u8[^"\'<> ]*', resp.text
            )
            if m3u8_match:
                url = m3u8_match.group(0)
                return StreamSource(
                    url=url, site_name=self.name,
                    quality=quality_pref, is_direct=True,
                    headers={"Referer": embed_url.split("/stream")[0] + "/"},
                )

            mp4_match = re.search(
                r'https?://[^"\'<> ]+?\.mp4[^"\'<> ]*', resp.text
            )
            if mp4_match:
                return StreamSource(
                    url=mp4_match.group(0), site_name=self.name,
                    quality=quality_pref, is_direct=True,
                )

        except requests.RequestException:
            pass
        return None

    # ─── HLS VARIANT PICKER ──────────────────────────────
    def _pick_hls_variant(self, master_url: str, quality_pref: str) -> Optional[str]:
        """Pick the best matching HLS variant for the desired quality."""
        target_h = int(quality_pref.replace("p", ""))
        try:
            resp = SESSION.get(
                master_url,
                headers={"Referer": "https://megaplay.buzz/"},  # ← ADAPT
                timeout=SCRAPE_TIMEOUT,
            )
            if resp.status_code != 200:
                return None
            body = resp.text
        except requests.RequestException:
            return None

        best_url, best_h = None, 0
        fallback_url, fallback_h = None, 99999
        lines = body.splitlines()

        for i, line in enumerate(lines):
            if line.startswith("#EXT-X-STREAM-INF:"):
                m = re.search(r"RESOLUTION=(\d+)x(\d+)", line)
                if not m:
                    continue
                h = int(m.group(2))
                if i + 1 >= len(lines):
                    continue
                variant = lines[i + 1].strip()
                if not variant or variant.startswith("#"):
                    continue
                if not variant.startswith("http"):
                    base = master_url.rsplit("/", 1)[0]
                    variant = f"{base}/{variant.lstrip('/')}"

                if h <= target_h and h > best_h:
                    best_h, best_url = h, variant
                if h < fallback_h:
                    fallback_h, fallback_url = h, variant

        return best_url or fallback_url

    # ─── PNG-WRAPPED CDN PROXY ────────────────────────────
    def _proxy_hls(
        self, playlist_url: str, referer: str, cdn_domain: str = ""
    ) -> tuple[Optional[str], Optional[object]]:
        """Create local proxy for CDNs that wrap TS in PNG containers."""
        from http.server import HTTPServer, BaseHTTPRequestHandler
        import threading

        resp = SESSION.get(playlist_url, headers={"Referer": referer},
                          timeout=SCRAPE_TIMEOUT)
        if resp.status_code != 200:
            return playlist_url, None
        body = resp.text

        seg_map = {}
        idx = 0
        new_lines = []
        base = playlist_url.rsplit("/", 1)[0]
        for line in body.splitlines():
            if line.startswith("#") or not line.strip():
                new_lines.append(line)
                continue
            orig = line if "://" in line else f"{base}/{line.lstrip('/')}"
            path = f"/seg/{idx}"
            seg_map[path] = orig
            new_lines.append(path)
            idx += 1

        # IMPORTANT: Handler accesses attributes via self.server (automatically
        # set by HTTPServer when handling requests). Do NOT use a closure
        # variable — the handler class is defined at method scope but
        # self.server is populated at request time by the base class.
        class _ProxyHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path == "/playlist.m3u8":
                    self.send_response(200)
                    self.send_header("Content-Type", "application/vnd.apple.mpegurl")
                    self.end_headers()
                    self.wfile.write(self.server._playlist.encode())
                    return
                orig_url = self.server._seg_map.get(self.path)
                if not orig_url:
                    self.send_response(404)
                    self.end_headers()
                    return
                try:
                    resp = SESSION.get(orig_url, headers={"Referer": self.server._referer"},
                                      timeout=SCRAPE_TIMEOUT)
                    if resp.status_code != 200:
                        self.send_response(502)
                        self.end_headers()
                        return
                    data = resp.content
                    # Strip PNG wrapper if present
                    if data[:1] == b"\x89":
                        offset = 8
                        while offset < len(data) - 8:
                            chunk_len = int.from_bytes(data[offset:offset+4], 'big')
                            if data[offset+4:offset+8] == b"IEND":
                                data = data[offset + 12:]
                                break
                            offset += 12 + chunk_len
                    self.send_response(200)
                    self.send_header("Content-Type", "video/MP2T")
                    self.send_header("Content-Length", str(len(data)))
                    self.send_header("Connection", "close")
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.end_headers()
                    self.wfile.write(data)
                except Exception:
                    self.send_response(502)
                    self.end_headers()
            def log_message(self, *a): pass

        class _ProxyServer(HTTPServer):
            def __init__(self, playlist, ref, seg_map):
                self._playlist = playlist
                self._referer = ref
                self._seg_map = seg_map
                super().__init__(("127.0.0.1", 0), _ProxyHandler)

        server = _ProxyServer("\n".join(new_lines), referer, seg_map)
        port = server.server_address[1]
        t = threading.Thread(target=server.serve_forever, daemon=True)
        t.start()

        return f"http://127.0.0.1:{port}/playlist.m3u8", server
```

### Applying to Any Site — Checklist

| Phase | What to Look For |
|---|---|
| **Search** | Filter/search pages with card-based results. Look for `data-tip`, `data-id`, or `data-slug` on poster elements. |
| **Episodes** | AJAX JSON endpoints returning HTML. Episode `<a>` tags carrying `data-num` and `data-ids`. |
| **Servers** | AJAX JSON with HTML list of servers; `li[data-link-id]` pattern. Sub/dub types in parent wrappers. |
| **Mapper API** | External lookup API keyed by MAL ID + slug + timestamp. Returns dict of server_name → sub/dub entries. |
| **Embed** | `/ajax/server?get={link_id}` → embed URL. Patterns: `/stream/` for MegaClone, generic iframe. |
| **Sources** | MegaClone: `File {n} -` regex → `/{base}/getSources?id={n}` → m3u8 + subtitles. |
| **HLS Vars** | `#EXT-X-STREAM-INF` with `RESOLUTION=WxH` — pick by height, fallback to lowest. |
| **CDN Bypass** | PNG-wrapped segments: detect by `\x89` magic byte, strip IEND chunk in local proxy. |
| **Generic** | Regex `https?://[^"\'<> ]+?\.(m3u8|mp4)` for simple embeds. |

### Common Pitfalls

1. **Cookie/Session persistence** — Always use a shared `requests.Session()` to maintain cookies across AJAX calls
2. **X-Requested-With header** — Many AJAX endpoints reject requests without this header
3. **Referer header** — Each API call often needs the correct Referer matching the previous page
4. **HTML-in-JSON pattern** — Server responses may embed HTML inside JSON fields; always parse both layers
5. **Mapper/fallback APIs** — Some servers only appear via external APIs, not the primary AJAX call
6. **Relative URIs in HLS playlists** — Always resolve variant URLs against the master playlist base
7. **PNG-wrapped segments** — When segments start with `\x89` (PNG magic), strip the PNG frame and extract raw TS
8. **Thread-safe proxy server** — Start the local proxy as a daemon thread; shutdown after playback ends
