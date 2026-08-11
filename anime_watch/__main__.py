import os, sys
sys.path.insert(0, os.path.dirname(__file__) + "/..")

def _progress_bar(done: int, total: int, width: int = 18) -> str:
    frac = done / total if total else 0.0
    filled = int(width * frac)
    pct = int(frac * 100)
    return "[" + "#" * filled + "-" * (width - filled) + f"] {pct:>3}%  {done}/{total}"

def _check_providers_terminal() -> None:
    from anime_watch.updater import (
        apply_providers_update_sync, fetch_providers_version,
        _parse_version, _read_local_providers_version,
    )
    import os as _os
    _pkg = _os.path.dirname(_os.path.abspath(__file__))
    missing = not _os.path.isfile(_os.path.join(_pkg, "providers", "__init__.py"))
    try:
        remote_text = fetch_providers_version()
    except Exception:
        return
    remote = _parse_version(remote_text)
    local = _read_local_providers_version()
    if remote <= local and not missing:
        return
    print(f"New providers version {remote_text.strip()} available.")
    print("Downloading providers…")

    def _on_progress(done: int, total: int, rel: str) -> None:
        sys.stderr.write(f"\r\033[K  {_progress_bar(done, total)}  {rel}")
        sys.stderr.flush()
        if done == total:
            sys.stderr.write("\n")

    if apply_providers_update_sync(remote_text, progress_cb=_on_progress):
        print("Providers updated.")
    else:
        print("Providers update failed — continuing with the installed ones.")

def main():
    if len(sys.argv) > 1 and sys.argv[1] == "plugin":
        from anime_watch.plugin.cli import run_plugin_cli
        return run_plugin_cli(sys.argv[2:])
    from anime_watch.updater import check_for_updates
    check_for_updates()
    _check_providers_terminal()
    try:
        from anime_watch.tui.app import run_app
    except ImportError:
        print("Providers unavailable — retrying fetch…")
        _check_providers_terminal()
        from anime_watch.tui.app import run_app
    run_app()
    return 0

if __name__ == "__main__":
    sys.exit(main())
