"""DiscTray installer and updater, per user (no admin rights, nothing outside your profile).

Downloads the latest release from GitHub, installs it to
%LOCALAPPDATA%\\Programs\\DiscTray, adds a Start Menu shortcut and an entry under
Settings > Apps for this user only, then starts DiscTray.

    DiscTrayUpdater.exe               install, or update to the newest release
    DiscTrayUpdater.exe --from-app    the same, started from DiscTray's tray menu
    DiscTrayUpdater.exe --uninstall   remove DiscTray for this user
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import json
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import winreg
import zipfile
from pathlib import Path

REPO = "ASQUALL-hub/disctray"
APP = "DiscTray"
ASSET_SUFFIX = "-win64.zip"

INSTALL_DIR = Path(os.environ.get("DISCTRAY_INSTALL_DIR")
                   or Path(os.environ["LOCALAPPDATA"]) / "Programs" / APP)
EXE = INSTALL_DIR / "DiscTray.exe"
UPDATER = INSTALL_DIR / "DiscTrayUpdater.exe"
VERSION_FILE = INSTALL_DIR / "version.txt"
SHORTCUT = Path(os.environ["APPDATA"]) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / f"{APP}.lnk"
SETTINGS_DIR = Path(os.environ["APPDATA"]) / APP
UNINSTALL_KEY = rf"Software\Microsoft\Windows\CurrentVersion\Uninstall\{APP}"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
PIPE = rf"\\.\pipe\{APP}-{os.environ.get('USERNAME', 'user')}"   # DiscTray's single-instance server
TEST_MODE = bool(os.environ.get("DISCTRAY_INSTALL_DIR"))         # tests: files only, no shortcut/registry
CREATE_NO_WINDOW = 0x08000000
DETACHED = 0x00000008 | 0x00000200   # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP


# --------------------------------------------------------------------------- GitHub

def parse_version(text: str) -> tuple[int, ...]:
    parts = []
    for piece in text.strip().lstrip("vV").split("."):
        digits = "".join(ch for ch in piece if ch.isdigit())
        parts.append(int(digits or 0))
    return tuple(parts)


def latest_release() -> tuple[str, str, int]:
    """(version, download url, size in bytes) of the newest release's app zip."""
    req = urllib.request.Request(f"https://api.github.com/repos/{REPO}/releases/latest",
                                 headers={"Accept": "application/vnd.github+json", "User-Agent": f"{APP}-updater"})
    with urllib.request.urlopen(req, timeout=20) as r:
        data = json.load(r)
    for asset in data.get("assets", []):
        if asset["name"].endswith(ASSET_SUFFIX):
            return data["tag_name"].lstrip("vV"), asset["browser_download_url"], int(asset.get("size") or 0)
    raise RuntimeError("The latest release has no Windows build attached.")


def installed_version() -> str | None:
    try:
        return VERSION_FILE.read_text(encoding="utf-8").strip() if EXE.is_file() else None
    except OSError:
        return None


def download(url: str, dest: Path, progress) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": f"{APP}-updater"})
    with urllib.request.urlopen(req, timeout=30) as r, open(dest, "wb") as f:
        total = int(r.headers.get("Content-Length") or 0)
        done = 0
        while True:
            chunk = r.read(256 * 1024)
            if not chunk:
                break
            f.write(chunk)
            done += len(chunk)
            progress(done, total)


# --------------------------------------------------------------------------- running copies

kernel32 = ctypes.windll.kernel32
psapi = ctypes.windll.psapi
kernel32.OpenProcess.restype = wt.HANDLE
kernel32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
kernel32.QueryFullProcessImageNameW.argtypes = [wt.HANDLE, wt.DWORD, wt.LPWSTR, ctypes.POINTER(wt.DWORD)]
kernel32.TerminateProcess.argtypes = [wt.HANDLE, wt.UINT]
kernel32.CloseHandle.argtypes = [wt.HANDLE]
PROCESS_TERMINATE, PROCESS_QUERY_LIMITED_INFORMATION = 0x0001, 0x1000


def _installed_processes():
    """(pid, handle) of processes whose exe lives in the install folder, except us."""
    pids = (wt.DWORD * 4096)()
    needed = wt.DWORD()
    psapi.EnumProcesses(pids, ctypes.sizeof(pids), ctypes.byref(needed))
    me = os.getpid()
    root = str(INSTALL_DIR).lower().rstrip("\\") + "\\"
    found = []
    for pid in pids[: needed.value // ctypes.sizeof(wt.DWORD)]:
        if not pid or pid == me:
            continue
        h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_TERMINATE, False, pid)
        if not h:
            continue
        buf = ctypes.create_unicode_buffer(1024)
        size = wt.DWORD(1024)
        if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)) and buf.value.lower().startswith(root):
            found.append((pid, h))
        else:
            kernel32.CloseHandle(h)
    return found


def close_running(timeout: float = 6.0) -> None:
    """Ask DiscTray to quit (it saves its settings), and only force it if it won't."""
    try:
        with open(PIPE, "wb", buffering=0) as pipe:
            pipe.write(b"quit")
    except OSError:
        pass   # not running (or an old build without the quit command)
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        procs = _installed_processes()
        for _, h in procs:
            kernel32.CloseHandle(h)
        if not procs:
            return
        time.sleep(0.2)
    for _, h in _installed_processes():
        kernel32.TerminateProcess(h, 0)
        kernel32.CloseHandle(h)
    time.sleep(0.5)


# --------------------------------------------------------------------------- install / uninstall

def _remove(path: Path) -> None:
    for attempt in range(25):   # files can stay locked for a moment after a process exits
        try:
            if path.is_dir():
                shutil.rmtree(path)
            elif path.exists():
                path.unlink()
            return
        except OSError:
            time.sleep(0.2)
    raise RuntimeError(f"Couldn't replace {path.name} (is it open somewhere?)")


def install_zip(zip_path: Path, version: str) -> None:
    INSTALL_DIR.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix="disctray-", dir=INSTALL_DIR.parent))
    try:
        with zipfile.ZipFile(zip_path) as z:
            z.extractall(staging)
        running_self = Path(sys.executable).resolve() if getattr(sys, "frozen", False) else None
        for child in INSTALL_DIR.iterdir():
            if running_self is not None and child.resolve() == running_self:
                continue   # can't delete the updater that is running; it's replaced below if needed
            _remove(child)
        for child in staging.iterdir():
            shutil.move(str(child), str(INSTALL_DIR / child.name))
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    if getattr(sys, "frozen", False) and Path(sys.executable).resolve() != UPDATER.resolve():
        shutil.copy2(sys.executable, UPDATER)   # keep a copy for "Check for updates…" and uninstall
    VERSION_FILE.write_text(version, encoding="utf-8")


def _powershell(script: str) -> None:
    subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                   creationflags=CREATE_NO_WINDOW, check=False, timeout=30)


def register(version: str) -> None:
    """Start Menu shortcut, Settings > Apps entry, and an existing autostart pointed at the new exe."""
    if TEST_MODE:
        return
    SHORTCUT.parent.mkdir(parents=True, exist_ok=True)
    _powershell(
        "$s = (New-Object -ComObject WScript.Shell).CreateShortcut('{lnk}');"
        "$s.TargetPath = '{exe}'; $s.WorkingDirectory = '{dir}'; $s.IconLocation = '{exe},0';"
        "$s.Description = 'DiscTray'; $s.Save()".format(lnk=str(SHORTCUT).replace("'", "''"),
                                                        exe=str(EXE).replace("'", "''"),
                                                        dir=str(INSTALL_DIR).replace("'", "''")))
    size_kb = sum(f.stat().st_size for f in INSTALL_DIR.rglob("*") if f.is_file()) // 1024
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY) as k:
        for name, kind, value in (
                ("DisplayName", winreg.REG_SZ, APP),
                ("DisplayVersion", winreg.REG_SZ, version),
                ("Publisher", winreg.REG_SZ, REPO.split("/")[0]),
                ("DisplayIcon", winreg.REG_SZ, str(EXE)),
                ("InstallLocation", winreg.REG_SZ, str(INSTALL_DIR)),
                ("UninstallString", winreg.REG_SZ, f'"{UPDATER}" --uninstall'),
                ("URLInfoAbout", winreg.REG_SZ, f"https://github.com/{REPO}"),
                ("EstimatedSize", winreg.REG_DWORD, int(size_kb)),
                ("NoModify", winreg.REG_DWORD, 1),
                ("NoRepair", winreg.REG_DWORD, 1)):
            winreg.SetValueEx(k, name, 0, kind, value)
    try:   # "Start with Windows" was on (maybe from a source checkout): point it at the installed exe
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_READ | winreg.KEY_SET_VALUE) as k:
            winreg.QueryValueEx(k, APP)
            winreg.SetValueEx(k, APP, 0, winreg.REG_SZ, f'"{EXE}"')
    except OSError:
        pass


def launch() -> None:
    subprocess.Popen([str(EXE)], cwd=str(INSTALL_DIR), creationflags=DETACHED, close_fds=True)


def uninstall(remove_settings: bool) -> None:
    close_running()
    if not TEST_MODE:
        for key, value in ((RUN_KEY, APP),):
            try:
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key, 0, winreg.KEY_SET_VALUE) as k:
                    winreg.DeleteValue(k, value)
            except OSError:
                pass
        try:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY)
        except OSError:
            pass
        SHORTCUT.unlink(missing_ok=True)
        if remove_settings:
            shutil.rmtree(SETTINGS_DIR, ignore_errors=True)
    running_from_install = getattr(sys, "frozen", False) and \
        str(Path(sys.executable).resolve()).lower().startswith(str(INSTALL_DIR.resolve()).lower())
    if running_from_install:
        # this exe is inside the folder: delete the folder once we've exited
        subprocess.Popen(f'cmd /c ping 127.0.0.1 -n 3 > nul & rmdir /s /q "{INSTALL_DIR}"',
                         creationflags=CREATE_NO_WINDOW | DETACHED, close_fds=True)
    else:
        shutil.rmtree(INSTALL_DIR, ignore_errors=True)


# --------------------------------------------------------------------------- window

def run_ui(argv: list[str]) -> int:
    import tkinter as tk
    from tkinter import messagebox, ttk

    root = tk.Tk()
    root.title(f"{APP} Updater")
    root.resizable(False, False)
    root.geometry("400x150")
    icon = Path(getattr(sys, "_MEIPASS", Path(__file__).parent)) / "assets" / "disctray.ico"
    if icon.is_file():
        try:
            root.iconbitmap(str(icon))
        except tk.TclError:
            pass
    style = ttk.Style(root)
    try:
        style.theme_use("vista")
    except tk.TclError:
        pass
    frame = ttk.Frame(root, padding=16)
    frame.pack(fill="both", expand=True)
    title = ttk.Label(frame, text="Checking for updates…", font=("Segoe UI Semibold", 11))
    title.pack(anchor="w")
    detail = ttk.Label(frame, text="", foreground="#555")
    detail.pack(anchor="w", pady=(2, 8))
    bar = ttk.Progressbar(frame, mode="indeterminate", length=368)
    bar.pack(fill="x")
    bar.start(12)
    buttons = ttk.Frame(frame)
    buttons.pack(anchor="e", pady=(12, 0))
    events: queue.Queue = queue.Queue()

    def post(**kw):
        events.put(kw)

    def set_buttons(*specs):
        for w in buttons.winfo_children():
            w.destroy()
        for text, cmd in specs:
            ttk.Button(buttons, text=text, command=cmd).pack(side="left", padx=(6, 0))

    def pump():
        try:
            while True:
                ev = events.get_nowait()
                if "title" in ev:
                    title.config(text=ev["title"])
                if "detail" in ev:
                    detail.config(text=ev["detail"])
                if "progress" in ev:
                    bar.stop()
                    bar.config(mode="determinate", maximum=100, value=ev["progress"])
                if ev.get("busy"):
                    bar.config(mode="indeterminate")
                    bar.start(12)
                if ev.get("done") is not None:
                    bar.stop()
                    bar.config(mode="determinate", maximum=100, value=100 if ev["done"] else 0)
                if "buttons" in ev:
                    set_buttons(*ev["buttons"])
                if ev.get("close_after"):
                    root.after(ev["close_after"], root.destroy)
        except queue.Empty:
            pass
        root.after(50, pump)

    def open_app():
        launch()
        root.destroy()

    def work_update():
        try:
            latest, url, size = latest_release()
            current = installed_version()
            if current and parse_version(current) >= parse_version(latest):
                post(title=f"{APP} {current} is up to date.", detail="You already have the newest version.", done=True,
                     buttons=[("Close", root.destroy)] if "--from-app" in argv
                     else [("Open DiscTray", open_app), ("Close", root.destroy)])
                return
            verb = "Updating" if current else "Installing"
            post(title=f"{verb} {APP} {latest}…", detail="Downloading…", progress=0)
            tmp = Path(tempfile.gettempdir()) / f"DiscTray-{latest}{ASSET_SUFFIX}"

            def progress(done, total):
                total = total or size or 1
                post(progress=done * 100 / total, detail=f"Downloading… {done / 1e6:.1f} of {total / 1e6:.1f} MB")

            download(url, tmp, progress)
            post(detail="Closing DiscTray…", busy=True)
            close_running()
            post(detail="Installing…")
            install_zip(tmp, latest)
            register(latest)
            tmp.unlink(missing_ok=True)
            post(detail="Starting DiscTray…")
            launch()
            post(title=f"{APP} {latest} is installed.", detail=f"Installed to {INSTALL_DIR}", done=True,
                 buttons=[("Close", root.destroy)], close_after=2500)
        except Exception as exc:   # show any failure instead of vanishing
            reason = str(exc) or exc.__class__.__name__
            if isinstance(exc, OSError) and "urlopen" in repr(exc):
                reason = "Couldn't reach GitHub. Check your internet connection."
            post(title="Update failed", detail=reason[:180], done=False, buttons=[("Close", root.destroy)])

    def work_uninstall(remove_settings: bool):
        try:
            uninstall(remove_settings)
            post(title=f"{APP} was removed.", detail="Thanks for listening.", done=True,
                 buttons=[("Close", root.destroy)], close_after=2500)
        except Exception as exc:
            post(title="Uninstall failed", detail=str(exc)[:180], done=False, buttons=[("Close", root.destroy)])

    if "--uninstall" in argv:
        root.withdraw()
        if not messagebox.askyesno(f"Uninstall {APP}", f"Remove {APP} from this PC (for your user)?"):
            root.destroy()
            return 0
        keep = messagebox.askyesno(f"Uninstall {APP}", "Keep your settings (last track, style, volume) "
                                   "in case you install it again?")
        root.deiconify()
        title.config(text=f"Removing {APP}…")
        threading.Thread(target=work_uninstall, args=(not keep,), daemon=True).start()
    else:
        threading.Thread(target=work_update, daemon=True).start()
    root.after(50, pump)
    root.mainloop()
    return 0


def main() -> int:
    argv = sys.argv[1:]
    if "--zip" in argv:   # developer/test path: install a local build instead of downloading
        i = argv.index("--zip")
        zip_path, version = Path(argv[i + 1]), argv[i + 2]
        close_running()
        install_zip(zip_path, version)
        register(version)
        if "--no-launch" not in argv:
            launch()
        return 0
    if "--uninstall-silent" in argv:
        uninstall(remove_settings=False)
        return 0
    return run_ui(argv)


if __name__ == "__main__":
    sys.exit(main())
