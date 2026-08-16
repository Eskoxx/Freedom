import sys
from textual.app import App
from textual.binding import Binding
from anime_watch.core import _which
from anime_watch.tui.screens import SplashScreen

AW_CSS = '''
Screen { background: $bg; }

.splash-root { align: center middle; width: 100%; height: 100%; }
.splash-center { align: center middle; width: 100%; height: auto; min-width: 50%; }
.splash-center > LogoWidget { text-align: center; }
.splash-center > .spacer { height: 2; }
.splash-center > .spacer-sm { height: 1; }
#splash-search { min-width: 60; width: 80w; max-width: 100; border: none; background: transparent; color: $accent; margin-top: 2; }
.splash-hints-row { width: 100%; height: 1; align: center middle; }
.hint-text { width: auto; color: $muted; }
.hint-key { width: auto; color: $alt; }
.hint-btn { width: auto; height: 1; min-width: 1; padding: 0; border: none; background: transparent; color: $muted; }
.hint-btn:focus { color: $accent; text-style: bold; }
.hint-btn:hover { color: $bright; }

.splash-toggle-row { width: 100%; height: 1; align: center middle; margin: 0 0 1 0; }
.cat-btn { width: auto; height: 1; min-width: 1; padding: 0 1; border: none; background: transparent; color: $muted; }
.cat-btn:hover { color: $bright; }
.cat-btn:focus { color: $accent; text-style: bold; }
.cat-btn.active { color: $accent; text-style: bold; }

.branch-row { height: 1; width: 100%; display: none; align: center middle; }
.branch-row.visible { display: block; }
.branch-btn { width: auto; height: 1; min-width: 1; padding: 0 1; border: none; background: transparent; color: $muted; }
.branch-btn:hover { color: $bright; }
.branch-btn:focus { color: $accent; text-style: bold; }
.branch-btn.active { color: $accent; text-style: bold; }

.browser-root { width: 100%; height: 100%; layout: vertical; }
.top-bar { height: 3; width: 100%; }
.top-bar > LogoWidget { width: auto; min-width: 40; }
.rule { height: 1; width: 100%; }
.body { height: 1fr; width: 100%; }
#sidebar { display: none; width: 22; height: 100%; padding: 1 0 0 1; }
#sidebar.visible { display: block; }
.content-area { height: 100%; width: 1fr; padding: 0 1 0 1; }
.search-row { height: 3; width: 100%; }
#browser-search { width: 1fr; border: none; background: transparent; color: $accent; }
#episode-jump { width: 12; display: none; border: none; background: transparent; color: $accent; }
#episode-jump.visible { display: block; }
.panel-wrap { height: 1fr; width: 100%; border: round $muted; padding: 0 0 0 1; }
#results-list { height: 100%; width: 100%; }
#downloads-list { height: 100%; width: 100%; }
#history-list { height: 100%; width: 100%; }

.provider-filter { height: 1; width: 100%; display: none; align: left middle; padding: 0 0 0 1; }
.provider-filter.visible { display: block; }
.filter-btn { width: auto; height: 1; min-width: 1; padding: 0 1; border: none; background: transparent; color: $muted; }
.filter-btn:hover { color: $bright; }
.filter-btn:focus { color: $accent; text-style: bold; }
.filter-btn.active { color: $accent; text-style: bold; }

.continue-box { width: 100%; height: auto; align: center middle; }
.cw-btn { width: 100%; height: 1; min-width: 1; padding: 0; border: none; background: transparent; color: $alt; }
.cw-btn:hover { color: $bright; }
.cw-btn:focus { color: $accent; text-style: bold; }

.op-root { align: center middle; width: 100%; height: 100%; }
.op-card { width: 60; height: auto; padding: 1 2; border: round $muted; background: $bg; }
.op-title { width: 100%; text-align: center; color: $accent; text-style: bold; }
.op-log { width: 100%; min-height: 6; height: auto; max-height: 12; border: none; background: $bg_deep; padding: 0 0 0 1; scrollbar-size: 0 0; }
.op-log-line { width: 100%; color: $log; }
#ns-body { width: 100%; color: $log; text-align: center; }
#ns-status { width: 100%; color: $log; text-align: center; height: 1; }
.op-buttons { width: 100%; height: auto; align: center middle; margin: 1 0 0 0; }
.op-btn, .ns-btn { width: auto; height: 1; min-width: 1; padding: 0 1; margin: 0 1; border: none; background: transparent; color: $muted; display: none; }
.ns-btn { display: block; }
.op-btn:hover, .ns-btn:hover { color: $bright; }
.op-btn:focus, .ns-btn:focus { color: $accent; text-style: bold; }

.mp-card { width: 76; }
.mp-track { width: 100%; color: $track; text-style: bold; margin-top: 1; }
.mp-progress { width: 100%; color: $accent; }
.mp-vol { width: 100%; color: $muted; margin-bottom: 1; }
.mp-btn { display: block; }
.mp-section { width: 100%; color: $alt; text-style: bold; margin-top: 1; }
.mp-list { width: 100%; height: auto; max-height: 8; border: none; background: $bg_deep; padding: 0 0 0 1; }
.mp-list:focus { background: $bg_focus; }
.mp-list ListItem { width: 100%; color: $log; }
.mp-list ListItem:focus { background: $item_focus; color: $track; text-style: bold; }
.mp-list ListItem.-highlight { background: $item_focus; color: $track; text-style: bold; }
.mp-hints { width: 100%; color: $muted; margin-top: 1; text-align: center; }

#status-bar { height: 1; width: 1fr; }
#dl-actions { height: 3; width: 100%; align: center middle; display: none; }
#dl-actions.visible { display: block; }
.dl-btn { width: auto; height: 1; min-width: 1; padding: 0 1; border: none; background: transparent; color: $muted; }
.dl-btn:hover { color: $bright; }
.dl-btn:focus { color: $accent; text-style: bold; }
#footer { height: 1; width: 100%; padding: 0 0 0 1; }

Input { border: none; background: transparent; }
.cw-title { color: $alt; text-style: bold; }
*:focus { border: none; }
'''

class AnimeWatch(App):
    TITLE = "FREEDOM"
    CSS = AW_CSS
    BINDINGS = [
        Binding("ctrl+t", "cycle_theme", "Theme"),
    ]
    _theme_name = "midnight"

    def __init__(self, *args, **kwargs):
        # Load the persisted theme BEFORE super().__init__: Textual builds the
        # stylesheet (with CSS variables) inside the parent constructor, so the
        # theme must be known before it runs — not after.
        from anime_watch.tui.player import load_settings
        from anime_watch.tui.themes import DEFAULT_THEME
        self._theme_name = str(load_settings().get("theme", DEFAULT_THEME))
        super().__init__(*args, **kwargs)
        self.search_query = ""
        self.audio_pref = "sub"
        self.quality_pref = "1080p"
        self.search_category = "anime"
        self.downloads = {}
        self.torrent_downloads: dict[str, str] = {}
        self.torrents: dict[str, dict] = {}
        from anime_watch.tui.player import load_settings
        self.volume = int(load_settings().get("volume", 100))

    def get_css_variables(self) -> dict:
        from anime_watch.tui.themes import THEMES
        pal = THEMES.get(self._theme_name) or THEMES["midnight"]
        base = super().get_css_variables()
        base.update({k: v for k, v in pal.items() if k != "name"})
        return base

    def on_mount(self):
        self._force_theme()

    def _force_theme(self):
        """Apply the active theme everywhere. Called at mount AND after the
        app is fully booted (on_ready) so any stylesheet built with the wrong
        variables (older Textual / stale build order) gets corrected."""
        from anime_watch.tui.widgets import apply_palette
        apply_palette(self._theme_name)
        try:
            self.refresh_css()
        except Exception:
            pass
        for widget in self.query("*"):
            try:
                widget.refresh()
            except Exception:
                pass


    def action_cycle_theme(self):
        from anime_watch.tui.player import load_settings, save_settings
        from anime_watch.tui.themes import THEMES, next_theme
        from anime_watch.tui.widgets import apply_palette
        self._theme_name = next_theme(self._theme_name)
        apply_palette(self._theme_name)
        self.refresh_css()
        settings = load_settings()
        settings["theme"] = self._theme_name
        save_settings(settings)
        for widget in self.query("*"):
            try:
                widget.refresh()
            except Exception:
                pass
        try:
            self.notify(f"Theme: {THEMES[self._theme_name]['name']}", timeout=2)
        except Exception:
            pass

    def run(self, *args, **kwargs):
        try:
            return super().run(*args, **kwargs)
        finally:
            self._cleanup_webtorrent()

    def on_ready(self):
        # Post-boot safety net: re-apply the theme now that every screen and
        # widget is mounted and laid out.
        try:
            self._force_theme()
        except Exception:
            pass
        try:
            from anime_watch.updater import pending_update_notice, clear_pending_notice
            notice = pending_update_notice()
            if notice:
                self.notify(f"Freedom updated to v{notice} — restart to load new code.", title="Update applied", timeout=8)
                clear_pending_notice()
        except Exception:
            pass
        self.push_screen(SplashScreen())
        try:
            import os as _os
            if not _os.path.exists(_os.path.expanduser("~/.config/anime-watch/community_seen")):
                from anime_watch.tui.screens import CommunityOverlay
                self.set_timer(0.6, lambda: self.push_screen(CommunityOverlay()))
        except Exception:
            pass

    def _cleanup_webtorrent(self):
        from anime_watch.torrent.engine import get_engine
        get_engine().stop_all()

def run_app():
    if not _which("mpv"):
        sys.stderr.write("mpv not found. Install it: apt install mpv / brew install mpv\n")
        sys.exit(1)
    app = AnimeWatch()
    app.run()
