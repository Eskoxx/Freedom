import hashlib
import os
from typing import Optional
import re
import threading
import urllib.request
from concurrent.futures import ThreadPoolExecutor

_DESKTOP_REPO = "Eskoxx/Freedom"
_ANDROID_REPO = "Eskoxx/Freedom-Android"
_ANDROID_ASSET_PREFIX = "app/src/main/assets/anime_watch"
_ANDROID_DATA_DIR = "/data/data/io.freedom"

_UA = "Freedom/2.0 (+auto-update)"

# Parallel downloads: most updates touch 1-3 files, but big pushes (vendored
# trees, fresh installs) sync dozens — fetching them one at a time is the
# bottleneck. Verification stays per-file (digest check before write).
_UPDATE_WORKERS = 5


def _download_verified(url: str, digest: str, timeout: float) -> Optional[bytes]:
    """Download a file and return it only if its sha256 matches the manifest."""
    try:
        data = _http_get(url, timeout=timeout)
    except Exception:
        return None
    if hashlib.sha256(data).hexdigest() != digest:
        return None
    return data


def _is_android() -> bool:
    return os.environ.get("ANDROID_ROOT") is not None or os.path.isdir(_ANDROID_DATA_DIR)


def _pkg_dir() -> str:
    return os.path.dirname(os.path.abspath(__file__))


def _repo() -> str:
    if _is_android():
        return _ANDROID_REPO
    return _DESKTOP_REPO


def _asset_prefix() -> str:
    if _is_android():
        return _ANDROID_ASSET_PREFIX
    return "anime_watch"


def _raw_url(path: str) -> str:
    return f"https://raw.githubusercontent.com/{_repo()}/main/{_asset_prefix()}/{path}"


def _http_get(url: str, timeout: float) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def _parse_version(text: str) -> int:
    m = re.search(r"\d+", text or "")
    return int(m.group(0)) if m else 0


def _read_local_version() -> int:
    marker = os.path.join(_pkg_dir(), ".update-version")
    try:
        with open(marker, "r", encoding="utf-8") as f:
            return _parse_version(f.read())
    except (OSError, ValueError):
        return 0


def _write_local_version(remote_text: str) -> None:
    marker = os.path.join(_pkg_dir(), ".update-version")
    try:
        with open(marker, "w", encoding="utf-8") as f:
            f.write(remote_text.strip() + "\n")
    except OSError:
        pass


def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
    except OSError:
        return ""
    return h.hexdigest()


def fetch_remote_version(timeout: float = 5.0) -> str:
    return _http_get(_raw_url(".update-version"), timeout).decode("utf-8", "replace")


def _fetch_manifest(timeout: float = 10.0) -> dict[str, str]:
    body = _http_get(_raw_url(".update-manifest"), timeout).decode("utf-8", "replace")
    prefix = _asset_prefix() + "/"
    manifest = {}
    for line in body.splitlines():
        line = line.strip()
        if not line or "  " not in line:
            continue
        digest, rel = line.split("  ", 1)
        rel = rel.strip()
        if rel.startswith(prefix):
            rel = rel[len(prefix):]
        manifest[rel] = digest.strip()
    return manifest


def _apply_manifest(manifest: dict[str, str]) -> int:
    pkg = _pkg_dir()
    changed = 0
    todo = []
    for rel, digest in manifest.items():
        dest = os.path.join(pkg, rel)
        if os.path.isfile(dest) and _sha256_file(dest) == digest:
            continue
        todo.append((rel, digest, dest))

    if todo:
        def _fetch(item):
            rel, digest, dest = item
            data = _download_verified(_raw_url(rel), digest, 30)
            if data is None:
                return None
            return (dest, data)
        with ThreadPoolExecutor(max_workers=_UPDATE_WORKERS) as pool:
            for result in pool.map(_fetch, todo):
                if result is None:
                    continue
                dest, data = result
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                tmp = dest + ".tmp"
                with open(tmp, "wb") as f:
                    f.write(data)
                os.replace(tmp, dest)
                changed += 1

    keep = set(manifest.keys()) | {".update-version", ".update-pending", ".update-manifest"}
    for root, _dirs, files in os.walk(pkg):
        if "__pycache__" in root:
            continue
        for fname in files:
            full = os.path.join(root, fname)
            rel = os.path.relpath(full, pkg)
            if rel in keep:
                continue
            if rel.startswith("providers/"):
                # providers/ is owned by the separate providers feed
                continue
            try:
                os.unlink(full)
            except OSError:
                pass
    return changed


def _apply_update_async(remote_text: str) -> None:
    def run():
        try:
            manifest = _fetch_manifest()
            if not manifest:
                return
            if _apply_manifest(manifest) > 0:
                _write_local_version(remote_text)
                notice = os.path.join(_pkg_dir(), ".update-pending")
                try:
                    with open(notice, "w", encoding="utf-8") as f:
                        f.write(remote_text.strip() + "\n")
                except OSError:
                    pass
        except Exception:
            pass

    t = threading.Thread(target=run, daemon=True, name="freedom-updater")
    t.start()


def check_for_updates(timeout: float = 5.0, apply: bool = True) -> bool:
    """Check remote for a newer version. Returns True if an update is pending/applying.

    Fail-open: any error (offline, HTTP, parse) is swallowed and returns False.
    """
    try:
        remote_text = fetch_remote_version(timeout)
    except Exception:
        return False
    remote = _parse_version(remote_text)
    local = _read_local_version()
    if remote <= local:
        return False
    if apply:
        _apply_update_async(remote_text)
    return True



# ── Providers feed (separate repo: Eskoxx/providers) ──

_PROVIDERS_REPO = "Eskoxx/providers"
_PROVIDERS_PREFIX = "providers"

def _providers_raw_url(path: str) -> str:
    return f"https://raw.githubusercontent.com/{_PROVIDERS_REPO}/main/{_PROVIDERS_PREFIX}/{path}"

def _providers_dir() -> str:
    return os.path.join(_pkg_dir(), "providers")

def _read_local_providers_version() -> int:
    marker = os.path.join(_providers_dir(), ".providers-version")
    try:
        with open(marker, "r", encoding="utf-8") as f:
            return _parse_version(f.read())
    except (OSError, ValueError):
        return 0

def _write_local_providers_version(remote_text: str) -> None:
    marker = os.path.join(_providers_dir(), ".providers-version")
    try:
        with open(marker, "w", encoding="utf-8") as f:
            f.write(remote_text.strip() + "\\n")
    except OSError:
        pass

def fetch_providers_version(timeout: float = 5.0) -> str:
    return _http_get(_providers_raw_url(".update-version"), timeout).decode("utf-8", "replace")

def _fetch_providers_manifest(timeout: float = 10.0) -> dict[str, str]:
    body = _http_get(_providers_raw_url(".update-manifest"), timeout).decode("utf-8", "replace")
    prefix = _PROVIDERS_PREFIX + "/"
    manifest = {}
    for line in body.splitlines():
        line = line.strip()
        if not line or "  " not in line:
            continue
        digest, rel = line.split("  ", 1)
        rel = rel.strip()
        if rel.startswith(prefix):
            rel = rel[len(prefix):]
        manifest[rel] = digest.strip()
    return manifest

def _apply_providers_manifest(manifest: dict[str, str]) -> int:
    pkg = _providers_dir()
    os.makedirs(pkg, exist_ok=True)
    changed = 0
    todo = []
    for rel, digest in manifest.items():
        dest = os.path.join(pkg, rel)
        if os.path.isfile(dest) and _sha256_file(dest) == digest:
            continue
        todo.append((rel, digest, dest))

    if todo:
        def _fetch(item):
            rel, digest, dest = item
            data = _download_verified(_providers_raw_url(rel), digest, 30)
            if data is None:
                return None
            return (dest, data)
        with ThreadPoolExecutor(max_workers=_UPDATE_WORKERS) as pool:
            for result in pool.map(_fetch, todo):
                if result is None:
                    continue
                dest, data = result
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                tmp = dest + ".tmp"
                with open(tmp, "wb") as f:
                    f.write(data)
                os.replace(tmp, dest)
                changed += 1
    keep = set(manifest.keys()) | {".providers-version"}
    for root, _dirs, files in os.walk(pkg):
        if "__pycache__" in root:
            continue
        for fname in files:
            full = os.path.join(root, fname)
            rel = os.path.relpath(full, pkg)
            if rel in keep:
                continue
            try:
                os.unlink(full)
            except OSError:
                pass
    return changed

def apply_providers_update_sync(remote_text: str, timeout: float = 30.0, progress_cb=None) -> bool:
    """Download + apply the providers feed synchronously (two-phase).

    All files are downloaded and verified before anything is written, so a
    failure leaves the current providers untouched.
    """
    try:
        manifest = _fetch_providers_manifest(timeout)
    except Exception:
        return False
    if not manifest:
        return False
    entries = sorted(manifest.items())
    total = len(entries)
    staged: list[tuple[str, bytes]] = []

    def _fetch(item):
        rel, digest = item
        data = _download_verified(_providers_raw_url(rel), digest, timeout)
        if data is None:
            return None
        return (rel, data)

    if total:
        with ThreadPoolExecutor(max_workers=_UPDATE_WORKERS) as pool:
            for i, result in enumerate(pool.map(_fetch, entries), start=1):
                if result is None:
                    return False
                staged.append(result)
                if progress_cb:
                    progress_cb(i, total, result[0])
    pkg = _providers_dir()
    for rel, data in staged:
        dest = os.path.join(pkg, rel)
        try:
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            tmp = dest + ".tmp"
            with open(tmp, "wb") as f:
                f.write(data)
            os.replace(tmp, dest)
        except OSError:
            return False
    keep = set(manifest.keys()) | {".providers-version"}
    for root, _dirs, files in os.walk(pkg):
        if "__pycache__" in root:
            continue
        for fname in files:
            full = os.path.join(root, fname)
            rel = os.path.relpath(full, pkg)
            if rel in keep:
                continue
            try:
                os.unlink(full)
            except OSError:
                pass
    _write_local_providers_version(remote_text)
    return True

def _apply_providers_update_async(remote_text: str) -> None:
    def run():
        try:
            manifest = _fetch_providers_manifest()
            if not manifest:
                return
            if _apply_providers_manifest(manifest) > 0:
                _write_local_providers_version(remote_text)
        except Exception:
            pass
    t = threading.Thread(target=run, daemon=True, name="freedom-providers-updater")
    t.start()

def check_providers_updates(timeout: float = 5.0, apply: bool = True) -> bool:
    """Check the providers repo for a newer version. Fail-open."""
    try:
        remote_text = fetch_providers_version(timeout)
    except Exception:
        return False
    remote = _parse_version(remote_text)
    local = _read_local_providers_version()
    if remote <= local:
        return False
    if apply:
        _apply_providers_update_async(remote_text)
    return True

def pending_update_notice() -> str:
    notice = os.path.join(_pkg_dir(), ".update-pending")
    try:
        with open(notice, "r", encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def clear_pending_notice() -> None:
    notice = os.path.join(_pkg_dir(), ".update-pending")
    try:
        os.unlink(notice)
    except OSError:
        pass
