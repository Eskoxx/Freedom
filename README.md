# Freedom

**Freedom is a terminal-based media client for searching, streaming, and managing online video through a mouse-driven TUI.**

It provides a unified interface for connecting to external media providers, playing streams through [MPV](https://mpv.io/), downloading media through `yt-dlp`, and extending the application through a plugin system.

Freedom does **not host, store, or distribute video content**. Media is obtained directly from the external sources configured or selected by the user.

> **Important:** Freedom is a client application. Users are responsible for ensuring that their use of the software and the content they access complies with the laws and terms applicable to them.

Freedom also runs on **Android** — a dedicated build with a native APK. See the [Freedom Android](https://github.com/Eskoxx/Freedom-Android) repo, or grab the ready-to-install **arm64 APK** from the [releases](https://github.com/Eskoxx/Freedom-Android/releases) page.

## Features

* **Search** — query configured media providers from a single terminal interface.
* **Streaming** — play compatible streams directly through MPV, including hardware-accelerated playback where supported.
* **Quality selection** — select from the quality options exposed by a provider.
* **Audio selection** — switch between available audio tracks where supported.
* **Downloads** — download supported media through `yt-dlp` for offline playback.
* **Watch history** — resume previously watched content.
* **Plugin system** — add custom providers without modifying the core application.
* **Torrent support** — optionally handle magnet links through WebTorrent when torrent functionality is enabled.
* **Mouse-driven TUI** — navigate the application using both keyboard and mouse.

## Architecture

Freedom is designed as a **client-side media interface**.

```text
                    Freedom
                       │
          ┌────────────┼────────────┐
          │            │            │
       Providers      MPV        yt-dlp
          │            │            │
          ↓            ↓            ↓
    External media   Playback    Downloads
       sources
```

Freedom does not operate a media hosting service and does not upload media to a central Freedom server.

Providers are independent components that retrieve metadata or playable media from external services. Provider availability can change when those services change their APIs, pages, authentication requirements, or access policies.

## Providers

Freedom supports a plugin-based provider architecture.

Providers are responsible for implementing the logic required to search an external service and resolve playable media. Users can create their own providers and load them without modifying the core application.

Because external services can change or restrict access at any time, provider compatibility is not guaranteed.

## Getting Started

### Requirements

* Linux or Windows (native or WSL)
* Python 3.10+
* ~500MB free disk space
* Internet connection (required for streaming)
* **MPV** — video player (required — the app will not start without it)
* `yt-dlp` — download functionality (recommended)
* FFmpeg — optional media processing / subtitle muxing for downloads
* WebTorrent CLI — optional torrent functionality (`npm install -g webtorrent-cli`)
* Python packages: `requests`, `beautifulsoup4`, `textual`, `lxml`, `curl_cffi` (installed via `pip install -r requirements.txt`)

### Linux

Debian / Ubuntu:

```bash
sudo apt install mpv yt-dlp ffmpeg
npm install -g webtorrent-cli
```

Arch / Manjaro:

```bash
sudo pacman -S mpv yt-dlp ffmpeg
npm install -g webtorrent-cli
```

Fedora:

```bash
sudo dnf install mpv yt-dlp ffmpeg
npm install -g webtorrent-cli
```

openSUSE:

```bash
sudo zypper install mpv yt-dlp ffmpeg
npm install -g webtorrent-cli
```

### Windows

Using `winget`:

```powershell
winget install mpv-player.mpv
winget install yt-dlp.yt-dlp
winget install Gyan.FFmpeg
npm install -g webtorrent-cli
```

A terminal capable of mouse input is recommended (PowerShell or Windows Terminal). Alternatively, use [WSL](https://learn.microsoft.com/windows/wsl/install) and follow the Debian/Ubuntu instructions inside the WSL distro.

### Install Freedom

```bash
git clone https://github.com/Eskoxx/Freedom.git
cd Freedom
pip install -r requirements.txt
```

Run:

```bash
./anime-watch
```

## Keybindings

| Key                    | Action                         |
| ---------------------- | ------------------------------ |
| `↑` / `↓` or `k` / `j` | Navigate                       |
| `Enter`                | Select                         |
| `/`                    | Search                         |
| `s`                    | Toggle sidebar                 |
| `h`                    | Watch history                  |
| `L`                    | Downloads                      |
| `a`                    | Change available audio track   |
| `v`                    | Change available video quality |
| `d`                    | Download supported media       |
| `Escape`               | Go back                        |
| `q` / `Ctrl+C`         | Quit                           |

Mouse navigation is supported throughout the TUI.

## Plugins

Freedom supports external provider plugins.

A provider can be added without changing the core application:

```text
user_providers/
└── my_provider.py
```

User providers can also be stored in:

```text
~/.config/anime-watch/providers/
```

See `AI_PLUGIN_DEV.md` and `extraction-method.md` for information about developing providers.

## Limitations

Freedom depends on external services and therefore has inherent limitations:

* Providers can stop working without notice.
* External services may require authentication or cookies.
* Stream availability depends on the source.
* Playback performance depends on the provider, network connection, and local hardware.
* Torrent playback requires available peers/seeders.
* Some providers may impose geographic, authentication, or other access restrictions.

## Legal & Responsible Use

Freedom is **client software**. It does not host or maintain a central library of movies, anime, or other media.

The application can interact with external services and protocols that may provide both authorized and unauthorized content. The legality of accessing or downloading particular material depends on the source, the rights associated with that material, and the laws applicable to the user.

**Users are solely responsible for the content they access, stream, download, or share using Freedom.**

The project does not grant users any rights to copyrighted material and does not endorse unauthorized copying or distribution of copyrighted works.

## License

Freedom is released under the **GNU General Public License v3.0**.

See [`LICENSE`](LICENSE) for the full license text.

## Disclaimer

Freedom is provided as open-source software without warranty.

The project is not affiliated with, endorsed by, or sponsored by third-party media providers, content owners, MPV, `yt-dlp`, WebTorrent, or other external services used by the application.

External providers and services may change or become unavailable at any time.
