# AI Plugin Development Guide

This document explains how an AI agent can autonomously create,
validate, test, and install a streaming provider plugin for this app.

---

## 1. Understand the Plugin Contract

Every plugin is a Python file with a class that inherits `BaseProvider`.

**Required attributes:**
| Attribute  | Type   | Example                            |
|------------|--------|------------------------------------|
| `name`     | `str`  | `"MyProvider"`                     |
| `slug`     | `str`  | `"myprovider"`                     |
| `url`      | `str`  | `"https://mysite.com"`             |
| `category` | `str`  | `"anime"` or `"movies"`            |

**Required methods:**

| Method | Signature | Returns | Purpose |
|--------|-----------|---------|---------|
| `search` | `(self, query: str)` | `list[SearchResult]` | Search for anime/movies |
| `get_episodes` | `(self, result: SearchResult)` | `list[Episode]` | Get episode list for a show |
| `extract_stream` | `(self, episode: Episode, audio_pref="sub", quality_pref="best")` | `Optional[StreamSource]` | Get playable stream URL |

**Optional methods:**

| Method | Signature | Returns | Purpose |
|--------|-----------|---------|---------|
| `get_servers` | `(self, episode: Episode)` | `list[dict]` | Return available servers for server-selection TUI |
| `resolve` | `(self, media: MediaResult, audio_pref="sub", quality_pref="best")` | `Optional[StreamSource]` | Resolve a movie directly (bypass episode list) |

**`get_servers()` return format:**
```python
[
    {"name": "server_id", "display": "Server Name (Sub)", "link_id": "...", "type": "sub"},
]
```
Each dict has keys: `name` (internal ID), `display` (shown to user, may include `(Sub)`/`(Dub)`), `link_id` (used by `_check_server_alive`), `type` (audio type hint).

**`resolve()`** is for providers that support movie search (non-episodic content). It receives a `MediaResult` (e.g. from TMDB search) and returns a `StreamSource` directly. If your provider doesn't handle movies, skip this method.

**Available utilities** (import from `anime_watch.core`):
- `SESSION` — pre-configured `requests.Session` with browser User-Agent
- `SCRAPE_TIMEOUT` — default timeout (8 seconds)
- `scrape_page_for_video(url, name)` — generic video URL extractor
- `extract_with_ytdlp(url)` — yt-dlp based stream extraction

---

## 2. Explore the Target Site

Use the browser tool to explore the site. For each step, examine the
HTML structure and record CSS selectors.

### Step A: Search

1. Navigate to the site's search page URL (e.g., `https://site.com/search?q=naruto`)
2. Try different URL patterns: `/search?q=`, `/search?keyword=`, `/?s=`
3. Examine the HTML of search results — find the container, title elements,
   links, and images
4. Note down:
   - **Search URL pattern** (path + params)
   - **CSS selector for result items** (e.g., `.anime-card`, `a[href*="/anime/"]`)
   - **How to extract title** (text content? attribute like `data-title`?)
   - **How to extract link** (`href` attribute)
   - **How to extract image** (optional: `img` tag, `src` attribute)

### Step B: Episode List

1. Open a search result page (the series/movie page)
2. Look for episode links — they often match patterns like `/episode/1`, `/ep-1`, `/watch/123`
3. Note down:
   - **CSS selector for episode items** (e.g., `a[href*="/episode/"]`)
   - **Episode number extraction** (from link URL? from element text? from a `data-*` attribute?)
   - **Can the generic extractor handle this?** (Generic works when episode links match common patterns)

### Step C: Stream / Video

1. Open an episode page
2. Look for where the video player is embedded:
   - **Direct in-page `<video>` tag** with `<source src="...">`
   - **Iframe** pointing to an embed host (e.g., `iframe[src*="embed"]`)
   - **JavaScript** that builds the player
3. For iframes: open the embed URL and check for:
   - Direct `.m3u8` or `.mp4` URLs in the page source
   - Whether yt-dlp can handle it

---

## 3. Finding Sites to Plugin

The best source of new streaming sites to build plugins for:

- **Movies**: https://fmhy.net/video — curated, regularly updated list of
  movie streaming sites
- **Anime**: https://everythingmoe.com/ — comprehensive directory of anime
  streaming sites

Browse these directories, pick a site whose HTML structure looks consistent
and parseable, then explore it as described in step 2.

---

## 4. Build the Config File

Create a JSON config file describing the site structure. Here's a
complete reference with examples:

### Minimal config (generic extraction):

```json
{
  "name": "MyAnime",
  "slug": "myanime",
  "url": "https://myanime.site",
  "category": "anime",
  "search": {
    "url": "/search",
    "params": {"q": "{query}"},
    "result_selector": "a[href*='/anime/']",
    "title_from": "text",
    "link_attr": "href"
  },
  "episodes": {
    "use_generic": true
  },
  "stream": {
    "type": "scrape"
  }
}
```

### Full config with custom selectors:

```json
{
  "name": "AnimeHub",
  "slug": "animehub",
  "url": "https://animehub.gg",
  "category": "anime",
  "search": {
    "url": "/search",
    "params": {"keyword": "{query}"},
    "method": "GET",
    "result_selector": "div.item",
    "title_from": "text",
    "link_attr": "href",
    "image_selector": "img",
    "image_attr": "src",
    "headers": {
      "X-Requested-With": "XMLHttpRequest"
    }
  },
  "episodes": {
    "use_generic": false,
    "result_selector": "ul.episodes li a",
    "title_attr": "text",
    "link_attr": "href",
    "number_regex": "(\\d+)",
    "number_from": "regex"
  },
  "stream": {
    "type": "iframe",
    "iframe_selector": "iframe[src*='embed']",
    "use_ytdlp": true,
    "referer": "https://animehub.gg/",
    "extract_mp4": true,
    "extract_m3u8": true
  }
}
```

### Config for yt-dlp only sites:

```json
{
  "name": "DirectPlay",
  "slug": "directplay",
  "url": "https://directplay.tv",
  "category": "movies",
  "search": {
    "url": "/search",
    "params": {"q": "{query}"},
    "result_selector": "div.movie-card a",
    "title_from": "text",
    "link_attr": "href",
    "image_selector": "img",
    "image_attr": "src"
  },
  "episodes": {
    "use_generic": true
  },
  "stream": {
    "type": "ytdlp"
  }
}
```

### Config for sites with M3U8 directly in page:

```json
{
  "name": "StreamFast",
  "slug": "streamfast",
  "url": "https://streamfast.io",
  "category": "anime",
  "search": {
    "url": "/search",
    "params": {"s": "{query}"},
    "result_selector": "article a",
    "title_from": "text",
    "link_attr": "href"
  },
  "episodes": {
    "use_generic": false,
    "result_selector": "div.ep-list a",
    "title_attr": "text",
    "link_attr": "href",
    "number_from": "regex",
    "number_regex": "/(\\d+)$"
  },
  "stream": {
    "type": "m3u8_in_page",
    "extract_m3u8": true
  }
}
```

### Config fields reference:

| Section | Field | Type | Default | Description |
|---------|-------|------|---------|-------------|
| (root) | `name` | string | — | Display name for the provider |
| (root) | `slug` | string | (auto) | Lowercase identifier, used as filename |
| (root) | `url` | string | — | Base URL (no trailing slash) |
| (root) | `category` | string | `"anime"` | `"anime"` or `"movies"` |
| `search` | `url` | string | `"/search"` | Search path relative to base URL |
| `search` | `params` | object | `{"q": "{query}"}` | Query params; `{query}` is replaced |
| `search` | `method` | string | `"GET"` | HTTP method |
| `search` | `headers` | object | `{}` | Extra HTTP headers |
| `search` | `result_selector` | string | — | CSS selector for each result item |
| `search` | `title_from` | string | `"text"` | `"text"` or an attribute name like `"data-title"` |
| `search` | `link_attr` | string | `"href"` | Attribute to extract link from |
| `search` | `image_selector` | string | *none* | CSS selector for image within result |
| `search` | `image_attr` | string | `"src"` | Attribute to extract image URL from |
| `episodes` | `use_generic` | bool | `true` | Use generic episode link finder |
| `episodes` | `result_selector` | string | — | CSS selector for episode links (if not generic) |
| `episodes` | `title_attr` | string | `"text"` | `"text"` or attribute for episode title |
| `episodes` | `link_attr` | string | `"href"` | Attribute for episode link |
| `episodes` | `number_regex` | string | `"(\\d+)"` | Regex to extract episode number |
| `episodes` | `number_from` | string | `"text"` | Where to apply regex: `"text"`, `"attr"`, or `"regex"` |
| `episodes` | `number_attr` | string | — | Attribute name if `number_from` is `"attr"` |
| `stream` | `type` | string | `"scrape"` | `"scrape"`, `"iframe"`, `"ytdlp"`, or `"m3u8_in_page"` |
| `stream` | `iframe_selector` | string | `"iframe[src*='embed']"` | CSS selector for video iframe |
| `stream` | `use_ytdlp` | bool | `true` | Fall back to yt-dlp for iframe URLs |
| `stream` | `referer` | string | *episode URL* | Custom Referer header for embed requests |
| `stream` | `extract_mp4` | bool | `true` | Look for `.mp4` URLs in embed page |
| `stream` | `extract_m3u8` | bool | `true` | Look for `.m3u8` URLs in embed page |

---

## 4. Generate the Plugin

Once the config JSON is ready:

```bash
python -m anime_watch plugin generate /tmp/animehub_config.json
```

This produces a `.py` file in `user_providers/` and runs validation.

---

## 5. Validate

The generator runs validation automatically. To re-validate later:

```bash
python -m anime_watch plugin validate user_providers/animehub.py
```

Fix any ERROR level issues (missing attributes, wrong method signatures).
WARNING level issues are informational (unimplemented method bodies).

---

## 6. Test Live

Run a real query against the plugin:

```bash
python -m anime_watch plugin test user_providers/animehub.py --query "One Piece"
```

This tests all three methods in sequence:

1. **search** — expects at least 1 result
2. **get_episodes** (using first search result) — expects at least 1 episode
3. **extract_stream** (using first episode) — expects a valid `StreamSource` with URL

If a step fails, diagnose and fix:
- **search fails**: Check the URL pattern, parameter names, CSS selectors.
  Use the browser tool to manually visit the search URL and examine HTML.
- **episodes fails**: Check the episode page URL structure. Try the generic
  extractor first; if it doesn't work, add custom selectors.
- **stream fails**: This is the trickiest. Check:
  - Is the correct iframe/embed URL being extracted?
  - Can the embed page be fetched? (Check for Cloudflare, Referer requirements)
  - Is the .m3u8/.mp4 URL pattern correct?
  - Try yt-dlp on the embed URL manually: `yt-dlp --dump-json <embed-url>`

### Common stream extraction patterns & troubleshooting:

| Symptom | Likely cause | Fix |
|---------|-------------|-----|
| 403 on embed page | Missing Referer header | Add `"referer"` to stream config |
| 403 on CDN (.m3u8) | CDN checks Referer | Add Referer to `StreamSource.headers` in generated code |
| Cloudflare challenge | Server blocks bots | Use `"type": "ytdlp"` — yt-dlp handles CF |
| Empty page returned | Site requires cookies | Not easily fixable; find alternative site |
| No .m3u8 in page | Player is JS-generated | Try `"type": "ytdlp"` or `scrape_page_for_video` |
| PNG-wrapped HLS | Anti-leech (PNG prefix on TS segments) | Requires proxy server (see anikoto provider for reference) |

---

## 7. Install

```bash
python -m anime_watch plugin install user_providers/animehub.py
```

This copies the plugin to `~/.config/anime-watch/providers/`.
Restart the app — the provider is loaded automatically.

---

## 8. Building a Proxy-Based Provider (Advanced)

Some sites serve HLS video from CDN networks that rotate segment URLs across
hundreds of domains. Each new domain requires a cold DNS+TCP+TLS handshake
(4-8 seconds). The browser handles this by downloading segments from multiple
domains in parallel — your provider must do the same.

### When You Need This Pattern

- The variant playlist contains segment URLs from many different domains
- Direct playback (passing the m3u8 URL to mpv) is slow, with audio underruns
- Segments download fine individually but sequential fetching starves the buffer
- The player stalls at startup or during playback

### Architecture Overview

Your `extract_stream` method should:

1. Extract the variant playlist URL (`.m3u8`)
2. Fetch and parse the playlist to discover all segment URLs and their domains
3. Rewrite the playlist to localhost paths (`/seg/0`, `/seg/1`, etc.)
4. Start a local HTTP proxy server that:
   a. **Domain-race the first segment** — download seg 0 from ALL unique domains
      concurrently using **dedicated `requests.Session` per thread** (separate
      connection pools avoid pool contention)
   b. **Parallel prefetch** — spawn 3 background worker threads that download
      remaining segments from a shared `queue.Queue`; failed segments go back
      on the queue for retry
   c. **Handler fallback** — when the player requests a not-yet-cached segment,
      the HTTP handler downloads it on-demand with a 15s timeout
5. Return `StreamSource(url=f"http://127.0.0.1:{port}/playlist.m3u8")`
   with `proxy_server=server` so the app shuts it down on playback end

### Detailed Implementation Steps

#### Step 8a — Imports and Constants

```python
from __future__ import annotations
import queue
import socketserver
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import Optional
import requests

PREFETCH_WORKERS = 3   # 3, not 7 — more workers saturates limited bandwidth
SEGMENT_TIMEOUT = 15   # max seconds per segment download
```

#### Step 8b — Playlist Rewriter

Fetch the variant playlist and rewrite every segment URL to `/seg/N`.

```python
def _build_proxy_playlist(variant_url: str, session: requests.Session
                          ) -> Optional[tuple[str, list[str], dict]]:
    resp = session.get(variant_url, headers={"Referer": "https://yoursite.com/"},
                       timeout=15)
    if resp.status_code != 200:
        return None
    lines = resp.text.splitlines()
    seg_urls: list[str] = []
    seg_idx = 0
    out: list[str] = []
    for line in lines:
        if "://" in line:
            seg_urls.append(line.rstrip())
            out.append(f"/seg/{seg_idx}")
            seg_idx += 1
        else:
            out.append(line.rstrip())
    if not seg_urls:
        return None
    return "\n".join(out), seg_urls, {}
```

This keeps the original CDN domain per segment — **do NOT normalize all segments
to a single domain**. Each CDN edge has variable performance; spreading requests
across edges avoids hitting the slow ones repeatedly.

#### Step 8c — Threaded HTTP Server with Prefetch

Create a threaded HTTP server that serves the rewritten playlist and segments.

```python
class _ThreadedHTTPServer(socketserver.ThreadingMixIn, HTTPServer):
    allow_reuse_address = True


class _ProxyServer(_ThreadedHTTPServer):
    def __init__(self, playlist: str, seg_urls: list[str],
                 cache: dict, session: requests.Session):
        self._playlist = playlist
        self._seg_urls = seg_urls
        self._cache = cache          # dict[int, bytes] — segment index → data
        self._session = session
        self._seg_queue = queue.Queue()
        for i in range(len(seg_urls)):
            self._seg_queue.put_nowait(i)
        super().__init__(("127.0.0.1", 0), _ProxyHandler)
        self._race_first_segment()   # blocks until seg 0 is cached
        self._start_prefetch()       # starts background workers
```

##### First-Segment Domain Racing

```python
    def _race_first_segment(self):
        if not self._seg_urls or 0 in self._cache:
            return
        first = self._seg_urls[0]
        path = first.split("/", 3)[3]
        domains = set()
        for url in self._seg_urls:
            domains.add(url.split("/")[2])

        done = threading.Event()

        def _race(domain: str):
            url = f"https://{domain}/{path}"
            # CRITICAL: each race thread gets its own Session.
            # Sharing the main session causes connection-pool contention
            # and can leave threads in a hung state.
            rsess = requests.Session()
            rsess.headers.update({
                "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36",
                "Referer": "https://yoursite.com/",
            })
            try:
                resp = rsess.get(url, timeout=SEGMENT_TIMEOUT)
                if resp.status_code == 200:
                    self._cache[0] = resp.content
                    done.set()
            except Exception:
                pass

        for d in domains:
            threading.Thread(target=_race, args=(d,), daemon=True).start()

        done.wait(timeout=SEGMENT_TIMEOUT)
```

Why dedicated sessions: if all race threads and prefetch workers share one
`requests.Session`, the single connection pool becomes a bottleneck. All threads
contend for pool slots, and read-timeout connections can leave the pool in a
corrupted state. Each race thread using its own pool avoids this entirely.

The race runs **before** prefetch starts, so all available bandwidth goes to
the first segment. This gives the fastest possible startup (typically 4-7s for
the winner, vs 8-15s if prefetch workers compete).

##### Parallel Prefetch Workers

```python
    def _start_prefetch(self):
        for _ in range(PREFETCH_WORKERS):
            t = threading.Thread(target=self._prefetch_worker, daemon=True)
            t.start()

    def _prefetch_worker(self):
        while True:
            try:
                idx = self._seg_queue.get(timeout=5)
            except queue.Empty:
                return            # no more segments to prefetch
            if idx in self._cache:
                continue
            url = self._seg_urls[idx]
            try:
                resp = self._session.get(url, timeout=SEGMENT_TIMEOUT)
                if resp.status_code == 200:
                    self._cache[idx] = resp.content
                else:
                    self._seg_queue.put_nowait(idx)   # retry on non-200
            except Exception:
                self._seg_queue.put_nowait(idx)       # retry on exception
```

Key design decisions:

- **3 workers** — With 7+ workers, concurrent downloads saturate the typical
  5-10 Mbps downstream, making every segment slower. 3 workers provide enough
  parallelism to keep ahead of playback without flooding the link.

- **queue.Queue** — Workers grab the next un-downloaded index atomically. On
  failure, the index goes back on the queue. This guarantees every segment is
  eventually retried without needing complex tracking.

- **No Event/wait mechanism** — The handler fallback (step 8d) handles the case
  where the player requests a segment before a worker has cached it. Workers and
  handler may briefly duplicate a download, which is harmless.

##### HTTP Request Handler with Fallback

```python
class _ProxyHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        server: _ProxyServer = self.server
        if self.path == "/playlist.m3u8":
            body = server._playlist.encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/vnd.apple.mpegurl")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if not self.path.startswith("/seg/"):
            self.send_response(404)
            self.end_headers()
            return

        idx_str = self.path[5:]
        if not idx_str.isdigit():
            self.send_response(404)
            self.end_headers()
            return
        idx = int(idx_str)

        cached = server._cache.get(idx)
        if cached is not None:
            self.send_response(200)
            self.send_header("Content-Type", "video/MP2T")
            self.send_header("Content-Length", str(len(cached)))
            self.end_headers()
            self.wfile.write(cached)
            return

        # Handler fallback — segment not yet cached by prefetch workers
        orig = server._seg_urls[idx]
        try:
            resp = server._session.get(orig, timeout=SEGMENT_TIMEOUT)
            resp.raise_for_status()
            server._cache[idx] = resp.content
            self.send_response(200)
            self.send_header("Content-Type", "video/MP2T")
            self.send_header("Content-Length", str(len(resp.content)))
            self.end_headers()
            self.wfile.write(resp.content)
        except Exception:
            self.send_response(502)
            self.end_headers()

    def log_message(self, *a):
        pass
```

The handler checks the cache first. On a cache miss, it downloads the segment
from the CDN, caches it, and serves it. This is the safety net — after the
first 2-3 segments have been served via fallback, the prefetch workers usually
catch up and subsequent segments are served from cache instantly.

#### Step 8d — Wired into `extract_stream`

```python
def extract_stream(self, episode, audio_pref="sub", quality_pref="best"):
    # ... fetch sources, get master playlist URL ...
    sess = self._sess()

    # Pick variant by quality preference
    max_h = QUALITY_HEIGHTS.get(quality_pref, 99999)
    v = _pick_variant(master_url, max_h, headers)
    if v:
        url = v

    # Build proxy playlist and start server
    result = _build_proxy_playlist(url, sess)
    if not result:
        return None
    playlist_text, seg_urls, cache = result

    server = _ProxyServer(playlist_text, seg_urls, cache, sess)
    port = server.server_address[1]
    st = threading.Thread(target=server.serve_forever, daemon=True)
    st.start()

    return StreamSource(
        url=f"http://127.0.0.1:{port}/playlist.m3u8",
        site_name=self.name,
        quality=quality_pref,
        is_direct=True,
        headers=headers,
        proxy_server=server,
    )
```

The `proxy_server=server` field tells the player to call `server.shutdown()`
when playback ends, freeing the port.

#### Step 8e — Session Setup

```python
from requests.adapters import HTTPAdapter

def _sess(self) -> requests.Session:
    if self._session is None:
        s = requests.Session()
        adapter = HTTPAdapter(pool_maxsize=50, pool_connections=50,
                              max_retries=0)
        s.mount("https://", adapter)
        s.mount("http://", adapter)
        s.headers.update({
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36",
            "Referer": self.url,
        })
        self._session = s
    return self._session
```

- `pool_maxsize=50`: allow up to 50 concurrent connections to the same host
- `pool_connections=50`: cache up to 50 distinct host connection pools
- `max_retries=0`: the proxy's queue-based retry handles failures, not urllib3

### Performance Expectations

With this proxy architecture:

| Metric | Before (no proxy) | After (with proxy) |
|--------|-------------------|-------------------|
| First frame | 13-15 seconds | ~10 seconds |
| Audio underruns | Frequent | None |
| Cache buffer | 2-5 seconds | 25+ seconds |
| Segment delivery | Sequential, cold per domain | Parallel + cached |

The remaining startup overhead is mpv's GPU initialization (typically 3-4s),
which is outside the proxy's control.

### mpv Tuning (in `player.py`)

When a provider returns a proxied stream, the player should launch mpv with
these flags:

```
mpv --cache=yes --cache-secs=30 --cache-pause-initial=no
    --demuxer-max-bytes=10M --stream-lavf-o=http_multiple=1
```

- `cache-secs=30`: allow 30 seconds of video buffer
- `demuxer-max-bytes=10M`: allow 10MB pre-buffer (3-4 segments)
- `cache-pause-initial=no`: start playback immediately after first segment,
  don't wait for the full buffer
- `http_multiple=1`: enable parallel segment fetching in ffmpeg's HLS demuxer

### Common Pitfalls

- **Don't normalize segment domains.** Pinning all segments to one CDN edge
  makes every segment slow if that edge has a bad route. Keep original domains.

- **Don't share the race session with workers.** Each race thread must use
  its own `requests.Session`. Sharing causes connection-pool corruption and
  mysterious timeouts.

- **Don't warm domains with partial reads.** Never do `resp.raw.read(1)` to
  "warm" a connection — this leaves the connection in a half-consumed state,
  corrupting the pool.

- **Don't use Events to coordinate cache misses.** If the handler waits for
  a worker to cache a segment, and the worker fails, the handler hangs for
  the full timeout. Let the handler download on-demand instead.

- **Don't exceed 3-4 prefetch workers.** More parallel downloads saturate
  the typical 5-10 Mbps residential connection, making every segment slower.

### Reference Implementation

See `anime_watch/providers/fmovies.py` — the complete proxy with domain racing,
parallel prefetch, and queue-based retry.

---

## Complete Workflow Summary

```bash
# 1. Explore (AI uses browser tool to find CSS selectors)
# 2. Write config JSON
cat > /tmp/config.json << 'EOF'
{
  "name": "MyProvider",
  "slug": "myprovider",
  "url": "https://mysite.com",
  "category": "anime",
  "search": {
    "url": "/search",
    "params": {"q": "{query}"},
    "result_selector": "a.link",
    "title_from": "text",
    "link_attr": "href"
  },
  "episodes": {"use_generic": true},
  "stream": {"type": "scrape"}
}
EOF

# 3. Generate
python -m anime_watch plugin generate /tmp/config.json

# 4. Validate (automatic, but can rerun)
python -m anime_watch plugin validate user_providers/myprovider.py

# 5. Test
python -m anime_watch plugin test user_providers/myprovider.py --query "One Piece"

# 6. If test fails, revise config or edit the .py file directly, goto 4
# 7. Install
python -m anime_watch plugin install user_providers/myprovider.py

# 8. Done — restart the app
```
