from __future__ import annotations
import asyncio
import json
import os
import tempfile
import time
from typing import Callable, Optional
import requests
from anime_watch.models import Episode, StreamSource
from anime_watch.history import HistoryEntry, add_entry as add_history_entry

CONFIG_DIR = os.path.expanduser("~/.config/anime-watch")
SETTINGS_FILE = os.path.join(CONFIG_DIR, "settings.json")


def load_settings() -> dict:
    try:
        with open(SETTINGS_FILE, "r") as f:
            raw = json.load(f)
        return raw if isinstance(raw, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_settings(settings: dict):
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        with open(SETTINGS_FILE, "w") as f:
            json.dump(settings, f, indent=2)
    except OSError:
        pass


def _write_extra_input_conf() -> str:
    """Temp mpv input.conf with the extras that have no default key:
    X = shuffle playlist, C = toggle repeat-all (L repeats the file)."""
    path = os.path.join(tempfile.gettempdir(), "aw_mpv_input.conf")
    try:
        with open(path, "w") as f:
            f.write("X playlist-shuffle\n")
            f.write("C cycle-values loop-playlist inf no\n")
    except OSError:
        return ""
    return path


async def play_file(path_or_url: str, title: str = "") -> tuple[int, str]:
    """Play a file/URL in mpv. Returns (exit_code, last_error_lines)."""
    import tempfile as _tempfile
    mpv_verbose_log = os.path.join(_tempfile.gettempdir(), "anime_watch_mpv_verbose.log")
    args = [
        "mpv", "--no-terminal", "--osd-level=0", "--vo=gpu",
        "--keep-open=yes", "--cache=yes", "--cache-secs=30",
        "--ontop", "--cache-pause-initial=yes",
        f"--log-file={mpv_verbose_log}", "--msg-level=all=info",
    ]
    if title:
        args.append(f"--force-media-title={title}")
    args.append(path_or_url)
    log_path = os.path.join(_tempfile.gettempdir(), "anime_watch_mpv.log")
    try:
        proc = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
        )
        _, stderr_bytes = await proc.communicate()
        err_text = stderr_bytes.decode(errors="replace") if stderr_bytes else ""
        try:
            with open(log_path, "w") as f:
                f.write(f"mpv args: {' '.join(args)}\n\n{err_text}")
        except OSError:
            pass
        lines = [l for l in err_text.splitlines() if l.strip()]
        tail = " | ".join(lines[-2:]) if lines else ""
        try:
            with open(mpv_verbose_log, "a") as f:
                f.write(f"\n[anime_watch] mpv exit code: {proc.returncode}\n")
        except OSError:
            pass
        return (proc.returncode if proc.returncode is not None else 0), tail
    except FileNotFoundError:
        return 127, "mpv not found"
    except OSError as e:
        return 1, str(e)


class PlaybackHandler:
    def __init__(self, app, update_status, update_footer):
        self.app = app
        self._update_content = update_status
        self._update_footer = update_footer
        self._current_proc = None
        self._ipc_writer = None
        self._ipc_tasks: list[asyncio.Task] = []
        self._tracks: list[tuple[Episode, StreamSource]] = []
        self._cur_idx = -1
        self._hist_written_for = -1
        self._current_episode = None
        self._mpv_last_pos = 0.0
        self._mpv_last_dur = 0.0
        self._mpv_returncode = None
        self._eof_reached = False
        self._prefetch_cb: Optional[Callable] = None
        self._prefetching = False
        self._mpv_paused = False
        self._advance_requested = False
        self._overlay = None

    def kill_current(self):
        if self._current_proc and self._current_proc.returncode is None:
            try:
                self._current_proc.kill()
            except ProcessLookupError:
                pass

    async def _send_ipc(self, command: list) -> None:
        """Send one mpv IPC command; safe when the player is not running."""
        writer = getattr(self, "_ipc_writer", None)
        if writer is None:
            return
        try:
            writer.write(json.dumps({"command": command}).encode() + b"\n")
            await writer.drain()
        except (OSError, BrokenPipeError):
            pass

    def _spawn_ipc(self, *commands):
        async def _run():
            for cmd in commands:
                await self._send_ipc(cmd)
        task = asyncio.create_task(_run())
        self._ipc_tasks = [t for t in self._ipc_tasks if not t.done()] + [task]
        return task

    # ── transport controls (mpv native, driven from the music panel) ──

    def next_track(self):
        self._spawn_ipc(["playlist-next"])

    def prev_track(self):
        self._spawn_ipc(["playlist-prev"])

    def play_index(self, index: int):
        self._spawn_ipc(["playlist-play-index", index])

    def remove_at(self, index: int):
        self._spawn_ipc(["playlist-remove", index])

    def pause_resume(self):
        self._spawn_ipc(["cycle", "pause"])

    def set_volume(self, delta: int):
        if not hasattr(self.app, "volume"):
            return
        self.app.volume = max(0, min(130, self.app.volume + delta))
        settings = load_settings()
        settings["volume"] = self.app.volume
        save_settings(settings)
        self._spawn_ipc(
            ["add", "volume", delta],
            ["show-text", f"Volume: ${{volume}}%"],
        )

    def seek_rel(self, seconds: int):
        self._spawn_ipc(["seek", seconds, "relative"])

    def request_advance(self):
        """Force the autoplay continuation now. Used when skipping past the
        last track in mpv's playlist — the next tracks live in the prefetched
        batch, so mpv itself has nothing to advance to."""
        self._advance_requested = True
        self._spawn_ipc(["quit"])

    def is_playing(self) -> bool:
        return self._current_proc is not None and self._current_proc.returncode is None

    @property
    def current_track_title(self) -> str:
        if 0 <= self._cur_idx < len(self._tracks):
            ep = self._tracks[self._cur_idx][0]
            return ep.title
        return ""

    @staticmethod
    def _sub_path(label: str, ext: str) -> str:
        safe = "".join(c if c not in "/\\" else "-" for c in label if c >= " ").strip()[:60] or "sub"
        base = os.path.join(tempfile.gettempdir(), f"{safe}{ext}")
        if not os.path.exists(base):
            return base
        i = 2
        while os.path.exists(f"{base}-{i}{ext}"):
            i += 1
        return f"{base}-{i}{ext}"

    async def _download_sub(self, url: str, headers: dict, out: list[str], label: str = ""):
        try:
            resp = await asyncio.to_thread(
                requests.get, url, headers=headers, timeout=10
            )
            if resp.status_code == 200:
                ext = ".vtt" if url.endswith(".vtt") else ".srt"
                path = self._sub_path(label, ext)
                with open(path, "wb") as f:
                    f.write(resp.content)
                out.append(path)
        except Exception:
            pass

    def _per_file_args(self, stream: StreamSource, episode: Episode, first: bool) -> list[str]:
        """Per-track options wrapped in mpv's --{ ... --} markers so each
        playlist entry carries its own audio-file / headers / title."""
        args = ["--{"]
        name = episode.anime_name or episode.title
        args.append(f"--force-media-title={name} — {episode.title}")

        audio_url = ""
        vid_off = False
        extra = getattr(stream, "extra_mpv_args", None)
        if extra:
            for a in extra:
                if a.startswith("--audio-file="):
                    audio_url = a[len("--audio-file="):]
                elif a == "--vid=no":
                    vid_off = True
                elif a.startswith("--http-header-fields="):
                    pass
                else:
                    args.append(a)
        if audio_url:
            args.append(f"--audio-file={audio_url}")
        if vid_off:
            args.append("--vid=no")

        headers = getattr(stream, "headers", None)
        if headers:
            mpv_headers = ",".join(f"{k}: {v}" for k, v in headers.items())
            args.append(f"--http-header-fields={mpv_headers}")

        resume_at = 0
        if first and (episode.data or {}).get("_resume_at"):
            resume_at = episode.data.pop("_resume_at", 0)
        if resume_at > 0:
            args.append(f"--start={resume_at}")

        subs = getattr(stream, "subtitles", None)
        if subs:
            for sub in subs:
                lang = (sub.get("lang") or sub.get("label") or "").lower()
                if "en" not in lang and "english" not in lang:
                    continue
                _url = sub.get("url")
                if _url and os.path.exists(_url):
                    args.append(f"--sub-file={_url}")

        args.append(stream.url)
        args.append("--}")
        return args

    async def _poll_mpv_position(self, reader, poll_interval: float = 5.0):
        self._mpv_last_pos = 0.0
        self._mpv_last_dur = 0.0
        while True:
            try:
                for prop in ("time-pos", "duration"):
                    req = json.dumps({"command": ["get_property", prop]}).encode() + b"\n"
                    self._ipc_writer.write(req)
                    await self._ipc_writer.drain()
                    resp = await asyncio.wait_for(reader.readline(), timeout=2.0)
                    data = json.loads(resp)
                    if data.get("error") == "success" and isinstance(data.get("data"), (int, float)):
                        if prop == "time-pos":
                            self._mpv_last_pos = float(data["data"])
                        else:
                            self._mpv_last_dur = float(data["data"])
            except (ConnectionResetError, BrokenPipeError, asyncio.IncompleteReadError):
                break
            except (OSError, asyncio.TimeoutError, json.JSONDecodeError):
                pass
            await asyncio.sleep(poll_interval)

    @staticmethod
    def _sub_path(label: str, ext: str) -> str:
        safe = "".join(c if c not in "/\\" else "-" for c in label if c >= " ").strip()[:60] or "sub"
        base = os.path.join(tempfile.gettempdir(), f"{safe}{ext}")
        if not os.path.exists(base):
            return base
        i = 2
        while os.path.exists(f"{base}-{i}{ext}"):
            i += 1
        return f"{base}-{i}{ext}"

    async def _download_sub(self, url: str, headers: dict, out: list[str], label: str = ""):
        try:
            resp = await asyncio.to_thread(
                requests.get, url, headers=headers, timeout=10
            )
            if resp.status_code == 200:
                ext = ".vtt" if url.endswith(".vtt") else ".srt"
                path = self._sub_path(label, ext)
                with open(path, "wb") as f:
                    f.write(resp.content)
                out.append(path)
        except Exception:
            pass

    async def _do_play_classic(self, stream: StreamSource, episode: Episode, overlay=None):

        self._update_content(f"Now playing: {episode.title}\nClose mpv to return...")
        args = ["mpv", "--no-terminal", "--osd-level=0", "--hwdec=no",
                "--vo=gpu", "--ontop", "--cache=yes", "--cache-secs=30",
                "--cache-pause-initial=no"]

        extra = getattr(stream, 'extra_mpv_args', None)
        if extra:
            args.extend(extra)

        ipc_path = f"/tmp/aw-mpv-{os.getpid()}.sock"
        args.append(f"--input-ipc-server={ipc_path}")

        headers = getattr(stream, 'headers', None)
        if headers:
            mpv_headers = ",".join(f"{k}: {v}" for k, v in headers.items())
            args.append(f"--http-header-fields={mpv_headers}")

        sub_files: list[str] = []
        sub_tasks: list[asyncio.Task] = []
        subs = getattr(stream, 'subtitles', None)
        if subs:
            sub_headers = getattr(stream, 'headers', None) or {}
            for sub in subs:
                lang = (sub.get("lang") or sub.get("label") or "").lower()
                if "en" not in lang and "english" not in lang:
                    continue
                _url = sub.get("url")
                if not _url:
                    continue
                if os.path.exists(_url):
                    sub_files.append(_url)
                else:
                    sub_tasks.append(asyncio.create_task(
                        self._download_sub(_url, sub_headers, sub_files, sub.get("label") or lang)
                    ))
            if sub_tasks:
                await asyncio.gather(*sub_tasks)
            for f in sub_files:
                args.append(f"--sub-file={f}")

        name = episode.anime_name or episode.title
        label = f"{name} — {episode.title}"
        args.append(f"--title={label}")
        args.append(f"--force-media-title={label}")

        resume_at = episode.data.pop("_resume_at", 0)
        if resume_at > 0:
            args.append(f"--start={resume_at}")

        poll_task = None
        self._ipc_writer = None
        try:
            raw_playlist = getattr(stream, 'raw_playlist', None)
            stdin = asyncio.subprocess.PIPE if raw_playlist else None
            _url = "-" if raw_playlist else stream.url
            args.append(_url)
            proc = await asyncio.create_subprocess_exec(
                *args, stdin=stdin,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL
            )
            self._current_proc = proc
            if raw_playlist:
                proc.stdin.write(raw_playlist.encode())
                await proc.stdin.drain()
                proc.stdin.close()

            for _ in range(50):
                if os.path.exists(ipc_path):
                    break
                await asyncio.sleep(0.1)
            try:
                self._ipc_reader, self._ipc_writer = await asyncio.open_unix_connection(ipc_path)
                poll_task = asyncio.create_task(self._poll_mpv_position(self._ipc_reader))
            except (ConnectionRefusedError, FileNotFoundError, OSError):
                self._ipc_writer = None
            await proc.wait()
            self._mpv_returncode = proc.returncode
            if self._current_proc is proc:
                self._current_proc = None
        except FileNotFoundError:
            self._update_content("Error: mpv not found. Install it: apt install mpv / brew install mpv")
            self._update_footer()
            return
        finally:
            if poll_task is not None:
                poll_task.cancel()
                try:
                    await poll_task
                except asyncio.CancelledError:
                    pass
            if self._ipc_writer is not None:
                try:
                    self._ipc_writer.close()
                    await self._ipc_writer.wait_closed()
                except Exception:
                    pass
            try:
                os.unlink(ipc_path)
            except OSError:
                pass
            proxy = getattr(stream, 'proxy_server', None)
            if proxy:
                try:
                    proxy.shutdown()
                except Exception:
                    pass
            if sub_files:
                if stream.cleanup_paths is None:
                    stream.cleanup_paths = []
                stream.cleanup_paths.extend(sub_files)
            paths = getattr(stream, 'cleanup_paths', None)
            if paths:
                import shutil
                for p in paths:
                    try:
                        if os.path.isfile(p):
                            os.unlink(p)
                        elif os.path.isdir(p):
                            shutil.rmtree(p, ignore_errors=True)
                    except Exception:
                        pass
        rc = getattr(self, "_mpv_returncode", None)
        if rc not in (0, None):
            self._update_content(f"Playback failed (mpv exit {rc}): {episode.title[:40]} - stream link may be dead, expired, or blocked")
            self._update_footer()
            return
        entry = HistoryEntry(
            anime_name=episode.anime_name,
            episode_title=episode.title,
            episode_number=episode.number,
            site_name=episode.site_name,
            url=episode.url,
            data=episode.data.copy(),
            timestamp=time.time(),
            progress=getattr(self, "_mpv_last_pos", 0.0),
            duration=getattr(self, "_mpv_last_dur", 0.0),
        )
        add_history_entry(entry)
        self._update_content(f"Done: {episode.title[:40]}")
        self._update_footer()

    async def _do_play(self, stream_or_tracks, episode=None, overlay=None,
                       prefetch_cb: Optional[Callable] = None):
        """Dispatch: music (ytmusic) uses the mpv-native playlist path;
        everything else keeps the original single-track playback exactly."""
        if isinstance(stream_or_tracks, list):
            tracks = list(stream_or_tracks)
            ep0 = tracks[0][0] if tracks else episode
        else:
            tracks = None
            ep0 = episode
        is_music = getattr(ep0, "site_name", "") == "ytmusic"
        if tracks is not None and is_music:
            await self._do_play_music(tracks, overlay, prefetch_cb)
        else:
            if tracks is not None:
                stream = tracks[0][1]
            else:
                stream = stream_or_tracks
            await self._do_play_classic(stream, ep0, overlay)

    async def _do_play_music(self, tracks: list[tuple[Episode, StreamSource]], overlay=None,
                             prefetch_cb: Optional[Callable] = None):
        """Play one or more tracks in a single mpv instance. Each track is
        passed with its per-file options via --{ ... --} markers, so mpv's
        native playlist controls (</>/Enter next/prev, F8 playlist, L loop,
        space pause, 9/0 volume, X shuffle, C repeat-all) all work directly.
        When prefetch_cb is set, it is called (near the end of the playlist)
        with the current episode so the caller can prepare the next batch."""
        if not tracks:
            return
        self._tracks = tracks
        self._cur_idx = 0
        self._hist_written_for = -1
        self._current_episode = tracks[0][0]
        self._eof_reached = False
        self._advance_requested = False
        self._prefetch_cb = prefetch_cb
        self._prefetching = False
        self._overlay = overlay
        first_ep = tracks[0][0]
        self._update_content(f"Now playing: {first_ep.title}\nClose mpv to return...")
        args = ["mpv", "--no-terminal", "--osd-level=1", "--hwdec=no",
                "--vo=gpu", "--ontop", "--cache=yes", "--cache-secs=30",
                "--cache-pause-initial=no", "--keep-open=yes",
                f"--volume={getattr(self.app, 'volume', 100)}"]
        ipc_path = f"/tmp/aw-mpv-{os.getpid()}.sock"
        args.append(f"--input-ipc-server={ipc_path}")

        input_conf = _write_extra_input_conf()
        if input_conf:
            args.append(f"--input-conf={input_conf}")

        sub_tasks: list[asyncio.Task] = []
        for i, (ep, stream) in enumerate(tracks):
            subs = getattr(stream, "subtitles", None)
            if not subs:
                continue
            sub_headers = getattr(stream, "headers", None) or {}
            for sub in subs:
                lang = (sub.get("lang") or sub.get("label") or "").lower()
                if "en" not in lang and "english" not in lang:
                    continue
                _url = sub.get("url")
                if not _url:
                    continue
                if not os.path.exists(_url):
                    sub_tasks.append(asyncio.create_task(
                        self._download_sub(_url, sub_headers, [], sub.get("label") or lang)
                    ))
        if sub_tasks:
            await asyncio.gather(*sub_tasks)

        for i, (ep, stream) in enumerate(tracks):
            args.extend(self._per_file_args(stream, ep, first=(i == 0)))

        reader_task = None
        self._ipc_writer = None
        self._mpv_last_pos = 0.0
        self._mpv_last_dur = 0.0
        try:
            raw_playlist = getattr(tracks[0][1], 'raw_playlist', None)
            stdin = asyncio.subprocess.PIPE if raw_playlist else None
            if raw_playlist:
                args.append("-")
            proc = await asyncio.create_subprocess_exec(
                *args, stdin=stdin,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL
            )
            self._current_proc = proc
            if raw_playlist:
                proc.stdin.write(raw_playlist.encode())
                await proc.stdin.drain()
                proc.stdin.close()

            for _ in range(50):
                if os.path.exists(ipc_path):
                    break
                await asyncio.sleep(0.1)
            try:
                self._ipc_reader, self._ipc_writer = await asyncio.open_unix_connection(ipc_path)
                await self._send_ipc(["observe_property", 1, "time-pos"])
                await self._send_ipc(["observe_property", 2, "duration"])
                await self._send_ipc(["observe_property", 3, "playlist-pos"])
                await self._send_ipc(["observe_property", 4, "eof-reached"])
                await self._send_ipc(["observe_property", 5, "volume"])
                await self._send_ipc(["observe_property", 6, "pause"])
                reader_task = asyncio.create_task(self._mpv_reader(self._ipc_reader))
            except (ConnectionRefusedError, FileNotFoundError, OSError):
                self._ipc_writer = None
            await proc.wait()
            self._mpv_returncode = proc.returncode
            if self._current_proc is proc:
                self._current_proc = None
        except FileNotFoundError:
            self._update_content("Error: mpv not found. Install it: apt install mpv / brew install mpv")
            self._update_footer()
            return
        finally:
            if reader_task is not None:
                reader_task.cancel()
                try:
                    await reader_task
                except asyncio.CancelledError:
                    pass
            if self._ipc_writer is not None:
                try:
                    self._ipc_writer.close()
                    await self._ipc_writer.wait_closed()
                except Exception:
                    pass
            try:
                os.unlink(ipc_path)
            except OSError:
                pass
            cleaned = set()
            for _ep, stream in tracks:
                proxy = getattr(stream, 'proxy_server', None)
                if proxy and id(proxy) not in cleaned:
                    cleaned.add(id(proxy))
                    try:
                        proxy.shutdown()
                    except Exception:
                        pass
                paths = getattr(stream, 'cleanup_paths', None)
                if paths:
                    import shutil
                    for p in paths:
                        try:
                            if os.path.isfile(p):
                                os.unlink(p)
                            elif os.path.isdir(p):
                                shutil.rmtree(p, ignore_errors=True)
                        except Exception:
                            pass
        rc = getattr(self, "_mpv_returncode", None)
        if rc not in (0, None):
            self._update_content(f"Playback failed (mpv exit {rc}): {first_ep.title[:40]} - stream link may be dead, expired, or blocked")
            self._update_footer()
            return
        self._write_history()
        self._update_content(f"Done: {first_ep.title[:40]}")
        self._update_footer()

    def _log(self, text: str):
        overlay = getattr(self, "_overlay", None)
        if overlay is None:
            return
        try:
            overlay.add_log(text)
        except Exception:
            pass

    def _write_history(self):
        ep = self._current_episode
        if ep is None or self._cur_idx == self._hist_written_for:
            return
        self._hist_written_for = self._cur_idx
        entry = HistoryEntry(
            anime_name=ep.anime_name,
            episode_title=ep.title,
            episode_number=ep.number,
            site_name=ep.site_name,
            url=ep.url,
            data=(ep.data or {}).copy(),
            timestamp=time.time(),
            progress=getattr(self, "_mpv_last_pos", 0.0),
            duration=getattr(self, "_mpv_last_dur", 0.0),
        )
        add_history_entry(entry)

    async def _mpv_reader(self, reader) -> None:
        """Event loop: mpv pushes observe_property changes. We track position
        (history), playlist position (history per track), volume (persist),
        pause state, EOF (for autoplay) and trigger prefetch near the end."""
        while True:
            try:
                raw = await reader.readline()
            except (OSError, asyncio.IncompleteReadError):
                break
            if not raw:
                break
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if msg.get("event") != "property-change":
                continue
            prop = msg.get("name")
            data = msg.get("data")
            if prop == "time-pos" and isinstance(data, (int, float)):
                self._mpv_last_pos = float(data)
            elif prop == "duration" and isinstance(data, (int, float)):
                self._mpv_last_dur = float(data)
            elif prop == "playlist-pos" and isinstance(data, int) and data >= 0:
                self._write_history()
                self._cur_idx = data
                if 0 <= data < len(self._tracks):
                    self._current_episode = self._tracks[data][0]
                self._maybe_prefetch()
            elif prop == "eof-reached" and data is True:
                # With --keep-open=yes mpv stays alive at the end, so this
                # event is reliable (a quitting mpv can drop it). Signal the
                # caller, then quit so _do_play returns.
                self._eof_reached = True
                self._write_history()
                if self._cur_idx >= len(self._tracks) - 1:
                    await self._send_ipc(["quit"])
            elif prop == "volume" and isinstance(data, (int, float)):
                if hasattr(self.app, "volume"):
                    v = int(data)
                    if v != self.app.volume:
                        self.app.volume = v
                        settings = load_settings()
                        settings["volume"] = v
                        save_settings(settings)
            elif prop == "pause" and isinstance(data, bool):
                self._mpv_paused = data

    def _maybe_prefetch(self):
        """When the playlist is nearly done (2 tracks left), ask the caller
        to prepare the next batch so playback can continue seamlessly."""
        if self._prefetch_cb is None or self._prefetching:
            self._log("Autoplay: skipped (no callback or already fetching)")
            return
        if self._cur_idx < len(self._tracks) - 2:
            return
        self._log("Autoplay: prefetch triggered")
        self._prefetching = True
        asyncio.create_task(self._run_prefetch())

    async def _run_prefetch(self):
        try:
            if self._current_episode is not None:
                await self._prefetch_cb(self._current_episode)
        finally:
            self._prefetching = False
