from __future__ import annotations
import asyncio
import json
import os
import tempfile
import time
from typing import Optional
import requests
from anime_watch.models import Episode, StreamSource
from anime_watch.history import HistoryEntry, add_entry as add_history_entry


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

    def kill_current(self):
        if self._current_proc and self._current_proc.returncode is None:
            try:
                self._current_proc.kill()
            except ProcessLookupError:
                pass

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

    async def _do_play(self, stream: StreamSource, episode: Episode, overlay=None):
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
