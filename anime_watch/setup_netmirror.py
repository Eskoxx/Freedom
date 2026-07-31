import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.request

CONFIG_DIR = os.path.expanduser("~/.config/anime-watch")
CONFIG_FILE = os.path.join(CONFIG_DIR, "net77_cookies.json")
BRAVE_BIN = ""

CDP_PORT = 19222


def find_brave():
    for path in ("/usr/bin/brave", "/usr/bin/brave-browser", "/snap/bin/brave"):
        if os.path.isfile(path):
            return path
    return shutil.which("brave") or shutil.which("brave-browser") or ""


def find_free_port():
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def cdp_send(ws, msg_id, method, params=None):
    payload = {"id": msg_id, "method": method}
    if params:
        payload["params"] = params
    ws.send(json.dumps(payload))


def cdp_recv(ws, expected_id, timeout=60):
    deadline = time.time() + timeout
    while time.time() < deadline:
        raw = ws.recv()
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if data.get("id") == expected_id:
            return data
    raise TimeoutError(f"CDP response {expected_id} not received")


def cdp_recv_event(ws, method, timeout=60):
    deadline = time.time() + timeout
    while time.time() < deadline:
        raw = ws.recv()
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if data.get("method") == method:
            return data
    raise TimeoutError(f"CDP event {method} not received")


def get_cookies_via_cdp(brave_path, port):
    import websocket

    args = [
        brave_path,
        f"--remote-debugging-port={port}",
        "--remote-allow-origins=*",
        "--no-first-run",
        "--new-window",
    ]

    proc = subprocess.Popen(
        args,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        preexec_fn=lambda: signal.signal(signal.SIGCHLD, signal.SIG_DFL),
    )

    success = False
    try:
        browser_ws_url = None
        for attempt in range(15):
            time.sleep(1)
            try:
                resp = urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=3)
                data = json.loads(resp.read())
                browser_ws_url = data.get("webSocketDebuggerUrl")
                if browser_ws_url:
                    break
            except Exception:
                continue

        if not browser_ws_url:
            raise RuntimeError("Brave did not start in time")

        ws = websocket.create_connection(browser_ws_url, timeout=30)

        cdp_send(ws, 1, "Target.getTargets")
        targets = cdp_recv(ws, 1)
        tab_id = None
        for t in targets.get("result", {}).get("targetInfos", []):
            if t["type"] == "page":
                tab_id = t["targetId"]
                break

        if not tab_id:
            cdp_send(ws, 2, "Target.createTarget", {"url": "about:blank"})
            result = cdp_recv(ws, 2)
            tab_id = result["result"]["targetId"]

        tab_ws_url = f"ws://127.0.0.1:{port}/devtools/page/{tab_id}"
        tab_ws = websocket.create_connection(tab_ws_url, timeout=30)

        cdp_send(tab_ws, 1, "Page.enable")
        cdp_recv(tab_ws, 1)

        cdp_send(tab_ws, 2, "Network.enable")
        cdp_recv(tab_ws, 2)

        cdp_send(tab_ws, 3, "Page.navigate", {"url": "https://net77.cc/"})
        cdp_recv(tab_ws, 3)
        print("  Navigated to net77.cc")
        print("")
        print("  Log in with Gmail on the page that opened.")
        print("  Waiting for login... (check the browser window)")

        saved_cookies = None
        poll_id = 4
        while True:
            time.sleep(2)
            cdp_send(tab_ws, poll_id, "Network.getAllCookies")
            result = cdp_recv(tab_ws, poll_id)
            poll_id += 1
            cookies = result.get("result", {}).get("cookies", [])

            for c in cookies:
                if c["name"] == "user_token" and c["value"]:
                    net77_cookies = {}
                    for cc in cookies:
                        domain = cc.get("domain", "")
                        name = cc["name"]
                        if "net77" in domain or "net52" in domain or "nm-cdn" in domain or name in ("user_token", "t_hash", "t_hash_p", "cf_clearance", "t_hash"):
                            net77_cookies[name] = cc["value"]
                            print(f"    cookie: {name} (domain: {domain})")
                    if "user_token" in net77_cookies:
                        saved_cookies = net77_cookies
                    break

            if saved_cookies:
                print(f"  Login detected! user_token: {saved_cookies['user_token'][:20]}...")
                break

            sys.stdout.write(".")
            sys.stdout.flush()

        tab_ws.close()
        ws.close()
        success = True
        return saved_cookies

    finally:
        if success:
            proc.kill()
            proc.wait()
        else:
            print("  Error during CDP handshake — browser left open for debugging.")
            print("  Close the browser window manually when done.")


def main():
    global BRAVE_BIN
    BRAVE_BIN = find_brave()
    if not BRAVE_BIN:
        print("Brave not found. Please install Brave or specify the path.")
        return 1

    print("NetMirror Cookie Setup")
    print("======================")
    print()

    port = CDP_PORT
    try:
        cookies = get_cookies_via_cdp(BRAVE_BIN, port)
    except Exception as e:
        print(f"  Error: {e}")
        return 1

    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(CONFIG_FILE, "w") as f:
        json.dump(cookies, f, indent=2)

    print()
    print(f"  Cookies saved to: {CONFIG_FILE}")
    print(f"  You can now use the NetMirror provider.")
    print()
    print(f"  Captured cookies: {list(cookies.keys())}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
