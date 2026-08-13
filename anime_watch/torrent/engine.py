from __future__ import annotations
import asyncio
import os
import re
import shutil
import signal
import tempfile
from typing import Optional

from anime_watch.core import _which

TMPDIR_PREFIX = "aw-torrent-"

# webtorrent-cli wraps its status UI in chalk ANSI escapes even when stdout is a pipe.
# Strip them before parsing the "Server running at:" URL, or mpv gets a garbage URL.
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

class TorrentEngine:
    def __init__(self):
        self._processes: dict[str, asyncio.subprocess.Process] = {}
        self._tmpdirs: dict[str, str] = {}
        self._monitor_tasks: dict[str, asyncio.Task] = {}

    def is_available(self) -> bool:
        return _which("webtorrent")

    async def stream_and_save(
        self,
        magnet: str,
        info_hash: str,
        save_path: str,
        on_progress: Optional[callable] = None,
    ) -> None:
        """Download to disk (webtorrent HTTP server) while mpv plays the URL."""
        import threading as _threading
        dest_dir = os.path.dirname(save_path) or "."
        os.makedirs(dest_dir, exist_ok=True)
        done_evt = _threading.Event()
        url = self.download_to_dir_sync(
            magnet, info_hash, dest_dir, on_progress, on_done=done_evt.set,
        )
        if not url:
            self.stop(info_hash)
            return

        mpv = await asyncio.create_subprocess_exec(
            "mpv", "--no-terminal", "--osd-level=0", "--vo=gpu",
            "--cache=yes", "--cache-secs=30",
            "--demuxer-max-bytes=128MiB", url,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )

        await mpv.wait()
        self.stop(info_hash)
        # Let webtorrent finish writing the remaining pieces (if it was still
        # downloading when the player closed) so the saved copy is complete.
        done_evt.wait(timeout=30)

        largest = max(
            (f for f in self._find_video_files(dest_dir) if os.path.exists(f)),
            key=os.path.getsize,
            default=None,
        )
        if largest and os.path.abspath(largest) != os.path.abspath(save_path):
            try:
                os.replace(largest, save_path)
            except OSError:
                pass

    async def stream_pipe(
        self,
        magnet: str,
        info_hash: str,
        on_progress: Optional[callable] = None,
    ) -> None:
        """Watch-only: download to a temp dir (disk) and play via the HTTP URL."""
        import os
        tmpdir = tempfile.mkdtemp(prefix=TMPDIR_PREFIX)
        self._tmpdirs[info_hash] = tmpdir
        url = self.download_to_dir_sync(magnet, info_hash, tmpdir, on_progress)
        if not url:
            self.stop(info_hash)
            return

        mpv = await asyncio.create_subprocess_exec(
            "mpv", "--no-terminal", "--osd-level=0", "--vo=gpu",
            "--cache=yes", "--cache-secs=30",
            "--demuxer-max-bytes=128MiB", url,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )

        await mpv.wait()
        self.stop(info_hash)

    async def download_to_dir(
        self,
        magnet: str,
        info_hash: str,
        dest_dir: str,
        on_progress: Optional[callable] = None,
    ) -> Optional[str]:
        """Download to a permanent directory. Returns file path when 50MB+ buffered, or None on timeout."""
        os.makedirs(dest_dir, exist_ok=True)

        proc = await asyncio.create_subprocess_exec(
            "webtorrent", "download", magnet,
            "--out", dest_dir, "--quiet",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            preexec_fn=os.setsid,
        )
        self._processes[info_hash] = proc

        stderr_task = asyncio.create_task(
            self._pipe_stderr(proc.stderr, info_hash, on_progress)
        )

        waited = 0
        while waited < 120:
            files = self._find_video_files(dest_dir)
            if files:
                largest = max(files, key=lambda f: os.path.getsize(f) if os.path.exists(f) else 0)
                if (os.path.exists(largest) and os.path.getsize(largest) > 50 * 1024 * 1024
                        and self._file_has_header(largest)):
                    stderr_task.cancel()
                    self._close_stderr(proc)
                    return largest
            await asyncio.sleep(2)
            waited += 2

        stderr_task.cancel()
        self._close_stderr(proc)
        return None

    def _wait_until_serving(self, url: str, timeout: float = 15.0) -> bool:
        """HEAD-poll the webtorrent HTTP server until it serves the file (or timeout)."""
        import urllib.request
        import time as _time
        deadline = _time.time() + timeout
        while _time.time() < deadline:
            try:
                req = urllib.request.Request(url, method="HEAD")
                with urllib.request.urlopen(req, timeout=2) as resp:
                    if 200 <= getattr(resp, "status", 200) < 400:
                        return True
            except Exception:
                pass
            _time.sleep(0.5)
        return False

    def _discover_webtorrent_port(self, info_hash: str) -> Optional[int]:
        """Find the HTTP port of the webtorrent process serving this torrent.

        The CLI binds port 8000 by default but silently takes a random port on
        EADDRINUSE, and 6.x never prints the URL in download mode — so scan the
        common range for a server that answers the /webtorrent/<hash> page.
        """
        import urllib.request
        import time as _time
        for port in range(8000, 8100):
            url = f"http://localhost:{port}/webtorrent/{info_hash}"
            try:
                req = urllib.request.Request(url, method="HEAD")
                with urllib.request.urlopen(req, timeout=1) as resp:
                    if 200 <= getattr(resp, "status", 200) < 400:
                        return port
            except Exception:
                pass
        return None

    def _construct_webtorrent_url(self, info_hash: str, dest_dir: str, base_url: Optional[str] = None) -> Optional[str]:
        import urllib.parse
        videos = self._find_video_files(dest_dir)
        if not videos:
            return None
        largest = max(videos, key=lambda f: os.path.getsize(f) if os.path.exists(f) else 0)
        base = (base_url or "http://localhost:8000/").rstrip("/")
        rel = os.path.relpath(largest, dest_dir)

        # webtorrent's HTTP server matches requests against the IN-TORRENT
        # file path, while the chunk store writes to disk under
        # <out>/<torrent-name>/<in-torrent path>. Try disk-relative, the
        # stripped form (drop the torrent-name prefix) and the bare filename.
        candidates = [rel]
        first, sep, rest = rel.partition(os.sep)
        if sep:
            candidates.append(rest)
        candidates.append(os.path.basename(rel))
        seen = set()
        for cand in candidates:
            if cand in seen:
                continue
            seen.add(cand)
            encoded = urllib.parse.quote(cand, safe="/")
            url = f"{base}/webtorrent/{info_hash}/{encoded}"
            if self._wait_until_serving(url, timeout=4):
                return url
        return None

    def download_to_dir_sync(
        self,
        magnet: str,
        info_hash: str,
        dest_dir: str,
        on_progress: Optional[callable] = None,
        track: bool = True,
        on_done: Optional[callable] = None,
    ) -> Optional[str]:
        import subprocess as _subprocess
        import threading as _threading
        import time as _time

        os.makedirs(dest_dir, exist_ok=True)

        # Hint a stable port so concurrent torrents don't both fight for 8000;
        # if taken the CLI picks a random one and we discover it below.
        port_hint = 8000 + int(info_hash[:2], 16) % 100

        # --keep-seeding: webtorrent-cli exits on download completion when
        # nobody has connected to its HTTP server yet, killing the stream
        # URL right before the player connects.
        # --download-limit: cap the rate so in-flight piece buffers and the
        # page-cache/dirty-page pileup from a GB-scale download stay bounded
        # on low-RAM machines (8GB here) instead of OOM-killing the player.
        proc = _subprocess.Popen(
            ["webtorrent", "download", magnet, "--out", dest_dir, "--keep-seeding",
             "--port", str(port_hint),
             "--download-limit", "30000", "--upload-limit", "2000"],
            stdout=_subprocess.PIPE,
            stderr=_subprocess.PIPE,
            preexec_fn=os.setsid,
        )
        if track:
            self._processes[info_hash] = proc

        base_url: Optional[str] = None
        stop_event = _threading.Event()

        def _read_stdout():
            nonlocal base_url
            try:
                for line in iter(proc.stdout.readline, b""):
                    if stop_event.is_set():
                        break
                    # strip chalk ANSI escapes (webtorrent emits them even on a pipe)
                    text = _ANSI_RE.sub("", line.decode(errors="replace"))
                    if base_url is None and "Server running at:" in text:
                        idx = text.index("Server running at:")
                        candidate = text[idx + len("Server running at:"):].strip()
                        if candidate.startswith("http"):
                            base_url = candidate
            except ValueError:
                pass
            # stdout EOF = webtorrent exited; never wait forever for completion.
            if on_done:
                on_done()

        stdout_reader = _threading.Thread(target=_read_stdout, daemon=True)
        stdout_reader.start()

        def _read_stderr():
            try:
                for line in iter(proc.stderr.readline, b""):
                    if stop_event.is_set():
                        break
                    text = line.decode(errors="replace").strip()
                    if on_progress and text:
                        msg = self._parse_progress_sync(text)
                        if msg:
                            on_progress(msg)
                            if on_done and msg.startswith("100%"):
                                on_done()
            except ValueError:
                pass

        stderr_reader = _threading.Thread(target=_read_stderr, daemon=True)
        stderr_reader.start()

        # Wait for metadata + first bytes on disk. webtorrent-cli 6.x never
        # prints the server URL in download mode, so only require the torrent
        # folder to appear (its store files are created once metadata
        # resolves), then construct the URL ourselves.
        waited = 0
        limit = 120
        while waited < limit and not self._find_video_files(dest_dir) and not stop_event.is_set():
            _time.sleep(1)
            waited += 1

        if not self._find_video_files(dest_dir):
            stop_event.set()
            self._abort_proc(proc)
            return None

        # If the CLI DID print a full stream URL, prefer it.
        if base_url and "/webtorrent/" in base_url:
            if self._wait_until_serving(base_url, timeout=15):
                return base_url

        # Otherwise locate the server's actual port and construct the URL
        # (tries disk-relative, name-stripped and basename forms until one
        # answers).
        port = self._discover_webtorrent_port(info_hash)
        if port is None:
            stop_event.set()
            self._abort_proc(proc)
            return None
        file_url = self._construct_webtorrent_url(
            info_hash, dest_dir, base_url=f"http://localhost:{port}/")
        if not file_url:
            stop_event.set()
            self._abort_proc(proc)
            return None
        return file_url

    @staticmethod
    def _abort_proc(proc) -> None:
        """Kill a webtorrent process and close its pipes after a give-up."""
        try:
            proc.kill()
        except Exception:
            pass
        try:
            proc.stdout.close()
        except OSError:
            pass
        try:
            proc.stderr.close()
        except OSError:
            pass

    def _parse_progress_sync(self, text: str) -> Optional[str]:
        m = re.search(r'(\d+\.?\d*)\s*%', text)
        if m:
            pct = m.group(1)
            m2 = re.search(r'([\d.]+)\s*(KB|MB|GB)/s', text)
            if m2:
                return f"{pct}% {m2.group(1)} {m2.group(2)}/s"
            return f"{pct}%"
        m = re.search(r'([\d.]+)\s*(KB|MB|GB)/s', text)
        if m:
            return f"{m.group(1)} {m.group(2)}/s"
        return None

    async def stream(
        self,
        magnet: str,
        info_hash: str,
        on_ready: callable,
        on_progress: Optional[callable] = None,
        cleanup_after: bool = True,
    ) -> None:
        tmpdir = tempfile.mkdtemp(prefix=TMPDIR_PREFIX)
        self._tmpdirs[info_hash] = tmpdir

        proc = await asyncio.create_subprocess_exec(
            "webtorrent", "download", magnet,
            "--out", tmpdir,
            "--quiet",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            preexec_fn=os.setsid,
        )
        self._processes[info_hash] = proc

        monitor = asyncio.create_task(
            self._monitor_download(info_hash, tmpdir, on_ready, on_progress, cleanup_after)
        )
        self._monitor_tasks[info_hash] = monitor

        stdout_task = asyncio.create_task(self._pipe_stderr(proc.stderr, info_hash, on_progress))
        await proc.wait()
        stdout_task.cancel()
        self._close_stderr(proc)

        if cleanup_after and info_hash in self._tmpdirs:
            self._cleanup(info_hash)

    async def download(
        self,
        magnet: str,
        info_hash: str,
        dest_dir: str,
        on_complete: Optional[callable] = None,
        on_progress: Optional[callable] = None,
    ) -> None:
        os.makedirs(dest_dir, exist_ok=True)

        proc = await asyncio.create_subprocess_exec(
            "webtorrent", "download", magnet,
            "--out", dest_dir,
            "--quiet",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            preexec_fn=os.setsid,
        )
        self._processes[info_hash] = proc

        stdout_task = asyncio.create_task(self._pipe_stderr(proc.stderr, info_hash, on_progress))
        await proc.wait()
        stdout_task.cancel()
        self._close_stderr(proc)

        if on_complete:
            on_complete(dest_dir)

    def download_sync(
        self,
        magnet: str,
        info_hash: str,
        dest_dir: str,
        on_complete: Optional[callable] = None,
        on_progress: Optional[callable] = None,
    ) -> None:
        import subprocess as _subprocess
        import threading as _threading

        os.makedirs(dest_dir, exist_ok=True)

        proc = _subprocess.Popen(
            ["webtorrent", "download", magnet, "--out", dest_dir, "--quiet"],
            stdout=_subprocess.DEVNULL,
            stderr=_subprocess.PIPE,
            preexec_fn=os.setsid,
        )
        self._processes[info_hash] = proc

        stop_event = _threading.Event()

        def _read_stderr():
            try:
                for line in iter(proc.stderr.readline, b""):
                    if stop_event.is_set():
                        break
                    text = line.decode(errors="replace").strip()
                    if on_progress and text:
                        msg = self._parse_progress_sync(text)
                        if msg:
                            on_progress(msg)
            except ValueError:
                pass

        reader = _threading.Thread(target=_read_stderr, daemon=True)
        reader.start()

        try:
            proc.wait()
        finally:
            stop_event.set()
            try:
                proc.stderr.close()
            except OSError:
                pass
            reader.join(timeout=2)

        if on_complete:
            on_complete(dest_dir)

    async def _monitor_download(
        self,
        info_hash: str,
        tmpdir: str,
        on_ready: callable,
        on_progress: Optional[callable] = None,
        cleanup_after: bool = True,
    ) -> None:
        waited = 0
        while waited < 120:
            files = self._find_video_files(tmpdir)
            if files:
                largest = max(files, key=lambda f: os.path.getsize(f) if os.path.exists(f) else 0)
                if os.path.exists(largest) and os.path.getsize(largest) > 50 * 1024 * 1024:
                    on_ready(largest)
                    return
            if on_progress:
                size = self._dir_size(tmpdir)
                on_progress(f"Buffering {_format_bytes(size)}…")
            await asyncio.sleep(2)
            waited += 2

    def _find_video_files(self, directory: str) -> list[str]:
        found = []
        for root, dirs, files in os.walk(directory):
            for f in files:
                if f.lower().endswith((".mp4", ".mkv", ".webm", ".avi", ".mov", ".ts", ".m4v", ".flv")):
                    found.append(os.path.join(root, f))
        return found

    def _dir_size(self, directory: str) -> int:
        total = 0
        for root, dirs, files in os.walk(directory):
            for f in files:
                try:
                    total += os.path.getsize(os.path.join(root, f))
                except OSError:
                    pass
        return total

    def _file_has_header(self, path: str, min_nonzero: int = 1024) -> bool:
        try:
            with open(path, "rb") as f:
                data = f.read(4096)
            nonzero = sum(1 for b in data if b != 0)
            return nonzero >= min_nonzero
        except OSError:
            return False

    def _close_stderr(self, proc) -> None:
        if proc.stderr and hasattr(proc.stderr, '_transport') and proc.stderr._transport:
            try:
                proc.stderr._transport.close()
            except Exception:
                pass

    async def _pipe_stderr(self, stderr, info_hash: str, on_progress: Optional[callable]):
        if not stderr:
            return
        try:
            while True:
                line = await stderr.readline()
                if not line:
                    break
                text = line.decode(errors="replace").strip()
                if on_progress and text:
                    pct = speed = None
                    m = re.search(r'(\d+\.?\d*)\s*%', text)
                    if m:
                        pct = m.group(1)
                    m = re.search(r'([\d.]+)\s*(KB|MB|GB)/s', text)
                    if m:
                        speed = f"{m.group(1)} {m.group(2)}/s"
                    if pct and speed:
                        on_progress(f"{pct}% {speed}")
                    elif pct:
                        on_progress(f"{pct}%")
                    elif speed:
                        on_progress(speed)
                    else:
                        m = re.search(r'\b(\d{1,3})\b', text)
                        if m:
                            val = int(m.group(1))
                            if 0 <= val <= 100:
                                on_progress(f"{val}%")
        except Exception:
            pass

    def stop(self, info_hash: str) -> None:
        proc = self._processes.pop(info_hash, None)
        if proc and proc.returncode is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
        self._cleanup(info_hash)

    def pause(self, info_hash: str) -> None:
        proc = self._processes.get(info_hash)
        if proc and proc.returncode is None:
            try:
                os.kill(proc.pid, signal.SIGSTOP)
            except ProcessLookupError:
                pass

    def resume(self, info_hash: str) -> None:
        proc = self._processes.get(info_hash)
        if proc and proc.returncode is None:
            try:
                os.kill(proc.pid, signal.SIGCONT)
            except ProcessLookupError:
                pass

    def _cleanup(self, info_hash: str) -> None:
        tmpdir = self._tmpdirs.pop(info_hash, None)
        if tmpdir and os.path.isdir(tmpdir):
            shutil.rmtree(tmpdir, ignore_errors=True)

    def stop_all(self) -> None:
        for info_hash in list(self._processes.keys()):
            self.stop(info_hash)
        import subprocess
        try:
            subprocess.run(
                ["pkill", "-9", "-f", "webtorrent"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=2,
            )
        except Exception:
            pass


def _format_bytes(b: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if b < 1024:
            return f"{b:.0f} {unit}"
        b //= 1024
    return f"{b:.1f} GB"


_engine_instance: Optional[TorrentEngine] = None

def get_engine() -> TorrentEngine:
    global _engine_instance
    if _engine_instance is None:
        _engine_instance = TorrentEngine()
    return _engine_instance
