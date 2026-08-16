import asyncio, json, os, re, sys, threading, time
from rich.style import Style
from rich.text import Text
from textual import work
from textual.app import ComposeResult
from textual.containers import Container, Horizontal, Vertical, ScrollableContainer
from textual.screen import Screen
from textual.widgets import Input, Static, Button, ListView, ListItem, Label
from textual.binding import Binding

from anime_watch.models import SearchResultGroup, TorrentResult
from anime_watch.providers import ANIME_PROVIDERS, CONFIGURED_PROVIDERS, CONFIGURED_SITES, MOVIE_PROVIDERS, TORRENT_PROVIDERS, search_configured, get_episodes
from anime_watch.tui import widgets as _w
from anime_watch.tui.widgets import LogoWidget, RuleWidget, SidebarWidget, ResultsPanel, DownloadsPanel, HistoryPanel, FooterHints
from anime_watch.history import HistoryEntry, add_entry as add_history_entry, get_continue_watching, get_history
from anime_watch.tui.player import PlaybackHandler
from anime_watch.tui.downloader import DownloadHandler

_request_log_ctx = threading.local()

def _ensure_meta(torrent, dest):
    os.makedirs(dest, exist_ok=True)
    with open(os.path.join(dest, ".meta.json"), "w") as f:
        json.dump({"info_hash": torrent.info_hash, "magnet": torrent.magnet, "name": torrent.name}, f)

def _remove_meta(dest):
    p = os.path.join(dest, ".meta.json")
    if os.path.exists(p):
        os.remove(p)

def _check_alive(url: str, timeout: int = 5) -> bool | None:
    try:
        import requests
        r = requests.get(url, timeout=timeout, headers={"User-Agent": "Mozilla/5.0"})
        return r.status_code < 500
    except Exception:
        return False

def _check_server_alive(provider, episode, link_id: str) -> bool | None:
    try:
        from anime_watch.core import SESSION, SCRAPE_TIMEOUT
        if hasattr(provider, 'url') and getattr(provider, 'slug', '') == 'anikoto':
            resp = SESSION.get(
                f"{provider.url}/ajax/server?get={link_id}",
                headers={"X-Requested-With": "XMLHttpRequest", "Referer": episode.url},
                timeout=8,
            )
            if resp.status_code == 200:
                body = resp.json()
                return body.get("status") == 200 and bool(body.get("result", {}).get("url"))
        return None
    except Exception:
        return False

class SplashScreen(Screen):
    BINDINGS = [
        Binding("escape", "quit", "Quit"),
        Binding("ctrl+c", "quit", "Quit"),
        Binding("h", "view_history", "History"),
    ]

    def compose(self) -> ComposeResult:
        with Container(classes="splash-root"):
            with Vertical(classes="splash-center"):
                yield LogoWidget()
                yield Static("", classes="spacer")
                with Horizontal(classes="splash-toggle-row"):
                    yield Button("Anime", id="splash-cat-anime", classes="cat-btn")
                    yield Button("Movies", id="splash-cat-movies", classes="cat-btn")
                    yield Button("Torrent", id="splash-cat-torrent", classes="cat-btn")
                    yield Button("Music", id="splash-cat-music", classes="cat-btn")
                    yield Button("Video", id="splash-cat-video", classes="cat-btn")
                with Horizontal(id="torrent-branch", classes="branch-row"):
                    yield Button("├── Anime", id="splash-sub-anime", classes="branch-btn")
                    yield Button("└── Movies", id="splash-sub-movies", classes="branch-btn")
                with Horizontal(id="provider-row", classes="branch-row"):
                    yield Static("Pick a provider", id="provider-label", classes="branch-btn")
                yield Input(placeholder="❯ Search anime…", id="splash-search")
                yield Static("", classes="spacer-sm")
                with Horizontal(classes="splash-hints-row"):
                    yield Static("↵", classes="hint-key")
                    yield Static(" search  ·  ", classes="hint-text")
                    yield Button("downloads", id="splash-downloads-btn", classes="hint-btn")
                    yield Static("  ·  ", classes="hint-text")
                    yield Button("history", id="splash-history-btn", classes="hint-btn")
                    yield Static("  ·  ", classes="hint-text")
                    yield Button("dev?", id="splash-community-btn", classes="hint-btn")
                    yield Static("  ·  ", classes="hint-text")
                    yield Button("theme", id="splash-theme-btn", classes="hint-btn")
                    yield Static("  ·  ", classes="hint-text")
                    yield Static("^c", classes="hint-key")
                    yield Static(" quit", classes="hint-text")
                yield Static("", classes="spacer-sm")
                yield Container(id="continue-watching", classes="continue-box")

    def on_mount(self):
        self._set_category(self.app.search_category)
        self.query_one("#splash-search", Input).focus()
        self._refresh_continue_watching()
        self._update_theme_label()

    def _update_theme_label(self):
        try:
            from anime_watch.tui.themes import THEMES
            btn = self.query_one("#splash-theme-btn", Button)
            btn.label = f"theme: {THEMES.get(self.app._theme_name, {}).get('name', 'Midnight')}"
        except Exception:
            pass

    def on_screen_resume(self):
        self.query_one("#splash-search", Input).focus()

    def _populate_providers(self, cat: str):
        from anime_watch.providers import ANIME_PROVIDERS, MOVIE_PROVIDERS, TORRENT_PROVIDERS
        row = self.query_one("#provider-row")
        if cat in ("torrent", "torrent-anime", "torrent-movies"):
            providers = TORRENT_PROVIDERS
        elif cat == "music":
            from anime_watch.providers import MUSIC_PROVIDERS
            providers = MUSIC_PROVIDERS
        elif cat == "video":
            from anime_watch.providers import VIDEO_PROVIDERS
            providers = VIDEO_PROVIDERS
        elif cat == "anime":
            providers = ANIME_PROVIDERS
        else:
            providers = MOVIE_PROVIDERS
        existing = {c.id for c in row.children if c.id and c.id.startswith("sp-prov-")}
        need = {f"sp-prov-{slug}" for slug in providers}
        if existing == need:
            return
        for child in list(row.children):
            child.remove()
        rank = {s.slug: s.rank for s in CONFIGURED_SITES}
        for slug in sorted(providers.keys(), key=lambda k: (rank.get(k, 99), k)):
            btn = Button(providers[slug].name, id=f"sp-prov-{slug}", classes="branch-btn")
            row.mount(btn)
        row.add_class("visible")

    def _set_category(self, cat: str):
        from anime_watch.providers import set_target_provider
        set_target_provider("")
        self.app.search_category = cat
        has_branch = cat in ("torrent", "torrent-anime", "torrent-movies")
        branch = self.query_one("#torrent-branch")
        if has_branch:
            branch.add_class("visible")
        else:
            branch.remove_class("visible")
        if cat == "torrent":
            self.query_one("#splash-search", Input).placeholder = "❯ Select a torrent type…"
        else:
            self.query_one("#splash-search", Input).placeholder = f"❯ Search {cat.replace('torrent-', 'torrent ')}…"
        for btn_id in ["splash-cat-anime", "splash-cat-movies", "splash-cat-video", "splash-cat-torrent", "splash-cat-music"]:
            self.query_one(f"#{btn_id}").remove_class("active")
        self.query_one("#splash-sub-anime").remove_class("active")
        self.query_one("#splash-sub-movies").remove_class("active")
        if cat in ("anime", "movies", "music", "video"):
            self.query_one(f"#splash-cat-{cat}").add_class("active")
        elif cat == "torrent-anime":
            self.query_one("#splash-cat-torrent").add_class("active")
            self.query_one("#splash-sub-anime").add_class("active")
        elif cat == "torrent-movies":
            self.query_one("#splash-cat-torrent").add_class("active")
            self.query_one("#splash-sub-movies").add_class("active")
        elif cat == "torrent":
            self.query_one("#splash-cat-torrent").add_class("active")
        self._populate_providers(cat)
        self.query_one("#splash-search", Input).focus()

    def _refresh_continue_watching(self):
        cw = get_continue_watching(limit=7)
        box = self.query_one("#continue-watching", Container)
        for child in list(box.children):
            child.remove()
        if not cw:
            return
        title = Static(" Continue Watching", classes="cw-title")
        box.mount(title)
        for entry in cw:
            pct = entry.progress_pct
            label = f"  {entry.display}  [{pct:.0f}%]"
            safe_id = "cw-" + re.sub(r'[^a-zA-Z0-9_-]', '_', entry.anime_name)[:50]
            btn = Button(label, id=safe_id, classes="cw-btn")
            btn._cw_entry = entry
            box.mount(btn)

    def on_button_pressed(self, event: Button.Pressed):
        if event.button.id == "splash-cat-anime":
            self._set_category("anime")
        elif event.button.id == "splash-cat-movies":
            self._set_category("movies")
        elif event.button.id == "splash-cat-video":
            self._set_category("video")
        elif event.button.id == "splash-cat-torrent":
            self._set_category("torrent")
        elif event.button.id == "splash-cat-music":
            self._set_category("music")
        elif event.button.id == "splash-theme-btn":
            self.app.action_cycle_theme()
            self._update_theme_label()
        elif event.button.id == "splash-sub-anime":
            self._set_category("torrent-anime")
            self.query_one("#splash-search", Input).focus()
        elif event.button.id == "splash-sub-movies":
            self._set_category("torrent-movies")
            self.query_one("#splash-search", Input).focus()
        elif event.button.id and event.button.id.startswith("sp-prov-"):
            slug = event.button.id.replace("sp-prov-", "")
            from anime_watch.providers import set_target_provider
            set_target_provider(slug)
            for btn in self.query("#provider-row .branch-btn"):
                btn.remove_class("active")
            event.button.add_class("active")
            if slug == "netmirror":
                self.app.push_screen(NetmirrorSetupOverlay())
            else:
                self.query_one("#splash-search", Input).focus()
        elif event.button.id == "splash-downloads-btn":
            self.app.push_screen(DownloadsScreen())
        elif event.button.id == "splash-history-btn":
            self.app.push_screen(HistoryScreen())
        elif event.button.id == "splash-community-btn":
            self.app.push_screen(CommunityOverlay())
        elif event.button.id and event.button.id.startswith("cw-"):
            entry = getattr(event.button, "_cw_entry", None)
            if entry:
                self._continue_watching(entry)

    def _continue_watching(self, entry: HistoryEntry):
        from anime_watch.models import Episode
        data = entry.data.copy()
        if entry.progress > 0:
            data["_resume_at"] = entry.progress
        ep = Episode(
            title=entry.episode_title,
            url=entry.url,
            number=entry.episode_number,
            site_name=entry.site_name,
            anime_name=entry.anime_name,
            data=data,
        )
        self.app.search_query = entry.anime_name
        self.app._direct_play_episode = ep
        self.app.switch_screen(BrowserScreen())

    def on_input_submitted(self, event: Input.Submitted):
        q = event.value.strip()
        if not q:
            return
        cat = self.app.search_category
        if cat == "torrent":
            self.query_one("#splash-search", Input).placeholder = "❯ Pick Anime or Movies above first"
            return
        self.app.search_query = q
        self.app.switch_screen(BrowserScreen())

    def action_quit(self):
        self.app.exit()

    def action_view_history(self):
        self.app.push_screen(HistoryScreen())

def _fmt_age(seconds: float) -> str:
    if seconds < 60:
        return f"{int(seconds)}s"
    elif seconds < 3600:
        return f"{int(seconds // 60)}m"
    elif seconds < 86400:
        return f"{int(seconds // 3600)}h"
    else:
        return f"{int(seconds // 86400)}d"


class CommunityOverlay(Screen):
    """One-time invite to join the project's Telegram community."""

    COMMUNITY_URL = "https://t.me/+9pd7FaXTnYYzY2Zl"
    MARKER = os.path.expanduser("~/.config/anime-watch/community_seen")

    BINDINGS = [
        Binding("escape", "later", "Later"),
        Binding("ctrl+c", "quit", "Quit"),
    ]

    def compose(self) -> ComposeResult:
        with Container(classes="op-root"):
            with Vertical(classes="op-card"):
                yield Static("Join the Community", id="cm-title", classes="op-title")
                yield Static("", classes="spacer-sm")
                yield Static(
                    "Report bugs, request features, or just\n"
                    "hang out — the dev is active there.\n\n"
                    "[dim]Telegram[/dim]\n"
                    f"[dim]{CommunityOverlay.COMMUNITY_URL}[/dim]",
                    id="cm-body",
                )
                yield Static("", classes="spacer-sm")
                with Horizontal(classes="splash-hints-row"):
                    yield Button("join", id="cm-join-btn", classes="hint-btn")
                    yield Static(" · ", classes="hint-text")
                    yield Button("maybe later", id="cm-later-btn", classes="hint-btn")
                    yield Static(" · ", classes="hint-text")
                    yield Static("esc", classes="hint-key")
                    yield Static(" close", classes="hint-text")

    def action_later(self):
        self.app.pop_screen()

    @staticmethod
    def _mark_seen():
        try:
            os.makedirs(os.path.dirname(CommunityOverlay.MARKER), exist_ok=True)
            with open(CommunityOverlay.MARKER, "w") as f:
                f.write("1")
        except OSError:
            pass

    def _open_link(self):
        try:
            if os.environ.get("ANDROID_ROOT") is not None:
                import shutil as _shutil
                import subprocess as _sp
                cmd = ["termux-am", "start", "-a", "android.intent.action.VIEW", "-d", self.COMMUNITY_URL]
                if _shutil.which("termux-am") is None:
                    cmd[0] = "am"
                _sp.Popen(cmd, stdout=_sp.DEVNULL, stderr=_sp.DEVNULL)
            else:
                import subprocess as _sp
                _sp.Popen(["xdg-open", self.COMMUNITY_URL],
                          stdout=_sp.DEVNULL, stderr=_sp.DEVNULL)
        except Exception:
            pass

    def on_button_pressed(self, event: Button.Pressed):
        if event.button.id == "cm-join-btn":
            self._mark_seen()
            self._open_link()
            self.app.pop_screen()
        elif event.button.id == "cm-later-btn":
            self.app.pop_screen()

    def on_mount(self):
        self.query_one("#cm-join-btn", Button).focus()


class NetmirrorSetupOverlay(Screen):
    BINDINGS = [
        Binding("escape", "close", "Close"),
        Binding("ctrl+c", "quit", "Quit"),
    ]

    def compose(self) -> ComposeResult:
        with Container(classes="op-root"):
            with Vertical(classes="op-card"):
                yield Static("NetMirror Setup Required", id="ns-title", classes="op-title")
                yield Static("", classes="spacer-sm")
                yield Static("", id="ns-body")
                yield Static("", id="ns-status", classes="op-log-line")
                with Horizontal(classes="op-buttons"):
                    yield Button("OK", id="ns-ok-btn", classes="ns-btn")
                    yield Button("Run Script", id="ns-run-btn", classes="ns-btn")

    def action_close(self):
        self.app.pop_screen()

    def on_mount(self):
        cookie_path = os.path.expanduser("~/.config/anime-watch/net77_cookies.json")
        if os.path.exists(cookie_path):
            age = time.time() - os.path.getmtime(cookie_path)
            if age > 3 * 3600:
                # Cookies fetched more than 3 hours ago: force the setup
                # panel (Run Script) regardless of the server-side check.
                self._show_setup_instructions()
                self.query_one("#ns-ok-btn", Button).focus()
                return
            self.query_one("#ns-title", Static).update("Checking NetMirror Cookies")
            self.query_one("#ns-body", Static).update(
                "Saved cookies found — verifying with server...\n\n"
                f"[dim]Fetched {_fmt_age(age)} ago[/dim]"
            )
            self.query_one("#ns-run-btn").display = False
            asyncio.create_task(self._check_cookies_async())
        else:
            self._show_setup_instructions()
            self.query_one("#ns-ok-btn", Button).focus()

    def _show_setup_instructions(self):
        self.query_one("#ns-title", Static).update("NetMirror Setup Required")
        self.query_one("#ns-body", Static).update(
            "NetMirror needs authentication cookies from\n"
            "net77.cc to search and stream.\n\n"
            "Open a terminal and run:\n\n"
            "  [bold yellow]python3 -m anime_watch.setup_netmirror[/bold yellow]\n\n"
            "Or press [bold]Run Script[/bold] to launch it from here.\n"
            "This opens a browser window — sign in with\n"
            "Gmail and cookies are saved automatically."
        )
        self.query_one("#ns-run-btn").display = True
        self.query_one("#ns-ok-btn", Button).focus()

    def _cookie_label(self) -> str:
        import json as _json
        try:
            with open(os.path.expanduser("~/.config/anime-watch/net77_cookies.json")) as _f:
                cid = _json.load(_f).get("_id")
            if cid:
                return f"Cookies #{cid}"
        except Exception:
            pass
        return "Cookies"

    def _show_cookies_valid(self, age: str):
        self.query_one("#ns-title", Static).update("[green]✓ Cookies Valid[/green]")
        self.query_one("#ns-body", Static).update(
            "NetMirror cookies are valid — you can search\n"
            "and stream normally.\n\n"
            f"[dim]{self._cookie_label()} — fetched {age} ago[/dim]"
        )
        self.query_one("#ns-run-btn").display = False
        self.query_one("#ns-ok-btn", Button).focus()

    def _show_cookies_expired(self, age: str):
        self.query_one("#ns-title", Static).update("[red]✗ Cookies Expired[/red]")
        self.query_one("#ns-body", Static).update(
            "Saved cookies are no longer valid.\n\n"
            "Open a terminal and run:\n\n"
            "  [bold yellow]python3 -m anime_watch.setup_netmirror[/bold yellow]\n\n"
            "Or press [bold]Run Script[/bold] to launch it from here.\n"
            "This opens a browser window — sign in with\n"
            "Gmail and cookies are saved automatically."
            + f"\n\n[dim]{self._cookie_label()} — fetched {age} ago[/dim]"
        )
        self.query_one("#ns-run-btn").display = True
        self.query_one("#ns-ok-btn", Button).focus()

    async def _check_cookies_async(self):
        import json as _json
        cookie_path = os.path.expanduser("~/.config/anime-watch/net77_cookies.json")
        age = _fmt_age(time.time() - os.path.getmtime(cookie_path))
        try:
            with open(cookie_path) as _f:
                raw = _json.load(_f)
            # Strip metadata keys (_id, _fetched_at) — sending them as cookies
            # makes the server-side check fail.
            cookies = {k: v for k, v in raw.items() if not k.startswith("_")}
            if not cookies:
                self._show_cookies_expired(age)
                return
            valid = await asyncio.to_thread(self._test_cookies, cookies)
            if valid:
                self._show_cookies_valid(age)
            else:
                self._show_cookies_expired(age)
        except Exception:
            self._show_setup_instructions()

    @staticmethod
    def _test_cookies(cookies: dict) -> bool:
        import requests as _req
        try:
            resp = _req.post(
                "https://net77.cc/play.php",
                data={"id": "82911782"},
                headers={
                    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36",
                    "Referer": "https://net77.cc/",
                },
                cookies=cookies,
                timeout=10,
            )
            if resp.status_code != 200:
                return False
            raw = resp.json().get("h", "")
            parts = raw.split("::")
            return len(parts) >= 6 and bool(parts[5])
        except Exception:
            return False

    def on_button_pressed(self, event: Button.Pressed):
        if event.button.id == "ns-ok-btn":
            self.app.pop_screen()
        elif event.button.id == "ns-run-btn":
            self._run_script()

    def _run_script(self):
        self.query_one("#ns-run-btn", Button).disabled = True
        self.query_one("#ns-ok-btn", Button).disabled = True
        self.query_one("#ns-status", Static).update("Running setup...")
        asyncio.create_task(self._run_script_async())

    async def _run_script_async(self):
        import subprocess as _sp
        loop = asyncio.get_running_loop()
        try:
            from importlib.resources import files as _pkg_files
            script_path = str(_pkg_files("anime_watch").joinpath("setup_netmirror.py"))
            result = await loop.run_in_executor(None, lambda: _sp.run(
                [sys.executable, "-u", script_path],
                capture_output=True, text=True, timeout=300,
            ))
            out = result.stdout.strip()
            if out:
                last = out.splitlines()[-1]
                self._add_status(last)
            if result.returncode == 0:
                self._add_status("[green]Done! Cookies saved.[/green]")
            else:
                self._add_status(f"[red]Script failed (code {result.returncode})[/red]")
        except _sp.TimeoutExpired:
            self._add_status("[red]Script timed out (5 min)[/red]")
        except FileNotFoundError:
            self._add_status("[red]python3 not found[/red]")
        self._enable_buttons()

    def _add_status(self, msg: str):
        self.query_one("#ns-status", Static).update(msg)

    def _enable_buttons(self):
        self.query_one("#ns-run-btn", Button).disabled = False
        self.query_one("#ns-ok-btn", Button).disabled = False


class DownloadsScreen(Screen):
    BINDINGS = [
        Binding("escape", "back", "Back"),
        Binding("q", "quit", "Quit"),
        Binding("ctrl+c", "quit", "Quit"),
        Binding("up", "move_up", "", priority=True),
        Binding("down", "move_down", "", priority=True),
        Binding("k", "move_up", ""),
        Binding("j", "move_down", ""),
        Binding("enter", "activate", ""),
        Binding("p", "pause_resume", ""),
        Binding("x", "cancel", ""),
        Binding("w", "watch_file", ""),
    ]

    def __init__(self):
        super().__init__()
        self._current_path = ""

    def compose(self) -> ComposeResult:
        with Container(classes="browser-root"):
            with Horizontal(classes="top-bar"):
                yield LogoWidget()
                yield Static("  Downloads Library", id="status-bar", classes="status")
            yield RuleWidget(classes="rule")
            with Horizontal(classes="body"):
                with Vertical(classes="content-area"):
                    with Container(classes="panel-wrap"):
                        yield DownloadsPanel(id="downloads-list")
                    with Horizontal(id="dl-actions"):
                        yield Button("Pause", id="dl-pause", classes="dl-btn")
                        yield Button("Watch", id="dl-watch", classes="dl-btn")
                        yield Button("Cancel", id="dl-cancel", classes="dl-btn")
            yield FooterHints(id="footer")

    def on_mount(self):
        self._update_footer()
        self.refresh_downloads()
        dl_panel = self.query_one("#downloads-list", DownloadsPanel)
        dl_panel.focus()
        self.set_interval(2.0, self.refresh_downloads)

    def _dl_dir(self):
        return os.path.join("downloads", self._current_path).rstrip("/")

    def refresh_downloads(self):
        import os
        items = []
        base = self._dl_dir()
        seen_hashes = set()
        seen_folders = set()

        # Ongoing section
        ongoing_added = False

        # Active torrents (in-memory)
        if not self._current_path:
            for info_hash, meta in list(getattr(self.app, "torrents", {}).items()):
                if not ongoing_added:
                    items.append({"type": "section_header", "title": "Ongoing Downloads"})
                    ongoing_added = True
                title = meta.get("name", info_hash[:8])
                status = meta.get("status", "Downloading")
                if meta.get("paused"):
                    status = f"⏸ PAUSED — {status}"
                safe = re.sub(r'[^a-zA-Z0-9 _-]', '', title)[:60]
                dest = os.path.join("downloads", "torrents", safe)
                items.append({"title": title, "status": status, "info_hash": info_hash, "dest": dest, "type": "torrent"})
                seen_hashes.add(info_hash)

            # Stale / resumable torrents (disk meta, no active process)
            meta_dir = os.path.join("downloads", "torrents")
            if os.path.isdir(meta_dir):
                for entry in sorted(os.listdir(meta_dir)):
                    full = os.path.join(meta_dir, entry)
                    meta_file = os.path.join(full, ".meta.json")
                    if not (os.path.isdir(full) and os.path.isfile(meta_file)):
                        continue
                    try:
                        with open(meta_file) as f:
                            m = json.load(f)
                    except (json.JSONDecodeError, OSError):
                        continue
                    info_hash = m.get("info_hash", "")
                    if info_hash in seen_hashes:
                        continue
                    seen_hashes.add(info_hash)
                    seen_folders.add(entry)
                    if not ongoing_added:
                        items.append({"type": "section_header", "title": "Ongoing Downloads"})
                        ongoing_added = True
                    items.append({
                        "title": m.get("name", entry),
                        "status": "Paused (exit → resume)",
                        "info_hash": info_hash,
                        "magnet": m.get("magnet", ""),
                        "dest": full,
                        "type": "resumable",
                    })

            for ep_title, prog_str in getattr(self.app, "downloads", {}).items():
                if not ongoing_added:
                    items.append({"type": "section_header", "title": "Ongoing Downloads"})
                    ongoing_added = True
                items.append({"title": ep_title, "status": prog_str, "path": None, "type": "dl"})

        # Completed section
        if os.path.isdir(base):
            entries = sorted(os.listdir(base))
            completed = []
            for entry in entries:
                if entry in seen_folders or entry == ".meta.json":
                    continue
                full = os.path.join(base, entry)
                if os.path.isdir(full):
                    completed.append({"title": entry, "status": "Folder", "path": full, "type": "folder"})
                elif entry.lower().endswith((".mp4", ".mkv", ".webm", ".ts")):
                    completed.append({"title": entry, "status": "Completed", "path": os.path.abspath(full), "type": "file"})
            if completed:
                items.append({"type": "section_header", "title": "Completed"})
                items.extend(completed)

        dl_panel = self.query_one("#downloads-list", DownloadsPanel)
        prev_id = None
        if dl_panel._items and 0 <= dl_panel.cursor < len(dl_panel._items):
            prev = dl_panel._items[dl_panel.cursor]
            prev_id = prev.get("info_hash") or prev.get("title")

        dl_panel.set_items(items)

        if prev_id:
            for i, item in enumerate(items):
                if item.get("info_hash") == prev_id or (item.get("title") == prev_id and item.get("type") != "section_header"):
                    dl_panel.cursor = i
                    break
        dl_panel._fix_cursor()
        self._update_actions()

    def _update_actions(self):
        actions = self.query_one("#dl-actions", Horizontal)
        item = self._get_current_dl_item()
        if self._current_path or not item or item.get("type") not in ("torrent", "resumable"):
            actions.remove_class("visible")
            return
        actions.add_class("visible")
        pause_btn = self.query_one("#dl-pause", Button)
        watch_btn = self.query_one("#dl-watch", Button)
        if item.get("type") == "resumable":
            pause_btn.label = "Resume"
            watch_btn.display = True if self._find_video_in_dest(item.get("dest", "")) else False
            return
        watch_btn.display = True if self._find_video_in_dest(item.get("dest", "")) else False
        meta = self.app.torrents.get(item.get("info_hash", ""), {})
        pause_btn.label = "Resume" if meta.get("paused") else "Pause"

    def _find_video_in_dest(self, dest):
        if not dest or not os.path.isdir(dest):
            return None
        videos = []
        for root, dirs, files in os.walk(dest):
            for f in files:
                if f.lower().endswith((".mp4", ".mkv", ".webm", ".avi", ".mov", ".ts", ".m4v")):
                    videos.append(os.path.join(root, f))
        if not videos:
            return None
        return max(videos, key=os.path.getsize)

    def on_button_pressed(self, event: Button.Pressed):
        if event.button.id == "dl-watch":
            self._watch_torrent_file()
            self._update_actions()
            return
        item = self._get_current_dl_item()
        if item and item.get("type") == "resumable":
            if event.button.id == "dl-pause":
                self._resume_torrent(item)
            elif event.button.id == "dl-cancel":
                self.action_cancel()
            self._update_actions()
            return
        if event.button.id == "dl-pause":
            self.action_pause_resume()
        elif event.button.id == "dl-cancel":
            self.action_cancel()
        self._update_actions()

    def _watch_torrent_file(self):
        item = self._get_current_dl_item()
        if not item:
            return
        dest = item.get("dest", "")
        path = self._find_video_in_dest(dest)
        if not path:
            return
        sb = self.query_one("#status-bar", Static)
        title = item.get("title", "Torrent")[:40]
        sb.update(f"  Playing: {title}")
        self.app.run_worker(self._play_local(path, item.get("title", "")))

    def _update_footer(self):
        footer = self.query_one("#footer", FooterHints)
        if self._current_path:
            footer.set_hints([
                ("↑↓", "Navigate"), ("↵", "Play"), ("esc", "Back to Folders"),
                ("q", "Quit"),
            ])
        else:
            item = self._get_current_dl_item()
            if item and item.get("type") == "resumable":
                footer.set_hints([
                    ("↑↓", "Navigate"), ("↵", "Resume Download"),
                    ("w", "Watch Partial"), ("p", "Resume"), ("x", "Remove"),
                    ("esc", "Back to Search"), ("q", "Quit"),
                ])
            elif item and item.get("type") in ("torrent",):
                footer.set_hints([
                    ("↑↓", "Navigate"), ("↵", "Open Folder / Play"),
                    ("w", "Watch Partial"), ("p", "Pause/Resume"), ("x", "Cancel"),
                    ("esc", "Back to Search"), ("q", "Quit"),
                ])
            else:
                footer.set_hints([
                    ("↑↓", "Navigate"), ("↵", "Open Folder / Play"),
                    ("p", "Pause/Resume"), ("x", "Cancel Download"),
                    ("esc", "Back to Search"), ("q", "Quit"),
                ])

    def _get_current_dl_item(self):
        dl_panel = self.query_one("#downloads-list", DownloadsPanel)
        idx = dl_panel.cursor
        items = dl_panel._items
        if idx < len(items):
            return items[idx]
        return None

    def on_base_list_panel_activated(self, event):
        self.action_activate()

    def action_back(self):
        if self._current_path:
            self._current_path = os.path.dirname(self._current_path.rstrip("/"))
            label = os.path.basename(self._current_path) if self._current_path else "Downloads Library"
            sb = self.query_one("#status-bar", Static)
            sb.update(f"  {label}")
            self.refresh_downloads()
            self._update_footer()
            self._update_actions()
        else:
            self.app.pop_screen()

    def action_quit(self):
        self.app.exit()

    def action_move_up(self):
        self.query_one("#downloads-list", DownloadsPanel).move_up()
        self._update_actions()

    def action_move_down(self):
        self.query_one("#downloads-list", DownloadsPanel).move_down()
        self._update_actions()

    def action_activate(self):
        dl_panel = self.query_one("#downloads-list", DownloadsPanel)
        idx = dl_panel.cursor
        items = dl_panel._items
        if idx >= len(items):
            return
        item = items[idx]
        typ = item.get("type")
        if typ == "section_header":
            return
        if typ == "folder":
            name = item["title"]
            self._current_path = os.path.join(self._current_path, name) if self._current_path else name
            sb = self.query_one("#status-bar", Static)
            sb.update(f"  {self._current_path}")
            self.refresh_downloads()
            self._update_footer()
        elif typ == "file":
            path = item.get("path")
            if path:
                self.app.run_worker(self._play_local(path, item["title"]))
        elif typ == "resumable":
            self._resume_torrent(item)
        self._update_actions()

    def _resume_torrent(self, item):
        magnet = item.get("magnet", "")
        info_hash = item.get("info_hash", "")
        name = item.get("title", "Unknown")
        if not magnet or not info_hash:
            return
        from anime_watch.models import TorrentResult
        t = TorrentResult(name=name, magnet=magnet, info_hash=info_hash, source="", seeders=0, leechers=0, size_bytes=0)
        for screen in self.app.screen_stack:
            if hasattr(screen, '_start_watch_and_download'):
                screen._start_watch_and_download(t)
                return
        # Fallback: push a BrowserScreen to handle the download
        from anime_watch.tui.screens import BrowserScreen
        bs = BrowserScreen()
        self.app.push_screen(bs)
        bs._start_watch_and_download(t)

    def action_pause_resume(self):
        if self._current_path:
            return
        item = self._get_current_dl_item()
        if not item or item.get("type") not in ("torrent", "resumable"):
            return
        if item.get("type") == "resumable":
            self._resume_torrent(item)
            return
        info_hash = item.get("info_hash")
        if not info_hash:
            return
        from anime_watch.torrent.engine import get_engine
        engine = get_engine()
        meta = self.app.torrents.get(info_hash)
        if not meta:
            return
        if meta.get("paused"):
            engine.resume(info_hash)
            meta["paused"] = False
        else:
            engine.pause(info_hash)
            meta["paused"] = True
        self.refresh_downloads()
        self._update_actions()

    def action_cancel(self):
        if self._current_path:
            return
        item = self._get_current_dl_item()
        if not item or item.get("type") not in ("torrent", "resumable"):
            return
        info_hash = item.get("info_hash")
        if not info_hash:
            return
        from anime_watch.torrent.engine import get_engine
        engine = get_engine()
        engine.stop(info_hash)
        self.app.torrents.pop(info_hash, None)
        self.app.torrent_downloads.pop(info_hash, None)
        dest = item.get("dest") or os.path.join("downloads", "torrents", info_hash)
        _remove_meta(dest)
        self.refresh_downloads()
        self._update_actions()

    def action_watch_file(self):
        if self._current_path:
            return
        self._watch_torrent_file()

    async def _play_local(self, path, title):
        import asyncio
        sb = self.query_one("#status-bar", Static)
        sb.update(f"  Playing: {title[:40]}")
        args = ["mpv", "--no-terminal", "--ontop", path]
        try:
            proc = await asyncio.create_subprocess_exec(
                *args, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL
            )
            await proc.wait()
            sb.update(f"  {self._current_path}" if self._current_path else "  Downloads Library")
        except FileNotFoundError:
            sb.update("  Error: mpv not found.")


class OperationOverlay(Screen):
    BINDINGS = [
        Binding("ctrl+c", "quit", "Quit"),
        Binding("escape", "close", "Close"),
        Binding("n", "next_episode", "Next"),
        Binding("m", "music_panel", "Music"),
    ]

    _SPINNER = ["◜", "◝", "◞", "◟"]

    def __init__(self, title: str, kill_callback=None, next_callback=None, browser=None):
        super().__init__()
        self._op_title = title
        self._kill = kill_callback
        self._next_cb = next_callback
        self._browser = browser
        self._spinner_task = None
        self._spinner_base = title

    def compose(self) -> ComposeResult:
        with Container(classes="op-root"):
            with Vertical(classes="op-card"):
                yield Static(self._op_title, id="op-title", classes="op-title")
                yield ScrollableContainer(id="op-log", classes="op-log")
                with Horizontal(classes="op-buttons"):
                    yield Button("Next Episode", id="op-next-btn", classes="op-btn")
                    yield Button("Stop", id="op-stop-btn", classes="op-btn")

    def on_mount(self):
        self._start_spinner(self._spinner_base)

    def _stop_spinner(self):
        if self._spinner_task:
            self._spinner_task.cancel()
            self._spinner_task = None

    def _start_spinner(self, base: str):
        self._stop_spinner()
        self._spinner_base = base
        idx = 0
        async def _spin():
            nonlocal idx
            while True:
                try:
                    self.query_one("#op-title", Static).update(f"{self._spinner_base} {self._SPINNER[idx % len(self._SPINNER)]}")
                except Exception:
                    break
                idx += 1
                await asyncio.sleep(0.3)
        self._spinner_task = asyncio.create_task(_spin())

    def set_base(self, text: str):
        self._spinner_base = text

    def stage(self, title: str, log_message: str | None = None):
        self._start_spinner(title)
        if log_message:
            self.add_log(f"\u25b6 {log_message}")

    def add_log(self, text: str):
        # The overlay may already be dismissed (replaced by the control panel)
        # when background tasks log — swallow quietly in that case.
        try:
            log = self.query_one("#op-log", ScrollableContainer)
        except Exception:
            return
        line = Static(text, classes="op-log-line", markup=False)
        log.mount(line)
        self.app.call_after_refresh(lambda: log.scroll_end(animate=False))

    def show_playing(self, episode_title: str, has_next: bool = True):
        self.set_base(f"\u25b6 {episode_title}")
        try:
            self.query_one("#op-next-btn").display = has_next
            self.query_one("#op-stop-btn").display = True
        except Exception:
            pass

    def show_ended(self):
        self.set_base("Playback Ended")
        self.query_one("#op-next-btn").display = bool(self._next_cb)
        self.query_one("#op-stop-btn").display = True

    def fail(self):
        self.set_base("Extraction Failed")
        self.add_log("\u2716 Stream could not be extracted")
        self.query_one("#op-next-btn").display = False
        self.query_one("#op-stop-btn").display = True

    def on_button_pressed(self, event: Button.Pressed):
        if event.button.id == "op-next-btn":
            self.action_next_episode()
        elif event.button.id == "op-stop-btn":
            self.action_close()

    def action_next_episode(self):
        if not self._next_cb:
            return
        self._stop_spinner()
        if self._kill:
            self._kill()
        self.query_one("#op-next-btn").display = False
        self.query_one("#op-stop-btn").display = False
        log = self.query_one("#op-log", ScrollableContainer)
        log.remove_children()
        self._start_spinner("Extracting Next Episode")
        self._next_cb()

    def action_quit(self):
        self._stop_spinner()
        if self._kill:
            self._kill()
        self.dismiss(None)
        self.app.exit()

    def action_close(self):
        self._stop_spinner()
        if self._kill:
            self._kill()
        self.dismiss(None)

    def action_music_panel(self):
        browser = self._browser
        if browser is None or not hasattr(browser, "_player"):
            return
        # The control panel replaces the extraction overlay entirely.
        self.dismiss(None)
        self.app.push_screen(MusicPlayerOverlay(browser, browser._player))


def _is_music_like(episode) -> bool:
    """ytmusic and YouTube both get the music player treatment: autoplay
    prefetch, queue and the control panel overlay."""
    return getattr(episode, "site_name", "") in ("ytmusic", "YouTube")


def _fmt_ts(sec: float) -> str:
    sec = max(0, int(sec or 0))
    return f"{sec // 60}:{sec % 60:02d}"


class MusicPlayerOverlay(Screen):
    """Full music control panel — a separate popup on top of the extraction
    overlay. Drives mpv over IPC and lets the user browse/trim the queue and
    the prefetched autoplay batch."""

    BINDINGS = [
        Binding("escape", "close", "Close"),
        Binding("q", "close", "Close"),
        Binding("p", "pause", "Pause"),
        Binding("space", "pause", "Pause"),
        Binding("left", "seek_back", "Seek-"),
        Binding("right", "seek_fwd", "Seek+"),
        Binding("<", "prev_track", "Prev"),
        Binding(">", "next_track", "Next"),
        Binding("-", "vol_down", "Vol-"),
        Binding("=", "vol_up", "Vol+"),
        Binding("+", "vol_up", "Vol+"),
        Binding("x", "remove", "Remove"),
        Binding("j", "move_down", ""),
        Binding("k", "move_up", ""),
        Binding("z", "toggle_autoplay_mode", "Split"),
    ]

    def __init__(self, browser, player: PlaybackHandler):
        super().__init__()
        self._browser = browser
        self._player = player
        self._last_sig = None
        self._last_pre_sig = None

    def compose(self) -> ComposeResult:
        with Container(classes="op-root"):
            with Vertical(classes="op-card mp-card"):
                yield Static("", id="mp-title", classes="op-title")
                yield Static("", id="mp-track", classes="mp-track")
                yield Static("", id="mp-progress", classes="mp-progress")
                with Horizontal(classes="op-buttons"):
                    yield Button("⏮", id="mp-prev-btn", classes="op-btn mp-btn")
                    yield Button("⏯", id="mp-pause-btn", classes="op-btn mp-btn")
                    yield Button("⏭", id="mp-next-btn", classes="op-btn mp-btn")
                    yield Button("⏹", id="mp-stop-btn", classes="op-btn mp-btn")
                    yield Button("Vol−", id="mp-voldn-btn", classes="op-btn mp-btn")
                    yield Button("Vol+", id="mp-volup-btn", classes="op-btn mp-btn")
                yield Static("", id="mp-vol", classes="mp-vol")
                yield Static("Queue", id="mp-queue-head", classes="mp-section")
                yield ListView(id="mp-queue", classes="mp-list")
                yield Static("↵ play · x remove · p pause · < > prev/next · - = volume · ←→ seek", classes="mp-hints")

    def on_mount(self):
        self.set_interval(0.5, self._refresh)
        self.call_after_refresh(self._refresh)
        title = self.query_one("#mp-title", Static)
        ep = getattr(self._player, "_current_episode", None)
        site = getattr(ep, "site_name", "") if ep else ""
        label = "▶ Video Controls" if site == "YouTube" else "♫ Music Controls"
        title.update(label)
        q = self.query_one("#mp-queue", ListView)
        q.focus()

    def _fmt_progress(self) -> str:
        pos = getattr(self._player, "_mpv_last_pos", 0.0)
        dur = getattr(self._player, "_mpv_last_dur", 0.0)
        if dur <= 0:
            return f"  {_fmt_ts(pos)} / --:--"
        w = 18
        frac = min(1.0, pos / dur)
        bar = "█" * int(frac * w) + "░" * (w - int(frac * w))
        return f"  {bar} {_fmt_ts(pos)} / {_fmt_ts(dur)} ({int(frac*100)}%)"

    def _rebuild_queue(self):
        batch = getattr(self._browser, "_autoplay_batch", None) or []
        sig = (len(self._player._tracks), self._player._cur_idx, len(batch))
        if sig == self._last_sig:
            return
        self._last_sig = sig
        q = self.query_one("#mp-queue", ListView)
        q.clear()
        for i, (ep, _st) in enumerate(self._player._tracks):
            mark = "▶" if i == self._player._cur_idx else " "
            item = ListItem(Label(f" {mark} {ep.title[:48]}"))
            item._mp_index = i
            q.append(item)
        for j, ep in enumerate(batch):
            item = ListItem(Label(f"  ↳ {ep.title[:48]}"))
            item._batch_index = j
            q.append(item)
        if q.children:
            q.index = 0

    def _refresh(self):
        if not self.is_mounted:
            return
        player = self._player
        track = self.query_one("#mp-track", Static)
        title = player.current_track_title or "…"
        paused = "⏸" if getattr(player, "_mpv_paused", False) else "▶"
        try:
            from anime_watch.tui.player import load_settings as _ls
            _mode = "SPLIT" if _ls().get("yt_autoplay_split", False) else "TITLE"
        except Exception:
            _mode = ""
        suffix = f"  [{_mode}]" if _mode else ""
        track.update(Text(f"  {paused} {title}{suffix}", style=_w.SA_B))
        self.query_one("#mp-progress", Static).update(self._fmt_progress())
        vol = getattr(self.app, "volume", 100)
        self.query_one("#mp-vol", Static).update(Text(f"  Volume: {vol}%", style=_w.SD))
        self.query_one("#mp-pause-btn", Button).label = "⏸" if getattr(player, "_mpv_paused", False) else "⏯"
        self._rebuild_queue()

    def action_toggle_autoplay_mode(self):
        """Flip between suggestion mechanism 1 (whole-title search) and
        mechanism 2 (character x song cross-product searches)."""
        try:
            from anime_watch.tui.player import load_settings, save_settings
            st = load_settings()
            cur = st.get("yt_autoplay_split", False)
            st["yt_autoplay_split"] = not cur
            save_settings(st)
            mode = "SPLIT (character x song)" if not cur else "TITLE"
            try:
                self.app.notify(f"Autoplay mode: {mode}", title="Autoplay", timeout=3)
            except Exception:
                pass
            try:
                self._browser._update_content(f"Autoplay mode: {mode}")
            except Exception:
                pass
        except Exception:
            pass

    def _remove_from_mpv(self, index: int):
        try:
            self._player.remove_at(index)
        except Exception:
            pass

    def _remove_from_prefetch(self, index: int):
        batch = getattr(self._browser, "_autoplay_batch", None)
        if batch and 0 <= index < len(batch):
            batch.pop(index)
            self._last_sig = None
            self._rebuild_queue()

    def on_list_view_selected(self, event):
        item = event.item
        li = getattr(item, "_mp_index", None)
        if li is not None:
            n = len(self._player._tracks)
            if not (0 <= li < n) or li == self._player._cur_idx:
                return
            try:
                self._player.play_index(li)
            except Exception:
                pass
            return
        bj = getattr(item, "_batch_index", None)
        if bj is not None:
            try:
                self._browser._play_prefetched_now(bj)
            except Exception:
                pass

    def on_button_pressed(self, event: Button.Pressed):
        bid = event.button.id
        if bid == "mp-prev-btn":
            self.action_prev_track()
        elif bid == "mp-next-btn":
            self.action_next_track()
        elif bid == "mp-pause-btn":
            self.action_pause()
        elif bid == "mp-stop-btn":
            self.action_stop()
        elif bid == "mp-voldn-btn":
            self.action_vol_down()
        elif bid == "mp-volup-btn":
            self.action_vol_up()

    def action_pause(self):
        self._player.pause_resume()

    def action_next_track(self):
        if self._player._cur_idx < len(self._player._tracks) - 1:
            self._player.next_track()
        else:
            self._player.request_advance()

    def action_prev_track(self):
        self._player.prev_track()

    def action_vol_down(self):
        self._player.set_volume(-10)

    def action_vol_up(self):
        self._player.set_volume(10)

    def action_seek_back(self):
        self._player.seek_rel(-10)

    def action_seek_fwd(self):
        self._player.seek_rel(10)

    def action_remove(self):
        q = self.query_one("#mp-queue", ListView)
        if not q.has_focus:
            return
        hl = q.highlighted_child
        if hl is None:
            return
        mi = getattr(hl, "_mp_index", None)
        if mi is not None:
            self._remove_from_mpv(mi)
            self._last_sig = None
            return
        bj = getattr(hl, "_batch_index", None)
        if bj is not None:
            self._remove_from_prefetch(bj)

    def action_move_up(self):
        q = self.query_one("#mp-queue", ListView)
        target = q if q.has_focus else None
        if target:
            target.action_cursor_up()

    def action_move_down(self):
        q = self.query_one("#mp-queue", ListView)
        target = q if q.has_focus else None
        if target:
            target.action_cursor_down()

    def action_stop(self):
        self._player.kill_current()
        self.dismiss(None)

    def action_close(self):
        self.dismiss(None)


class HistoryScreen(Screen):
    BINDINGS = [
        Binding("escape", "back", "Back"),
        Binding("q", "quit", "Quit"),
        Binding("ctrl+c", "quit", "Quit"),
        Binding("up", "move_up", "", priority=True),
        Binding("down", "move_down", "", priority=True),
        Binding("k", "move_up", ""),
        Binding("j", "move_down", ""),
        Binding("left", "prev_cat", "Tab"),
        Binding("right", "next_cat", "Tab"),
        Binding("enter", "activate", ""),
    ]

    def compose(self) -> ComposeResult:
        with Container(classes="browser-root"):
            with Horizontal(classes="top-bar"):
                yield LogoWidget()
                yield Static("  Watch History", id="status-bar", classes="status")
            yield RuleWidget(classes="rule")
            with Horizontal(classes="body"):
                with Vertical(classes="content-area"):
                    with Container(classes="panel-wrap"):
                        yield HistoryPanel(id="history-list")
            yield FooterHints(id="footer")

    def on_mount(self):
        from anime_watch.history import get_history
        entries = get_history(limit=100)
        panel = self.query_one("#history-list", HistoryPanel)
        panel.set_items(entries)
        panel.focus()
        self._update_footer(entries)

    def _update_footer(self, entries=None):
        footer = self.query_one("#footer", FooterHints)
        if entries and len(entries) > 0:
            footer.set_hints([
                ("↑↓", "Navigate"), ("←→", "Tab"), ("↵", "Continue"),
                ("esc", "Back"), ("q", "Quit"),
            ])
        else:
            footer.set_hints([
                ("esc", "Back"), ("q", "Quit"),
            ])

    def on_base_list_panel_activated(self, event):
        self.action_activate()

    def action_back(self):
        self.app.pop_screen()

    def action_quit(self):
        self.app.exit()

    def action_move_up(self):
        self.query_one("#history-list", HistoryPanel).move_up()

    def action_move_down(self):
        self.query_one("#history-list", HistoryPanel).move_down()

    def action_prev_cat(self):
        self.query_one("#history-list", HistoryPanel).prev_category()

    def action_next_cat(self):
        self.query_one("#history-list", HistoryPanel).next_category()

    def action_activate(self):
        panel = self.query_one("#history-list", HistoryPanel)
        idx = panel.cursor
        if idx >= len(panel._items):
            return
        entry = panel._items[idx]
        from anime_watch.models import Episode
        data = entry.data.copy()
        if entry.progress > 0:
            data["_resume_at"] = entry.progress
        ep = Episode(
            title=entry.episode_title,
            url=entry.url,
            number=entry.episode_number,
            site_name=entry.site_name,
            anime_name=entry.anime_name,
            data=data,
        )
        self.app.search_query = entry.anime_name
        self.app._direct_play_episode = ep
        self.app.switch_screen(BrowserScreen())


class BrowserScreen(Screen):
    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("ctrl+c", "quit", "Quit"),
        Binding("escape", "back", "Back"),
        Binding("up", "move_up", "", priority=True),
        Binding("down", "move_down", "", priority=True),
        Binding("k", "move_up", ""),
        Binding("j", "move_down", ""),
        Binding("/", "search", "Search"),
        Binding("s", "toggle_sidebar", "Sidebar"),
        Binding("L", "view_downloads", "Downloads"),
        Binding("h", "view_history", "History"),
        Binding("left", "prev_category", "", priority=True),
        Binding("right", "next_category", "", priority=True),
        Binding("a", "toggle_audio", "Audio"),
        Binding("v", "toggle_quality", "Quality"),
        Binding("d", "download", "Download"),
        Binding("e", "enqueue", "Queue"),
    ]

    def __init__(self):
        super().__init__()
        self.results = []
        self.episodes = []
        self.downloads_list = []
        self.cursor = 0
        self.mode = "results"
        self.status = ""
        self._group_results = []
        self._episodes_backup = []
        self.servers = []
        self._server_episode = None
        self._provider_results = {}
        self._active_provider = "all"
        self._selected_torrent = None
        self._playback_episodes = []
        self._playback_idx = 0
        self._playback_episode = None
        self._playback_gen = 0
        self._playback_task = None
        self._queue: list = []
        self._playback_overlay = None
        self._autoplay_batch: list = []
        self._autoplay_streams: dict = {}
        self._autoplay_resolving = False
        self._autoplay_fetched_for = ""
        self._autoplay_fetching = False
        self._played_vids: set = set()

    def compose(self) -> ComposeResult:
        with Container(classes="browser-root"):
            with Horizontal(classes="top-bar"):
                yield LogoWidget()
                yield Static("", id="status-bar", classes="status")
            yield RuleWidget(classes="rule")
            with Horizontal(classes="body"):
                yield SidebarWidget(id="sidebar")
                with Vertical(classes="content-area"):
                    with Horizontal(classes="search-row"):
                        yield Input(placeholder="❯ Search anime…", id="browser-search")
                        yield Input(placeholder="Ep #", id="episode-jump")
                    with Horizontal(classes="provider-filter"):
                        yield Static("", classes="filter-btn")
                    with Container(classes="panel-wrap"):
                        yield ResultsPanel(id="results-list")
            yield FooterHints(id="footer")

    def on_mount(self):
        self._player = PlaybackHandler(self.app, self._update_content, self._update_footer)
        self._downloader = DownloadHandler(self.app, self._update_content)
        cat = getattr(self.app, "search_category", "anime")
        self.query_one("#browser-search", Input).placeholder = f"❯ Search {cat}…"
        self.action_search()
        self._update_footer()
        direct = getattr(self.app, '_direct_play_episode', None)
        if direct:
            self.app._direct_play_episode = None
            self._start_playback(direct)
            self._run_continue_watching_episodes(direct)
        else:
            q = getattr(self.app, 'search_query', '')
            if q:
                self._do_search(q)
        rl = self.query_one("#results-list", ResultsPanel)
        rl.focus()
        self.set_interval(2.0, self._refresh_sidebar)

    def _refresh_sidebar(self):
        sb = self.query_one("#sidebar", SidebarWidget)
        sb.refresh()

    def on_input_submitted(self, event: Input.Submitted):
        q = event.value.strip()
        if not q:
            self.focus_results()
            return
        if event.input.id == "episode-jump":
            self._jump_to_episode(q)
            self.focus_results()
            return
        self.app.search_query = q
        self._do_search(q)
        self.focus_results()

    def _do_search(self, query: str):
        self.mode = "results"
        self.results = []
        self._group_results = []
        self.cursor = 0
        self._update_content("Searching…")
        self._update_footer()
        self._run_search(query)

    @work(thread=True, exclusive=True)
    def _run_search(self, query: str):
        def on_progress(site, status):
            self.app.call_from_thread(
                self._update_content, f"Searching {site}: {status}"
            )
        cat = getattr(self.app, "search_category", "")
        all_results = search_configured(query, on_progress=on_progress, category=cat)
        provider_results = {}
        for r in all_results:
            key = r.source.lower() if isinstance(r, TorrentResult) else r.site_name.lower().strip()
            provider_results.setdefault(key, []).append(r)
        self.app.call_from_thread(self._show_results_with_filters, provider_results)

    def _show_results(self, results):
        self.results = results
        self.cursor = 0
        rl = self.query_one("#results-list", ResultsPanel)
        rl.set_items(results)
        self._update_content(f"{len(results)} result{'s' if len(results) != 1 else ''}" if results else "No results")
        self._update_search_input()
        self._update_footer()

    def _show_results_with_filters(self, provider_results: dict[str, list]):
        self._provider_results = provider_results
        self._active_provider = "all"
        all_res = []
        for lst in provider_results.values():
            all_res.extend(lst)
        if all_res and isinstance(all_res[0], TorrentResult):
            all_res.sort(key=lambda r: r.seeders, reverse=True)
        self._show_results(all_res)
        name_map = {k: p.name for k, p in CONFIGURED_PROVIDERS.items()}
        name_map.update({k: p.name for k, p in TORRENT_PROVIDERS.items()})
        filter_row = self.query_one(".provider-filter")
        existing = {b.id: b for b in filter_row.children}
        slugs = sorted(provider_results.keys())
        seen: set[str] = set()
        if "filter-all" not in existing:
            filter_row.mount(Button("All", id="filter-all", classes="filter-btn"), before=0)
            existing["filter-all"] = filter_row.query_one("#filter-all")
        seen.add("filter-all")
        for slug in slugs:
            bid = f"filter-{slug}"
            seen.add(bid)
            label = name_map.get(slug, slug.upper())
            if bid in existing:
                existing[bid].label = label
            else:
                filter_row.mount(Button(label, id=bid, classes="filter-btn"))
        for bid, btn in list(existing.items()):
            if bid not in seen:
                btn.remove()
        filter_row.add_class("visible")

    def _switch_provider_source(self, slug: str):
        self._active_provider = slug
        for btn in self.query(".filter-btn"):
            btn.remove_class("active")
        self.query_one(f"#filter-{slug}", Button).add_class("active")
        if slug == "all":
            all_res = []
            for lst in self._provider_results.values():
                all_res.extend(lst)
            if all_res and isinstance(all_res[0], TorrentResult):
                all_res.sort(key=lambda r: r.seeders, reverse=True)
            self._show_results(all_res)
        else:
            results = self._provider_results.get(slug, [])
            self._show_results(results)

    def _show_episodes(self, eps, title):
        self.episodes = eps
        self.cursor = 0
        rl = self.query_one("#results-list", ResultsPanel)
        rl.set_items(eps)
        self.mode = "episodes"
        self._update_content(f"{title} — {len(eps)} ep{'s' if len(eps) != 1 else ''}")
        self._update_search_input()
        self._update_footer()

    def _update_search_input(self):
        ep = self.query_one("#episode-jump", Input)
        if self.mode == "episodes":
            ep.add_class("visible")
        else:
            ep.remove_class("visible")
            ep.value = ""

    def _update_content(self, status_text=""):
        self.status = status_text
        sb = self.query_one("#status-bar", Static)
        if status_text:
            sb.update(Text(f"  {status_text}", style=_w.SD))
        else:
            sb.update(Text("", style=_w.SD))

    def _get_current_item(self):
        rl = self.query_one("#results-list", ResultsPanel)
        if self.mode in ("results", "providers", "servers", "torrent_options"):
            idx = rl.cursor
            if idx < len(self.results):
                return ("result", self.results[idx])
        elif self.mode == "episodes":
            item = rl.get_item_at_cursor()
            if item is not None:
                return ("episode", item)
        return (None, None)

    def _show_providers(self, group: SearchResultGroup):
        self._group_results = self.results
        self.results = group.results
        from anime_watch.providers import CONFIGURED_PROVIDERS
        for r in self.results:
            key = r.site_name.lower().strip()
            prov = CONFIGURED_PROVIDERS.get(key)
            if prov:
                site = prov.url
                r.data["alive"] = _check_alive(site, timeout=3) if site else None
            else:
                r.data["alive"] = None
        self.mode = "providers"
        self.cursor = 0
        rl = self.query_one("#results-list", ResultsPanel)
        rl.set_items(self.results)
        self._update_content(f"{group.title} — select provider")
        self._update_search_input()
        self._update_footer()

    def _start_server_pick(self, episode, for_download=False):
        episode.data["_server_pick_dl"] = for_download
        self._update_content(f"Loading servers for {episode.title[:40]}…")
        self._run_server_fetch(episode)

    @work(thread=True, exclusive=True)
    def _run_server_fetch(self, episode):
        from anime_watch.providers import CONFIGURED_PROVIDERS
        key = episode.site_name.lower().strip()
        provider = CONFIGURED_PROVIDERS.get(key)
        try:
            servers = provider.get_servers(episode) if provider and hasattr(provider, 'get_servers') else []
        except Exception:
            self.app.call_from_thread(self._update_content, "Server list unavailable — source timed out")
            return
        for sv in servers:
            link_id = sv.get("link_id", "")
            if link_id:
                sv["alive"] = _check_server_alive(provider, episode, link_id)
            else:
                sv["alive"] = None
        self.app.call_from_thread(self._show_servers, servers, episode)

    def _show_servers(self, servers, episode):
        if not servers:
            self._update_content("No servers available for this episode")
            return
        self._server_episode = episode
        self.servers = servers
        self._episodes_backup = self.episodes
        self._group_results = self.results
        from anime_watch.models import SearchResult
        self.results = [
            SearchResult(title=s["display"], url=s.get("link_id", ""),
                        site_name="", image="", data={"alive": s.get("alive")})
            for s in servers
        ]
        self.mode = "servers"
        self.cursor = 0
        rl = self.query_one("#results-list", ResultsPanel)
        rl.set_items(self.results)
        self._update_content(f"{episode.title[:40]} — select server")
        self._update_search_input()
        self._update_footer()

    def _pick_server(self, item):
        ep = self._server_episode
        if ep is None:
            return
        ep.data["server_name"] = item.title
        if "(" in item.title:
            label = item.title.split("(")[-1].rstrip(")").strip().lower()
            if label in ("sub", "dub", "hsub"):
                self.app.audio_pref = "sub" if label in ("sub", "hsub") else label
            elif len(label) in (2, 4) and label.isalpha():
                self.app.audio_pref = label
        is_dl = ep.data.pop("_server_pick_dl", False) or item.title.startswith("[DL]")
        self._restore_episodes()
        if is_dl:
            self._start_download(ep)
        else:
            self._start_playback(ep)

    def _restore_episodes(self):
        self.mode = "episodes"
        self.episodes = self._episodes_backup
        self.results = self._group_results
        rl = self.query_one("#results-list", ResultsPanel)
        rl.set_items(self.episodes)
        if self.episodes:
            self._update_content(f"{len(self.episodes)} ep{'s' if len(self.episodes) != 1 else ''}")
        self._update_search_input()
        self._update_footer()

    def _jump_to_episode(self, raw: str):
        self.query_one("#episode-jump", Input).value = ""
        try:
            target = int(raw)
        except ValueError:
            self._update_content(f"Not a number: {raw}")
            return
        rl = self.query_one("#results-list", ResultsPanel)
        # The list is paginated into categories (All 1-100, 101-200, ...):
        # search the FULL list, switch to the right category, then highlight.
        for ep in rl._all_items:
            try:
                if int(ep.number) != target:
                    continue
            except (ValueError, TypeError):
                continue
            if getattr(ep, "category", ""):
                rl.switch_category(ep.category)
            try:
                idx = rl._items.index(ep)
            except ValueError:
                idx = 0
            rl.set_cursor(idx)
            self._update_content(f"Ep {target} — {ep.title[:50]}")
            return
        self._update_content(f"Episode {target} not found")

    def action_move_up(self):
        if self.mode == "episodes":
            rl = self.query_one("#results-list", ResultsPanel)
            if not rl._items: return
        if self.mode in ("results", "providers", "torrent_options") and not self.results: return
        rl = self.query_one("#results-list", ResultsPanel)
        rl.move_up()

    def action_move_down(self):
        if self.mode == "episodes":
            rl = self.query_one("#results-list", ResultsPanel)
            if not rl._items: return
        if self.mode in ("results", "providers", "torrent_options") and not self.results: return
        rl = self.query_one("#results-list", ResultsPanel)
        rl.move_down()

    def action_next_category(self):
        if self.mode != "episodes":
            return
        rl = self.query_one("#results-list", ResultsPanel)
        rl.next_category()
        self._update_content_after_category(rl)

    def action_prev_category(self):
        if self.mode != "episodes":
            return
        rl = self.query_one("#results-list", ResultsPanel)
        rl.prev_category()
        self._update_content_after_category(rl)

    def _update_content_after_category(self, rl):
        if rl.category_count > 1:
            n = len(rl._items)
            cat = rl.active_category or "All"
            self._update_content(f"{cat} — {n} ep{'s' if n != 1 else ''}")

    def action_activate(self):
        typ, item = self._get_current_item()
        if typ == "result":
            if isinstance(item, TorrentResult):
                self._show_torrent_options(item)
            elif self.mode == "torrent_options":
                self._pick_torrent_mode(item)
            elif isinstance(item, SearchResultGroup):
                self._show_providers(item)
            elif self.mode == "servers":
                self._pick_server(item)
            else:
                self._start_episode_fetch(item)
        elif typ == "episode":
            from anime_watch.providers import CONFIGURED_PROVIDERS
            key = item.site_name.lower().strip()
            provider = CONFIGURED_PROVIDERS.get(key)
            if provider and hasattr(provider, 'get_servers'):
                self._start_server_pick(item)
            else:
                rl = self.query_one("#results-list", ResultsPanel)
                idx = rl.cursor
                self._start_playback(item, self.episodes, idx)

    def _start_episode_fetch(self, result):
        self._update_content(f"Loading episodes for {result.title[:40]}…")
        self._run_episode_fetch(result)

    @work(thread=True, exclusive=True)
    def _run_episode_fetch(self, result):
        eps = get_episodes(result)
        # YouTube results are single videos — play them directly instead of
        # showing a one-item episode list.
        if len(eps) == 1 and getattr(eps[0], "site_name", "") == "YouTube":
            self.app.call_from_thread(self._start_playback, eps[0], eps, 0)
            return
        self.app.call_from_thread(self._show_episodes, eps, result.title[:40])

    def action_view_downloads(self):
        self.app.push_screen(DownloadsScreen())

    def action_view_history(self):
        self.app.push_screen(HistoryScreen())

    def action_download(self):
        typ, item = self._get_current_item()
        if isinstance(item, TorrentResult):
            self._start_torrent_download(item)
        elif self.mode == "servers":
            if self._server_episode:
                self._server_episode.data["_server_pick_dl"] = True
            self._pick_server(item)
        elif typ == "episode":
            from anime_watch.providers import CONFIGURED_PROVIDERS
            key = item.site_name.lower().strip()
            provider = CONFIGURED_PROVIDERS.get(key)
            if provider and hasattr(provider, 'get_servers'):
                self._start_server_pick(item, for_download=True)
            else:
                self._start_download(item)
        else:
            self._update_content("Select an episode to download.")

    def action_toggle_sidebar(self):
        sidebar = self.query_one("#sidebar", SidebarWidget)
        sidebar.toggle_class("visible")

    def _start_torrent_stream(self, torrent: TorrentResult):
        self._update_content(f"Starting torrent stream: {torrent.name[:40]}…")
        self._run_torrent_stream(torrent)

    @work(thread=True, exclusive=True)
    def _run_torrent_stream(self, torrent: TorrentResult):
        from anime_watch.torrent.engine import get_engine
        engine = get_engine()
        if not engine.is_available():
            self.app.call_from_thread(self._update_content, "webtorrent-cli not found")
            return

        def on_progress(msg):
            self.app.call_from_thread(self._update_content, f"Buffering: {msg}")

        asyncio.run(engine.stream_pipe(
            torrent.magnet, torrent.info_hash, on_progress
        ))
        self.app.call_from_thread(self._update_content, "Stream ended")

    def _show_torrent_options(self, torrent: TorrentResult):
        self._selected_torrent = torrent
        self._group_results = self.results
        from anime_watch.models import SearchResult
        self.results = [
            SearchResult(title="Watch & Download", url="", site_name="stream+save", image=""),
            SearchResult(title="Watch Only", url="", site_name="stream only", image=""),
        ]
        self.mode = "torrent_options"
        self.cursor = 0
        rl = self.query_one("#results-list", ResultsPanel)
        rl.set_items(self.results)
        self._update_content(f"{torrent.name[:40]} — select mode")
        self._update_footer()

    def _pick_torrent_mode(self, item):
        torrent = self._selected_torrent
        if not torrent:
            return
        if item.site_name == "stream+save":
            self._start_watch_and_download(torrent)
        else:
            self._start_torrent_stream(torrent)

    def _start_watch_and_download(self, torrent: TorrentResult):
        self._update_content(f"Watch & Download: {torrent.name[:40]}…")
        self._run_watch_and_download(torrent)

    @work(thread=True, exclusive=True)
    def _run_watch_and_download(self, torrent: TorrentResult):
        from anime_watch.torrent.engine import get_engine
        engine = get_engine()
        if not engine.is_available():
            self.app.call_from_thread(self._update_content, "webtorrent-cli not found")
            return

        safe = re.sub(r'[^a-zA-Z0-9 _-]', '', torrent.name)[:60]
        dest = os.path.join("downloads", "torrents", safe)
        save_path = os.path.join(dest, f"{safe}.mp4")

        self.app.torrents[torrent.info_hash] = {"name": torrent.name, "status": "Downloading", "paused": False}
        _ensure_meta(torrent, dest)

        def on_progress(msg):
            self.app.call_from_thread(self._update_content, f"Downloading: {msg}")
            if torrent.info_hash in self.app.torrents:
                self.app.torrents[torrent.info_hash]["status"] = msg

        self.app.call_from_thread(self._update_content, f"Playing & saving: {torrent.name[:40]}")
        asyncio.run(engine.stream_and_save(
            torrent.magnet, torrent.info_hash, save_path, on_progress
        ))
        self.app.call_from_thread(self._update_content, "Download complete")
        self.app.torrents.pop(torrent.info_hash, None)
        self.app.call_from_thread(_remove_meta, dest)
        # Clean up the .meta.json after full download since webtorrent is done
        # torrent sits on disk at save_path for the user

    def _start_torrent_download(self, torrent: TorrentResult):
        self._update_content(f"Downloading torrent: {torrent.name[:40]}…")
        self._run_torrent_download(torrent)

    @work(thread=True, exclusive=True)
    def _run_torrent_download(self, torrent: TorrentResult):
        from anime_watch.torrent.engine import get_engine
        engine = get_engine()
        if not engine.is_available():
            self.app.call_from_thread(self._update_content, "webtorrent-cli not found")
            return

        safe = re.sub(r'[^a-zA-Z0-9 _-]', '', torrent.name)[:60]
        dest = os.path.join("downloads", "torrents", safe)

        self.app.torrents[torrent.info_hash] = {"name": torrent.name, "status": "Starting", "paused": False}
        _ensure_meta(torrent, dest)

        def on_progress(msg):
            self.app.call_from_thread(self._update_content, f"Downloading {torrent.name[:30]} — {msg}")
            self.app.torrent_downloads[torrent.info_hash] = msg
            if torrent.info_hash in self.app.torrents:
                self.app.torrents[torrent.info_hash]["status"] = msg

        def on_complete(path):
            self.app.call_from_thread(self._update_content, f"Downloaded: {torrent.name[:40]}")
            self.app.torrent_downloads.pop(torrent.info_hash, None)
            self.app.torrents.pop(torrent.info_hash, None)
            self.app.call_from_thread(_remove_meta, dest)

        engine.download_sync(torrent.magnet, torrent.info_hash, dest, on_complete, on_progress)

    def _resume_torrent(self, item):
        magnet = item.get("magnet", "")
        info_hash = item.get("info_hash", "")
        name = item.get("title", "Unknown")
        dest = item.get("dest", "")
        if not magnet or not info_hash:
            return
        from anime_watch.models import TorrentResult
        t = TorrentResult(name=name, magnet=magnet, info_hash=info_hash, source="", seeders=0, leechers=0, size_bytes=0)
        self._start_watch_and_download(t)

    def _start_download(self, episode):
        self._update_content(f"Extracting stream for download: {episode.title[:30]}…")
        self._run_download(episode)

    @work(thread=True, exclusive=False)
    def _run_download(self, episode):
        from anime_watch.providers import extract_stream
        stream = extract_stream(episode, self.app.audio_pref, self.app.quality_pref)
        if stream and stream.url:
            self.app.call_from_thread(self._downloader._do_download, stream, episode)
        else:
            self.app.call_from_thread(self._update_content, "Could not extract stream for download")

    def _shutdown_autoplay_proxies(self):
        """Shut down the local HLS proxies of prefetched tracks that never
        played — they keep serving in the background otherwise."""
        for _st in self._autoplay_streams.values():
            _proxy = getattr(_st, 'proxy_server', None)
            if _proxy:
                try:
                    _proxy.shutdown()
                except Exception:
                    pass

    def _start_playback(self, episode, episodes=None, current_idx=0):
        self._playback_gen += 1
        gen = self._playback_gen
        if self._playback_task:
            self._playback_task.cancel()
            self._playback_task = None
        self._player.kill_current()
        self._playback_episodes = episodes or []
        self._playback_idx = current_idx
        self._autoplay_batch = []
        self._shutdown_autoplay_proxies()
        self._autoplay_streams = {}
        self._autoplay_resolving = False
        self._autoplay_fetched_for = ""
        has_next = current_idx + 1 < len(episodes or [])
        def _on_next():
            self._playback_idx += 1
            next_ep = self._playback_episodes[self._playback_idx]
            self._playback_episode = next_ep
            self._playback_gen += 1
            self._run_playback(next_ep, overlay, self._playback_gen)
        def _kill_and_mark():
            self._playback_gen += 1
            self._player.kill_current()
        overlay = OperationOverlay(
            "Extracting Stream",
            kill_callback=_kill_and_mark,
            next_callback=_on_next if has_next else None,
            browser=self,
        )
        self._playback_overlay = overlay
        self._playback_episode = episode
        if isinstance(self.app.screen, OperationOverlay):
            self.app.pop_screen()
        self.app.push_screen(overlay)
        self._run_playback(episode, overlay, gen)

    @work(thread=True, exclusive=True)
    def _run_playback(self, episode, overlay, gen):
        from anime_watch.providers import extract_stream

        import requests as _req
        _request_log_ctx.overlay = overlay
        if not hasattr(_req.Session, '_aw_patched'):
            _req.Session._aw_patched = True
            _orig = _req.Session.request
            def _logged(self, method, url, *args, **kwargs):
                ctx = getattr(_request_log_ctx, 'overlay', None)
                if ctx:
                    try:
                        ctx.add_log(f"  {method} {url}")
                    except Exception:
                        pass
                return _orig(self, method, url, *args, **kwargs)
            _req.Session.request = _logged

        self.app.call_from_thread(overlay.stage, "Contacting Provider", "Sending request...")

        try:
            stream = extract_stream(episode, self.app.audio_pref, self.app.quality_pref)
        except Exception as e:
            _request_log_ctx.overlay = None
            err_msg = str(e)
            if "Read timed out" in err_msg or "timeout" in err_msg.lower():
                friendly = "Source server timed out — try again"
            else:
                friendly = f"{type(e).__name__}: {err_msg[:80]}"
            self.app.call_from_thread(overlay.stage, "Error", friendly)
            self.app.call_from_thread(overlay.fail)
            return
        finally:
            _request_log_ctx.overlay = None

        has_next = self._playback_idx + 1 < len(self._playback_episodes)
        if gen != self._playback_gen:
            return
        if stream and stream.url:
            self.app.call_from_thread(overlay.add_log, f"  Resolved [{stream.quality}] {stream.url[:70]}…")
            if stream.subtitles:
                self.app.call_from_thread(overlay.add_log, f"  Subtitles: {len(stream.subtitles)} track(s)")
            self.app.call_from_thread(overlay.show_playing, episode.title, has_next)
            if gen != self._playback_gen:
                return
            self.app.call_from_thread(self._launch_mpv, stream, episode, overlay, gen)
        else:
            if gen != self._playback_gen:
                return
            self.app.call_from_thread(overlay.stage, "Failed", "No stream returned")
            self.app.call_from_thread(overlay.fail)

    def _launch_mpv(self, stream, episode, overlay, gen):
        if gen != self._playback_gen:
            return
        if self._playback_task:
            self._playback_task.cancel()
            self._playback_task = None
        self._player.kill_current()
        tracks = [(episode, stream)]
        if _is_music_like(episode) and self._queue:
            tracks.extend(self._queue)
            self._queue = []
        self._launch_mpv_tracks(tracks, overlay, gen)
        if (tracks and _is_music_like(tracks[0][0])
                and not isinstance(self.app.screen, MusicPlayerOverlay)):
            # The control panel replaces the extraction overlay entirely.
            try:
                overlay.dismiss(None)
            except Exception:
                pass
            self.app.push_screen(MusicPlayerOverlay(self, self._player))

    def _launch_mpv_tracks(self, tracks, overlay, gen):
        if self._playback_task:
            self._playback_task.cancel()
            self._playback_task = None
        self._player.kill_current()
        is_music = _is_music_like(tracks[0][0])
        prefetch_cb = self._prefetch_next_batch if is_music else None
        self._player._on_advance_cb = self._on_track_advance if is_music else None
        async def _run():
            try:
                await self._player._do_play(tracks, overlay, prefetch_cb=prefetch_cb)
            except Exception:
                # A playback task exception must never kill the app — log and
                # move on (e.g. mpv died unexpectedly on a device).
                try:
                    overlay.add_log("Playback ended unexpectedly")
                except Exception:
                    pass
                try:
                    self._update_content("Playback ended")
                except Exception:
                    pass
                self._shutdown_autoplay_proxies()
                return
            if gen != self._playback_gen:
                return
            natural = (bool(getattr(self._player, "_eof_reached", False))
                       or bool(getattr(self._player, "_advance_requested", False)))
            batch = self._autoplay_batch
            if is_music:
                overlay.add_log(f"Autoplay: ended, natural={natural}, batch={len(batch)}")
            if natural and is_music:
                # ymc waitingAtQueueEnd: give an in-flight prefetch time to land
                wait_until = time.time() + 20.0
                while not batch and time.time() < wait_until:
                    still_busy = self._autoplay_fetching or getattr(self._player, "_prefetching", False)
                    if not still_busy:
                        break
                    await asyncio.sleep(0.5)
                    batch = self._autoplay_batch
            if natural and batch and is_music:
                overlay.add_log(f"Autoplay: relaunching with {len(batch)} tracks")
                # NOTE: must NOT reassign the closure var `tracks` here — that
                # makes it local to _run and breaks the first _do_play call.
                def _ready():
                    return [(ep, self._autoplay_streams.get((ep.data or {}).get("video_id")))
                            for ep in batch if (self._autoplay_streams.get((ep.data or {}).get("video_id")) or {}).url]
                await self._resolve_pending()
                rel_tracks = _ready()
                # The resolution lottery can come up empty — retry a few
                # rounds before giving up; never lose the queued batch.
                rounds = 0
                while not rel_tracks and rounds < 4:
                    await asyncio.sleep(2)
                    await self._resolve_pending()
                    rel_tracks = _ready()
                    rounds += 1
                if rel_tracks:
                    resolved_vids = {(ep.data or {}).get("video_id") for ep, _ in rel_tracks}
                    # Keep unresolved entries queued — only consume what plays.
                    self._autoplay_batch = [ep for ep in batch
                                            if (ep.data or {}).get("video_id") not in resolved_vids]
                    self._played_vids |= resolved_vids
                    self._playback_gen += 1
                    self._launch_mpv_tracks(rel_tracks, overlay, self._playback_gen)
                    return
                overlay.add_log("Autoplay: no streams resolved — queue kept for manual play")
                try:
                    self._update_content("Autoplay: streams still resolving…")
                except Exception:
                    pass
                return
            self._shutdown_autoplay_proxies()
            try:
                overlay.show_ended()
            except Exception:
                pass
        self._playback_task = asyncio.create_task(_run())

    async def _resolve_pending(self):
        """Background: resolve any queued episodes lacking streams (cheap,
        parallel). The queue is episodes-only; streams land when ready."""
        if self._autoplay_resolving:
            return
        self._autoplay_resolving = True
        try:
            from anime_watch.providers import CONFIGURED_PROVIDERS
            prov = CONFIGURED_PROVIDERS.get("youtube") or CONFIGURED_PROVIDERS.get("ytmusic")
            if prov is None:
                return
            await asyncio.to_thread(
                self._resolve_many, list(self._autoplay_batch), prov.resolve_suggestion)
        except Exception:
            pass
        finally:
            self._autoplay_resolving = False

    def _on_track_advance(self, episode):
        """Queue advanced to a new track — append ONE more suggestion at the
        end so the Up Next list keeps growing."""
        try:
            asyncio.create_task(self._prefetch_next_batch(episode))
        except Exception:
            pass

    def _play_prefetched_now(self, batch_index: int):
        """Play a prefetched (autoplay) track immediately, keeping the rest
        of the batch in the queue. Relaunches mpv with the selected track
        first — per-track audio-file options require a fresh instance."""
        batch = self._autoplay_batch
        if not (0 <= batch_index < len(batch)):
            return
        selected = batch[batch_index]
        # Wrap the queue around the picked item: it plays now, followed by
        # the items AFTER it in queue order, then the items before it — so
        # picking 4 out of 1,2,3,4,5,6 plays 4,5,6,…1,2,3 instead of
        # replaying 1,2,3 right after 4.
        ordered = batch[batch_index + 1:] + batch[:batch_index]
        overlay = self._playback_overlay
        if overlay is None:
            return
        # Resolve the selected episode now (cheap), drop it if it fails.
        from anime_watch.providers import CONFIGURED_PROVIDERS
        prov = CONFIGURED_PROVIDERS.get(selected.site_name.lower().strip())
        stream = None
        if prov and hasattr(prov, "resolve_suggestion"):
            try:
                stream = prov.resolve_suggestion(selected)
            except Exception:
                stream = None
        if not (stream and stream.url):
            self._update_content("Could not resolve that track")
            return
        self._player.kill_current()
        self._playback_gen += 1
        rest = [(ep, self._autoplay_streams.get((ep.data or {}).get("video_id"))) for ep in ordered]
        rest = [t for t in rest if t[1] and t[1].url]
        play_tracks = [(selected, stream)] + rest
        # These are now queued in the player — remove them from the batch so
        # the end-of-list relaunch doesn't restart them from the beginning.
        played_vids = {(ep.data or {}).get("video_id") for ep, _ in play_tracks}
        self._autoplay_batch = [ep for ep in batch
                                if (ep.data or {}).get("video_id") not in played_vids]
        self._played_vids |= played_vids
        self._launch_mpv_tracks(play_tracks, overlay, self._playback_gen)

    async def _prefetch_next_batch(self, episode):
        """Called by the player near the end of the playlist: fetch the next
        Up Next batch from YouTube Music and resolve it (full quality)."""
        if self._autoplay_fetching:
            return
        site = getattr(episode, "site_name", "")
        if site not in ("ytmusic", "YouTube"):
            return
        vid = (episode.data or {}).get("video_id", "")
        if not vid or vid == self._autoplay_fetched_for:
            return
        self._autoplay_fetching = True
        self._autoplay_fetched_for = vid
        kind = (episode.data or {}).get("kind", "song")
        # Initial queue: 10 suggestions. Afterwards every queue advance
        # appends ONE more at the end, keeping the Up Next list growing.
        limit = 1 if self._autoplay_batch else 10
        # Never re-queue the currently playing / queued tracks (the watch
        # playlist leads with the current video, which would repeat it).
        playing_vids = {(ep.data or {}).get("video_id")
                        for ep, _ in getattr(self._player, "_tracks", [])}
        skip_ids = ({ep.data.get("video_id") for ep in self._autoplay_batch
                     if (ep.data or {}).get("video_id")} | playing_vids)
        try:
            try:
                self._playback_overlay_log(f"Autoplay: fetching Up Next for {vid[:10]}…")
            except Exception:
                pass
            if site == "YouTube":
                batch = await asyncio.to_thread(
                    self._fetch_youtube_suggestions,
                    (episode.data or {}).get("title", ""), limit, skip_ids)
            else:
                batch = await asyncio.to_thread(self._fetch_suggestions, vid, kind, limit, skip_ids)
            if batch:
                if limit == 1:
                    self._autoplay_batch.extend(batch)
                else:
                    self._autoplay_batch = batch
                asyncio.create_task(self._resolve_pending())
                try:
                    self._update_content(f"Autoplay ready — {len(self._autoplay_batch)} more track{'s' if len(self._autoplay_batch) != 1 else ''}")
                    self._playback_overlay_log(f"Autoplay: batch ready ({len(self._autoplay_batch)} tracks)")
                except Exception:
                    pass
            else:
                try:
                    self._playback_overlay_log("Autoplay: no suggestions found")
                except Exception:
                    pass
        finally:
            self._autoplay_fetching = False

    def _playback_overlay_log(self, text: str):
        """Write a line into the visible playback overlay log."""
        scr = self.app.screen
        if isinstance(scr, OperationOverlay):
            scr.add_log(text)
        else:
            self._update_content(text)

    def _resolve_many(self, episodes: list, resolver) -> int:
        """Resolve suggestion episodes in parallel (5 workers) with the cheap
        single-attempt resolver, caching results in _autoplay_streams. Runs in
        the background so the queue is never blocked. Returns how many got
        resolved."""
        from concurrent.futures import ThreadPoolExecutor
        pending = [ep for ep in episodes
                   if (ep.data or {}).get("video_id") not in self._autoplay_streams]
        if not pending:
            return 0
        def _one(ep):
            try:
                return (ep, resolver(ep))
            except Exception:
                return (ep, None)
        with ThreadPoolExecutor(max_workers=5) as pool:
            results = list(pool.map(_one, pending))
        ok = 0
        for ep, stream in results:
            vid = (ep.data or {}).get("video_id")
            if stream and stream.url and vid:
                prev = self._autoplay_streams.get(vid)
                if prev is not stream:
                    _proxy = getattr(prev, 'proxy_server', None) if prev else None
                    if _proxy:
                        try:
                            _proxy.shutdown()
                        except Exception:
                            pass
                self._autoplay_streams[vid] = stream
                ok += 1
        return ok

    def _amv_parts(self, title: str):
        """Split an AMV title into (character, song). Titles like
        'Mikasa - Wanna Be Yours [EDIT]' or 'Mikasa | Wanna Be Yours Edit'.
        Returns None when the structure isn't a clean character - song pair."""
        import re as _re
        t = _re.split(r"[\[\()]", title)[0]
        t = _re.sub(r"\|.*$", "", t).strip()
        for sep in (" - ", " \u2013 ", " \u2014 "):
            if sep in t:
                c, s = t.split(sep, 1)
                c = c.strip().strip("'\"")
                s = s.strip()
                s = _re.sub(r"\b(edit|amv|amvs?|mv|music video)\b.*$", "", s, flags=_re.I).strip()
                if c and s and 1 <= len(c.split()) <= 5 and 1 <= len(s.split()) <= 8:
                    return c, s
        return None

    def _fetch_youtube_suggestions(self, title: str, limit: int = 10, skip_ids=None) -> list:
        """Up Next for YouTube via a yt-dlp search on the video title,
        resolved to probed direct streams."""
        import json as _json
        import subprocess as _sp
        from anime_watch.models import Episode
        from anime_watch.providers import CONFIGURED_PROVIDERS
        out = []
        prov = CONFIGURED_PROVIDERS.get("youtube")
        # Mechanism 1 (default): search the whole title.
        # Mechanism 2 (yt_autoplay_split): break the title into character x
        # song and search the cross-product — same character/other songs and
        # other characters/same song.
        queries = [f"ytsearch{max(limit, 10)}:{title}"]
        try:
            from anime_watch.tui.player import load_settings
            parts = self._amv_parts(title) if load_settings().get("yt_autoplay_split", False) else None
            if parts:
                c, s = parts
                queries = [f"ytsearch4:{c} edit amv",
                           f"ytsearch4:{s} edit amv",
                           f"ytsearch4:{title}"]
        except Exception:
            pass
        try:
            candidates = []
            seen = set()
            seen_titles = set()
            for q in queries:
                r = _sp.run(
                    ["yt-dlp", "--flat-playlist", "--no-warnings", "-J", q],
                    capture_output=True, text=True, timeout=30,
                )
                if r.returncode != 0:
                    continue
                data = _json.loads(r.stdout)
                for e in data.get("entries", []) or []:
                    vid = e.get("id")
                    # ytsearch can list the same video twice in one response,
                    # and _played_vids is never populated in the episodes-only
                    # flow — dedupe within the response as well.
                    if (not vid or vid in seen or vid in self._played_vids
                            or (skip_ids and vid in skip_ids)):
                        continue
                    seen.add(vid)
                    t = e.get("title", "") or ""
                    if not t:
                        continue
                    ch = e.get("channel") or e.get("uploader") or ""
                    label = f"{t} — {ch}" if ch else t
                    # Exact normalized-title dupes (same title, different
                    # channel re-uploads) are the same content — skip them.
                    # Titles like "Trailer" vs "Trailer 2" differ and stay.
                    import re as _re
                    nt = _re.sub(r"[^a-z0-9]+", " ", t.lower()).strip()
                    if nt in seen_titles:
                        continue
                    seen_titles.add(nt)
                    candidates.append(Episode(
                        title=label,
                        url=f"https://www.youtube.com/watch?v={vid}",
                        number="1",
                        site_name="YouTube",
                        anime_name=t,
                        data={"video_id": vid, "title": t, "channel": ch},
                    ))
                    if len(candidates) >= limit:
                        break
                if len(candidates) >= limit:
                    break
        except Exception:
            pass
        return candidates[:limit]

    def _fetch_suggestions(self, video_id: str, kind: str = "song", limit: int = 10, skip_ids=None) -> list:
        """Up Next via ytmusicapi, resolved to full-quality direct streams.
        kind='song' -> album-art tracks; kind='video' -> the music videos."""
        from anime_watch.models import Episode
        from anime_watch.providers import CONFIGURED_PROVIDERS
        from anime_watch.providers.youtubemusic import ytmusic_client
        prov = CONFIGURED_PROVIDERS.get("ytmusic")
        try:
            ym = ytmusic_client()
            panel = ym.get_watch_playlist(video_id)
        except Exception:
            return []
        tracks = panel.get("tracks") or []
        out = []
        candidates = []
        for t in tracks:
            tvid = t.get("videoId")
            if not tvid or tvid == video_id or tvid in self._played_vids or (skip_ids and tvid in skip_ids):
                continue
            title = t.get("title") or ""
            artists = t.get("artists") or []
            artist = (artists[0].get("name") if artists else "") or ""
            label = f"{title} — {artist}" if artist else title
            if kind == "video":
                ep = Episode(
                    title=f"▶ {label} (Video)",
                    url=f"https://music.youtube.com/watch?v={tvid}",
                    number="1v",
                    site_name="ytmusic",
                    anime_name=label,
                    data={"kind": "video", "video_id": tvid},
                )
            else:
                ep = Episode(
                    title=f"♫ {label}",
                    url=f"https://music.youtube.com/watch?v={tvid}",
                    number="1",
                    site_name="ytmusic",
                    anime_name=label,
                    data={"kind": "song", "video_id": tvid},
                )
                candidates.append(ep)
        return candidates[:limit]

    def action_enqueue(self):
        if getattr(self.app, "search_category", "") != "music":
            self._update_content("Queue is music-only")
            return
        typ, item = self._get_current_item()
        if typ == "episode":
            self._run_enqueue([item])
        elif typ == "result":
            self._update_content(f"Loading queue entry: {item.title[:40]}…")
            self._run_enqueue_result(item)
        else:
            self._update_content("Select a track to enqueue.")

    @work(thread=True, exclusive=False)
    def _run_enqueue(self, eps: list):
        from anime_watch.providers import extract_stream
        added = 0
        for ep in eps:
            try:
                stream = extract_stream(ep, self.app.audio_pref, self.app.quality_pref)
            except Exception:
                stream = None
            if stream and stream.url:
                self._queue.append((ep, stream))
                added += 1
        self.app.call_from_thread(self._update_content,
            f"Queued {added} track{'s' if added != 1 else ''} — {len(self._queue)} in queue" if added else
            "Could not extract stream to enqueue")

    @work(thread=True, exclusive=False)
    def _run_enqueue_result(self, result):
        from anime_watch.providers import get_episodes, extract_stream
        try:
            eps = get_episodes(result)
        except Exception:
            eps = []
        if not eps:
            self.app.call_from_thread(self._update_content, "No tracks found to enqueue")
            return
        audio = None
        for ep in eps:
            kind = (ep.data or {}).get("kind", "")
            if kind == "song" or str(ep.number).endswith("a"):
                audio = ep
                break
        if audio is None and eps:
            audio = eps[0]
        if audio is None:
            self.app.call_from_thread(self._update_content, "No tracks found to enqueue")
            return
        try:
            stream = extract_stream(audio, self.app.audio_pref, self.app.quality_pref)
        except Exception:
            stream = None
        if not (stream and stream.url):
            self.app.call_from_thread(self._update_content, "Could not extract stream to enqueue")
            return
        self._queue.append((audio, stream))
        self.app.call_from_thread(self._update_content,
            f"Queued: {audio.title[:40]} — {len(self._queue)} in queue")

    @work(thread=True)
    def _run_continue_watching_episodes(self, episode):
        from anime_watch.models import SearchResult
        from anime_watch.providers import get_episodes
        result = SearchResult(
            title=episode.anime_name,
            url=episode.url,
            site_name=episode.site_name,
        )
        eps = get_episodes(result)
        if eps:
            self.app.call_from_thread(self._show_episodes, eps, episode.anime_name)

    def action_search(self):
        inp = self.query_one("#browser-search", Input)
        inp.focus()

    def action_back(self):
        if self.mode == "servers":
            self._restore_episodes()
        elif self.mode == "providers":
            self.results = self._group_results
            self.mode = "results"
            rl = self.query_one("#results-list", ResultsPanel)
            rl.set_items(self.results)
            self._update_content(f"{len(self.results)} result{'s' if len(self.results) != 1 else ''}" if self.results else "")
            self._update_search_input()
            self._update_footer()
        elif self.mode == "torrent_options":
            self.results = self._group_results
            self.mode = "results"
            rl = self.query_one("#results-list", ResultsPanel)
            rl.set_items(self.results)
            self._update_content(f"{len(self.results)} result{'s' if len(self.results) != 1 else ''}" if self.results else "")
            self._update_footer()
        elif self.mode == "episodes":
            if not self._group_results and not self.results:
                self.app.switch_screen(SplashScreen())
                return
            self.mode = "results"
            self.episodes = []
            if self._group_results:
                self.results = self._group_results
            rl = self.query_one("#results-list", ResultsPanel)
            rl.set_items(self.results)
            if self.results:
                self._update_content(f"{len(self.results)} result{'s' if len(self.results) != 1 else ''}")
            else:
                self._update_content("")
            self._update_search_input()
            self._update_footer()
        else:
            self.app.switch_screen(SplashScreen())

    def action_quit(self):
        self.app.exit()

    def on_button_pressed(self, event: Button.Pressed):
        eid = event.button.id
        if eid and eid.startswith("filter-"):
            slug = eid.replace("filter-", "")
            self._switch_provider_source(slug)

    def on_sidebar_widget_open_downloads(self, event):
        self.action_view_downloads()

    def on_base_list_panel_activated(self, event):
        self.action_activate()

    def focus_results(self):
        self.query_one("#results-list", ResultsPanel).focus()

    def _get_provider_for_current(self):
        if not CONFIGURED_SITES:
            return None
        key = CONFIGURED_SITES[0].name.lower().strip()
        return CONFIGURED_PROVIDERS.get(key)

    def action_toggle_audio(self):
        prov = self._get_provider_for_current()
        opts = prov.get_supported_audio() if prov else ["sub", "dub"]
        try:
            idx = opts.index(self.app.audio_pref)
        except ValueError:
            idx = 0
        self.app.audio_pref = opts[(idx + 1) % len(opts)]
        self._update_content(f"Audio preference set to: {self.app.audio_pref.upper()}")
        self._update_footer()

    def action_toggle_quality(self):
        prov = self._get_provider_for_current()
        qs = prov.get_supported_qualities() if prov else ["1080p", "720p", "360p", "best"]
        try:
            idx = qs.index(self.app.quality_pref)
        except ValueError:
            idx = 0
        self.app.quality_pref = qs[(idx + 1) % len(qs)]
        self._update_content(f"Quality preference set to: {self.app.quality_pref}")
        self._update_footer()

    def _update_footer(self):
        footer = self.query_one("#footer", FooterHints)
        ap = self.app.audio_pref.upper()
        qp = self.app.quality_pref
        cat = getattr(self.app, "search_category", "")

        if self.mode == "torrent_options":
            footer.set_hints([
                ("↑↓", "Navigate"), ("↵", "Select Mode"), ("esc", "Back"), ("q", "Quit"),
            ])
        elif cat == "torrent":
            if self.mode == "results":
                footer.set_hints([
                    ("↑↓", "Navigate"), ("/", "Search"), ("↵", "Stream"), ("d", "Download"),
                    ("s", "Sidebar"), ("esc", "Back"), ("q", "Quit"),
                ])
            else:
                footer.set_hints([
                    ("↑↓", "Navigate"), ("↵", "Play"), ("d", "Download"), ("s", "Sidebar"),
                    ("esc", "Back"), ("q", "Quit"),
                ])
        elif self.mode == "results":
            hints = [
                ("↑↓", "Navigate"), ("/", "Search"), ("↵", "Episodes"),
                ("s", "Sidebar"), ("L", "Library"), ("h", "History"), ("q", "Quit"),
            ]
            if cat == "music":
                hints.insert(3, ("e", "Enqueue"))
            footer.set_hints(hints)
        elif self.mode == "servers":
            footer.set_hints([
                ("↑↓", "Navigate"), ("↵", "Select"), ("d", "Download"),
                ("esc", "Back"), ("q", "Quit"),
            ])
        elif self.mode == "episodes":
            hints = [("↑↓", "Navigate"), ("↵", "Play"), ("/", "Ep #"), ("d", "Download")]
            if cat == "music":
                hints.append(("e", "Queue"))
            try:
                rl = self.query_one("#results-list", ResultsPanel)
                if rl.category_count > 1:
                    hints.insert(1, ("←→", "Category"))
            except Exception:
                pass
            hints += [("s", "Sidebar"), ("esc", "Back"), ("h", "History"),
                      ("a", f"Audio({ap})"), ("v", f"Qual({qp})"), ("q", "Quit")]
            footer.set_hints(hints)
        else:
            footer.set_hints([
                ("↑↓", "Navigate"), ("↵", "Play"), ("d", "Download"), ("s", "Sidebar"),
                ("esc", "Back"), ("L", "Library"), ("h", "History"),
                ("a", f"Audio({ap})"), ("v", f"Qual({qp})"), ("q", "Quit"),
            ])

    def key_up(self, event=None):
        self.action_move_up()

    def key_down(self, event=None):
        self.action_move_down()
