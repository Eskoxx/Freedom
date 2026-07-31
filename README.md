# Freedom

Freedom turns your terminal into a portable media streamer. Open the app, and a text-based interface (with full mouse support) lets you search across multiple providers, stream or download content, and play it back with hardware-accelerated MPV — all without leaving the terminal.

Freedom also runs on **Android** — the Freedom app has a dedicated Android build with a native APK. See the [Freedom Android](https://github.com/Eskoxx/Freedom-Android) repo.

## Contents

- [For Users](#for-users)
  - [What You Can Do](#what-you-can-do)
  - [Getting Started](#getting-started)
  - [Keybindings](#keybindings)
  - [Requirements](#requirements)
  - [Updating](#updating)
  - [Known Limitations](#known-limitations)
- [For Developers and AI Agents](#for-developers-and-ai-agents)

## For Users

### What You Can Do

- **Search** — search for any anime, movie, or show across multiple online providers, or pick a specific provider to search individually
- **Stream** — play videos directly in MPV (supports hardware-accelerated GPU rendering). Most providers offer multiple quality options (360p, 720p, 1080p) — toggle with the `v` key. Audio can be toggled between sub and dub with the `a` key
- **Torrents** — search torrent sites with two separate categories: torrent-anime (Nyaa) and torrent-movies (TPB, EZTV); stream magnet links via built-in webtorrent (no external torrent client needed). Supports pause/resume and partial playback (starts after 50MB buffered)
- **Download** — save content to disk for offline viewing with yt-dlp, including subtitle embedding
- **Resume** — watch history is maintained so you can pick up where you left off
- **Plugin system** — write and load custom provider plugins without modifying the app, or use the AI-powered plugin generator to auto-discover site APIs

### Getting Started

1. **Clone or download** the repository.
2. **Install system dependencies:**

   ```bash
   sudo apt install mpv yt-dlp ffmpeg
   npm install -g webtorrent-cli
   ```

3. **Install Python packages:**

   ```bash
   pip install -r requirements.txt
   ```

4. **Run the app:**

   ```bash
   ./anime-watch
   ```

   On first launch you'll see the splash screen. Type a query and press `Enter` to start searching.

### Keybindings

**Splash Screen:**
| Key | Action |
|---|---|
| Type + `Enter` | Search |
| `h` | View watch history |
| `Tab` / click | Switch category (Anime / Movies / Torrent) |
| `Ctrl+C` or `Escape` | Quit |

**Browser Screen (search results, episodes):**
| Key | Action |
|---|---|
| `↑`/`↓` or `k`/`j` | Navigate list |
| `Enter` | Select / open next level |
| `/` | Focus search bar |
| `s` | Toggle sidebar |
| `L` | View downloads |
| `h` | View history |
| `←`/`→` | Switch category |
| `a` | Toggle audio sub / dub (anime section) |
| `v` | Toggle quality (1080p / 720p / 360p or 480p — lowest quality varies by provider) |
| `d` | Download current item |
| `Escape` | Go back |
| `q` or `Ctrl+C` | Quit |

**Downloads Screen:**
| Key | Action |
|---|---|
| `↑`/`↓` or `k`/`j` | Navigate list |
| `Enter` | Activate selected |
| `p` | Pause / resume download |
| `x` | Cancel download |
| `d` | Delete file from disk |
| `w` | Watch completed download |
| `Escape` | Go back |
| `q` or `Ctrl+C` | Quit |

**History Screen:**
| Key | Action |
|---|---|
| `↑`/`↓` or `k`/`j` | Navigate list |
| `Enter` | Resume watching selection |
| `Escape` | Go back |
| `q` or `Ctrl+C` | Quit |

**Torrent Operation Screen:**
| Key | Action |
|---|---|
| `Escape` | Close |
| `n` | Next episode |
| `Ctrl+C` | Quit |

### Requirements

- Linux (terminal environment)
- Python 3.10+
- ~500MB free disk space
- Internet connection (for streaming)
- Recommended for Wi-Fi users — streaming uses a lot of mobile data
- Be patient: playback can take up to 10 seconds to start
- `mpv` — video player (required)
- `yt-dlp` — download support (recommended)
- `ffmpeg` — subtitle muxing (optional, for downloads)
- `webtorrent-cli` — torrent streaming (optional, npm package)
- For torrents: active internet with DHT/tracker access

### Updating

Pull the latest code and reinstall if needed. Python dependencies are pinned in `requirements.txt`. The plugin system is file-based — user plugins in `user_providers/` and `~/.config/anime-watch/providers/` persist across updates.

### Known Limitations

- Search can occasionally return no results — just try searching again, a fresh request often works.
- Torrent downloads have no progress bar yet (not implemented).
- The project is refined primarily for streaming; downloading works for most providers but is not thoroughly configured.
- The 11 built-in providers are temporary — they work as of 31 July 2026 but can break at any time.
- Stream providers (scrapers) scrape undocumented websites that change without warning and **will** break over time.
- Torrent streaming requires at least one active seeder on the magnet link.
- Some providers (e.g. NetMirror) require cookies from a real browser login.
- The app is designed for a terminal — best experience in a fullscreen terminal with a dark theme.
- **Mouse support is fully built-in** — you can click buttons, select categories, and navigate lists with your mouse. The TUI is mouse-friendly throughout all screens.

---

> **This is a fun side project, not a product.** The built-in streaming providers (scrapers) are brittle — they scrape undocumented websites that change without warning, and **will** break over time. The real goal of this project is the **plugin system**: anyone can write their own provider plugin without modifying the app. If a provider breaks, fix it yourself, share it, or write a new one. The plugin system is the part that lasts; the providers are just examples... And if you're a technical person, none of this matters — everything is editable; just change the files.

## For Developers and AI Agents

See [`AI_PLUGIN_DEV.md`](AI_PLUGIN_DEV.md) — it covers the plugin contract, site exploration, config-driven code generation, proxy-based providers, performance optimization, and development workflow. Additional documentation in [`SPEEDRACE_API.md`](SPEEDRACE_API.md) (Fmovies SpeedRace decryption) and [`extraction-method.md`](extraction-method.md) (Anikoto PNG proxy pipeline).

### Quick plugin commands

```bash
# List all plugin subcommands
python3 -m anime_watch plugin --help

# Generate a provider from a discovered config
python3 -m anime_watch plugin generate config.json

# Validate a provider plugin
python3 -m anime_watch plugin validate user_providers/myprovider.py

# Test a plugin live
python3 -m anime_watch plugin test user_providers/myprovider.py --query "One Piece"

# Auto-discover API patterns on a site
python3 -m anime_watch plugin discover https://mysite.com --test

# Install a plugin
python3 -m anime_watch plugin install user_providers/myprovider.py
```

### Architecture

```
anime-watch                  ← shell entrypoint
└── python3 -m anime_watch
    ├── tui/                 ← Textual TUI (screens, widgets, player, downloader)
    ├── providers/           ← Streaming site scrapers + plugin loader
    ├── torrent/             ← BitTorrent engine (webtorrent-cli subprocess)
    ├── plugin/              ← AI-capable plugin system (generate, validate, test, discover)
    └── user_providers/      ← User-installed third-party plugins
```

---

## Disclaimer

Freedom is not hosting any kind of content and the developer(s) of this application does not have any affiliation with the content providers that are freely available in the internet.
