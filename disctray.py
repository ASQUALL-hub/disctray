"""DiscTray: a tray-only audio player with a spinning CD.

Drag an audio file onto the tray icon (or onto the panel / pop-out) and the
track swaps with a short fade. Playback pauses while another program is truly
fullscreen and resumes when it leaves fullscreen.
"""
from __future__ import annotations

import base64
import ctypes
import ctypes.wintypes as wt
import json
import math
import os
import random
import sys
import threading
import time
import traceback
import winreg
import zlib
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import (QCoreApplication, QEasingCurve, QEvent, QObject, QPoint, QPointF, QRect, QRectF, Qt, QThread,
                            QTimer, QUrl, QVariantAnimation, Signal)
from PySide6.QtGui import (QAction, QActionGroup, QColor, QConicalGradient, QCursor, QFont, QFontMetrics, QFontMetricsF, QTransform,
                           QGuiApplication, QIcon, QImage, QLinearGradient, QPainter,
                           QPainterPath, QPen, QPixmap, QRadialGradient)
from PySide6.QtMultimedia import QAudio, QAudioDecoder, QAudioFormat, QAudioSink, QMediaDevices
from PySide6.QtNetwork import QLocalServer, QLocalSocket
import numpy as np
from PySide6.QtWidgets import QApplication, QFileDialog, QMenu, QSystemTrayIcon, QWidget

APP_NAME = "DiscTray"
__version__ = "1.6.1"
DATA_DIR = Path(os.environ.get("APPDATA", str(Path.home()))) / APP_NAME
CONFIG_PATH = DATA_DIR / "config.json"
SERVER_NAME = f"{APP_NAME}-{os.environ.get('USERNAME', 'user')}"
AUDIO_EXTS = {".mp3", ".flac", ".wav", ".ogg", ".oga", ".opus", ".m4a", ".aac", ".wma",
              ".aif", ".aiff", ".alac", ".mka", ".webm", ".mp4", ".mkv", ".mov"}

SPIN_DPS = 150.0          # degrees per second at full speed (25 rpm)
FADE_OUT_MS = 380         # old track eases out
FADE_IN_MS = 520          # new track eases in


# --------------------------------------------------------------------------- win32

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32
dwmapi = ctypes.windll.dwmapi
shell32 = ctypes.windll.shell32


class RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wt.DWORD), ("biWidth", ctypes.c_long), ("biHeight", ctypes.c_long),
                ("biPlanes", wt.WORD), ("biBitCount", wt.WORD), ("biCompression", wt.DWORD),
                ("biSizeImage", wt.DWORD), ("biXPelsPerMeter", ctypes.c_long), ("biYPelsPerMeter", ctypes.c_long),
                ("biClrUsed", wt.DWORD), ("biClrImportant", wt.DWORD)]


class MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD), ("rcMonitor", RECT), ("rcWork", RECT), ("dwFlags", wt.DWORD)]


user32.GetForegroundWindow.restype = wt.HWND
user32.GetDC.argtypes = [wt.HWND]
user32.GetDC.restype = wt.HDC
user32.ReleaseDC.argtypes = [wt.HWND, wt.HDC]
gdi32.CreateDIBSection.argtypes = [wt.HDC, ctypes.c_void_p, wt.UINT, ctypes.POINTER(ctypes.c_void_p), wt.HANDLE, wt.DWORD]
gdi32.CreateDIBSection.restype = wt.HBITMAP
gdi32.CreateCompatibleDC.argtypes = [wt.HDC]
gdi32.CreateCompatibleDC.restype = wt.HDC
gdi32.SelectObject.argtypes = [wt.HDC, wt.HGDIOBJ]
gdi32.SelectObject.restype = wt.HGDIOBJ
gdi32.BitBlt.argtypes = [wt.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                         wt.HDC, ctypes.c_int, ctypes.c_int, wt.DWORD]
gdi32.DeleteDC.argtypes = [wt.HDC]
gdi32.DeleteObject.argtypes = [wt.HGDIOBJ]
user32.GetClassNameW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
user32.GetWindowRect.argtypes = [wt.HWND, ctypes.POINTER(RECT)]
user32.MonitorFromWindow.argtypes = [wt.HWND, wt.DWORD]
user32.MonitorFromWindow.restype = wt.HMONITOR
user32.GetMonitorInfoW.argtypes = [wt.HMONITOR, ctypes.POINTER(MONITORINFO)]
user32.GetWindowLongW.argtypes = [wt.HWND, ctypes.c_int]
user32.GetWindowLongW.restype = ctypes.c_long
user32.IsZoomed.argtypes = [wt.HWND]
user32.IsIconic.argtypes = [wt.HWND]
user32.IsWindowVisible.argtypes = [wt.HWND]
user32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
user32.GetAsyncKeyState.restype = ctypes.c_short
user32.GetKeyState.argtypes = [ctypes.c_int]
user32.GetKeyState.restype = ctypes.c_short
user32.SetWindowLongW.argtypes = [wt.HWND, ctypes.c_int, ctypes.c_long]
user32.SetWindowLongW.restype = ctypes.c_long
user32.SetWindowDisplayAffinity.argtypes = [wt.HWND, wt.DWORD]
dwmapi.DwmSetWindowAttribute.argtypes = [wt.HWND, wt.DWORD, ctypes.c_void_p, wt.DWORD]
dwmapi.DwmGetWindowAttribute.argtypes = [wt.HWND, wt.DWORD, ctypes.c_void_p, wt.DWORD]

GWL_STYLE = -16
SRCCOPY = 0x00CC0020
GWL_EXSTYLE = -20
WS_EX_TRANSPARENT = 0x20
WS_EX_LAYERED = 0x80000
VK_CAPITAL = 0x14
WS_CAPTION = 0x00C00000
MONITOR_DEFAULTTONEAREST = 2
DWMWA_CLOAKED = 14
DWMWA_WINDOW_CORNER_PREFERENCE = 33
DWMWCP_DONOTROUND = 1
DWMWA_BORDER_COLOR = 34
DWMWA_COLOR_NONE = 0xFFFFFFFE
WDA_EXCLUDEFROMCAPTURE = 0x11
WDA_NONE = 0x0
QUNS_RUNNING_D3D_FULL_SCREEN = 3

# Shell surfaces that can cover the screen without being "a program in fullscreen".
SHELL_CLASSES = {"Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd",
                 "XamlExplorerHostIslandWindow", "Windows.UI.Core.CoreWindow",
                 "MultitaskingViewFrame", "ForegroundStaging", "NotifyIconOverflowWindow",
                 "TopLevelWindowForOverflowXamlIsland", "LockScreenControllerProxyWindow"}


def prepare_glass_window(hwnd: int) -> bool:
    """Square window corners (the card draws its own shape), no Windows 11 border."""
    try:
        pref = ctypes.c_int(DWMWCP_DONOTROUND)
        dwmapi.DwmSetWindowAttribute(hwnd, DWMWA_WINDOW_CORNER_PREFERENCE, ctypes.byref(pref), 4)
        no_border = wt.DWORD(DWMWA_COLOR_NONE)
        dwmapi.DwmSetWindowAttribute(hwnd, DWMWA_BORDER_COLOR, ctypes.byref(no_border), 4)
        return True
    except Exception:
        return False


def set_capturable(hwnd: int, on: bool) -> bool:
    """Visible to screenshots and recordings, or hidden from every capture (then the
    glass can grab its own rectangle and see exactly what is behind it)."""
    return bool(user32.SetWindowDisplayAffinity(hwnd, WDA_NONE if on else WDA_EXCLUDEFROMCAPTURE))


def foreground_is_fullscreen() -> bool:
    """True when another program's window covers its whole monitor (not merely maximized)."""
    state = ctypes.c_int(0)
    if shell32.SHQueryUserNotificationState(ctypes.byref(state)) == 0 \
            and state.value == QUNS_RUNNING_D3D_FULL_SCREEN:
        return True
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return False
    pid = wt.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    if pid.value == os.getpid():
        return False
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buf, 256)
    if buf.value in SHELL_CLASSES:
        return False
    if not user32.IsWindowVisible(hwnd) or user32.IsIconic(hwnd):
        return False
    cloaked = wt.DWORD(0)
    dwmapi.DwmGetWindowAttribute(hwnd, DWMWA_CLOAKED, ctypes.byref(cloaked), 4)
    if cloaked.value:
        return False
    r = RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(r)):
        return False
    mi = MONITORINFO()
    mi.cbSize = ctypes.sizeof(MONITORINFO)
    if not user32.GetMonitorInfoW(user32.MonitorFromWindow(hwnd, MONITOR_DEFAULTTONEAREST), ctypes.byref(mi)):
        return False
    m = mi.rcMonitor
    if not (r.left <= m.left and r.top <= m.top and r.right >= m.right and r.bottom >= m.bottom):
        return False
    style = user32.GetWindowLongW(hwnd, GWL_STYLE)
    if (style & WS_CAPTION) == WS_CAPTION and user32.IsZoomed(hwnd):
        return False  # a normal maximized window (e.g. with an auto-hide taskbar)
    return True


def left_button_down() -> bool:
    return bool(user32.GetAsyncKeyState(0x01) & 0x8000)


def key_down(vk: int) -> bool:
    return bool(user32.GetAsyncKeyState(vk) & 0x8000)


def any_mouse_button_down() -> bool:
    return any(key_down(vk) for vk in (0x01, 0x02, 0x04, 0x05, 0x06))   # left, right, middle, X1, X2


def caps_lock_on() -> bool:
    return bool(user32.GetKeyState(VK_CAPITAL) & 1)


def tap_caps_lock() -> None:
    user32.keybd_event(VK_CAPITAL, 0x3A, 0, 0)
    user32.keybd_event(VK_CAPITAL, 0x3A, 2, 0)   # KEYEVENTF_KEYUP


def set_click_through(hwnd: int, on: bool) -> None:
    """WS_EX_TRANSPARENT lets clicks fall through to whatever is underneath."""
    ex = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
    ex = (ex | WS_EX_TRANSPARENT | WS_EX_LAYERED) if on else (ex & ~WS_EX_TRANSPARENT)
    user32.SetWindowLongW(hwnd, GWL_EXSTYLE, ex)


# --------------------------------------------------------------------------- config / startup

def load_config() -> dict:
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_config(cfg: dict) -> None:
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        CONFIG_PATH.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    except Exception:
        pass


RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def launch_command() -> str:
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}"'
    exe = Path(sys.executable)
    pyw = exe.with_name("pythonw.exe")
    script = Path(__file__).resolve().with_name("DiscTray.pyw")
    return f'"{pyw if pyw.exists() else exe}" "{script}"'


def updater_path() -> Path:
    """The installer/updater sits next to DiscTray.exe in an installed build."""
    return Path(sys.executable).with_name("DiscTrayUpdater.exe")


def startup_enabled() -> bool:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            winreg.QueryValueEx(k, APP_NAME)
        return True
    except OSError:
        return False


def set_startup(on: bool) -> None:
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        if on:
            winreg.SetValueEx(k, APP_NAME, 0, winreg.REG_SZ, launch_command())
        else:
            try:
                winreg.DeleteValue(k, APP_NAME)
            except FileNotFoundError:
                pass


# --------------------------------------------------------------------------- track metadata

LOSSLESS_EXTS = {".flac", ".wav", ".aif", ".aiff", ".alac", ".wv", ".ape", ".dsf", ".dff"}


@dataclass
class TrackInfo:
    title: str
    length_ms: int
    art: QImage | None
    hires: bool = False    # lossless (FLAC, WAV, AIFF, ALAC...)


def _first_text(v) -> str | None:
    if isinstance(v, (list, tuple)):
        v = v[0] if v else None
    if v is None:
        return None
    if hasattr(v, "text"):
        return str(v.text[0]) if v.text else None
    return str(v)


def _tag_title(tags) -> str | None:
    for key in ("TIT2", "\xa9nam", "title", "Title"):
        try:
            if key in tags:
                s = _first_text(tags[key])
                if s and s.strip():
                    return s.strip()
        except Exception:
            continue
    return None


def _tag_art(f, tags) -> bytes | None:
    try:
        if hasattr(tags, "getall"):  # ID3 (mp3, wav, aiff)
            pics = tags.getall("APIC")
            if pics:
                return max(pics, key=lambda p: p.type == 3).data
        if "covr" in tags:  # MP4
            return bytes(tags["covr"][0])
        if getattr(f, "pictures", None):  # FLAC
            return f.pictures[0].data
        if "metadata_block_picture" in tags:  # Ogg Vorbis / Opus
            from mutagen.flac import Picture
            return Picture(base64.b64decode(tags["metadata_block_picture"][0])).data
    except Exception:
        pass
    return None


def read_track(path: str) -> TrackInfo:
    title, length, art_bytes = Path(path).stem, 0, None
    hires = Path(path).suffix.lower() in LOSSLESS_EXTS
    try:
        import mutagen
        f = mutagen.File(path)
        if f is not None:
            length = int((getattr(f.info, "length", 0) or 0) * 1000)
            if str(getattr(f.info, "codec", "")).lower() == "alac":   # Apple Lossless inside .m4a
                hires = True
            if f.tags is not None:
                title = _tag_title(f.tags) or title
                art_bytes = _tag_art(f, f.tags)
    except Exception:
        pass
    art = None
    if art_bytes:
        img = QImage.fromData(art_bytes)
        if not img.isNull():
            art = img
    return TrackInfo(title, length, art, hires)


def accent_from(art: QImage | None, seed: int) -> QColor:
    if art is None:
        return QColor.fromHsv(seed % 360, 140, 235)
    c = art.scaled(1, 1, Qt.IgnoreAspectRatio, Qt.SmoothTransformation).pixelColor(0, 0)
    h, s, v, _ = c.getHsv()
    if h < 0 or s < 30:
        return QColor(200, 205, 220)
    return QColor.fromHsv(h, max(s, 120), max(v, 200))


def fmt_time(ms: int) -> str:
    s = max(0, int(ms) // 1000)
    h, m, sec = s // 3600, (s % 3600) // 60, s % 60
    return f"{h}:{m:02}:{sec:02}" if h else f"{m}:{sec:02}"


def is_audio(p: Path) -> bool:
    return p.suffix.lower() in AUDIO_EXTS and p.is_file()


def audio_paths_from_mime(md) -> list[str]:
    """Dropped audio files in drop order; a dropped folder adds its audio files."""
    out: list[str] = []
    if md is None or not md.hasUrls():
        return out
    for url in md.urls():
        if not url.isLocalFile():
            continue
        p = Path(url.toLocalFile())
        if p.is_dir():
            out += [str(f) for f in sorted(p.iterdir(), key=lambda f: f.name.lower()) if is_audio(f)]
        elif is_audio(p):
            out.append(str(p))
    return out


# --------------------------------------------------------------------------- disc art

LABEL_R = 0.36   # cover-art label, as a fraction of the record's radius
LENS_R = 0.17    # glassy center ring that bends the art under it
HOLE_R = 0.045


def render_label(art: QImage | None, d: int, seed: int, empty: bool) -> QImage:
    """The cover art cut into a circle (or a gradient when the file has no art)."""
    img = QImage(d, d, QImage.Format_ARGB32)
    img.fill(Qt.transparent)
    p = QPainter(img)
    p.setRenderHints(QPainter.Antialiasing | QPainter.SmoothPixmapTransform)
    clip = QPainterPath()
    clip.addEllipse(QRectF(0, 0, d, d))
    p.setClipPath(clip)
    if art is not None:
        scaled = art.scaled(d, d, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
        p.drawImage(QPointF((d - scaled.width()) / 2, (d - scaled.height()) / 2), scaled)
    else:
        c = QPointF(d / 2, d / 2)
        hue = 225 if empty else seed % 360
        sat = 30 if empty else 150
        g = QConicalGradient(c, 90)
        g.setColorAt(0.00, QColor.fromHsv(hue, sat, 140))
        g.setColorAt(0.33, QColor.fromHsv((hue + 35) % 360, sat, 200))
        g.setColorAt(0.66, QColor.fromHsv((hue + 70) % 360, sat, 130))
        g.setColorAt(1.00, QColor.fromHsv(hue, sat, 140))
        p.fillRect(img.rect(), g)
    p.end()
    return img


def render_lens(label: QImage, lens_r: float) -> QImage:
    """The art seen through a thick glass ring: magnified toward the middle,
    twisted a little, with the colour channels split like cheap glass."""
    d = label.width()
    c = d / 2
    out = QImage(d, d, QImage.Format_ARGB32)
    out.fill(Qt.transparent)
    lo, hi = max(0, int(c - lens_r) - 1), min(d, int(c + lens_r) + 2)

    def sample(t, ang, k):
        sr = lens_r * (t ** k)
        x = min(d - 1, max(0, int(c + sr * math.cos(ang))))
        y = min(d - 1, max(0, int(c + sr * math.sin(ang))))
        return label.pixel(x, y)

    for y in range(lo, hi):
        for x in range(lo, hi):
            dx, dy = x + 0.5 - c, y + 0.5 - c
            rr = math.hypot(dx, dy)
            if rr > lens_r:
                continue
            t = rr / lens_r
            ang = math.atan2(dy, dx) + 0.9 * (1 - t) ** 2
            r = (sample(t, ang + 0.05, 1.55) >> 16) & 0xFF
            g = (sample(t, ang, 1.75) >> 8) & 0xFF
            b = sample(t, ang - 0.05, 1.95) & 0xFF
            out.setPixel(x, y, 0xFF000000 | (r << 16) | (g << 8) | b)
    return out


def render_disc(art: QImage | None, px: int, seed: int, empty: bool = False) -> QPixmap:
    """The rotating part: a black vinyl record with the cover as its label and a
    glass lens ring around the spindle hole."""
    img = QImage(px, px, QImage.Format_ARGB32_Premultiplied)
    img.fill(Qt.transparent)
    p = QPainter(img)
    p.setRenderHints(QPainter.Antialiasing | QPainter.SmoothPixmapTransform)
    r = px / 2
    c = QPointF(r, r)

    # vinyl and its grooves
    p.setPen(Qt.NoPen)
    p.setBrush(QColor(14, 14, 17))
    p.drawEllipse(c, r, r)
    p.setBrush(Qt.NoBrush)
    hair = max(0.6, px / 640)
    rad, i = r * (LABEL_R + 0.05), 0
    while rad < r * 0.955:
        p.setPen(QPen(QColor(255, 255, 255, 13 if i % 2 else 5), hair))
        p.drawEllipse(c, rad, rad)
        rad += max(1.4, px / 170)
        i += 1
    for f in (0.55, 0.69, 0.83):  # the smooth gaps between songs
        p.setPen(QPen(QColor(0, 0, 0, 200), max(1.2, px / 210)))
        p.drawEllipse(c, r * f, r * f)
    p.setPen(QPen(QColor(255, 255, 255, 45), max(1.0, px / 260)))
    p.drawEllipse(c, r - px / 260, r - px / 260)

    # label + lens
    lr = r * LABEL_R
    ld = max(8, round(2 * lr))
    label = render_label(art, ld, seed, empty)
    origin = QPointF(c.x() - ld / 2, c.y() - ld / 2)
    p.drawImage(origin, label)
    lens_r = ld / 2 * (LENS_R / LABEL_R)
    p.drawImage(origin, render_lens(label, lens_r))

    p.setBrush(Qt.NoBrush)
    p.setPen(QPen(QColor(0, 0, 0, 150), max(1.0, px / 220)))
    p.drawEllipse(c, lr, lr)
    edge = QRadialGradient(c, lens_r)
    edge.setColorAt(0.70, QColor(0, 0, 0, 0))
    edge.setColorAt(0.94, QColor(0, 0, 0, 70))
    edge.setColorAt(1.00, QColor(0, 0, 0, 120))
    p.setPen(Qt.NoPen)
    p.setBrush(edge)
    p.drawEllipse(c, lens_r, lens_r)
    p.setBrush(Qt.NoBrush)
    p.setPen(QPen(QColor(255, 255, 255, 170), max(1.0, px / 240)))
    p.drawEllipse(c, lens_r, lens_r)

    # spindle hole
    hr = r * HOLE_R
    p.setCompositionMode(QPainter.CompositionMode_Clear)
    p.setPen(Qt.NoPen)
    p.setBrush(Qt.black)
    p.drawEllipse(c, hr, hr)
    p.setCompositionMode(QPainter.CompositionMode_SourceOver)
    p.setBrush(Qt.NoBrush)
    p.setPen(QPen(QColor(255, 255, 255, 120), max(1.0, px / 300)))
    p.drawEllipse(c, hr, hr)
    p.end()
    return QPixmap.fromImage(img)


def render_sheen(px: int) -> QPixmap:
    """Light on the record: drawn on top but not rotated, so the grooves and art turn under it."""
    img = QImage(px, px, QImage.Format_ARGB32_Premultiplied)
    img.fill(Qt.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.Antialiasing)
    r = px / 2
    c = QPointF(r, r)

    # two opposite wedges of reflected light across the grooves
    vinyl = QPainterPath()
    vinyl.addEllipse(c, r, r)
    label = QPainterPath()
    label.addEllipse(c, r * LABEL_R, r * LABEL_R)
    p.save()
    p.setClipPath(vinyl.subtracted(label))
    g = QConicalGradient(c, 130)
    for pos, a in ((0.00, 0), (0.05, 34), (0.08, 70), (0.11, 34), (0.17, 0), (0.50, 0),
                   (0.55, 26), (0.58, 55), (0.61, 26), (0.67, 0), (1.0, 0)):
        g.setColorAt(pos, QColor(255, 255, 255, a))
    p.fillRect(img.rect(), g)
    p.restore()

    # soft gloss on the label
    hi = QRadialGradient(QPointF(r * 0.85, r * 0.8), r * LABEL_R * 1.4)
    hi.setColorAt(0.0, QColor(255, 255, 255, 38))
    hi.setColorAt(1.0, QColor(255, 255, 255, 0))
    p.setPen(Qt.NoPen)
    p.setBrush(hi)
    p.drawEllipse(c, r * LABEL_R, r * LABEL_R)

    # specular crescent on the glass lens
    lens_r = r * LENS_R
    outer = QPainterPath()
    outer.addEllipse(c, lens_r * 0.9, lens_r * 0.9)
    bite = QPainterPath()
    bite.addEllipse(c + QPointF(lens_r * 0.22, lens_r * 0.26), lens_r * 0.88, lens_r * 0.88)
    p.setBrush(QColor(255, 255, 255, 120))
    p.drawPath(outer.subtracted(bite))
    p.end()
    return QPixmap.fromImage(img)


def render_glass_disc(art: QImage | None, px: int, seed: int, empty: bool = False) -> QPixmap:
    """Glossy Glass style, rotating part: a clear glass disc (the card refracts the
    backdrop under it), bright grooves, a few printed arcs so the spin shows, and
    the cover as a slightly see-through label with the glass lens in the middle."""
    img = QImage(px, px, QImage.Format_ARGB32_Premultiplied)
    img.fill(Qt.transparent)
    p = QPainter(img)
    p.setRenderHints(QPainter.Antialiasing | QPainter.SmoothPixmapTransform)
    r = px / 2
    c = QPointF(r, r)

    body = QRadialGradient(c, r)   # thicker-looking toward the rim
    body.setColorAt(0.00, QColor(255, 255, 255, 12))
    body.setColorAt(0.70, QColor(255, 255, 255, 22))
    body.setColorAt(0.93, QColor(255, 255, 255, 46))
    body.setColorAt(1.00, QColor(255, 255, 255, 80))
    p.setPen(Qt.NoPen)
    p.setBrush(body)
    p.drawEllipse(c, r, r)

    p.setBrush(Qt.NoBrush)
    hair = max(0.6, px / 640)
    rad, i = r * (LABEL_R + 0.05), 0
    while rad < r * 0.955:
        p.setPen(QPen(QColor(255, 255, 255, 16 if i % 2 else 6), hair))
        p.drawEllipse(c, rad, rad)
        rad += max(1.4, px / 170)
        i += 1
    rng = random.Random(seed)
    for _ in range(6):
        ar = r * rng.uniform(0.45, 0.92)
        pen = QPen(QColor(255, 255, 255, rng.randint(40, 80)), max(1.0, px / rng.choice((180, 240, 320))))
        pen.setCapStyle(Qt.RoundCap)
        p.setPen(pen)
        p.drawArc(QRectF(c.x() - ar, c.y() - ar, 2 * ar, 2 * ar),
                  int(rng.uniform(0, 360) * 16), int(rng.uniform(25, 85) * 16))
    w = max(1.0, px / 150)
    p.setPen(QPen(QColor(255, 255, 255, 170), w))
    p.drawEllipse(c, r - w / 2, r - w / 2)

    lr = r * LABEL_R
    ld = max(8, round(2 * lr))
    label = render_label(art, ld, seed, empty)
    origin = QPointF(c.x() - ld / 2, c.y() - ld / 2)
    p.setOpacity(0.86)
    p.drawImage(origin, label)
    lens_r = ld / 2 * (LENS_R / LABEL_R)
    p.drawImage(origin, render_lens(label, lens_r))
    p.setOpacity(1.0)
    p.setPen(QPen(QColor(255, 255, 255, 140), max(1.0, px / 260)))
    p.drawEllipse(c, lr, lr)
    p.setPen(QPen(QColor(255, 255, 255, 190), max(1.0, px / 240)))
    p.drawEllipse(c, lens_r, lens_r)

    hr = r * HOLE_R
    p.setCompositionMode(QPainter.CompositionMode_Clear)
    p.setPen(Qt.NoPen)
    p.setBrush(Qt.black)
    p.drawEllipse(c, hr, hr)
    p.setCompositionMode(QPainter.CompositionMode_SourceOver)
    p.setBrush(Qt.NoBrush)
    p.setPen(QPen(QColor(255, 255, 255, 150), max(1.0, px / 300)))
    p.drawEllipse(c, hr, hr)
    p.end()
    return QPixmap.fromImage(img)


def render_glossy_sheen(px: int) -> QPixmap:
    """Glossy Glass style, static light on the disc: a wet dome highlight, a sharp
    specular streak, light bouncing up from below and a sparkle."""
    img = QImage(px, px, QImage.Format_ARGB32_Premultiplied)
    img.fill(Qt.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.Antialiasing)
    r = px / 2
    c = QPointF(r, r)
    disc = QPainterPath()
    disc.addEllipse(c, r, r)
    dome = QPainterPath()
    dome.addEllipse(QPointF(r, r * 0.52), r * 0.84, r * 0.56)
    g = QLinearGradient(0, 0, 0, r * 1.05)
    g.setColorAt(0.0, QColor(255, 255, 255, 120))
    g.setColorAt(0.55, QColor(255, 255, 255, 34))
    g.setColorAt(1.0, QColor(255, 255, 255, 0))
    p.fillPath(dome.intersected(disc), g)

    streak = QConicalGradient(c, 128)
    for pos, a in ((0.0, 0), (0.025, 95), (0.05, 0), (0.5, 0), (0.52, 50), (0.54, 0), (1.0, 0)):
        streak.setColorAt(pos, QColor(255, 255, 255, a))
    p.save()
    p.setClipPath(disc)
    p.fillRect(img.rect(), streak)
    p.restore()

    bounce = QRadialGradient(QPointF(r, r * 1.95), r * 0.75)
    bounce.setColorAt(0.0, QColor(255, 255, 255, 60))
    bounce.setColorAt(1.0, QColor(255, 255, 255, 0))
    p.fillPath(disc, bounce)

    spark = QRadialGradient(QPointF(r * 0.42, r * 0.34), r * 0.09)
    spark.setColorAt(0.0, QColor(255, 255, 255, 230))
    spark.setColorAt(0.35, QColor(255, 255, 255, 90))
    spark.setColorAt(1.0, QColor(255, 255, 255, 0))
    p.setPen(Qt.NoPen)
    p.setBrush(spark)
    p.drawEllipse(QPointF(r * 0.42, r * 0.34), r * 0.09, r * 0.09)
    p.end()
    return QPixmap.fromImage(img)


# --------------------------------------------------------------------------- player

def _run(anim: QVariantAnimation, start: float, end: float, ms: int, curve) -> None:
    # A stopped animation re-emits its old end value when reconfigured; keep that
    # from snapping the value before the new run starts.
    anim.blockSignals(True)
    anim.setStartValue(float(start))
    anim.setEndValue(float(end))
    anim.setDuration(ms)
    anim.setEasingCurve(curve)
    anim.blockSignals(False)
    anim.start()


ENGINE_RATE = 48000   # PCM is decoded once at this rate; the sink plays it at the same rate


def engine_format(rate: int = ENGINE_RATE) -> QAudioFormat:
    fmt = QAudioFormat()
    fmt.setChannelCount(2)
    fmt.setSampleFormat(QAudioFormat.Int16)
    fmt.setSampleRate(rate)
    return fmt


NORM_TARGET_LUFS = -14.0   # what every track is brought to (same as Spotify's default)
NORM_MAX_BOOST_DB = 10.0
NORM_MAX_CUT_DB = 20.0
# BS.1770 K-weighting: a high-shelf (how loud the ear finds the upper range) and a
# high-pass (bass counts less). Coefficients are the standard 48 kHz design.
_K_SHELF = ((1.53512485958697, -2.69169618940638, 1.19839281085285), (1.0, -1.69065929318241, 0.73248077421585))
_K_HPF = ((1.0, -2.0, 1.0), (1.0, -1.99004745483398, 0.99007225036621))


def _k_weight_power(freqs: np.ndarray) -> np.ndarray:
    """|H(f)|^2 of the K-weighting filter, evaluated at physical frequencies (Hz)."""
    z = np.exp(-2j * np.pi * np.minimum(freqs, 23999.0) / 48000.0)

    def biquad(b, a):
        return (b[0] + b[1] * z + b[2] * z * z) / (a[0] + a[1] * z + a[2] * z * z)

    return np.abs(biquad(*_K_SHELF) * biquad(*_K_HPF)) ** 2


def normalization_gain(pcm: np.ndarray, sr: int) -> float | None:
    """Linear gain that brings 16-bit stereo `pcm` to NORM_TARGET_LUFS.

    Integrated loudness per ITU-R BS.1770: K-weighted energy in 400 ms blocks
    (computed in the frequency domain, which by Parseval equals filtering), an
    absolute gate at -70 LUFS and a relative gate 10 LU under the result. The
    gain is capped so the loudest sample can't clip."""
    block = int(0.4 * sr)
    nb = len(pcm) // block
    if nb < 2:
        return None
    w = _k_weight_power(np.fft.rfftfreq(block, 1.0 / sr))
    w[1:-1] *= 2.0                         # one-sided spectrum: interior bins count twice
    w /= float(block) * block
    energy = np.zeros(nb)
    for start in range(0, nb, 48):          # chunks keep memory small
        stop = min(nb, start + 48)
        seg = pcm[start * block: stop * block].astype(np.float32) * (1.0 / 32768.0)
        for ch in range(2):
            spec = np.fft.rfft(seg[:, ch].reshape(stop - start, block), axis=1)
            energy[start:stop] += (spec.real ** 2 + spec.imag ** 2) @ w
    with np.errstate(divide="ignore"):
        lufs = -0.691 + 10 * np.log10(energy)
    gated = energy[lufs > -70]
    if not len(gated):
        return None
    rel = -0.691 + 10 * np.log10(gated.mean()) - 10
    kept = energy[lufs > max(-70.0, rel)]
    loudness = -0.691 + 10 * np.log10(kept.mean())
    gain_db = max(-NORM_MAX_CUT_DB, min(NORM_MAX_BOOST_DB, NORM_TARGET_LUFS - loudness))
    peak = int(np.abs(pcm).max()) / 32768.0
    if peak > 0:
        gain_db = min(gain_db, 20 * math.log10(0.98 / peak))
    return 10 ** (gain_db / 20)


SF_EXTS = {".flac", ".wav", ".aif", ".aiff", ".ogg", ".oga", ".opus", ".mp3"}   # decoded by libsndfile
SINC_HALF = 8   # resampler taps on each side of the read point (more when downsampling)


class _Decimator:
    """Streams high-rate audio (88.2 to 384 kHz) down by an integer factor while it
    decodes: a long windowed-sinc low-pass, evaluated only at the kept samples, so
    ultrasonic content is removed instead of folding into the audible range."""

    def __init__(self, factor: int):
        self.m = factor
        n = 32 * factor + 1
        fc = 0.5 / factor * 0.9                    # just under the new Nyquist
        x = np.arange(n) - (n - 1) / 2
        h = 2 * fc * np.sinc(2 * fc * x) * np.blackman(n)
        self.h = (h / h.sum()).astype(np.float32)
        self.state = np.zeros((n - 1, 2), np.float32)
        self.t = 0                                 # input samples consumed so far

    def __call__(self, block: np.ndarray) -> np.ndarray:
        ext = np.concatenate([self.state, block.astype(np.float32)])
        first = (-self.t) % self.m                 # keep every m-th input sample, in phase across blocks
        out = np.empty(((len(block) - first + self.m - 1) // self.m, 2), np.float32)
        for c in range(2):
            win = np.lib.stride_tricks.sliding_window_view(ext[:, c], len(self.h))[first::self.m]
            out[:, c] = win @ self.h[::-1]
        self.state = ext[-(len(self.h) - 1):]
        self.t += len(block)
        return np.clip(np.rint(out), -32768, 32767).astype(np.int16)


def decimation_factor(rate: int) -> int:
    """How far to bring a hi-res rate down while staying at 44.1 kHz or more."""
    m = 1
    while rate % (m * 2) == 0 and rate // (m * 2) >= 44100:
        m *= 2
    return m


class DecodeJob(QObject):
    """Decodes one file to 16-bit stereo PCM on a worker thread, at the file's own
    sample rate (`rate`); the engine resamples on output. libsndfile reads the
    common formats in large blocks (Qt's decoder delivers FLAC in ~70-sample
    pieces, too slow for hi-res files); Qt's decoder covers the rest (M4A, WMA,
    video), converted to `fallback_fmt`. The engine reads `n` and then `buf`:
    `buf` is replaced before `n` grows, so a reader always sees `n` valid frames."""

    done = Signal()
    failed = Signal(str)

    def __init__(self, path: str, fallback_fmt: QAudioFormat, expect_ms: int):
        super().__init__()
        self.path, self.fallback_fmt, self.expect_ms = path, fallback_fmt, expect_ms
        self.rate = fallback_fmt.sampleRate()
        self.buf = np.zeros((0, 2), np.int16)
        self.n = 0
        self.complete = False
        self.cancelled = False
        self.gain: float | None = None   # loudness normalization; first estimate, then exact
        self._decoder = None

    def _reserve(self, frames: int):
        self.buf = np.zeros((max(int(frames), self.rate), 2), np.int16)

    def _append(self, a: np.ndarray):
        need = self.n + len(a)
        buf = self.buf
        if need > len(buf):
            grown = np.zeros((max(need, len(buf) * 2), 2), np.int16)
            grown[: self.n] = buf[: self.n]
            buf = grown
        buf[self.n: need] = a
        self.buf = buf
        self.n = need
        if self.gain is None and need >= self.rate * 15:
            self.gain = normalization_gain(buf[:need], self.rate)   # early estimate

    def run(self):
        if Path(self.path).suffix.lower() in SF_EXTS:
            try:
                self._run_soundfile()
                return
            except Exception:
                if self.cancelled:
                    self.thread().quit()
                    return
                self.n = 0   # libsndfile couldn't read it: let Qt try
        self._run_qt()

    def _run_soundfile(self):
        import soundfile as sf
        with sf.SoundFile(self.path) as f:
            factor = decimation_factor(f.samplerate)
            decimate = _Decimator(factor) if factor > 1 else None
            self.rate = f.samplerate // factor
            self._reserve((f.frames + 16) // factor if f.frames > 0 else self.rate * 240)
            for block in f.blocks(blocksize=1 << 16, dtype="int16", always_2d=True):
                if self.cancelled:
                    self.thread().quit()
                    return
                if block.shape[1] == 1:
                    block = np.repeat(block, 2, axis=1)
                elif block.shape[1] > 2:
                    block = block[:, :2]
                self._append(decimate(block) if decimate else block)
        self._finish()

    def _run_qt(self):
        self.rate = self.fallback_fmt.sampleRate()
        self._reserve(self.rate * (self.expect_ms / 1000 + 2) if self.expect_ms else self.rate * 240)
        d = QAudioDecoder(self)
        d.setAudioFormat(self.fallback_fmt)
        d.setSource(QUrl.fromLocalFile(self.path))
        d.bufferReady.connect(self._on_buffer)
        d.finished.connect(self._finish)
        d.error.connect(lambda _e: self._fail(d.errorString()))
        self._decoder = d
        d.start()

    def _on_buffer(self):
        if self.cancelled:
            self._decoder.stop()
            self.thread().quit()
            return
        b = self._decoder.read()
        if not b.isValid():
            return
        a = np.frombuffer(bytes(b.constData()), np.int16)
        self._append(a[: len(a) - len(a) % 2].reshape(-1, 2))

    def _finish(self):
        try:
            final = normalization_gain(self.buf[: self.n], self.rate)
            if final is not None or self.gain is None:
                self.gain = final if final is not None else 1.0
        except Exception:
            self.gain = self.gain or 1.0
        self.complete = True
        self.thread().quit()
        if not self.cancelled:
            self.done.emit()

    def _fail(self, msg: str):
        self.thread().quit()
        if not self.cancelled:
            self.failed.emit(msg or "Can't play this file")


class AudioEngine(QObject):
    """Plays decoded PCM through a QAudioSink, resampling every output frame at
    the current platter speed and ramping volume per sample. Speed changes and
    fades therefore never jump the waveform, which is what made QMediaPlayer's
    playback-rate changes pop."""

    progress = Signal(int)    # position in ms, after each write
    ended = Signal()          # a non-looping track ran out
    loaded = Signal()         # decoding finished; the duration is exact now
    failed = Signal(str)

    CHUNK = 512               # frames rendered per step
    BUFFER_S = 0.1            # what the sink holds ahead of the speakers

    def __init__(self, parent=None):
        super().__init__(parent)
        self.loop = True
        self.normalize = True     # loudness normalization on/off
        self._norm = 1.0          # gain currently applied; eases toward the track's
        self.rate_target = 0.0    # platter speed (1 = normal), ramped per sample
        self.vol_target = 0.0
        self._rate = 0.0
        self._vol = 0.0
        self._pos = 0.0           # read position, in source frames
        self._seek_ms: float | None = None
        self._job: DecodeJob | None = None
        self._threads: set[QThread] = set()
        self._ended = False
        self._quiet = 0
        self.fmt = engine_format()        # what the decoder produces
        self.out_fmt = self.fmt           # what the sink takes (converted on output)
        self.sr = ENGINE_RATE
        self._device_id = None
        self._sink: QAudioSink | None = None
        self._io = None
        self._timer = QTimer(self)
        self._timer.setTimerType(Qt.PreciseTimer)
        self._timer.setInterval(10)
        self._timer.timeout.connect(self._pump)
        # Follow the Windows default output. The change signal can arrive before Qt
        # knows the new default (or not at all), so also check once a second.
        self._devices = QMediaDevices(self)
        self._devices.audioOutputsChanged.connect(lambda: QTimer.singleShot(300, self._check_device))
        self._device_timer = QTimer(self)
        self._device_timer.setInterval(1000)
        self._device_timer.timeout.connect(self._check_device)
        self._device_timer.start()
        self._open()

    # --- output device
    def _check_device(self):
        if QMediaDevices.defaultAudioOutput().id() != self._device_id:
            self._open()

    def _open(self):
        """(Re)open the sink on the current default device. Playback carries on
        from the same spot; the decoded audio is reused unless the rate must change."""
        dev = QMediaDevices.defaultAudioOutput()
        self._device_id = dev.id()
        running = self._io is not None and self._timer.isActive()
        if self._sink is not None:
            self._sink.stop()
            self._sink.deleteLater()
        out = engine_format(self.sr)
        if not dev.isFormatSupported(out):
            out = dev.preferredFormat()   # always accepted; _convert maps channels and sample type
            if out.sampleRate() != self.sr:
                self._resample_to(out.sampleRate())
        self.out_fmt = out
        self._sink = QAudioSink(dev, out, self)
        self._sink.setBufferSize(int(self.sr * self.BUFFER_S) * out.bytesPerFrame())
        self._io = None
        if running:
            self._wake()

    def _resample_to(self, rate: int):
        """The new device wants another rate: output at it. Decoded audio stays at its
        own rate (the resampler adapts), so playback continues from the same spot."""
        self.sr = rate
        self.fmt = engine_format(rate)

    def _wake(self):
        if self._io is None:
            self._io = self._sink.start()
        elif self._sink.state().name == "SuspendedState":   # by name: Qt 6.7+ returns QtAudio.State, not QAudio.State
            self._sink.resume()
        self._quiet = 0
        if not self._timer.isActive():
            self._timer.start()

    def stop(self):
        self._timer.stop()
        self._device_timer.stop()
        if self._sink is not None:
            self._sink.stop()
        if self._job is not None:
            self._job.cancelled = True
        for t in list(self._threads):
            t.quit()
            t.wait(1000)

    # --- source
    def load(self, path: str, expect_ms: int = 0):
        if self._job is not None:
            self._job.cancelled = True
        job = DecodeJob(path, self.fmt, expect_ms)
        thread = QThread(self)
        job.moveToThread(thread)
        thread.started.connect(job.run)
        job.done.connect(self._on_done)
        job.failed.connect(self.failed)
        thread.finished.connect(lambda t=thread: self._threads.discard(t))
        thread.finished.connect(job.deleteLater)
        self._threads.add(thread)
        self._job = job
        self._pos = 0.0
        self._seek_ms: float | None = None
        self._ended = False
        thread.start()

    def seek(self, ms: float):
        """Jump to `ms` into the track. Before any audio is decoded the jump waits
        until the sample rate is known; past what's decoded so far, playback waits
        in place until the decoder gets there."""
        job = self._job
        if job is None or job.n == 0:
            self._seek_ms = max(0.0, float(ms))
            return
        pos = max(0.0, float(ms) / 1000 * job.rate)
        if job.complete:
            pos = min(pos, max(0.0, job.n - 2.0))
        self._pos = pos
        self._seek_ms = None
        self._ended = False

    def _on_done(self):
        if self.sender() is self._job:
            self.loaded.emit()

    # --- clock
    def position_ms(self) -> int:
        if self._seek_ms is not None:
            return int(self._seek_ms)
        job = self._job
        return int(self._pos / job.rate * 1000) if job is not None and job.n else 0

    def duration_ms(self) -> int:
        job = self._job
        if job is None:
            return 0
        if job.complete:
            return int(job.n / job.rate * 1000)
        return 0

    # --- rendering
    def _render(self, frames: int) -> np.ndarray:
        rates = np.linspace(self._rate, self.rate_target, frames, endpoint=False, dtype=np.float64)
        vols = np.linspace(self._vol, self.vol_target, frames, endpoint=False, dtype=np.float32)
        self._rate, self._vol = self.rate_target, self.vol_target
        silence = np.zeros((frames, 2), np.float32)
        job = self._job
        if job is None:
            return silence
        n = job.n            # read n before buf (see DecodeJob)
        data = job.buf
        complete = job.complete
        if n < 2:
            return silence
        if self._seek_ms is not None:   # a jump asked for before the sample rate was known
            self.seek(self._seek_ms)
        ratio = job.rate / self.sr                 # source frames per output frame at normal speed
        src = rates * ratio
        steps = np.concatenate(([0.0], np.cumsum(src[:-1])))
        end = self._pos + float(src.sum())
        looping = self.loop and complete
        if not looping and end >= n - 1:
            if not complete:
                return silence              # decoder hasn't got here yet: wait in place
            if not self._ended:
                self._ended = True
                QTimer.singleShot(0, self.ended.emit)
        idx = self._pos + steps
        if looping:
            idx %= n
            end %= n
        else:
            idx = np.minimum(idx, n - 1.0001)
            end = min(end, n - 1.0001)
        # windowed-sinc resampling: when the source runs faster than the output the
        # kernel widens, so it also removes what the output rate can't carry
        speed = ratio * max(self._rate, float(rates[0]), 1e-3)
        scale = speed / 0.92 if speed > 1.0 else 1.0   # downsampling: cut a little under the output Nyquist
        half = int(math.ceil(SINC_HALF * scale))
        k = np.arange(-half + 1, half + 1)
        i0 = np.floor(idx).astype(np.int64)
        x = k[None, :] - (idx - i0)[:, None]
        t = x / half
        kern = np.sinc(x / scale) * (0.42 + 0.5 * np.cos(np.pi * t) + 0.08 * np.cos(2 * np.pi * t))
        kern /= kern.sum(axis=1, keepdims=True)
        taps = i0[:, None] + k[None, :]
        taps = taps % n if looping else np.clip(taps, 0, n - 1)
        sampled = np.einsum("ft,ftc->fc", kern.astype(np.float32), data[taps].astype(np.float32))
        # loudness normalization eases toward the track's gain (estimate, then exact)
        want = job.gain if self.normalize and job.gain is not None else (self._norm if self.normalize else 1.0)
        nxt = want + (self._norm - want) * math.exp(-frames / self.sr / 0.4)
        norm = np.linspace(self._norm, nxt, frames, endpoint=False, dtype=np.float32)
        self._norm = nxt
        out = sampled * (vols * norm)[:, None]
        self._pos = end
        return out

    def _convert(self, out: np.ndarray) -> bytes:
        """Stereo float (int16 scale) -> the sink's channel count and sample type."""
        f = self.out_fmt
        ch = f.channelCount()
        if ch == 1:
            out = out.mean(axis=1, keepdims=True)
        elif ch > 2:
            wide = np.zeros((len(out), ch), np.float32)
            wide[:, :2] = out
            out = wide
        sf = f.sampleFormat()
        if sf == QAudioFormat.Float:
            return (out * (1 / 32768)).astype(np.float32).tobytes()
        out = np.clip(out, -32768, 32767)
        if sf == QAudioFormat.Int32:
            return (out * 65536).astype(np.int32).tobytes()
        if sf == QAudioFormat.UInt8:
            return (out / 256 + 128).astype(np.uint8).tobytes()
        return out.astype(np.int16).tobytes()

    def _pump(self):
        if self._io is None:
            return
        if self._sink.error().name != "NoError" and self._sink.state().name == "StoppedState":
            self._device_id = None   # device vanished or failed: reopen on the current default
            self._open()
            self._wake()
            return
        bpf = self.out_fmt.bytesPerFrame()
        free = self._sink.bytesFree() // bpf
        while free >= self.CHUNK:
            self._io.write(self._convert(self._render(self.CHUNK)))
            free -= self.CHUNK
        self.progress.emit(self.position_ms())
        # sleep only when the record has stopped; muted playback keeps moving
        silent = self._vol == 0.0 and self.vol_target == 0.0 and self._rate == 0.0 and self.rate_target == 0.0
        self._quiet = self._quiet + 1 if silent else 0
        if self._quiet > int(1.5 / 0.01):   # 1.5 s of silence: let the device sleep
            self._sink.suspend()
            self._timer.stop()

    def set_targets(self, rate: float, vol: float):
        self.rate_target = max(0.0, float(rate))
        self.vol_target = max(0.0, float(vol))
        if self.vol_target > 0 or self.rate_target > 0:
            self._wake()


class Player(QObject):
    changed = Signal()   # track, art or play state changed
    tick = Signal()      # animation frame

    def __init__(self):
        super().__init__()
        self.engine = AudioEngine(self)
        self.engine.failed.connect(self._on_error)
        self.engine.loaded.connect(self._on_loaded)
        self.engine.progress.connect(self._on_position)
        self.engine.ended.connect(self._on_ended)

        self.queue: list[str] = []   # one file loops forever; several play in turn, looping the list
        self.order: list[int] = []   # play order (queue indices): in turn, or a fresh shuffle every pass
        self.index = 0               # position in self.order
        self.shuffle = False
        self._err_streak = 0
        self.path: str | None = None
        self.title = "Drop an audio file here"
        self.length_ms = 0
        self.art: QImage | None = None
        self.seed = 0
        self.accent = QColor(150, 160, 200)
        self.error: str | None = None
        self.want_playing = False
        self.auto_paused = False
        self.style = "glassy"
        self.hires = False
        self.volume = 1.0        # user volume (Ctrl + scroll), on top of fades and the platter
        self.scrubbing = False   # Ctrl + dragging the record: hand on the platter, sound muted
        self._pending: str | None = None
        self._disc_cache: dict[int, QPixmap] = {}
        self._sheen_cache: dict[int, QPixmap] = {}

        self.angle = 0.0
        self.speed = 0.0
        self.gain = 0.0
        self.rate = 0.0          # platter speed: 1 = playing, 0 = stopped
        self._rate_then = None
        self._rate_anim = QVariantAnimation(self)
        self._rate_anim.valueChanged.connect(self._set_rate)
        self._rate_anim.finished.connect(self._rate_done)
        self.vis = 1.0
        self._gain_then = None
        self._vis_then = None
        self._gain_anim = QVariantAnimation(self)
        self._gain_anim.valueChanged.connect(self._set_gain)
        self._gain_anim.finished.connect(self._gain_done)
        self._vis_anim = QVariantAnimation(self)
        self._vis_anim.valueChanged.connect(self._set_vis)
        self._vis_anim.finished.connect(self._vis_done)

        self._viewers = 0
        self._last = time.perf_counter()
        self._clock = QTimer(self)
        self._clock.setTimerType(Qt.PreciseTimer)
        self._clock.setInterval(16)
        self._clock.timeout.connect(self._step)

    # --- drawing resources
    def disc_pixmap(self, px: int) -> QPixmap:
        glass = self.style == "glossy"
        if (px, glass) not in self._disc_cache:
            render = render_glass_disc if glass else render_disc
            self._disc_cache[(px, glass)] = render(self.art, px, self.seed, empty=self.path is None)
        return self._disc_cache[(px, glass)]

    def sheen_pixmap(self, px: int) -> QPixmap:
        glass = self.style == "glossy"
        if (px, glass) not in self._sheen_cache:
            self._sheen_cache[(px, glass)] = render_glossy_sheen(px) if glass else render_sheen(px)
        return self._sheen_cache[(px, glass)]

    def icon(self) -> QIcon:
        return QIcon(render_disc(self.art, 64, self.seed, empty=self.path is None))

    # --- animation clock, only runs while something is on screen
    def add_viewer(self):
        self._viewers += 1
        if not self._clock.isActive():
            self._last = time.perf_counter()
            self._clock.start()

    def remove_viewer(self):
        self._viewers = max(0, self._viewers - 1)
        if self._viewers == 0:
            self._clock.stop()

    def _step(self):
        now = time.perf_counter()
        dt = min(0.1, now - self._last)
        self._last = now
        self.speed = 0.0 if self.scrubbing else SPIN_DPS * self.rate   # the disc and the audio share one platter speed
        self.angle = (self.angle + self.speed * dt) % 360
        busy = self.rate > 0 or any(a.state() == QVariantAnimation.Running
                                    for a in (self._rate_anim, self._gain_anim, self._vis_anim))
        # 60 fps while something moves; a slow heartbeat while everything is still
        want = 16 if busy else 250
        if self._clock.interval() != want:
            self._clock.setInterval(want)
        self.tick.emit()

    def _kick(self):
        if self._clock.isActive() and self._clock.interval() != 16:
            self._last = time.perf_counter()
            self._clock.setInterval(16)

    # --- state
    def position(self) -> int:
        return self.engine.position_ms() if self.path else 0

    def duration(self) -> int:
        return self.engine.duration_ms() or self.length_ms

    def toggle(self):
        if self.want_playing:
            self.pause()
        else:
            self.play()

    def play(self):
        if not self.path or self.error:
            return
        self.want_playing = True
        self.auto_paused = False
        if self.gain < 1.0 and self._pending is None:
            self._fade_gain(1.0, 200, QEasingCurve.OutQuad)
        self._spin(1.0, 750, QEasingCurve.OutCubic)      # motor spins the platter up
        self.changed.emit()

    def pause(self, auto: bool = False):
        if not self.want_playing:
            return
        self.want_playing = False
        self.auto_paused = auto
        self._spin(0.0, 1100, QEasingCurve.InOutSine, then=self._finish_pause)   # platter winds down
        self.changed.emit()

    def _spin(self, target, ms, curve, then=None):
        self._kick()
        a = self._rate_anim
        a.stop()
        self._rate_then = then
        _run(a, self.rate, target, max(1, int(ms * abs(target - self.rate))), curve)

    def _set_rate(self, r):
        self.rate = float(r)   # slower platter = slower and lower audio, like a real record
        self._apply_volume()

    def _rate_done(self):
        cb, self._rate_then = self._rate_then, None
        if cb:
            cb()

    def _finish_pause(self):
        pass   # the platter is at 0: the engine outputs silence and lets the device sleep

    def load_list(self, paths: list[str], index: int | None = None, animate: bool = True,
                  order: list[int] | None = None, pos: int = 0):
        """Replace whatever is playing with these files (a single file just loops).
        `index` picks the file to start with (default: the first, or a random one
        when shuffling); `order` and `pos` restore a saved shuffle."""
        if not paths:
            return
        self.queue = [str(Path(p)) for p in paths]
        n = len(self.queue)
        if order is not None and sorted(order) == list(range(n)):
            self.order, self.index = list(order), pos % n
        elif self.shuffle:
            self.order = self._shuffled(first=None if index is None else index % n)
            self.index = 0
        else:
            self.order = list(range(n))
            self.index = (index or 0) % n
        self._err_streak = 0
        self.load(self.queue[self.order[self.index]], animate)

    def track_index(self) -> int:
        """Index in self.queue of the current file."""
        return self.order[self.index] if self.order else 0

    def _shuffled(self, first: int | None = None, avoid: int | None = None) -> list[int]:
        """Every file once, in random order. `first` starts the pass; `avoid` keeps
        the song that just ended from coming straight back when a new pass begins."""
        rest = [i for i in range(len(self.queue)) if i != first]
        random.shuffle(rest)
        if avoid is not None and len(rest) > 1 and rest[0] == avoid:
            j = random.randrange(1, len(rest))
            rest[0], rest[j] = rest[j], rest[0]
        return ([first] if first is not None else []) + rest

    def set_shuffle(self, on: bool):
        if on == self.shuffle:
            return
        self.shuffle = on
        if self.queue:
            cur = self.track_index()
            # the current song keeps playing; the rest of the pass follows the new order
            self.order = self._shuffled(first=cur) if on else list(range(len(self.queue)))
            self.index = 0 if on else cur
        self.changed.emit()

    def next(self, animate: bool = True):
        if len(self.queue) < 2:
            return
        if self.index + 1 >= len(self.order):   # a full pass: shuffle again for the next one
            if self.shuffle:
                self.order = self._shuffled(avoid=self.track_index())
            self.index = 0
        else:
            self.index += 1
        self.load(self.queue[self.order[self.index]], animate)

    def previous(self, animate: bool = True):
        if len(self.queue) < 2:
            return
        self.index = (self.index - 1) % len(self.order)
        self.load(self.queue[self.order[self.index]], animate)

    def skip(self, step: int):
        """Ctrl + click: a song forward or back; a single song starts over."""
        if not self.path:
            return
        if len(self.queue) < 2:
            self.engine.seek(0)
            self.play()
            return
        (self.next if step > 0 else self.previous)()

    def _on_position(self, pos):
        # start easing out just before the end so tracks blend into each other
        d = self.engine.duration_ms()
        if len(self.queue) > 1 and self._pending is None and self.want_playing \
                and d > 0 and d - pos <= FADE_OUT_MS + 60:
            self.next()

    def _on_loaded(self):
        self._err_streak = 0
        self.changed.emit()

    def _on_ended(self):
        if len(self.queue) > 1 and self._pending is None:
            self.next()

    def load(self, path: str, animate: bool = True):
        self._pending = str(Path(path))
        if not animate:
            self._vis_anim.stop()
            self._set_vis(0.0)
            self._swap()
            return
        self._fade_gain(0.0, FADE_OUT_MS, QEasingCurve.InQuad)
        self._fade_vis(0.0, FADE_OUT_MS, QEasingCurve.InCubic, then=self._swap)

    def _swap(self):
        path, self._pending = self._pending, None
        if not path:
            return
        info = read_track(path)
        self.path = path
        self.title = info.title
        self.length_ms = info.length_ms
        self.hires = info.hires
        self.art = info.art
        self.seed = zlib.crc32(path.lower().encode("utf-8"))
        self.accent = accent_from(info.art, self.seed)
        self.error = None
        self._disc_cache.clear()
        self.engine.loop = len(self.queue) < 2
        self.engine.load(path, info.length_ms)
        self._set_gain(0.0)
        self._rate_anim.stop()
        self._rate_then = None
        self._set_rate(1.0)
        self.want_playing = True
        self.auto_paused = False
        self._fade_gain(1.0, FADE_IN_MS, QEasingCurve.OutQuad)
        curve = QEasingCurve(QEasingCurve.OutBack)
        curve.setOvershoot(0.9)
        self._fade_vis(1.0, FADE_IN_MS + 80, curve)
        self.changed.emit()

    def _on_error(self, msg):
        if len(self.queue) > 1 and self._err_streak < len(self.queue):
            self._err_streak += 1
            QTimer.singleShot(300, lambda: self.next(animate=False))
            return
        self.error = msg or "Can't play this file"
        self.want_playing = False
        self._rate_anim.stop()
        self._set_rate(0.0)
        self.changed.emit()

    # --- fades
    def _set_gain(self, g):
        self.gain = float(g)
        self._apply_volume()

    def _apply_volume(self):
        # the sound also fades as the platter slows, so the wind-down ends in silence
        platter = 0.0 if self.scrubbing else min(1.0, self.rate / 0.3) ** 1.3
        self.engine.set_targets(self.rate, self.gain * self.gain * platter * self.volume ** 2)

    def begin_scrub(self):
        self.scrubbing = True
        self._apply_volume()
        self._kick()

    def scrub_to(self, ms: float):
        dur = self.duration()
        self.engine.seek(max(0.0, min(float(ms), dur - 250 if dur > 500 else float(ms))))

    def end_scrub(self):
        self.scrubbing = False
        self._apply_volume()
        self.changed.emit()

    def set_volume(self, v: float):
        self.volume = max(0.0, min(1.0, float(v)))
        self._apply_volume()

    def _set_vis(self, v):
        self.vis = float(v)

    def _fade_gain(self, target, ms, curve, then=None):
        self._kick()
        a = self._gain_anim
        a.stop()
        self._gain_then = then
        _run(a, self.gain, target, ms, curve)

    def _fade_vis(self, target, ms, curve, then=None):
        self._kick()
        a = self._vis_anim
        a.stop()
        self._vis_then = then
        _run(a, self.vis, target, ms, curve)

    def _gain_done(self):
        cb, self._gain_then = self._gain_then, None
        if cb:
            cb()

    def _vis_done(self):
        cb, self._vis_then = self._vis_then, None
        if cb:
            cb()


# --------------------------------------------------------------------------- UI

def ui_font(pt: float, weight=QFont.Normal) -> QFont:
    f = QFont("Segoe UI Variable Text")
    f.setFamilies(["Segoe UI Variable Text", "Segoe UI"])
    f.setPointSizeF(pt)
    f.setWeight(weight)
    return f


def rounded_shape(rect: QRectF, radius: float):
    return lambda inset: _rounded(rect.adjusted(inset, inset, -inset, -inset), max(0.0, radius - inset))


def _rounded(rect: QRectF, radius: float) -> QPainterPath:
    path = QPainterPath()
    path.addRoundedRect(rect, radius, radius)
    return path


SQUIRCLE_N = 4.0   # superellipse exponent for glass corners (> 2 keeps the joins G2)


def squircle_path(rect: QRectF, corner: float, n: float = SQUIRCLE_N, steps: int = 40) -> QPainterPath:
    """A rectangle whose corners are quarter superellipses |x|^n + |y|^n = 1.

    For n > 2 a superellipse has zero curvature where it meets the axes, and a
    straight edge has zero curvature too, so every edge flows into its corner
    with G2 continuity: no kink where a circular arc would start. When `corner`
    is half the shorter side the straight parts vanish and it is a full squircle."""
    c = max(0.0, min(corner, rect.width() / 2, rect.height() / 2))
    path = QPainterPath()
    if c <= 0:
        path.addRect(rect)
        return path
    e = 2.0 / n
    l, t, r, b = rect.left(), rect.top(), rect.right(), rect.bottom()
    quarter = [((math.cos(k * math.pi / 2 / steps)) ** e, (math.sin(k * math.pi / 2 / steps)) ** e)
               for k in range(steps + 1)]
    path.moveTo(l + c, t)
    path.lineTo(r - c, t)
    for co, si in quarter:                       # top-right
        path.lineTo(r - c + c * si, t + c - c * co)
    path.lineTo(r, b - c)
    for co, si in quarter:                       # bottom-right
        path.lineTo(r - c + c * co, b - c + c * si)
    path.lineTo(l + c, b)
    for co, si in quarter:                       # bottom-left
        path.lineTo(l + c - c * si, b - c + c * co)
    path.lineTo(l, t + c)
    for co, si in quarter:                       # top-left
        path.lineTo(l + c - c * co, t + c - c * si)
    path.closeSubpath()
    return path


def squircle_shape(rect: QRectF, corner: float, n: float = SQUIRCLE_N):
    """`shape(inset)` -> the squircle shrunk by `inset` px on every side."""
    return lambda inset: squircle_path(rect.adjusted(inset, inset, -inset, -inset), max(0.0, corner - inset), n)


def paint_glass_edge(p: QPainter, shape, rim: float = 1.0):
    """The optical edge of a glass shape: a 1px semi-transparent inner stroke that
    follows the outline exactly, brightened into specular highlights where light
    catches it (top-left) and bounces back out (bottom-right)."""
    edge = shape(0.5)   # a 1px pen centred half a pixel inside lies just inside the outline
    rect = edge.boundingRect()
    p.save()
    p.setBrush(Qt.NoBrush)
    p.setPen(QPen(QColor(255, 255, 255, min(255, int(64 * rim))), 1.0))
    p.drawPath(edge)
    spec = QConicalGradient(rect.center(), 0)
    for pos, a in ((0.0, 0), (0.24, 40), (0.33, 150), (0.375, 210), (0.42, 150), (0.52, 30), (0.62, 0),
                   (0.82, 0), (0.86, 70), (0.875, 110), (0.89, 70), (0.93, 0), (1.0, 0)):
        spec.setColorAt(pos, QColor(255, 255, 255, min(255, int(a * rim))))
    p.setPen(QPen(spec, 1.0))
    p.drawPath(edge)
    p.restore()


def paint_glass_chip(p: QPainter, shape, fill: int, rim: float = 1.0):
    """A small glass control (button, badge) sitting on the card's glass."""
    outline = shape(0)
    rect = outline.boundingRect()
    p.save()
    p.setPen(Qt.NoPen)
    p.fillPath(outline, QColor(255, 255, 255, fill))
    sheen = QLinearGradient(rect.topLeft(), rect.bottomLeft())
    sheen.setColorAt(0.0, QColor(255, 255, 255, int(40 * rim)))
    sheen.setColorAt(0.5, QColor(255, 255, 255, 0))
    sheen.setColorAt(1.0, QColor(255, 255, 255, int(12 * rim)))
    p.fillPath(outline, sheen)
    p.restore()
    paint_glass_edge(p, shape, rim)


# ------------------------------------------------------------------ backdrop blur

CAPTURE_PAD = 18   # desktop captured beyond each edge; the glass edge bends it inward
MOVING_PAD = 160   # wider while the window moves, so a capture still covers it between refreshes


def _box_blur(a: np.ndarray, r: int, axis: int) -> np.ndarray:
    pad = [(0, 0)] * a.ndim
    pad[axis] = (r + 1, r)
    c = np.cumsum(np.pad(a, pad, mode="edge"), axis=axis)
    n = a.shape[axis]
    hi = np.take(c, np.arange(2 * r + 1, 2 * r + 1 + n), axis=axis)
    lo = np.take(c, np.arange(0, n), axis=axis)
    return (hi - lo) / (2 * r + 1)


def _small_array(img: QImage, down: int) -> np.ndarray:
    w, h = img.width(), img.height()
    small = img.scaled(max(1, w // down), max(1, h // down), Qt.IgnoreAspectRatio,
                       Qt.SmoothTransformation).convertToFormat(QImage.Format_RGB32)
    sw, sh = small.width(), small.height()
    a = np.frombuffer(small.constBits(), np.uint8, count=small.sizeInBytes())
    return a.reshape(sh, small.bytesPerLine() // 4, 4)[:, :sw, :3].astype(np.float32)


def _to_qimage(a: np.ndarray) -> QImage:
    sh, sw = a.shape[:2]
    out = np.empty((sh, sw, 4), np.uint8)
    out[..., :3] = np.clip(a, 0, 255)
    out[..., 3] = 255
    return QImage(out.data, sw, sh, sw * 4, QImage.Format_RGB32).copy()


def fill_holes(img: np.ndarray, mask: np.ndarray) -> np.ndarray:
    """Fill the pixels where mask == 0 from their surroundings (push-pull): average
    the known pixels down a pyramid until the hole is covered, then bring the coarse
    colours back up, blending them in only where nothing was known."""
    levels = []
    c, m = img * mask[..., None], mask.astype(np.float32)
    while True:
        levels.append((c, m))
        if m.min() > 0 or min(m.shape) <= 2:
            break
        h, w = m.shape
        h2, w2 = (h + 1) // 2, (w + 1) // 2
        c = np.pad(c, ((0, h2 * 2 - h), (0, w2 * 2 - w), (0, 0))).reshape(h2, 2, w2, 2, 3).sum(axis=(1, 3))
        m = np.pad(m, ((0, h2 * 2 - h), (0, w2 * 2 - w))).reshape(h2, 2, w2, 2).sum(axis=(1, 3))
    c, m = levels[-1]
    fill = c / np.maximum(m, 1e-6)[..., None]
    if (m <= 0).any():
        fill[m <= 0] = (c.sum(axis=(0, 1)) / max(float(m.sum()), 1e-6))
    for c, m in reversed(levels[:-1]):
        h, w = m.shape
        up = np.repeat(np.repeat(fill, 2, axis=0), 2, axis=1)[:h, :w]
        up = _box_blur(_box_blur(up, 1, 0), 1, 1)
        known = c / np.maximum(m, 1e-6)[..., None]
        wgt = np.clip(m, 0, 1)[..., None]
        fill = known * wgt + up * (1 - wgt)
    return fill


def frost_from(a: np.ndarray, radius: int, saturation: float) -> QImage:
    """Three box passes (close to a gaussian) and a saturation boost."""
    if radius > 0:
        for _ in range(3):
            a = _box_blur(_box_blur(a, radius, 0), radius, 1)
    lum = (a @ np.array([0.114, 0.587, 0.299], np.float32))[..., None]
    return _to_qimage(lum + (a - lum) * saturation)


def blur_small(img: QImage, down: int, radius: int, saturation: float) -> QImage:
    """Downscale, three box passes (close to a gaussian) and a saturation boost.
    The result stays small: drawing it scaled up with smooth filtering is free blur."""
    return frost_from(_small_array(img, down), radius, saturation)


class _Dib:
    """A 32-bit DIB section the screen is copied into; numpy reads it in place."""

    def __init__(self, w: int, h: int):
        self.w, self.h = w, h
        bmi = BITMAPINFOHEADER(ctypes.sizeof(BITMAPINFOHEADER), w, -h, 1, 32, 0, 0, 0, 0, 0, 0)
        bits = ctypes.c_void_p()
        self.hbm = gdi32.CreateDIBSection(None, ctypes.byref(bmi), 0, ctypes.byref(bits), None, 0)
        self.dc = gdi32.CreateCompatibleDC(None)
        self._old = gdi32.SelectObject(self.dc, self.hbm)
        self.arr = np.ctypeslib.as_array(ctypes.cast(bits, ctypes.POINTER(ctypes.c_uint8)), shape=(h, w, 4))

    def grab(self, x: int, y: int) -> bool:
        screen = user32.GetDC(None)
        ok = gdi32.BitBlt(self.dc, 0, 0, self.w, self.h, screen, x, y, SRCCOPY)
        user32.ReleaseDC(None, screen)
        return bool(ok)

    def close(self):
        gdi32.SelectObject(self.dc, self._old)
        gdi32.DeleteDC(self.dc)
        gdi32.DeleteObject(self.hbm)


REFRESH_RATES = (5, 10, 15, 30, 60, 120, 240, 360)


class BackdropWorker(QThread):
    """Captures what is behind one window at the chosen rate, on its own thread,
    and blurs it only when it changed. A screen capture mostly waits on the
    compositor (about 3 ms, 0.5 ms of it CPU), so it must not run on the UI thread."""

    ready = Signal()

    def __init__(self, hwnd: int, dpr: float, hz: int):
        super().__init__()
        self.hwnd, self.dpr, self.hz = hwnd, dpr, hz
        self.boost = True        # speed up while something changes behind the window
        self.boost_hz = hz
        self.inpaint = False     # our window is in the capture: rebuild what's behind it
        self.running = True
        self._latest = None
        self._lock = threading.Lock()

    def take(self):
        with self._lock:
            data, self._latest = self._latest, None
        return data

    def run(self):
        dib = prev = prev_win = None
        moving_until = 0.0
        last_change = 0.0
        recent: list[tuple[float, tuple]] = []   # where the card was lately (screen rects)
        r = RECT()
        try:
            while self.running:
                t0 = time.perf_counter()
                if user32.GetWindowRect(self.hwnd, ctypes.byref(r)):
                    win = (r.left, r.top, r.right, r.bottom)
                    # Windows composes a moving window a frame or two late, so a capture
                    # can still show the card where it just was: treat those spots as card too
                    recent.append((t0, win))
                    recent = [rv for rv in recent if t0 - rv[0] < 0.15]
                    if prev_win is not None and win[:2] != prev_win[:2]:
                        moving_until = t0 + 0.25   # moving: capture wider so it keeps covering the card
                    pad = round((MOVING_PAD if t0 < moving_until else CAPTURE_PAD) * self.dpr)
                    x, y = r.left - pad, r.top - pad
                    w, h = r.right - r.left + 2 * pad, r.bottom - r.top + 2 * pad
                    if dib is None or (dib.w, dib.h) != (w, h):
                        if dib is not None:
                            dib.close()
                        dib, prev = _Dib(w, h), None
                    holes = []
                    if dib.grab(x, y):
                        frame = dib.arr
                        if self.inpaint:   # our own (animated) card is in the picture: ignore it
                            frame = frame.copy()
                            for _, (l, tp, rt, bt) in recent:
                                hl, ht, hr, hb = max(0, l - x), max(0, tp - y), min(w, rt - x), min(h, bt - y)
                                if hr > hl and hb > ht:
                                    frame[ht:hb, hl:hr] = 0
                                    holes.append((hl, ht, hr, hb))
                        changed = prev is None or win != prev_win or not np.array_equal(frame, prev)
                    else:
                        changed = False
                    if changed:
                        prev, prev_win = (frame if self.inpaint else frame.copy()), win
                        last_change = t0
                        frost, lens = self._layers(prev, pad, holes)
                        with self._lock:
                            self._latest = (frost, lens, (x, y, w, h))
                        self.ready.emit()
                lively = self.boost and (t0 - last_change < 1.0 or t0 < moving_until)
                hz = max(self.hz, self.boost_hz) if lively else self.hz
                time.sleep(max(0.0, 1.0 / hz - (time.perf_counter() - t0)))
        finally:
            if dib is not None:
                dib.close()

    def _layers(self, arr: np.ndarray, pad: int, holes=()):
        h, w = arr.shape[:2]
        img = QImage(arr.data, w, h, w * 4, QImage.Format_RGB32)
        if self.inpaint:
            # The window is visible to capture, so the capture shows the card itself
            # (where it is and where it just was). Rebuild what's behind those spots
            # from the surroundings instead (push-pull fill).
            small = _small_array(img, 4)
            sh, sw = small.shape[:2]
            mask = np.ones((sh, sw), np.float32)
            if not holes:
                holes = [(pad, pad, w - pad, h - pad)]
            for l, tp, rt, bt in holes:
                mask[max(0, tp // 4 - 1): bt // 4 + 2, max(0, l // 4 - 1): rt // 4 + 2] = 0
            filled = fill_holes(small, mask)
            frost = frost_from(filled, 4, 1.6)
            inset = max(0, (pad - round(CAPTURE_PAD * self.dpr)) // 4)
            lens = _to_qimage(filled[inset: sh - inset, inset: sw - inset])
            return frost, lens
        frost = blur_small(img, down=4, radius=4, saturation=1.6)
        # the edge refracts only the near surroundings, softly blurred
        inset = pad - round(CAPTURE_PAD * self.dpr)
        near = img.copy(inset, inset, w - 2 * inset, h - 2 * inset)
        lens = near.scaled(max(1, near.width() // 3), max(1, near.height() // 3),
                           Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
        return frost, lens


class Backdrop(QObject):
    """The desktop behind a glass window, blurred in two layers: a heavy frost for
    the body and a light blur that the edge refracts. Works because the window is
    excluded from capture (prepare_glass_window)."""

    updated = Signal()

    def __init__(self, widget: QWidget):
        super().__init__(widget)
        self.widget = widget
        self.frost: QImage | None = None
        self.lens: QImage | None = None
        self.capture: tuple | None = None     # screen rect of the capture, physical px
        self.serial = 0
        self.hz = 60
        self.boost = True
        self.capturable = False
        self._worker: BackdropWorker | None = None
        set_capturable(int(widget.winId()), False)

    def start(self):
        if self._worker is None:
            wk = BackdropWorker(int(self.widget.winId()), self.widget.devicePixelRatioF(), self.hz)
            self._configure(wk)
            wk.ready.connect(self._on_ready)
            wk.start()
            self._worker = wk

    def stop(self):
        wk, self._worker = self._worker, None
        if wk is not None:
            wk.running = False
            wk.wait(1000)

    def set_rate(self, hz: int, boost: bool):
        self.hz, self.boost = hz, boost
        if self._worker is not None:
            self._configure(self._worker)

    def set_capturable(self, on: bool):
        self.capturable = on
        set_capturable(int(self.widget.winId()), on)
        if self._worker is not None:
            self._configure(self._worker)

    def _configure(self, wk: BackdropWorker):
        screen = self.widget.screen() or QGuiApplication.primaryScreen()
        display_hz = round(screen.refreshRate()) if screen else 60
        wk.hz = self.hz
        wk.boost = self.boost
        wk.inpaint = self.capturable
        # while things move: double the chosen rate (at least 120), never past the monitor
        wk.boost_hz = max(self.hz, min(display_hz, 360, max(self.hz * 2, 120)))

    def _on_ready(self):
        wk = self._worker
        data = wk.take() if wk is not None else None
        if data is None:
            return
        wk.dpr = self.widget.devicePixelRatioF()
        self.frost, self.lens, self.capture = data
        self.serial += 1
        self.updated.emit()

    def frost_rect(self) -> QRectF | None:
        """Where the capture sits relative to the window now, in logical px. While
        the window moves this drifts, which keeps the last frost fixed on screen."""
        if self.capture is None:
            return None
        r = RECT()
        user32.GetWindowRect(int(self.widget.winId()), ctypes.byref(r))
        d = self.widget.devicePixelRatioF()
        x, y, w, h = self.capture
        return QRectF((x - r.left) / d, (y - r.top) / d, w / d, h / d)


def shadow_text(p: QPainter, rect: QRectF, flags, text: str, color: QColor):
    """Text with a soft drop shadow so it stays readable on see-through glass."""
    p.setPen(QColor(0, 0, 0, 110))
    p.drawText(rect.translated(0, 1), flags, text)
    p.setPen(color)
    p.drawText(rect, flags, text)


# --------------------------------------------------------------------------- physics styles

GLOBE_R = 106.0          # Snow Globe: radius of the glass globe around the record
SNOW_DISC_SCALE = 0.70   # the record is smaller inside the globe, leaving room for snow
WATER_LEVEL = 0.56       # Liquid: rest height of the water surface, as a fraction of the card


def snow_ground_y(x, radius: float):
    """Height of the snow bed inside the globe (globe coords, y down): a gentle mound."""
    return radius * 0.58 - 9.0 * np.cos(np.clip(x / radius, -1.0, 1.0) * (np.pi / 2))


class SnowSim:
    """Flakes in the water of a snow globe, in the globe's own frame (origin at its
    centre, px, y down). The water lags behind the glass when the globe is shoved,
    so it flows opposite to the globe's acceleration and slowly comes to rest; the
    flakes drift with that flow, sink at their own speed, bounce off the glass and
    settle on the snow bed until the water moves fast enough to lift them again."""

    N = 150

    def __init__(self, radius: float, seed: int = 7):
        self.R = radius
        self.rng = np.random.default_rng(seed)
        n = self.N
        ang = self.rng.uniform(0, 2 * np.pi, n)
        rad = radius * 0.88 * np.sqrt(self.rng.uniform(0, 1, n))
        self.p = np.stack([rad * np.cos(ang), rad * np.sin(ang)], axis=1)
        self.p[:, 1] = np.minimum(self.p[:, 1], snow_ground_y(self.p[:, 0], radius) - 1)
        self.v = np.zeros((n, 2))
        self.size = self.rng.uniform(1.0, 2.6, n)
        self.sink = 12.0 + self.size * 7.0          # bigger flakes sink faster (px/s)
        self.resting = np.zeros(n, bool)
        self.flow = np.zeros(2)                     # the water's own motion relative to the glass

    def step(self, dt: float, accel, stir: float) -> bool:
        rng, n, R = self.rng, self.N, self.R
        a = np.asarray(accel, float)
        # the water keeps going when the glass changes speed, then friction stops it
        self.flow += (-a * 0.30 - self.flow / 0.45) * dt
        speed = float(np.hypot(*self.flow))
        if speed > 380:
            self.flow *= 380 / speed
            speed = 380.0
        # water rushing over the bed, or the spinning record, lifts settled flakes
        lift_p = np.clip((speed - 30) / 140, 0, 1) * dt * 14 + stir * 0.14 * dt
        lift = self.resting & (rng.random(n) < lift_p)
        if lift.any():
            k = int(lift.sum())
            self.resting[lift] = False
            self.v[lift] = self.flow + np.stack([rng.normal(0, 22, k), -rng.uniform(45, 110, k)], axis=1)
        m = ~self.resting
        if m.any():
            x, y = self.p[m, 0], self.p[m, 1]
            r = np.hypot(x, y) + 1e-3
            swirl = stir * 24 * np.clip(r / R, 0, 1)          # the record stirs a slow vortex
            tx = self.flow[0] - y / r * swirl
            ty = self.flow[1] + x / r * swirl + self.sink[m]
            jitter = 5 + 0.22 * speed
            self.v[m, 0] += (tx - self.v[m, 0]) / 0.18 * dt + rng.normal(0, jitter, x.size) * dt * 4
            self.v[m, 1] += (ty - self.v[m, 1]) / 0.18 * dt + rng.normal(0, jitter, x.size) * dt * 4
            self.p[m] += self.v[m] * dt
        # the glass
        r = np.hypot(self.p[:, 0], self.p[:, 1])
        out = r > R - self.size
        if out.any():
            nrm = self.p[out] / r[out, None]
            self.p[out] = nrm * (R - self.size[out, None])
            vn = np.maximum((self.v[out] * nrm).sum(axis=1), 0)
            self.v[out] -= 1.35 * vn[:, None] * nrm
        # the snow bed
        gy = snow_ground_y(self.p[:, 0], R)
        hit = m & (self.p[:, 1] >= gy)
        if hit.any():
            self.p[hit, 1] = gy[hit]
            calm = hit & (speed < 40)
            self.resting[calm] = True
            self.v[calm] = 0
            bounce = hit & ~calm
            self.v[bounce, 1] = -np.abs(self.v[bounce, 1]) * 0.3
        return bool(m.any()) or speed > 1 or stir > 0


class WaterSim:
    """Water in the card as 1-D shallow-water (Saint-Venant) equations on a staggered
    grid: column heights `h` and the flow `u` between columns, walls at both ends.
    The card's own acceleration is a body force on the flow, so a shove sends the
    water's mass up the far wall and it rocks back at the tank's natural sloshing
    period, with waves that steepen as they travel. Fast-rising crests throw
    droplets; droplets that hit the glass stick and later run down; intense motion
    stirs air bubbles into the water. Card coordinates: px, y down."""

    N = 72
    G = 2200.0           # gravity for the water (px/s^2): about a 1 s slosh in this card
    G_DROP = 1700.0      # gravity for flying droplets
    FRICTION = 0.9       # 1/s, slows the flow
    VISCOSITY = 380.0    # px^2/s, keeps the crests smooth
    SHOVE = 0.35         # share of the window's acceleration the water feels
    TENSION = 220.0      # px^2/s, surface tension smoothing
    MAX_FLYING, MAX_CLING, MAX_BUBBLES = 120, 90, 70

    def __init__(self, width: float, height: float, level_y: float, seed: int = 3):
        self.rng = np.random.default_rng(seed)
        self.W, self.H = float(width), float(height)
        self.depth = self.H - level_y
        self.dx = self.W / self.N
        self.x = (np.arange(self.N) + 0.5) * self.dx
        self.h = np.full(self.N, self.depth)
        self.u = np.zeros(self.N + 1)
        self.volume = float(self.h.sum())
        self._prev_h = self.h.copy()
        self.w = np.zeros(self.N)                    # how fast the surface rises (px/s)
        self.flying = np.zeros((0, 5))               # x, y, vx, vy, r
        self.bub = np.zeros((0, 4))                  # x, y, r, wobble phase
        # condensation on the glass above the water, like the wallpaper
        n = 26
        r = np.clip(0.9 + self.rng.gamma(2.0, 0.55, n), 0.9, 4.2)
        self.cling = np.stack([self.rng.uniform(8, self.W - 8, n), self.rng.uniform(10, level_y - 26, n),
                               r, np.zeros(n)], axis=1)   # x, y, r, sliding speed

    # --- geometry
    def surface_y(self) -> np.ndarray:
        return self.H - self.h

    def surface_at(self, x) -> np.ndarray:
        return np.interp(x, self.x, self.surface_y())

    def _dip(self, x: float, amount: float):
        """Something fell in or popped out: push the surface locally (it spreads as a ripple)."""
        weights = np.exp(-0.5 * ((self.x - x) / (2.2 * self.dx)) ** 2)   # a soft dent, not a notch
        self.h -= amount * 0.6 * weights / weights.sum() * 5

    # --- one frame
    def step(self, dt: float, accel, stir: float) -> bool:
        rng = self.rng
        # the water feels only part of the window's acceleration (window moves are fast
        # in pixel terms; full strength would slosh it about on the slightest drag)
        ax, ay = accel[0] * self.SHOVE, accel[1] * self.SHOVE
        g = max(400.0, self.G + ay)                  # accelerating upward presses the water down
        h, u, dx = self.h, self.u, self.dx
        left = dt
        while left > 1e-6:
            # stable step: waves and flow must not cross more than half a column per step
            speed = float(np.abs(u).max()) + math.sqrt(g * float(h.max()))
            sdt = min(left, 0.004, 0.45 * dx / max(speed, 1.0))
            left -= sdt
            ui = u[1:-1]
            dhdx = (h[1:] - h[:-1]) / dx
            back = (ui - u[:-2]) / dx                # upwind slope for carrying the flow along
            fwd = (u[2:] - ui) / dx
            adv = ui * np.where(ui > 0, back, fwd)
            lap = (u[2:] - 2 * ui + u[:-2]) / (dx * dx)
            ui += sdt * (-g * dhdx - ax - adv - self.FRICTION * ui + self.VISCOSITY * lap)
            if stir > 0:   # the record's hum: faint ripples
                ui += rng.normal(0, 14 * stir, ui.size) * sdt
            np.clip(ui, -650, 650, out=ui)
            depth_up = np.where(ui > 0, h[:-1], h[1:])   # upwind: carry the water that flows
            flux = np.zeros(self.N + 1)
            flux[1:-1] = depth_up * ui
            h -= sdt * (flux[1:] - flux[:-1]) / dx
            hl = np.empty_like(h)                     # surface tension: rounds off sharp kinks
            hl[1:-1] = h[:-2] - 2 * h[1:-1] + h[2:]
            hl[0], hl[-1] = h[1] - h[0], h[-2] - h[-1]
            h += sdt * self.TENSION * hl / (dx * dx)
            np.clip(h, 1.0, self.H - 3, out=h)
        h += (self.volume - h.sum()) / self.N              # keep the amount of water exact
        if not (np.isfinite(h).all() and np.isfinite(u).all()):   # never let a bad step stick
            h[:] = self.depth
            u[:] = 0
            self._prev_h = h.copy()
        self.w = (h - self._prev_h) / dt
        self._prev_h = h.copy()
        surf = self.surface_y()

        # crests that shoot up throw droplets
        hot = np.nonzero((self.w > 300) & (h > self.depth + 8))[0]
        if len(hot) and len(self.flying) < self.MAX_FLYING:
            pick = hot[rng.random(len(hot)) < np.clip((self.w[hot] - 300) / 300, 0, 1) * dt * 30]
            for i in pick[: self.MAX_FLYING - len(self.flying)]:
                uc = 0.5 * (u[i] + u[i + 1])
                drop = [self.x[i] + rng.normal(0, 2), surf[i] - 1, uc * 0.8 + rng.normal(0, 55),
                        -(self.w[i] * rng.uniform(0.6, 1.2) + 40), rng.uniform(1.0, 3.0)]
                self.flying = np.vstack([self.flying, drop])

        # intense movement drags air under the surface
        intensity = max(0.0, (math.hypot(ax, ay) - 1100) / 500) + max(0.0, (float(np.abs(self.w).max()) - 420) / 160)
        if intensity > 0 and len(self.bub) < self.MAX_BUBBLES:
            k = int(min(self.MAX_BUBBLES - len(self.bub), rng.poisson(min(intensity, 6) * dt * 40)))
            if k:
                bx = rng.uniform(6, self.W - 6, k)
                by = self.surface_at(bx) + rng.uniform(3, 42, k)
                br = np.clip(rng.gamma(2.0, 0.9, k), 0.8, 4.8)
                self.bub = np.vstack([self.bub, np.stack([bx, by, br, rng.uniform(0, 6.3, k)], axis=1)])

        self._step_flying(dt, ax, ay)
        self._step_cling(dt)
        self._step_bubbles(dt)
        moving = float(np.abs(u).max()) > 2 or float(np.abs(self.w).max()) > 2
        return moving or len(self.flying) > 0 or len(self.bub) > 0 or bool((self.cling[:, 3] > 0).any()) or stir > 0

    def _step_flying(self, dt, ax, ay):
        f = self.flying
        if not len(f):
            return
        f[:, 2] += (-ax - 0.6 * f[:, 2]) * dt
        f[:, 3] += (self.G_DROP - ay - 0.6 * f[:, 3]) * dt
        f[:, 0] += f[:, 2] * dt
        f[:, 1] += f[:, 3] * dt
        keep = np.ones(len(f), bool)
        stick = []
        for i, (x, y, vx, vy, r) in enumerate(f):
            if y >= self.surface_at(x) and vy > 0:          # back into the water
                keep[i] = False
                self._dip(x, 0.08 * r)
            elif x < r + 2 or x > self.W - r - 2 or y < r + 2:  # hit the glass: it clings
                keep[i] = False
                stick.append([min(max(x, r + 2), self.W - r - 2), max(y, r + 2), r, 0.0])
        self.flying = f[keep]
        if stick and len(self.cling) < self.MAX_CLING:
            self.cling = np.vstack([self.cling, np.array(stick[: self.MAX_CLING - len(self.cling)])])

    def _step_cling(self, dt):
        c = self.cling
        if not len(c):
            return
        rng = self.rng
        # touching drops merge into one bigger drop
        for _ in range(3):
            if len(c) < 2:
                break
            d = np.hypot(c[:, None, 0] - c[None, :, 0], c[:, None, 1] - c[None, :, 1])
            np.fill_diagonal(d, 1e9)
            i, j = np.unravel_index(np.argmin(d - (c[:, None, 2] + c[None, :, 2]) * 0.85), d.shape)
            if d[i, j] >= (c[i, 2] + c[j, 2]) * 0.85:
                break
            vi, vj = c[i, 2] ** 3, c[j, 2] ** 3
            c[i, 0] = (c[i, 0] * vi + c[j, 0] * vj) / (vi + vj)
            c[i, 1] = (c[i, 1] * vi + c[j, 1] * vj) / (vi + vj)
            c[i, 2] = (vi + vj) ** (1 / 3)
            c = np.delete(c, j, axis=0)
        # heavy drops run down in fits and starts, leaving tiny droplets behind
        big = c[:, 2] > 2.4
        start = big & (c[:, 3] == 0) & (rng.random(len(c)) < 0.8 * dt * (c[:, 2] - 2.4))
        c[start, 3] = (c[start, 2] - 2.4) * 22 + 6
        stop = (c[:, 3] > 0) & (rng.random(len(c)) < dt * 1.2)
        c[stop, 3] = 0
        c[:, 1] += c[:, 3] * dt
        trail = (c[:, 3] > 0) & (rng.random(len(c)) < dt * 2.5) & (c[:, 2] > 1.6)
        if trail.any() and len(c) < self.MAX_CLING:
            bits = np.stack([c[trail, 0] + rng.normal(0, 0.6, trail.sum()), c[trail, 1] - c[trail, 2],
                             np.full(trail.sum(), 0.8), np.zeros(trail.sum())], axis=1)
            c[trail, 2] = np.maximum(1.0, c[trail, 2] * 0.97)
            c = np.vstack([c, bits])
        # reaching the water (or a wave reaching them) absorbs them
        wet = c[:, 1] + c[:, 2] >= self.surface_at(c[:, 0])
        for x, r in c[wet][:, [0, 2]]:
            self._dip(x, 0.05 * r)
        self.cling = c[~wet]

    def _step_bubbles(self, dt):
        b = self.bub
        if not len(b):
            return
        flow = np.interp(b[:, 0], np.arange(self.N + 1) * self.dx, self.u)
        b[:, 3] += dt * 9
        b[:, 0] += (flow * 0.5 + np.sin(b[:, 3]) * 14) * dt
        b[:, 1] -= (35 + 20 * b[:, 2]) * dt
        np.clip(b[:, 0], 3, self.W - 3, out=b[:, 0])
        popped = b[:, 1] - b[:, 2] <= self.surface_at(b[:, 0])
        for x, r in b[popped][:, [0, 2]]:
            self._dip(x, -0.05 * r)
        self.bub = b[~popped]


def drop_sprite(r: float, kind: str) -> QPixmap:
    """A water drop on glass (dark rim, light focused at the bottom, sharp highlight)
    or an air bubble in water (bright rim, clear middle)."""
    d = int(math.ceil(r * 2 + 4))
    img = QImage(d * 2, d * 2, QImage.Format_ARGB32_Premultiplied)
    img.setDevicePixelRatio(2.0)
    img.fill(Qt.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.Antialiasing)
    c = QPointF(d / 2, d / 2)
    if kind == "drop":
        body = QLinearGradient(c.x(), c.y() - r, c.x(), c.y() + r)
        body.setColorAt(0.0, QColor(15, 25, 30, 120))
        body.setColorAt(0.55, QColor(120, 135, 140, 35))
        body.setColorAt(1.0, QColor(255, 255, 255, 165))
        p.setPen(Qt.NoPen)
        p.setBrush(body)
        p.drawEllipse(c, r, r)
        rim = QLinearGradient(c.x(), c.y() - r, c.x(), c.y() + r)
        rim.setColorAt(0.0, QColor(0, 0, 0, 150))
        rim.setColorAt(1.0, QColor(0, 0, 0, 40))
        p.setPen(QPen(rim, max(0.6, r * 0.2)))
        p.setBrush(Qt.NoBrush)
        p.drawEllipse(c, r - max(0.3, r * 0.1), r - max(0.3, r * 0.1))
        p.setPen(QPen(QColor(255, 255, 255, 180), max(0.5, r * 0.2)))
        p.drawArc(QRectF(c.x() - r * 0.7, c.y() - r * 0.7, r * 1.4, r * 1.4), 205 * 16, 130 * 16)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(255, 255, 255, 240))
        p.drawEllipse(QPointF(c.x() - r * 0.35, c.y() - r * 0.42), r * 0.26, r * 0.18)
    else:
        body = QRadialGradient(c, r)
        body.setColorAt(0.0, QColor(255, 255, 255, 10))
        body.setColorAt(0.8, QColor(230, 245, 255, 40))
        body.setColorAt(1.0, QColor(255, 255, 255, 120))
        p.setPen(QPen(QColor(255, 255, 255, 175), max(0.5, r * 0.16)))
        p.setBrush(body)
        p.drawEllipse(c, r, r)
        p.setPen(QPen(QColor(0, 30, 50, 60), max(0.5, r * 0.14)))
        p.drawArc(QRectF(c.x() - r * 0.72, c.y() - r * 0.72, r * 1.44, r * 1.44), 210 * 16, 120 * 16)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(255, 255, 255, 235))
        p.drawEllipse(QPointF(c.x() - r * 0.38, c.y() - r * 0.38), r * 0.24, r * 0.2)
    p.end()
    return QPixmap.fromImage(img)


def rounded_polygon(points, r: float) -> QPainterPath:
    path = QPainterPath()
    n = len(points)
    for i in range(n):
        prev, cur, nxt = points[i - 1], points[i], points[(i + 1) % n]
        d1, d2 = prev - cur, nxt - cur
        l1, l2 = math.hypot(d1.x(), d1.y()), math.hypot(d2.x(), d2.y())
        a = cur + d1 * (min(r, l1 / 2) / l1)
        b = cur + d2 * (min(r, l2 / 2) / l2)
        if i == 0:
            path.moveTo(a)
        else:
            path.lineTo(a)
        path.quadTo(cur, b)
    path.closeSubpath()
    return path


def paint_pine(p: QPainter, base: QPointF, h: float):
    """A little snowy pine tree standing at `base`."""
    x, y0 = base.x(), base.y()
    p.setPen(Qt.NoPen)
    p.setBrush(QColor(92, 58, 34))
    p.drawRect(QRectF(x - h * 0.06, y0 - h * 0.16, h * 0.12, h * 0.16))
    for ty, by, hw in ((0.00, 0.46, 0.25), (0.22, 0.70, 0.33), (0.44, 0.90, 0.42)):
        top, bot = y0 - h + ty * h, y0 - h + by * h
        tier = QPainterPath()
        tier.moveTo(x, top)
        tier.lineTo(x + hw * h, bot)
        tier.lineTo(x - hw * h, bot)
        tier.closeSubpath()
        g = QLinearGradient(x - hw * h, 0, x + hw * h, 0)
        g.setColorAt(0.0, QColor(26, 92, 58))
        g.setColorAt(1.0, QColor(12, 58, 36))
        p.fillPath(tier, g)
        cap = QPainterPath()
        cap.moveTo(x, top)
        cap.lineTo(x + hw * h * 0.5, top + (bot - top) * 0.45)
        cap.lineTo(x - hw * h * 0.5, top + (bot - top) * 0.45)
        cap.closeSubpath()
        p.fillPath(cap, QColor(250, 252, 255, 235))


def flake_sprites() -> list[QPixmap]:
    sprites = []
    for r in (1.0, 1.6, 2.2, 2.8):
        d = int(math.ceil(r * 2 + 4))
        img = QImage(d * 2, d * 2, QImage.Format_ARGB32_Premultiplied)
        img.setDevicePixelRatio(2.0)
        img.fill(Qt.transparent)
        p = QPainter(img)
        p.setRenderHint(QPainter.Antialiasing)
        g = QRadialGradient(QPointF(d / 2, d / 2), r + 1.2)
        g.setColorAt(0.0, QColor(255, 255, 255, 250))
        g.setColorAt(0.55, QColor(245, 250, 255, 210))
        g.setColorAt(1.0, QColor(235, 245, 255, 0))
        p.setPen(Qt.NoPen)
        p.setBrush(g)
        p.drawEllipse(QPointF(d / 2, d / 2), r + 1.2, r + 1.2)
        p.end()
        sprites.append(QPixmap.fromImage(img))
    return sprites


def paint_speaker(p: QPainter, c: QPointF, level: float, color: QColor):
    """Speaker glyph with 0-3 sound waves, or a slash when muted."""
    p.save()
    p.setPen(Qt.NoPen)
    p.setBrush(color)
    body = QPainterPath()
    body.moveTo(c.x() - 7, c.y() - 2.6)
    body.lineTo(c.x() - 4, c.y() - 2.6)
    body.lineTo(c.x() + 0.5, c.y() - 6.5)
    body.lineTo(c.x() + 0.5, c.y() + 6.5)
    body.lineTo(c.x() - 4, c.y() + 2.6)
    body.lineTo(c.x() - 7, c.y() + 2.6)
    body.closeSubpath()
    p.drawPath(body)
    pen = QPen(color, 1.5)
    pen.setCapStyle(Qt.RoundCap)
    p.setPen(pen)
    p.setBrush(Qt.NoBrush)
    if level <= 0.001:
        p.drawLine(QPointF(c.x() + 3, c.y() - 3), QPointF(c.x() + 8.5, c.y() + 3))
        p.drawLine(QPointF(c.x() + 8.5, c.y() - 3), QPointF(c.x() + 3, c.y() + 3))
    else:
        waves = 1 + (level > 0.34) + (level > 0.67)
        for k in range(waves):
            rr = 3.5 + k * 3.2
            p.drawArc(QRectF(c.x() + 0.5 - rr, c.y() - rr, 2 * rr, 2 * rr), -45 * 16, 90 * 16)
    p.restore()


STYLES = {"glassy": "Glassy", "glossy": "Glossy Glass", "aero": "Frutiger Aero",
          "snow": "Snow Globe", "liquid": "Liquid", "turntable": "Record Player"}
OPAQUE_STYLES = {"turntable"}   # solid styles: no glass, so no background capture
POP_GLIDE_MS = 1250            # tray click: the pop-out's glide over and back (sine-out)
VOLUME_NOTCHES = 12             # Ctrl + scroll: wheel notches from silent to full (about one full turn)

# Record Player: the record lies on the deck, seen at an angle (vertical squash),
# and a tonearm pivoting at the back right plays it.
TT_CENTER = QPointF(112, 168)
TT_SCALE = 0.98                  # record size relative to the other styles
TT_TILT = 0.42                   # how flat the deck looks (1 = seen from straight above)
TT_PIVOT = (1.13, -0.42)         # tonearm pivot, in record radii from the spindle (deck plane)
TT_ARM = 1.05                    # tonearm length, in record radii


def leather_texture(w: int, h: int, dpr: float, seed: int = 5) -> QImage:
    """A fine pebbled grain for the case covering (light and dark specks, softened)."""
    pw, ph = round(w * dpr), round(h * dpr)
    rng = np.random.default_rng(seed)
    n = rng.normal(0, 1, (ph, pw)).astype(np.float32)
    n = _box_blur(_box_blur(n[..., None], 1, 0), 1, 1)[..., 0]
    n /= max(1e-6, float(np.abs(n).max()))
    out = np.zeros((ph, pw, 4), np.uint8)
    light = n > 0
    a = (np.abs(n) * 20).astype(np.uint8)
    out[..., 3] = a
    out[light, 0:3] = (a[light, None] * np.array([1, 1, 1])).astype(np.uint8)   # premultiplied white
    img = QImage(out.data, pw, ph, pw * 4, QImage.Format_ARGB32_Premultiplied).copy()
    img.setDevicePixelRatio(dpr)
    return img


def tonearm_geometry(r_target: float):
    """Tonearm angle (deck plane) that puts the stylus `r_target` (in record radii)
    from the spindle: the stylus sits where a circle around the pivot with the arm's
    length meets a circle around the spindle."""
    px, py = TT_PIVOT
    d = math.hypot(px, py)
    cos_a = (d * d + TT_ARM * TT_ARM - r_target * r_target) / (2 * d * TT_ARM)
    alpha = math.acos(max(-1.0, min(1.0, cos_a)))
    return math.atan2(-py, -px) - alpha



def paint_glossy_overlay(p: QPainter, shape, w: float, h: float, corner: float):
    """Glossy Glass card surface: a wet gloss over the top, light bouncing up from
    the bottom edge, a sparkle in the corner and a bright double rim."""
    outline = shape(0)
    band = QPainterPath()
    band.addEllipse(QPointF(w / 2, -h * 0.30), w * 0.98, h * 0.74)
    g = QLinearGradient(0, 0, 0, h * 0.44)
    g.setColorAt(0.0, QColor(255, 255, 255, 96))
    g.setColorAt(0.65, QColor(255, 255, 255, 26))
    g.setColorAt(1.0, QColor(255, 255, 255, 8))
    p.fillPath(band.intersected(outline), g)
    bounce = QRadialGradient(QPointF(w / 2, h * 1.08), w * 0.8)
    bounce.setColorAt(0.0, QColor(255, 255, 255, 46))
    bounce.setColorAt(1.0, QColor(255, 255, 255, 0))
    p.fillPath(outline, bounce)
    sx, sy = corner * 0.5, corner * 0.42
    spark = QRadialGradient(QPointF(sx, sy), 14)
    spark.setColorAt(0.0, QColor(255, 255, 255, 170))
    spark.setColorAt(1.0, QColor(255, 255, 255, 0))
    p.setPen(Qt.NoPen)
    p.setBrush(spark)
    p.drawEllipse(QPointF(sx, sy), 14, 14)
    paint_glass_edge(p, shape, rim=2.0)
    p.setBrush(Qt.NoBrush)
    p.setPen(QPen(QColor(255, 255, 255, 46), 1.0))
    p.drawPath(shape(2.5))


def glossy_text(p: QPainter, rect: QRectF, text: str, font: QFont, strength: float = 1.0):
    """Glossy lettering: split gradient (bright top half, cooler lower half), a crisp
    light edge and a soft shadow underneath."""
    fm = QFontMetricsF(font)
    x = rect.center().x() - fm.horizontalAdvance(text) / 2
    y = rect.center().y() + (fm.ascent() - fm.descent()) / 2
    path = QPainterPath()
    path.addText(x, y, font, text)
    top, bottom = y - fm.ascent() * 0.8, y + fm.descent() * 0.5
    p.save()
    p.setPen(Qt.NoPen)
    for dy, a in ((1.8, 55), (1.0, 90)):
        p.fillPath(path.translated(0, dy), QColor(0, 0, 0, int(a * strength)))
    outline = QPen(QColor(10, 14, 30, int(110 * strength)), 1.6)   # keeps it readable on bright backdrops
    outline.setJoinStyle(Qt.RoundJoin)
    p.strokePath(path, outline)
    body = QLinearGradient(0, top, 0, bottom)
    body.setColorAt(0.00, QColor(255, 255, 255, int(255 * strength)))
    body.setColorAt(0.47, QColor(246, 250, 255, int(250 * strength)))
    body.setColorAt(0.53, QColor(196, 212, 236, int(240 * strength)))
    body.setColorAt(1.00, QColor(226, 236, 252, int(245 * strength)))
    p.fillPath(path, body)
    p.setPen(QPen(QColor(255, 255, 255, int(90 * strength)), 0.5))
    p.setBrush(Qt.NoBrush)
    p.drawPath(path)
    p.restore()





def render_aero_bg(w: int, h: int, dpr: float, radius: float) -> QPixmap:
    """Frutiger Aero card: sky-to-meadow gradient, sun glow, a light ribbon,
    soap bubbles, a big glossy top highlight and the dark-outside/white-inside Aero border."""
    pm = QPixmap(round(w * dpr), round(h * dpr))
    pm.setDevicePixelRatio(dpr)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHints(QPainter.Antialiasing | QPainter.SmoothPixmapTransform)
    rect = QRectF(0, 0, w, h)
    card = _rounded(rect.adjusted(0.5, 0.5, -0.5, -0.5), radius)
    p.setClipPath(card)

    sky = QLinearGradient(0, 0, 0, h)
    sky.setColorAt(0.00, QColor(140, 214, 255, 232))
    sky.setColorAt(0.40, QColor(52, 162, 238, 222))
    sky.setColorAt(0.74, QColor(30, 136, 214, 222))
    sky.setColorAt(0.88, QColor(64, 182, 150, 226))
    sky.setColorAt(1.00, QColor(142, 214, 70, 236))
    p.fillRect(rect, sky)

    sun = QRadialGradient(QPointF(w * 0.12, h * 0.04), w * 0.85)
    sun.setColorAt(0.0, QColor(255, 255, 240, 130))
    sun.setColorAt(0.5, QColor(255, 255, 255, 40))
    sun.setColorAt(1.0, QColor(255, 255, 255, 0))
    p.fillRect(rect, sun)

    # rolling hills
    p.setPen(Qt.NoPen)
    for cx, rx, ry, col in ((w * 0.25, w * 0.75, h * 0.10, QColor(120, 205, 60, 200)),
                            (w * 0.95, w * 0.65, h * 0.085, QColor(90, 190, 70, 210))):
        hill = QRadialGradient(QPointF(cx, h + ry * 0.35), rx)
        hill.setColorAt(0.0, col)
        hill.setColorAt(1.0, QColor(col.red(), col.green(), col.blue(), 120))
        p.setBrush(hill)
        p.drawEllipse(QPointF(cx, h + ry * 0.35), rx, ry * 1.4)

    # light ribbons
    for off, alpha in ((0.0, 120), (0.06, 70)):
        band = QPainterPath()
        band.moveTo(-10, h * (0.66 + off))
        band.cubicTo(w * 0.30, h * (0.52 + off), w * 0.62, h * (0.86 + off), w + 10, h * (0.58 + off))
        band.lineTo(w + 10, h * (0.62 + off))
        band.cubicTo(w * 0.62, h * (0.92 + off), w * 0.30, h * (0.58 + off), -10, h * (0.71 + off))
        band.closeSubpath()
        g = QLinearGradient(0, 0, w, 0)
        g.setColorAt(0.0, QColor(255, 255, 255, 0))
        g.setColorAt(0.45, QColor(255, 255, 255, alpha))
        g.setColorAt(1.0, QColor(210, 255, 255, 0))
        p.fillPath(band, g)

    # soap bubbles
    rng = random.Random(11)
    for _ in range(11):
        bc = QPointF(rng.uniform(8, w - 8), rng.uniform(10, h - 10))
        br = rng.uniform(4, 15)
        body = QRadialGradient(bc, br)
        body.setColorAt(0.0, QColor(255, 255, 255, 6))
        body.setColorAt(0.75, QColor(220, 250, 255, 30))
        body.setColorAt(1.0, QColor(255, 255, 255, 120))
        p.setBrush(body)
        p.setPen(QPen(QColor(255, 255, 255, 110), 0.8))
        p.drawEllipse(bc, br, br)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(255, 255, 255, 190))
        p.drawEllipse(bc + QPointF(-br * 0.38, -br * 0.4), br * 0.28, br * 0.17)

    # the classic glossy top half
    top = QPainterPath()
    top.addRect(QRectF(0, 0, w, h * 0.47))
    curve = QPainterPath()
    curve.addEllipse(QPointF(w / 2, -h * 0.62), w * 1.15, h * 1.09)
    gloss = QLinearGradient(0, 0, 0, h * 0.47)
    gloss.setColorAt(0.0, QColor(255, 255, 255, 120))
    gloss.setColorAt(1.0, QColor(255, 255, 255, 18))
    p.fillPath(top.intersected(curve), gloss)

    p.setClipping(False)
    p.setBrush(Qt.NoBrush)
    p.setPen(QPen(QColor(8, 52, 104, 190), 1.0))
    p.drawPath(card)
    p.setPen(QPen(QColor(255, 255, 255, 200), 1.0))
    p.drawPath(_rounded(rect.adjusted(1.5, 1.5, -1.5, -1.5), radius - 1))
    p.end()
    return pm


def _aero_gloss(p: QPainter, rect: QRectF, path: QPainterPath, top: float = 0.5):
    """White gloss over the upper part of a shape, with the soft lower edge Aero buttons have."""
    hi = QRectF(rect.left() + rect.width() * 0.08, rect.top() + rect.height() * 0.04,
                rect.width() * 0.84, rect.height() * top)
    g = QLinearGradient(hi.topLeft(), hi.bottomLeft())
    g.setColorAt(0.0, QColor(255, 255, 255, 230))
    g.setColorAt(1.0, QColor(255, 255, 255, 35))
    shine = QPainterPath()
    shine.addRoundedRect(hi, hi.height() / 2, hi.height() / 2)
    p.fillPath(shine.intersected(path), g)


def paint_aero_orb(p: QPainter, c: QPointF, r: float, hover: bool):
    """A glossy blue orb button."""
    rect = QRectF(c.x() - r, c.y() - r, 2 * r, 2 * r)
    orb = QPainterPath()
    orb.addEllipse(c, r, r)
    body = QRadialGradient(c + QPointF(0, r * 0.55), r * 1.35)
    body.setColorAt(0.0, QColor(150, 240, 255) if hover else QColor(110, 225, 255))
    body.setColorAt(0.55, QColor(32, 150, 230))
    body.setColorAt(1.0, QColor(8, 70, 150))
    p.setPen(QPen(QColor(6, 46, 96, 220), 1.0))
    p.setBrush(body)
    p.drawEllipse(c, r, r)
    _aero_gloss(p, rect, orb, top=0.48)


def paint_aero_pill(p: QPainter, rect: QRectF):
    pill = _rounded(rect, rect.height() / 2)
    body = QLinearGradient(rect.topLeft(), rect.bottomLeft())
    body.setColorAt(0.0, QColor(90, 200, 250))
    body.setColorAt(1.0, QColor(16, 104, 190))
    p.setPen(QPen(QColor(6, 46, 96, 210), 1.0))
    p.setBrush(body)
    p.drawPath(pill)
    _aero_gloss(p, rect, pill, top=0.5)


def paint_aero_disc_gloss(p: QPainter, c: QPointF, r: float):
    """A glassy dome shine across the top of the record."""
    disc = QPainterPath()
    disc.addEllipse(c, r, r)
    dome = QPainterPath()
    dome.addEllipse(QPointF(c.x(), c.y() - r * 0.42), r * 0.86, r * 0.6)
    g = QLinearGradient(QPointF(c.x(), c.y() - r), QPointF(c.x(), c.y() + r * 0.15))
    g.setColorAt(0.0, QColor(255, 255, 255, 120))
    g.setColorAt(1.0, QColor(255, 255, 255, 0))
    p.fillPath(dome.intersected(disc), g)


def glow_text(p: QPainter, rect: QRectF, flags, text: str, color: QColor, glow: QColor):
    """Vista-style text: white letters with a soft blue glow around them."""
    p.setPen(glow)
    for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1), (-1, -1), (1, 1), (-1, 1), (1, -1), (0, 2)):
        p.drawText(rect.translated(dx, dy), flags, text)
    p.setPen(color)
    p.drawText(rect, flags, text)


class DiscCard(QWidget):
    """The glass card: spinning CD, pop-out button, title and time. Used for the
    tray panel and for the always-on-top pop-out."""

    W, H = 252, 318
    PAD, D = 22, 196
    BTN_R = 12
    BTN_GAP = 32     # centre to centre: pop-out and shuffle side by side under the record
    RADIUS = 10      # Frutiger Aero corners
    CORNER = 44      # Glassy squircle corner extent

    popToggle = Signal()
    shuffleToggle = Signal()
    volumeWheel = Signal(float)
    fileDropped = Signal(list)
    deactivated = Signal()
    moved = Signal(QPoint)

    def __init__(self, player: Player, popout: bool):
        super().__init__(None, Qt.Tool | Qt.FramelessWindowHint | Qt.NoDropShadowWindowHint
                         | Qt.WindowStaysOnTopHint)
        self.player = player
        self.popout = popout
        self.popped = False        # panel only: whether the pop-out is showing
        self.preview = False       # shown by a drag over the tray icon (not activated)
        self.summoned = False      # pop-out only: brought over by a tray click
        self._appear = 1.0
        self._hover_disc = False
        self._hover_btn = False
        self._hover_shuf = False
        self._drop_hover = False
        self._press = None
        self._press_win = None
        self._dragged = False
        self._slide = QVariantAnimation(self)
        self._slide.valueChanged.connect(self._slide_step)
        self._slide.finished.connect(self._slide_done)
        self._slide_from = QPoint()
        self._slide_to = QPoint()
        self._slide_hide = False
        self.ghosted = False
        self._ghost = 0.0
        self._ghost_anim = QVariantAnimation(self)
        self._ghost_anim.valueChanged.connect(self._ghost_step)

        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setMouseTracking(True)
        self.setAcceptDrops(True)
        self.setFixedSize(self.W, self.H)
        self.setWindowTitle(APP_NAME)
        self._aero_bg: QPixmap | None = None
        self._glide = QTimer(self)        # eases the pop-out toward the cursor while dragging
        self._glide.setTimerType(Qt.PreciseTimer)
        self._glide.setInterval(7)
        self._glide.timeout.connect(self._glide_step)
        self._glide_to = QPointF()
        self._glide_at = QPointF()
        self._glide_t = 0.0
        self._grab_offset = QPointF()
        self.bouncy = False                # pop-out keeps the release momentum and bounces off screen edges
        self._drag_trail: list[tuple[float, QPointF]] = []
        self._throw = QTimer(self)
        self._throw.setTimerType(Qt.PreciseTimer)
        self._throw.setInterval(7)
        self._throw.timeout.connect(self._throw_step)
        self._throw_at = QPointF()
        self._throw_v = QPointF()
        self._throw_t = 0.0
        self._body_key = None
        self._body_img: QImage | None = None
        self._decor_key = None
        self._decor_img: QImage | None = None
        self._lens_serial = -1
        self._tint_key = None
        self._tint_img: QImage | None = None
        self._masks: dict = {}
        self._text_key = None
        self._text_img: QImage | None = None
        self.glass = False   # winId() below already sends events that read it
        self.glass = prepare_glass_window(int(self.winId()))
        self.backdrop = Backdrop(self)
        self.backdrop.updated.connect(self._backdrop_updated)
        self._lens_layer: QImage | None = None
        self._edge_mask: QImage | None = None
        self._shape_mask: tuple | None = None

        player.tick.connect(self.update)
        player.changed.connect(self.update)
        player.changed.connect(self._wake_sim)

        # physics styles (Snow Globe, Liquid) feel the card's own movement
        self._sim_timer = QTimer(self)
        self._sim_timer.setTimerType(Qt.PreciseTimer)
        self._sim_timer.setInterval(16)
        self._sim_timer.timeout.connect(self._sim_step)
        self._snow: SnowSim | None = None
        self._water: WaterSim | None = None
        self._sim_t = 0.0
        self._win_prev = QPointF()
        self._win_v = QPointF()
        self._accel = (0.0, 0.0)
        self._flakes = flake_sprites()
        self._drop_sprites: dict = {}
        self._tt_key = None
        self._tt_img: QImage | None = None
        self._leather: QImage | None = None
        self._arm_t = 0.0           # 0 = tonearm on its rest, 1 = on the record
        self._scrub: dict | None = None   # Ctrl + drag on the record
        self._scrub_alpha = 0.0
        self._scrub_clock = time.perf_counter()
        self._marquee = None          # (strip image, rect, overflow) when the title doesn't fit
        self._marquee_title = None
        self._marquee_t0 = time.perf_counter()
        self._arm_clock = time.perf_counter()
        self._scene_key = None
        self._scene_back: QImage | None = None
        self._scene_front_key = None
        self._scene_front: QImage | None = None

    # --- geometry
    def disc_center(self) -> QPointF:
        if self.player.style == "turntable":
            return QPointF(TT_CENTER)
        return QPointF(self.W / 2, self.PAD + self.D / 2)

    def btn_center(self) -> QPointF:
        if self.player.style == "turntable":
            return QPointF(229, 207)          # a knob on the deck
        return QPointF(self.W / 2 - self.BTN_GAP / 2, self.PAD + self.D + 21)

    def shuffle_center(self) -> QPointF:
        if self.player.style == "turntable":
            return QPointF(207, 215)          # the smaller knob beside it
        return QPointF(self.W / 2 + self.BTN_GAP / 2, self.PAD + self.D + 21)

    def _button_at(self, pos) -> str | None:
        """"pop", "shuffle" or None: whichever button is nearest, if the point is on one."""
        best, best_d = None, 0.0
        tt = self.player.style == "turntable"
        for name, c, r in (("pop", self.btn_center(), self.BTN_R + 4),
                           ("shuffle", self.shuffle_center(), 11 if tt else self.BTN_R + 4)):
            d = math.hypot(QPointF(pos).x() - c.x(), QPointF(pos).y() - c.y())
            if d <= r and (best is None or d < best_d):
                best, best_d = name, d
        return best

    def _disc_scale(self) -> float:
        return {"snow": SNOW_DISC_SCALE, "turntable": TT_SCALE}.get(self.player.style, 1.0)

    def _disc_tilt(self) -> float:
        return TT_TILT if self.player.style == "turntable" else 1.0

    def _in_disc(self, pos) -> bool:
        d = QPointF(pos) - self.disc_center()
        r = self.D / 2 * self._disc_scale()
        return (d.x() / r) ** 2 + (d.y() / (r * self._disc_tilt())) ** 2 <= 1.0

    def _in_btn(self, pos) -> bool:
        return self._button_at(pos) == "pop"

    def _in_shuffle(self, pos) -> bool:
        return self._button_at(pos) == "shuffle"

    def _buttons_key(self) -> tuple:
        """Everything about the two buttons that changes how they're drawn."""
        return (self._hover_btn, self._hover_shuf, self.player.shuffle, self.popout or self.popped)

    # --- show / hide with a short slide
    def slide_in(self, target: QPoint, activate: bool):
        self._slide.stop()
        self._slide_hide = False
        self.setAttribute(Qt.WA_ShowWithoutActivating, not activate)
        self._slide_from = target + QPoint(0, 14)
        self._slide_to = target
        if not self.isVisible():
            self._appear = 0.0
            self.move(self._slide_from)
            self.show()
        self.raise_()
        if activate:
            self.activateWindow()
        self._run_slide(self._appear, 1.0, 240, QEasingCurve.OutCubic)

    def slide_out(self):
        if not self.isVisible() or self._slide_hide:
            return
        self._slide.stop()
        self._slide_hide = True
        self._slide_to = self.pos()
        self._slide_from = self.pos() + QPoint(0, 10)
        self._run_slide(self._appear, 0.0, 150, QEasingCurve.InCubic)

    def _run_slide(self, start, end, ms, curve):
        self._slide.setStartValue(float(start))
        self._slide.setEndValue(float(end))
        self._slide.setDuration(ms)
        self._slide.setEasingCurve(curve)
        self._slide.start()

    def wheelEvent(self, e):
        if e.modifiers() & Qt.ControlModifier:
            steps = e.angleDelta().y() / 120.0 or e.pixelDelta().y() / 40.0
            if steps:
                self.volumeWheel.emit(steps)
            e.accept()
        else:
            e.ignore()

    # --- physics styles
    def reset_sim(self):
        self._snow = self._water = None
        self._scene_key = self._scene_front_key = None
        self._wake_sim()

    def _wake_sim(self):
        if self.player.style in ("snow", "liquid") and self.isVisible() and not self._sim_timer.isActive():
            self._sim_t = time.perf_counter()
            self._win_prev = QPointF(self.pos())
            self._win_v = QPointF()
            self._sim_timer.start()

    def _sim_step(self):
        style = self.player.style
        if style not in ("snow", "liquid") or not self.isVisible():
            self._sim_timer.stop()
            return
        now = time.perf_counter()
        dt = min(0.05, max(0.001, now - self._sim_t))
        self._sim_t = now
        pos = QPointF(self.pos())
        v = (pos - self._win_prev) / dt
        a = (v - self._win_v) / dt
        self._win_prev, self._win_v = pos, v
        # moves arrive unevenly, so smooth the acceleration a little
        ax = max(-8000.0, min(8000.0, 0.5 * self._accel[0] + 0.5 * a.x()))
        ay = max(-8000.0, min(8000.0, 0.5 * self._accel[1] + 0.5 * a.y()))
        self._accel = (ax, ay)
        stir = self.player.rate
        if style == "snow":
            if self._snow is None:
                self._snow = SnowSim(GLOBE_R - 3)
            active = self._snow.step(dt, (ax, ay), stir)
        else:
            if self._water is None:
                self._water = WaterSim(self.W, self.H, self.H * WATER_LEVEL)
            active = self._water.step(dt, (ax, ay), stir)
        self.update()
        if not active and abs(v.x()) + abs(v.y()) < 1 and abs(ax) + abs(ay) < 1:
            self._sim_timer.stop()

    def _snow_back(self) -> QImage:
        """Inside the globe, behind the record: the water's tint, the snow bed and two pines."""
        key = (self.devicePixelRatioF(),)
        if key == self._scene_key and self._scene_back is not None:
            return self._scene_back
        img = self._layer()
        p = QPainter(img)
        p.setRenderHint(QPainter.Antialiasing)
        c, R = self.disc_center(), GLOBE_R
        globe = QPainterPath()
        globe.addEllipse(c, R, R)
        water = QRadialGradient(c + QPointF(0, -R * 0.2), R * 1.1)
        water.setColorAt(0.0, QColor(150, 190, 255, 26))
        water.setColorAt(1.0, QColor(50, 90, 190, 70))
        p.fillPath(globe, water)
        bed = QPainterPath()
        xs = np.linspace(-R, R, 64)
        ys = snow_ground_y(xs, R - 3)
        bed.moveTo(c.x() + xs[0], c.y() + ys[0])
        for x, y in zip(xs[1:], ys[1:]):
            bed.lineTo(c.x() + x, c.y() + y)
        bed.lineTo(c.x() + R, c.y() + R + 2)
        bed.lineTo(c.x() - R, c.y() + R + 2)
        bed.closeSubpath()
        bed = bed.intersected(globe)
        snow = QLinearGradient(0, c.y() + R * 0.45, 0, c.y() + R)
        snow.setColorAt(0.0, QColor(250, 252, 255))
        snow.setColorAt(1.0, QColor(186, 206, 236))
        p.fillPath(bed, snow)
        for side in (-1, 1):
            x = R * 0.68 * side
            gy = float(snow_ground_y(np.array([x]), R - 3)[0])
            paint_pine(p, QPointF(c.x() + x, c.y() + gy + 3), 30 if side < 0 else 24)
        p.end()
        self._scene_key, self._scene_back = key, img
        return img

    def _paint_snow(self, p: QPainter):
        sim = self._snow
        if sim is None:
            return
        c = self.disc_center()
        sprites = self._flakes
        for (x, y), size in zip(sim.p, sim.size):
            pm = sprites[min(3, int((size - 1.0) / 0.42))]
            w = pm.width() / pm.devicePixelRatio()
            p.drawPixmap(QPointF(c.x() + x - w / 2, c.y() + y - w / 2), pm)

    def _snow_front(self, vis: float) -> QImage:
        """The globe's glass, the red and gold base it stands on, button and badge."""
        pl = self.player
        key = (self.devicePixelRatioF(), self._buttons_key(), len(pl.queue), pl.index, bool(self._drop_hover))
        if key == self._scene_front_key and self._scene_front is not None:
            return self._scene_front
        img = self._layer()
        p = QPainter(img)
        p.setRenderHints(QPainter.Antialiasing | QPainter.TextAntialiasing)
        c, R = self.disc_center(), GLOBE_R
        globe = QPainterPath()
        globe.addEllipse(c, R, R)
        # glass: a broad crescent of light, a window reflection, a crisp rim
        crescent = QPainterPath()
        crescent.addEllipse(c, R - 3, R - 3)
        bite = QPainterPath()
        bite.addEllipse(c + QPointF(R * 0.16, R * 0.2), R * 0.98, R * 0.98)
        g = QLinearGradient(c + QPointF(-R, -R), c)
        g.setColorAt(0.0, QColor(255, 255, 255, 140))
        g.setColorAt(1.0, QColor(255, 255, 255, 10))
        p.fillPath(crescent.subtracted(bite), g)
        win = QRadialGradient(c + QPointF(R * 0.45, -R * 0.52), R * 0.2)
        win.setColorAt(0.0, QColor(255, 255, 255, 120))
        win.setColorAt(1.0, QColor(255, 255, 255, 0))
        p.setPen(Qt.NoPen)
        p.setBrush(win)
        p.drawEllipse(c + QPointF(R * 0.45, -R * 0.52), R * 0.2, R * 0.12)
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(QColor(0, 0, 0, 70), 1.0))
        p.drawEllipse(c, R + 0.6, R + 0.6)
        p.setPen(QPen(QColor(255, 255, 255, 170), 1.6))
        p.drawEllipse(c, R - 0.8, R - 0.8)

        # base: red lacquer, gold trim, a snow cap and holly
        cx = self.W / 2
        top_y, bot_y = c.y() + R * 0.70, self.H - 10.0
        thw, bhw = 86.0, 108.0
        corners = [QPointF(cx - thw, top_y), QPointF(cx + thw, top_y), QPointF(cx + bhw, bot_y), QPointF(cx - bhw, bot_y)]
        base = rounded_polygon(corners, 10)
        lacquer = QLinearGradient(0, top_y, 0, bot_y)
        lacquer.setColorAt(0.0, QColor(204, 34, 48))
        lacquer.setColorAt(0.55, QColor(150, 16, 30))
        lacquer.setColorAt(1.0, QColor(92, 8, 18))
        p.fillPath(base, lacquer)

        def band(y0, y1):
            def hw(y):
                return thw + (bhw - thw) * (y - top_y) / (bot_y - top_y)
            poly = QPainterPath()
            poly.moveTo(cx - hw(y0) - 2, y0)
            poly.lineTo(cx + hw(y0) + 2, y0)
            poly.lineTo(cx + hw(y1) + 2, y1)
            poly.lineTo(cx - hw(y1) - 2, y1)
            poly.closeSubpath()
            return poly.intersected(base)

        gold = QLinearGradient(0, 0, 0, 1)
        gold.setCoordinateMode(QLinearGradient.ObjectMode)
        gold.setColorAt(0.0, QColor(255, 232, 150))
        gold.setColorAt(0.5, QColor(222, 170, 60))
        gold.setColorAt(1.0, QColor(150, 100, 24))
        p.fillPath(band(top_y + 9, top_y + 16), gold)
        p.fillPath(band(bot_y - 11, bot_y - 5), gold)
        shine = QLinearGradient(0, top_y, 0, top_y + (bot_y - top_y) * 0.45)
        shine.setColorAt(0.0, QColor(255, 255, 255, 70))
        shine.setColorAt(1.0, QColor(255, 255, 255, 0))
        p.fillPath(base, shine)
        cap = QPainterPath()
        cap.moveTo(cx - thw + 2, top_y + 3)
        x = cx - thw + 2
        k = 0
        while x < cx + thw - 2:
            nx = min(cx + thw - 2, x + 11)
            p_y = top_y - (4.5 if k % 2 else 2.5)
            cap.quadTo(QPointF((x + nx) / 2, p_y), QPointF(nx, top_y + 1))
            x, k = nx, k + 1
        cap.lineTo(cx + thw - 2, top_y + 6)
        cap.lineTo(cx - thw + 2, top_y + 6)
        cap.closeSubpath()
        snowcap = QLinearGradient(0, top_y - 5, 0, top_y + 6)
        snowcap.setColorAt(0.0, QColor(255, 255, 255))
        snowcap.setColorAt(1.0, QColor(205, 222, 245))
        p.fillPath(cap, snowcap)
        hx, hy = cx - thw + 20, top_y + 12
        p.setPen(Qt.NoPen)
        for ang in (-35, 35):
            leaf = QPainterPath()
            leaf.addEllipse(QPointF(0, 0), 8, 3.4)
            tr = QTransform()
            tr.translate(hx + (6 if ang > 0 else -6), hy - 2)
            tr.rotate(ang)
            p.fillPath(tr.map(leaf), QColor(22, 110, 54))
        for dx, dy in ((-2.5, 0.5), (2.5, 0.5), (0, -2.5)):
            berry = QRadialGradient(QPointF(hx + dx - 0.8, hy + dy - 0.8), 2.8)
            berry.setColorAt(0.0, QColor(255, 120, 120))
            berry.setColorAt(1.0, QColor(170, 0, 20))
            p.setBrush(berry)
            p.drawEllipse(QPointF(hx + dx, hy + dy), 2.4, 2.4)

        paint_glass_edge(p, self._card_shape(), rim=1.8 if self._drop_hover else 1.0)
        r = self.BTN_R
        for bc, lit in ((self.btn_center(), self._hover_btn), (self.shuffle_center(), self._hover_shuf or pl.shuffle)):
            paint_glass_chip(p, squircle_shape(QRectF(bc.x() - r, bc.y() - r, 2 * r, 2 * r), r, n=4.0),
                             fill=46 if lit else 24, rim=1.3 if lit else 1.0)
        self._paint_pop_glyph(p, self.btn_center(), inward=self.popout or self.popped)
        self._paint_shuffle_glyph(p, self.shuffle_center(), pl.shuffle)
        if len(pl.queue) > 1:
            bf = ui_font(8, QFont.DemiBold)
            label = f"{pl.index + 1} / {len(pl.queue)}"
            bw = QFontMetrics(bf).horizontalAdvance(label) + 16
            pill_rect = QRectF(self.W - bw - 14, 14, bw, 20)
            p.setFont(bf)
            paint_glass_chip(p, squircle_shape(pill_rect, pill_rect.height() / 2), fill=24)
            shadow_text(p, pill_rect, Qt.AlignCenter, label, QColor(255, 255, 255, 235))
        p.end()
        self._scene_front_key, self._scene_front = key, img
        return img

    def _sprite(self, kind: str, r: float) -> QPixmap:
        key = (kind, round(r * 2) / 2)
        pm = self._drop_sprites.get(key)
        if pm is None:
            pm = self._drop_sprites[key] = drop_sprite(key[1], kind)
        return pm

    def _blit(self, p: QPainter, kind: str, x: float, y: float, r: float):
        pm = self._sprite(kind, r)
        w = pm.width() / pm.devicePixelRatio()
        p.drawPixmap(QPointF(x - w / 2, y - w / 2), pm)

    def _paint_water(self, p: QPainter, scene: QImage):
        """Clear water: what's under the surface is the card itself, magnified and a
        little darker toward the bottom, with a crisp waterline; then the bubbles in
        it and the drops on the glass."""
        sim = self._water
        if sim is None:
            xs, ys = np.linspace(0, self.W, 8), np.full(8, self.H * WATER_LEVEL)
        else:
            xs, ys = sim.x, sim.surface_y()
        surface = QPainterPath()
        surface.moveTo(-2, float(ys[0]))
        surface.lineTo(float(xs[0]), float(ys[0]))
        for i in range(1, len(xs)):   # smooth curve through the column tops
            mx, my = (xs[i - 1] + xs[i]) / 2, (ys[i - 1] + ys[i]) / 2
            surface.quadTo(QPointF(float(xs[i - 1]), float(ys[i - 1])), QPointF(float(mx), float(my)))
        surface.lineTo(self.W + 2, float(ys[-1]))
        body = QPainterPath(surface)
        body.lineTo(self.W + 2, self.H + 2)
        body.lineTo(-2, self.H + 2)
        body.closeSubpath()

        water = self._layer()
        wp = QPainter(water)
        wp.setRenderHints(QPainter.Antialiasing | QPainter.SmoothPixmapTransform)
        # refraction: the scene behind the water, magnified about the bottom centre
        wp.save()
        wp.translate(self.W / 2, self.H)
        wp.scale(1.07, 1.07)
        wp.translate(-self.W / 2, -self.H)
        wp.drawImage(0, 0, scene)
        wp.restore()
        top = float(ys.min())
        tint = QLinearGradient(0, top, 0, self.H)
        tint.setColorAt(0.0, QColor(175, 228, 236, 34))
        tint.setColorAt(0.5, QColor(120, 160, 175, 46))
        tint.setColorAt(1.0, QColor(30, 45, 58, 110))
        wp.fillRect(self.rect(), tint)
        if sim is not None:
            for x, y, r, _ in sim.bub:
                self._blit(wp, "bubble", x, y, r)
        wp.setCompositionMode(QPainter.CompositionMode_DestinationIn)
        wp.setPen(Qt.NoPen)
        wp.setBrush(Qt.black)
        wp.drawPath(body)
        wp.end()
        p.drawImage(0, 0, water)

        # the waterline: light just under it, a thin dark meniscus, a bright edge
        line = self._layer()
        lp = QPainter(line)
        lp.setRenderHint(QPainter.Antialiasing)
        lp.setPen(QPen(QColor(220, 245, 255, 34), 9))
        lp.drawPath(surface.translated(0, 6))
        lp.setPen(QPen(QColor(0, 20, 30, 70), 1.0))
        lp.drawPath(surface.translated(0, 1.6))
        lp.setPen(QPen(QColor(255, 255, 255, 235), 1.4))
        lp.drawPath(surface)
        if sim is not None:
            for x, y, r, _ in sim.cling:
                self._blit(lp, "drop", x, y, r)
            for x, y, _, _, r in sim.flying:
                self._blit(lp, "drop", x, y, r)
        lp.setCompositionMode(QPainter.CompositionMode_DestinationIn)
        card = self._card_path()
        lp.drawImage(0, 0, self._mask("card", lambda m: m.drawPath(card)))
        lp.end()
        p.drawImage(0, 0, line)

    def moveEvent(self, e):
        self._wake_sim()
        if self.glass and self.isVisible():
            self.update()   # redraw the frost at its new offset; the worker re-captures on its own
        super().moveEvent(e)

    GLIDE_TAU = 0.022   # seconds; how softly the pop-out follows the cursor
    THROW_FRICTION = 1.15   # per second: how quickly a thrown pop-out slows down
    THROW_BOUNCE = 0.72     # share of speed kept when it bounces off a screen edge

    def _release_velocity(self) -> QPointF:
        """Cursor speed over the last ~100 ms of the drag (px/s); zero if it was held still."""
        now = time.perf_counter()
        trail = [s for s in self._drag_trail if now - s[0] < 0.12]
        if len(trail) < 2 or now - trail[-1][0] > 0.08:
            return QPointF()
        (t0, p0), (t1, p1) = trail[0], trail[-1]
        if t1 - t0 < 0.01:
            return QPointF()
        v = (p1 - p0) / (t1 - t0)
        speed = math.hypot(v.x(), v.y())
        return v * (6000 / speed) if speed > 6000 else v

    def _throw_step(self):
        now = time.perf_counter()
        dt = min(0.03, now - self._throw_t)
        self._throw_t = now
        self._throw_at += self._throw_v * dt
        self._throw_v *= math.exp(-self.THROW_FRICTION * dt)
        screen = QGuiApplication.screenAt(self.frameGeometry().center()) or self.screen()
        area = screen.availableGeometry()
        left, top = float(area.left()), float(area.top())
        right, bottom = float(area.right() - self.width() + 1), float(area.bottom() - self.height() + 1)
        x, y = self._throw_at.x(), self._throw_at.y()
        vx, vy = self._throw_v.x(), self._throw_v.y()
        if x < left:
            x, vx = left + (left - x), abs(vx) * self.THROW_BOUNCE
        elif x > right:
            x, vx = right - (x - right), -abs(vx) * self.THROW_BOUNCE
        if y < top:
            y, vy = top + (top - y), abs(vy) * self.THROW_BOUNCE
        elif y > bottom:
            y, vy = bottom - (y - bottom), -abs(vy) * self.THROW_BOUNCE
        self._throw_at, self._throw_v = QPointF(x, y), QPointF(vx, vy)
        self.move(self._throw_at.toPoint())
        if math.hypot(vx, vy) < 25:
            self._throw.stop()
            self.moved.emit(self.pos())

    def _glide_step(self):
        now = time.perf_counter()
        dt = min(0.05, now - self._glide_t)
        self._glide_t = now
        k = 1 - math.exp(-dt / self.GLIDE_TAU)
        self._glide_at += (self._glide_to - self._glide_at) * k
        rest = self._glide_to - self._glide_at
        if self._press is None and abs(rest.x()) < 0.5 and abs(rest.y()) < 0.5:
            self._glide_at = QPointF(self._glide_to)
            self._glide.stop()
            self.move(self._glide_at.toPoint())
            self.moved.emit(self.pos())
            return
        self.move(self._glide_at.toPoint())

    def _slide_step(self, t):
        self._appear = float(t)
        f, to = self._slide_from, self._slide_to
        self.move(QPoint(round(f.x() + (to.x() - f.x()) * t), round(f.y() + (to.y() - f.y()) * t)))
        self.update()

    def set_ghost(self, on: bool):
        """Caps Lock peek: fade almost away and let the mouse pass straight through."""
        if on == self.ghosted:
            return
        self.ghosted = on
        set_click_through(int(self.winId()), on)
        if on:
            self._hover_disc = self._hover_btn = self._hover_shuf = False
            self._press = None
        self._ghost_anim.stop()
        if on:
            _run(self._ghost_anim, self._ghost, 1.0, 320, QEasingCurve.OutCubic)
        else:
            _run(self._ghost_anim, self._ghost, 0.0, 420, QEasingCurve.OutBack)

    def _ghost_step(self, g):
        self._ghost = float(g)
        self.update()

    def _slide_done(self):
        if self._slide_hide:
            self._slide_hide = False
            self.hide()

    # --- events
    def showEvent(self, e):
        QTimer.singleShot(0, self._wake_sim)
        self.player.add_viewer()
        if self.glass and self.player.style not in OPAQUE_STYLES:
            self.backdrop.start()
        super().showEvent(e)

    def hideEvent(self, e):
        self.player.remove_viewer()
        self.backdrop.stop()
        self._drop_hover = False
        super().hideEvent(e)

    def event(self, e):
        if e.type() == QEvent.WindowActivate:
            self.preview = False
        elif e.type() == QEvent.WindowDeactivate:
            self.deactivated.emit()
        return super().event(e)

    SCRUB_PX_PER_SONG = 240   # dragging this far (about the card's width) covers the whole song

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton and e.modifiers() & Qt.ControlModifier \
                and self._in_disc(e.position()) and self.player.path:
            # Ctrl + grab the record: drag right to skip ahead, left to go back
            # (a Ctrl + click that doesn't move goes back a song instead)
            self._scrub = {"x": e.globalPosition().x(), "ms": float(self.player.position()),
                           "angle": self.player.angle, "live": False}
            self.player.begin_scrub()
            self.setCursor(Qt.ClosedHandCursor)
            self.update()
            return
        if e.button() == Qt.LeftButton:
            if self._throw.isActive():   # catch it mid-flight
                self._throw.stop()
                self.moved.emit(self.pos())
            self._press = e.globalPosition().toPoint()
            self._grab_offset = e.globalPosition() - QPointF(self.pos())
            self._dragged = False
            self._drag_trail = [(time.perf_counter(), e.globalPosition())]

    def mouseMoveEvent(self, e):
        if self._scrub is not None:
            dx = e.globalPosition().x() - self._scrub["x"]
            if not self._scrub["live"] and abs(dx) <= 3:
                return   # still a click, not a drag
            self._scrub["live"] = True
            dur = self.player.duration()
            self.player.scrub_to(self._scrub["ms"] + dx * dur / self.SCRUB_PX_PER_SONG)
            self.player.angle = (self._scrub["angle"] + dx * 1.4) % 360   # the record turns under your hand
            self.update()
            return
        pos = e.position()
        btn = self._button_at(pos)
        hd, hb, hs = self._in_disc(pos), btn == "pop", btn == "shuffle"
        ctrl = bool(QGuiApplication.queryKeyboardModifiers() & Qt.ControlModifier)
        if (hd, hb, hs) != (self._hover_disc, self._hover_btn, self._hover_shuf) or hd:
            self._hover_disc, self._hover_btn, self._hover_shuf = hd, hb, hs
            self.setCursor(Qt.OpenHandCursor if hd and ctrl and self.player.path
                           else Qt.PointingHandCursor if hd or hb or hs else Qt.ArrowCursor)
            self.update()
        if self.popout and self._press is not None and e.buttons() & Qt.LeftButton:
            delta = e.globalPosition().toPoint() - self._press
            if self._dragged or delta.manhattanLength() > 4:
                self._dragged = True
                now = time.perf_counter()
                self._drag_trail.append((now, e.globalPosition()))
                self._drag_trail = [s for s in self._drag_trail if now - s[0] < 0.12]
                self._glide_to = e.globalPosition() - self._grab_offset
                if not self._glide.isActive():
                    self._glide_at = QPointF(self.pos())
                    self._glide_t = time.perf_counter()
                    self._glide.start()

    def mouseReleaseEvent(self, e):
        if self._scrub is not None and e.button() == Qt.LeftButton:
            clicked = not self._scrub["live"]
            self._scrub = None
            self.player.end_scrub()
            self.setCursor(Qt.PointingHandCursor)
            self.update()
            if clicked:
                self.player.skip(-1)
            return
        if e.button() == Qt.RightButton and e.modifiers() & Qt.ControlModifier:
            self.player.skip(+1)   # Ctrl + right click: next song
            return
        if e.button() != Qt.LeftButton or self._press is None:
            return
        self._press = None
        if self._dragged:
            self._glide_to = e.globalPosition() - self._grab_offset
            v = self._release_velocity()
            if self.bouncy and math.hypot(v.x(), v.y()) > 220:
                self._glide.stop()
                self._throw_at = QPointF(self.pos())
                self._throw_v = v
                self._throw_t = time.perf_counter()
                self._throw.start()
            return   # otherwise the glide settles on the target, then reports the new position
        btn = self._button_at(e.position())
        if e.modifiers() & Qt.ControlModifier:
            self.player.skip(-1)   # Ctrl + click: previous song
        elif btn == "pop":
            self.popToggle.emit()
        elif btn == "shuffle":
            self.shuffleToggle.emit()
        elif self._in_disc(e.position()):
            self.player.toggle()

    def leaveEvent(self, e):
        self._hover_disc = self._hover_btn = self._hover_shuf = False
        self.update()

    def dragEnterEvent(self, e):
        n = len(audio_paths_from_mime(e.mimeData()))
        if n:
            e.acceptProposedAction()
            self._drop_hover = n
            self.update()

    def dragLeaveEvent(self, e):
        self._drop_hover = False
        self.update()

    def dropEvent(self, e):
        paths = audio_paths_from_mime(e.mimeData())
        self._drop_hover = False
        if paths:
            e.acceptProposedAction()
            self.fileDropped.emit(paths)
        self.update()

    # --- painting
    def _card_shape(self):
        if self.player.style == "aero":
            return rounded_shape(QRectF(self.rect()), self.RADIUS)
        if self.player.style == "turntable":
            return rounded_shape(QRectF(self.rect()), 16)
        return squircle_shape(QRectF(self.rect()), self.CORNER)

    def _card_path(self) -> QPainterPath:
        return self._card_shape()(0)

    def _layer(self) -> QImage:
        dpr = self.devicePixelRatioF()
        img = QImage(round(self.W * dpr), round(self.H * dpr), QImage.Format_ARGB32_Premultiplied)
        img.setDevicePixelRatio(dpr)
        img.fill(Qt.transparent)
        return img

    def _backdrop_updated(self):
        self.update()   # the lens layer is rebuilt lazily, once per painted frame at most

    def _lens(self) -> QImage | None:
        """Edge refraction: the near surroundings squeezed into the card, so near
        the rim you see light bent in from just outside it. Shown only in a band
        along the edge that fades toward the middle."""
        lens = self.backdrop.lens
        if lens is None or self.player.style == "aero":
            return None
        if self._lens_serial == self.backdrop.serial and self._lens_layer is not None:
            return self._lens_layer
        dpr = self.devicePixelRatioF()
        if self._edge_mask is None or self._edge_mask.devicePixelRatio() != dpr:
            mask = self._layer()
            mp = QPainter(mask)
            mp.setRenderHint(QPainter.Antialiasing)
            outline = self._card_path()
            for width, a in ((30, 26), (20, 40), (12, 70), (6, 110), (2, 150)):
                mp.setPen(QPen(QColor(0, 0, 0, a), width))
                mp.drawPath(outline)
            mp.end()
            self._edge_mask = mask
        layer = self._layer()
        lp = QPainter(layer)
        lp.setRenderHint(QPainter.SmoothPixmapTransform)
        lp.drawImage(QRectF(0, 0, self.W, self.H), lens)
        lp.setCompositionMode(QPainter.CompositionMode_DestinationIn)
        lp.drawImage(0, 0, self._edge_mask)
        lp.end()
        self._lens_layer, self._lens_serial = layer, self.backdrop.serial
        return layer

    def _tint(self, vis: float) -> QImage:
        """Tint, cover-coloured glow and inner rim light: depend on the track and
        style only, so they are cached instead of redrawn for every capture."""
        pl = self.player
        key = (round(vis, 3), bool(self._drop_hover), pl.style, pl.accent.rgba(), self.devicePixelRatioF())
        if key == self._tint_key and self._tint_img is not None:
            return self._tint_img
        glossy = pl.style == "glossy"
        img = self._layer()
        tp = QPainter(img)
        tp.setRenderHint(QPainter.Antialiasing)
        if pl.style == "liquid":
            sky = QLinearGradient(0, 0, 0, self.H)
            sky.setColorAt(0.0, QColor(70, 160, 172, 70))
            sky.setColorAt(0.45, QColor(205, 210, 214, 46))
            sky.setColorAt(1.0, QColor(36, 38, 44, 90))
            tp.fillRect(self.rect(), sky)
            tp.end()
            self._tint_key, self._tint_img = key, img
            return img
        if pl.style == "snow":   # a winter night behind the globe
            night = QLinearGradient(0, 0, 0, self.H)
            night.setColorAt(0.0, QColor(16, 30, 76, 150))
            night.setColorAt(1.0, QColor(44, 20, 70, 160))
            tp.fillRect(self.rect(), night)
        else:
            tp.fillRect(self.rect(), QColor(8, 8, 14, 26 if glossy or pl.style == "liquid" else 46))
        glow = QRadialGradient(self.disc_center(), self.D * 0.9)
        ac = QColor(pl.accent)
        ac.setAlpha(int((45 if glossy else 60) * vis))
        glow.setColorAt(0.0, ac)
        glow.setColorAt(1.0, QColor(ac.red(), ac.green(), ac.blue(), 0))
        tp.fillRect(self.rect(), glow)
        tp.setBrush(Qt.NoBrush)
        card = self._card_path()
        for width, a in ((16, 7), (8, 9)):   # light gathered just inside the rim
            tp.setPen(QPen(QColor(255, 255, 255, a), width))
            tp.drawPath(card)
        sheen = QLinearGradient(0, 0, 0, self.H)
        sheen.setColorAt(0.0, QColor(255, 255, 255, 30))
        sheen.setColorAt(0.3, QColor(255, 255, 255, 0))
        tp.fillRect(self.rect(), sheen)
        if self._drop_hover:
            tp.fillRect(self.rect(), QColor(255, 255, 255, 26))
        tp.end()
        self._tint_key, self._tint_img = key, img
        return img

    def _mask(self, name: str, draw) -> QImage:
        """Full-size alpha masks (DestinationIn only affects pixels the source covers,
        so masking needs an image that is transparent outside the shape)."""
        key = (name, self.player.style, self.devicePixelRatioF())
        hit = self._masks.get(key)
        if hit is None:
            hit = self._layer()
            mp = QPainter(hit)
            mp.setRenderHint(QPainter.Antialiasing)
            mp.setPen(Qt.NoPen)
            mp.setBrush(Qt.black)
            draw(mp)
            mp.end()
            self._masks[key] = hit
        return hit

    def _paint_body(self, p: QPainter, vis: float):
        """Blurred backdrop + refraction + tint, masked to the card outline with an
        antialiased fill (painter clipping would leave jagged corners)."""
        pl = self.player
        aero = pl.style == "aero"
        body = self._layer()
        bp = QPainter(body)
        bp.setRenderHints(QPainter.Antialiasing | QPainter.SmoothPixmapTransform)
        frost = self.backdrop.frost if self.glass else None
        fr = self.backdrop.frost_rect() if frost is not None else None
        if fr is not None:
            # anchored to the screen: while moving, the last capture stays where it was
            # taken; if the card outruns it, it slides along instead of leaving a bare patch
            fr.moveLeft(min(0.0, max(fr.left(), self.W - fr.width())))
            fr.moveTop(min(0.0, max(fr.top(), self.H - fr.height())))
            bp.fillRect(self.rect(), QColor(24, 24, 32))
            bp.drawImage(fr, frost)
            lens = self._lens()
            if lens is not None and not aero:
                bp.drawImage(0, 0, lens)
            if pl.style == "glossy":
                # the glass disc magnifies what lies behind it
                c, R = self.disc_center(), self.D / 2
                mag = self._layer()
                mp = QPainter(mag)
                mp.setRenderHint(QPainter.SmoothPixmapTransform)
                mp.translate(c)
                mp.scale(1.14, 1.14)
                mp.translate(-c)
                mp.drawImage(fr, frost)
                mp.setCompositionMode(QPainter.CompositionMode_DestinationIn)
                mp.resetTransform()
                mp.drawImage(0, 0, self._mask("disc", lambda m: m.drawEllipse(c, R, R)))
                mp.end()
                bp.drawImage(0, 0, mag)
        else:
            bp.fillRect(self.rect(), QColor(22, 22, 30, 238))
        if not aero:
            bp.drawImage(0, 0, self._tint(vis))
        bp.setCompositionMode(QPainter.CompositionMode_DestinationIn)
        card = self._card_path()
        bp.drawImage(0, 0, self._mask("card", lambda m: m.drawPath(card)))
        bp.end()
        p.drawImage(0, 0, body)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHints(QPainter.Antialiasing | QPainter.SmoothPixmapTransform | QPainter.TextAntialiasing)
        p.setOpacity(self._appear)
        g = self._ghost
        if g != 0:   # Caps Lock peek: fade to a faint ghost and sink back (springs past 0 on return)
            p.setOpacity(self._appear * (1 - 0.9 * min(max(g, 0.0), 1.0)))
            s = 1 - 0.045 * g
            p.translate(self.W / 2, self.H / 2)
            p.scale(s, s)
            p.translate(-self.W / 2, -self.H / 2)
        pl = self.player
        vis = max(0.0, min(1.0, pl.vis))
        base_opacity = p.opacity()

        # everything that doesn't spin comes from cached layers
        if pl.style == "turntable":
            p.drawImage(0, 0, self._turntable_back())
            self._paint_disc(p, base_opacity, vis)
            p.setOpacity(base_opacity)
            self._paint_tonearm(p)
            p.setOpacity(base_opacity * vis)
            self._paint_text(p)
            p.end()
            return
        if pl.style == "liquid":
            # draw the dry scene first: the water refracts it
            scene = self._layer()
            sp = QPainter(scene)
            sp.setRenderHints(QPainter.Antialiasing | QPainter.SmoothPixmapTransform)
            sp.drawImage(0, 0, self._body(vis))
            sp.drawImage(0, 0, self._decor(vis))
            self._paint_disc(sp, 1.0, vis)
            sp.end()
            p.drawImage(0, 0, scene)
            p.setOpacity(base_opacity)
            self._paint_water(p, scene)
            p.setOpacity(base_opacity * vis)
            self._paint_text(p)
            p.end()
            return
        p.drawImage(0, 0, self._body(vis))
        if pl.style == "snow":
            p.drawImage(0, 0, self._snow_back())
            self._paint_disc(p, base_opacity, vis)
            self._paint_snow(p)
            p.drawImage(0, 0, self._snow_front(vis))
        else:
            p.drawImage(0, 0, self._decor(vis))
            self._paint_disc(p, base_opacity, vis)

        p.setOpacity(base_opacity * vis)
        self._paint_text(p)
        p.end()

    def _paint_disc(self, p: QPainter, base_opacity: float, vis: float):
        pl = self.player
        R = self.D / 2
        c = self.disc_center()
        px = max(32, round(self.D * self._disc_scale() * self.devicePixelRatioF()))
        target = QRectF(-R, -R, self.D, self.D)
        p.save()
        p.setOpacity(base_opacity * vis)
        p.translate(c)
        s = (0.84 + 0.16 * pl.vis) * self._disc_scale()
        p.scale(s, s * self._disc_tilt())      # squashed when the record lies on a deck
        p.save()
        p.rotate(pl.angle)
        disc = pl.disc_pixmap(px)
        p.drawPixmap(target, disc, QRectF(disc.rect()))
        p.restore()
        sheen_pm = pl.sheen_pixmap(px)
        p.drawPixmap(target, sheen_pm, QRectF(sheen_pm.rect()))
        if pl.style == "aero":
            paint_aero_disc_gloss(p, QPointF(0, 0), R)
        if (self._hover_disc or not pl.want_playing) and pl.path and self._scrub is None:
            self._paint_hub_glyph(p, R * LENS_R, playing=pl.want_playing)
        self._paint_scrub_ring(p, R)
        p.restore()

    def _paint_scrub_ring(self, p: QPainter, R: float):
        """While scrubbing: a thin glossy ring around the record showing where you are."""
        now = time.perf_counter()
        dt = min(0.1, now - self._scrub_clock)
        self._scrub_clock = now
        target = 1.0 if self._scrub is not None else 0.0
        self._scrub_alpha += (target - self._scrub_alpha) * (1 - math.exp(-dt / 0.12))
        if abs(target - self._scrub_alpha) > 0.01:
            QTimer.singleShot(16, self.update)
        if self._scrub_alpha < 0.01:
            return
        dur = self.player.duration()
        frac = min(1.0, max(0.0, self.player.position() / dur)) if dur else 0.0
        ring = QRectF(-R - 5, -R - 5, 2 * R + 10, 2 * R + 10)
        p.save()
        p.setOpacity(p.opacity() * self._scrub_alpha)
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(QColor(0, 0, 0, 90), 5.0))
        p.drawEllipse(ring)
        p.setPen(QPen(QColor(255, 255, 255, 60), 3.0))
        p.drawEllipse(ring)
        pen = QPen(QColor(255, 255, 255, 240), 3.0)
        pen.setCapStyle(Qt.RoundCap)
        p.setPen(pen)
        p.drawArc(ring, 90 * 16, -int(frac * 360 * 16))
        p.restore()

    def _turntable_back(self) -> QImage:
        """The suitcase record player around the record: leatherette case, open lid
        with a recessed panel, chrome latches, black deck with platter, knobs and
        power light, and the speaker grille in front."""
        pl = self.player
        dpr = self.devicePixelRatioF()
        key = (dpr, self._buttons_key(), len(pl.queue), pl.index,
               bool(pl.want_playing and pl.path), bool(self._drop_hover))
        if key == self._tt_key and self._tt_img is not None:
            return self._tt_img
        if self._leather is None or self._leather.devicePixelRatio() != dpr:
            self._leather = leather_texture(self.W, self.H, dpr)
        W, H = float(self.W), float(self.H)
        img = self._layer()
        p = QPainter(img)
        p.setRenderHints(QPainter.Antialiasing | QPainter.SmoothPixmapTransform | QPainter.TextAntialiasing)
        case = self._card_path()

        # case: burgundy leatherette with a lighter piping along the edge
        body = QLinearGradient(0, 0, 0, H)
        body.setColorAt(0.0, QColor(132, 36, 58))
        body.setColorAt(0.5, QColor(112, 26, 46))
        body.setColorAt(1.0, QColor(84, 18, 34))
        p.fillPath(case, body)
        p.save()
        p.setClipPath(case)
        p.drawImage(0, 0, self._leather)
        p.restore()
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(QColor(255, 190, 200, 70), 1.2))
        p.drawPath(_rounded(QRectF(1.5, 1.5, W - 3, H - 3), 15))
        p.setPen(QPen(QColor(30, 0, 8, 160), 1.0))
        p.drawPath(case)

        # open lid: a recessed panel with a soft inner shadow
        lid = _rounded(QRectF(14, 12, W - 28, 96), 6)
        inner = QLinearGradient(0, 12, 0, 108)
        inner.setColorAt(0.0, QColor(96, 20, 38))
        inner.setColorAt(1.0, QColor(122, 30, 52))
        p.fillPath(lid, inner)
        p.save()
        p.setClipPath(lid)
        p.drawImage(0, 0, self._leather)
        for width, a in ((10, 26), (5, 40), (2, 70)):
            p.setPen(QPen(QColor(20, 0, 6, a), width))
            p.drawPath(lid)
        p.restore()
        p.setPen(QPen(QColor(255, 200, 210, 45), 1.0))
        p.drawLine(QPointF(20, 108.5), QPointF(W - 20, 108.5))

        # hinge line and the chrome latches at the sides
        p.setPen(QPen(QColor(25, 0, 8, 200), 2.0))
        p.drawLine(QPointF(4, 116), QPointF(W - 4, 116))
        for x in (3.0, W - 9.0):
            latch = _rounded(QRectF(x, 98, 6, 22), 2)
            chrome = QLinearGradient(x, 0, x + 6, 0)
            chrome.setColorAt(0.0, QColor(150, 150, 158))
            chrome.setColorAt(0.5, QColor(240, 240, 246))
            chrome.setColorAt(1.0, QColor(120, 120, 128))
            p.fillPath(latch, chrome)
            p.setPen(QPen(QColor(60, 60, 66), 0.8))
            p.drawPath(latch)
            p.setBrush(QColor(90, 90, 98))
            p.drawEllipse(QPointF(x + 3, 103), 1.2, 1.2)
            p.setBrush(Qt.NoBrush)

        # deck plate
        deck = _rounded(QRectF(10, 119, W - 20, 104), 5)
        plate = QLinearGradient(0, 119, 0, 223)
        plate.setColorAt(0.0, QColor(34, 34, 38))
        plate.setColorAt(1.0, QColor(16, 16, 19))
        p.fillPath(deck, plate)
        p.setPen(QPen(QColor(255, 255, 255, 22), 1.0))
        p.drawPath(deck)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(70, 70, 76))
        for sx, sy in ((16, 125), (W - 16, 125), (16, 217), (W - 16, 217)):
            p.drawEllipse(QPointF(sx, sy), 1.3, 1.3)

        # platter under the record, and the record's edge (it has thickness)
        c = QPointF(TT_CENTER)
        R = self.D / 2 * TT_SCALE
        p.setBrush(QColor(0, 0, 0, 120))
        p.drawEllipse(c + QPointF(3, 7), R * 1.03, R * 1.03 * TT_TILT)
        rim = QLinearGradient(c.x() - R, 0, c.x() + R, 0)
        rim.setColorAt(0.0, QColor(90, 90, 96))
        rim.setColorAt(0.5, QColor(170, 170, 178))
        rim.setColorAt(1.0, QColor(80, 80, 86))
        p.setBrush(rim)
        p.drawEllipse(c + QPointF(0, 4), R * 1.02, R * 1.02 * TT_TILT)
        p.setBrush(QColor(10, 10, 12))
        p.drawEllipse(c + QPointF(0, 2.2), R, R * TT_TILT)

        # tonearm rest and the pivot's base plate (the arm itself moves; see _paint_tonearm)
        rest = self._arm_point(1.18)
        p.setBrush(QColor(20, 20, 22))
        p.drawEllipse(rest + QPointF(0, 2), 4, 2)
        p.setPen(QPen(QColor(120, 120, 128), 2.0))
        p.drawLine(rest + QPointF(0, 1), rest + QPointF(0, -6))
        p.setPen(Qt.NoPen)
        pv = self._arm_point(None)
        base = QRadialGradient(pv, 15)
        base.setColorAt(0.0, QColor(70, 70, 76))
        base.setColorAt(1.0, QColor(28, 28, 32))
        p.setBrush(base)
        p.drawEllipse(pv, 15, 15 * 0.6)
        p.setPen(QPen(QColor(150, 150, 160, 120), 0.8))
        p.setBrush(Qt.NoBrush)
        p.drawEllipse(pv, 15, 15 * 0.6)

        # knobs (shuffle, and the front one is the pop-out button) and the power light
        bc, sc = self.btn_center(), self.shuffle_center()
        for kc, kr in ((sc, 7.5), (bc, 9.5)):
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(0, 0, 0, 120))
            p.drawEllipse(kc + QPointF(1, 2), kr, kr)
            knob = QRadialGradient(kc + QPointF(-kr * 0.3, -kr * 0.4), kr * 1.4)
            hot = self._hover_btn if kc == bc else self._hover_shuf
            knob.setColorAt(0.0, QColor(110, 110, 118) if hot else QColor(80, 80, 88))
            knob.setColorAt(1.0, QColor(18, 18, 22))
            p.setBrush(knob)
            p.drawEllipse(kc, kr, kr)
            p.setPen(QPen(QColor(200, 200, 210, 90), 0.8))
            p.setBrush(Qt.NoBrush)
            p.drawEllipse(kc, kr - 0.5, kr - 0.5)
        self._paint_pop_glyph(p, bc, inward=self.popout or self.popped)
        self._paint_shuffle_glyph(p, sc - QPointF(0, 1.2), pl.shuffle, scale=0.62)
        on = bool(pl.want_playing and pl.path)
        led = QColor(90, 255, 120) if on else QColor(200, 40, 40)
        glow = QRadialGradient(QPointF(207, 199), 6)
        glow.setColorAt(0.0, QColor(led.red(), led.green(), led.blue(), 160 if on else 90))
        glow.setColorAt(1.0, QColor(led.red(), led.green(), led.blue(), 0))
        p.setPen(Qt.NoPen)
        p.setBrush(glow)
        p.drawEllipse(QPointF(207, 199), 6, 6)
        p.setBrush(led)
        p.drawEllipse(QPointF(207, 199), 1.6, 1.6)

        # front: speaker grille with a metal mesh and a small wordmark
        grille = _rounded(QRectF(24, 236, W - 48, 66), 7)
        p.fillPath(grille, QColor(14, 12, 14))
        p.save()
        p.setClipPath(grille)
        mesh = QPen(QColor(205, 205, 212, 150), 1.0)
        p.setPen(mesh)
        x = 27.0
        while x < W - 24:
            p.drawLine(QPointF(x, 236), QPointF(x, 302))
            x += 5.0
        y = 239.0
        while y < 302:
            p.drawLine(QPointF(24, y), QPointF(W - 24, y))
            y += 5.0
        shade = QLinearGradient(0, 236, 0, 302)
        shade.setColorAt(0.0, QColor(255, 255, 255, 30))
        shade.setColorAt(0.5, QColor(0, 0, 0, 0))
        shade.setColorAt(1.0, QColor(0, 0, 0, 90))
        p.fillRect(QRectF(24, 236, W - 48, 66), shade)
        p.restore()
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(QColor(40, 0, 10, 220), 1.6))
        p.drawPath(grille)
        p.setPen(QPen(QColor(255, 190, 200, 40), 1.0))
        p.drawPath(_rounded(QRectF(22.5, 234.5, W - 45, 69), 8))
        mark = ui_font(6.5, QFont.Bold)
        mark.setLetterSpacing(QFont.AbsoluteSpacing, 2.2)
        p.setFont(mark)
        p.setPen(QColor(0, 0, 0, 200))
        p.fillRect(QRectF(30, 287, 72, 10), QColor(14, 12, 14))
        p.setPen(QColor(225, 225, 232))
        p.drawText(QRectF(30, 287, 72, 10), Qt.AlignCenter, "DISCTRAY")

        if len(pl.queue) > 1:   # playlist position, as a little brass tag on the lid
            bf = ui_font(7.5, QFont.Bold)
            label = f"{pl.index + 1} / {len(pl.queue)}"
            bw = QFontMetrics(bf).horizontalAdvance(label) + 14
            tag = QRectF(W - bw - 22, 20, bw, 16)
            brass = QLinearGradient(tag.topLeft(), tag.bottomLeft())
            brass.setColorAt(0.0, QColor(240, 214, 150))
            brass.setColorAt(1.0, QColor(176, 136, 62))
            p.setPen(QPen(QColor(90, 60, 20), 0.8))
            p.setBrush(brass)
            p.drawRoundedRect(tag, 4, 4)
            p.setFont(bf)
            p.setPen(QColor(60, 36, 10))
            p.drawText(tag, Qt.AlignCenter, label)
        if self._drop_hover:
            p.fillPath(case, QColor(255, 255, 255, 30))
        p.end()
        self._tt_key, self._tt_img = key, img
        return img

    def _arm_point(self, r_target):
        """Screen point of the tonearm pivot (r_target None) or of the stylus when it
        sits r_target record radii from the spindle."""
        R = self.D / 2 * TT_SCALE
        px, py = TT_PIVOT
        if r_target is None:
            x, y = px, py
        else:
            phi = tonearm_geometry(r_target)
            x, y = px + TT_ARM * math.cos(phi), py + TT_ARM * math.sin(phi)
        return QPointF(TT_CENTER.x() + x * R, TT_CENTER.y() + y * R * TT_TILT)

    def _paint_tonearm(self, p: QPainter):
        """The arm swings onto the record when playing and creeps inward as the song
        plays, like a stylus following the groove; on pause it lifts and goes back."""
        pl = self.player
        now = time.perf_counter()
        dt = min(0.1, now - self._arm_clock)
        self._arm_clock = now
        target = 1.0 if (pl.want_playing and pl.path) else 0.0
        self._arm_t += (target - self._arm_t) * (1 - math.exp(-dt / 0.28))
        if abs(target - self._arm_t) > 0.002:
            QTimer.singleShot(16, self.update)   # keep animating even when the record is still
        dur = pl.duration()
        progress = min(1.0, pl.position() / dur) if dur else 0.0
        groove = 0.92 - 0.52 * progress                       # lead-in at the edge, run-out near the label
        t = self._arm_t
        r = 1.18 + (groove - 1.18) * t
        lift = math.sin(math.pi * min(1.0, max(0.0, t))) * 7  # raised while swinging over
        pivot = self._arm_point(None)
        stylus = self._arm_point(r)
        top = pivot + QPointF(0, -11)                         # the arm sits on a short post
        tip = stylus + QPointF(0, -3 - lift)
        d = tip - top
        length = math.hypot(d.x(), d.y()) or 1.0
        ux, uy = d.x() / length, d.y() / length
        nx, ny = -uy, ux

        # shadow on the deck / record
        p.setPen(QPen(QColor(0, 0, 0, 90), 3.0, Qt.SolidLine, Qt.RoundCap))
        p.drawLine(pivot + QPointF(3, 3), stylus + QPointF(3 + lift * 0.4, 3 + lift * 0.6))
        # post and counterweight
        p.setPen(QPen(QColor(110, 110, 118), 4.0, Qt.SolidLine, Qt.FlatCap))
        p.drawLine(pivot, top)
        back = top - QPointF(ux, uy) * 15
        cw = QPainterPath()
        cw.moveTo(back + QPointF(nx, ny) * 5)
        cw.lineTo(back + QPointF(nx, ny) * 5 + QPointF(ux, uy) * 9)
        cw.lineTo(back - QPointF(nx, ny) * 5 + QPointF(ux, uy) * 9)
        cw.lineTo(back - QPointF(nx, ny) * 5)
        cw.closeSubpath()
        weight = QLinearGradient(back + QPointF(nx, ny) * 5, back - QPointF(nx, ny) * 5)
        weight.setColorAt(0.0, QColor(90, 90, 98))
        weight.setColorAt(0.5, QColor(40, 40, 46))
        weight.setColorAt(1.0, QColor(16, 16, 20))
        p.setPen(QPen(QColor(10, 10, 12), 0.8))
        p.setBrush(weight)
        p.drawPath(cw)
        # the arm tube: silver with a highlight along it
        p.setPen(QPen(QColor(120, 120, 130), 4.2, Qt.SolidLine, Qt.RoundCap))
        p.drawLine(back + QPointF(ux, uy) * 6, tip - QPointF(ux, uy) * 8)
        p.setPen(QPen(QColor(245, 245, 250), 1.4, Qt.SolidLine, Qt.RoundCap))
        p.drawLine(back + QPointF(ux, uy) * 6 - QPointF(nx, ny) * 0.7, tip - QPointF(ux, uy) * 8 - QPointF(nx, ny) * 0.7)
        # pivot cap
        p.setPen(Qt.NoPen)
        cap = QRadialGradient(top + QPointF(-1.5, -1.5), 6)
        cap.setColorAt(0.0, QColor(200, 200, 210))
        cap.setColorAt(1.0, QColor(40, 40, 46))
        p.setBrush(cap)
        p.drawEllipse(top, 5, 4)
        # headshell with a red cartridge and a finger lift
        hs = QPainterPath()
        a0 = tip - QPointF(ux, uy) * 10
        for k, (along, side) in enumerate(((0, 3.2), (12, 3.6), (12, -3.6), (0, -3.2))):
            pt = a0 + QPointF(ux, uy) * along + QPointF(nx, ny) * side
            hs.moveTo(pt) if k == 0 else hs.lineTo(pt)
        hs.closeSubpath()
        p.setPen(QPen(QColor(0, 0, 0), 0.8))
        p.setBrush(QColor(28, 28, 32))
        p.drawPath(hs)
        cart = a0 + QPointF(ux, uy) * 6
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(210, 30, 40))
        p.drawEllipse(cart, 2.6, 2.0)
        p.setPen(QPen(QColor(150, 150, 160), 1.0, Qt.SolidLine, Qt.RoundCap))
        p.drawLine(a0 + QPointF(nx, ny) * 3.4, a0 + QPointF(nx, ny) * 7 - QPointF(ux, uy) * 2)

    def _body(self, vis: float) -> QImage:
        """The glass itself (blurred backdrop, refraction, tint). Rebuilt when a new
        capture arrives or the card moves, not on every animation frame."""
        pl = self.player
        fr = self.backdrop.frost_rect() if self.glass else None
        key = (self.backdrop.serial, None if fr is None else (round(fr.x(), 2), round(fr.y(), 2)), round(vis, 3),
               bool(self._drop_hover), pl.style, pl.accent.rgba(), self.devicePixelRatioF(), self.glass)
        if key != self._body_key or self._body_img is None:
            img = self._layer()
            p = QPainter(img)
            self._paint_body(p, vis)
            p.end()
            self._body_key, self._body_img = key, img
        return self._body_img

    def _decor(self, vis: float) -> QImage:
        """Edge, disc shadow, pop-out button and playlist badge: only change on
        hover, state or style."""
        pl = self.player
        key = (round(vis, 3), bool(self._drop_hover), pl.style, self.devicePixelRatioF(), self._buttons_key(),
               len(pl.queue), pl.index)
        if key == self._decor_key and self._decor_img is not None:
            return self._decor_img
        img = self._layer()
        p = QPainter(img)
        p.setRenderHints(QPainter.Antialiasing | QPainter.SmoothPixmapTransform | QPainter.TextAntialiasing)
        card = self._card_path()
        aero = pl.style == "aero"
        c = self.disc_center()
        if aero:
            dpr = self.devicePixelRatioF()
            if self._aero_bg is None or self._aero_bg.devicePixelRatio() != dpr:
                self._aero_bg = render_aero_bg(self.W, self.H, dpr, self.RADIUS)
            p.drawPixmap(0, 0, self._aero_bg)
            halo = QRadialGradient(c, self.D * 0.72)
            halo.setColorAt(0.6, QColor(200, 245, 255, int(70 * vis)))
            halo.setColorAt(1.0, QColor(255, 255, 255, 0))
            p.fillPath(card, halo)
            if self._drop_hover:
                p.fillPath(card, QColor(255, 255, 255, 50))
        elif pl.style == "glossy":
            paint_glossy_overlay(p, self._card_shape(), self.W, self.H, self.CORNER)
            if self._drop_hover:
                paint_glass_edge(p, self._card_shape(), rim=2.6)
        else:
            paint_glass_edge(p, self._card_shape(), rim=1.8 if self._drop_hover else 1.0)

        # shadow under the disc (glass casts a lighter one, with light focused through it)
        R = self.D / 2
        sc = c + QPointF(0, R * 0.08)
        sh = QRadialGradient(sc, R * 1.12)
        sh.setColorAt(0.80, QColor(0, 0, 0, int((55 if pl.style == "glossy" else 120) * vis)))
        sh.setColorAt(1.00, QColor(0, 0, 0, 0))
        p.setPen(Qt.NoPen)
        p.setBrush(sh)
        p.drawEllipse(sc, R * 1.12, R * 1.12)
        if pl.style == "glossy":
            caustic = QRadialGradient(c + QPointF(0, R * 0.95), R * 0.5)
            caustic.setColorAt(0.0, QColor(255, 255, 255, int(70 * vis)))
            caustic.setColorAt(1.0, QColor(255, 255, 255, 0))
            p.setBrush(caustic)
            p.drawEllipse(c + QPointF(0, R * 0.95), R * 0.5, R * 0.22)

        # pop-out and shuffle buttons
        for bc, hot in ((self.btn_center(), self._hover_btn), (self.shuffle_center(), self._hover_shuf)):
            lit = hot or (bc == self.shuffle_center() and pl.shuffle)
            if aero:
                paint_aero_orb(p, bc, self.BTN_R, lit)
            else:
                r = self.BTN_R
                glossy = pl.style == "glossy"
                paint_glass_chip(p, squircle_shape(QRectF(bc.x() - r, bc.y() - r, 2 * r, 2 * r), r, n=4.0),
                                 fill=(48 if lit else 26) if glossy else (40 if lit else 16),
                                 rim=(1.9 if glossy else 1.3) if lit else (1.6 if glossy else 1.0))
        self._paint_pop_glyph(p, self.btn_center(), inward=self.popout or self.popped)
        self._paint_shuffle_glyph(p, self.shuffle_center(), pl.shuffle)

        # playlist position
        if len(pl.queue) > 1:
            bf = ui_font(8, QFont.DemiBold)
            label = f"{pl.index + 1} / {len(pl.queue)}"
            bw = QFontMetrics(bf).horizontalAdvance(label) + 16
            pill_rect = QRectF(self.W - bw - 14, 14, bw, 20)
            p.setFont(bf)
            if aero:
                paint_aero_pill(p, pill_rect)
                glow_text(p, pill_rect, Qt.AlignCenter, label, QColor(255, 255, 255), QColor(0, 40, 100, 70))
            else:
                glossy = pl.style == "glossy"
                paint_glass_chip(p, squircle_shape(pill_rect, pill_rect.height() / 2), fill=28 if glossy else 20,
                                 rim=1.6 if glossy else 1.0)
                if glossy:
                    glossy_text(p, pill_rect, label, bf)
                else:
                    shadow_text(p, pill_rect, Qt.AlignCenter, label, QColor(255, 255, 255, 230))
        p.end()
        self._decor_key, self._decor_img = key, img
        return img

    def _title_spec(self, hires: bool):
        """Where the title goes in this style, its font, and how it's drawn."""
        style = self.player.style
        if style == "turntable":
            lf = ui_font(11.5, QFont.Bold)
            lf.setLetterSpacing(QFont.AbsoluteSpacing, 2.4)
            lf.setCapitalization(QFont.AllUppercase)
            return (QRectF(24, 38, self.W - 48, 24), lf,
                    lambda p, r, t: shadow_text(p, r, Qt.AlignCenter, t, QColor(250, 244, 238)))
        rect = QRectF(16, self.PAD + self.D + (33 if hires else 38), self.W - 32, 22)
        if style == "glossy":
            font = ui_font(11, QFont.Bold)
            return rect, font, lambda p, r, t: glossy_text(p, r, t, font)
        font = ui_font(10.5, QFont.DemiBold)
        if style == "aero":
            return rect, font, lambda p, r, t: glow_text(p, r, Qt.AlignCenter, t, QColor(255, 255, 255), QColor(0, 45, 110, 60))
        color = QColor(255, 250, 240) if style == "snow" else QColor(255, 255, 255, 245)
        return rect, font, lambda p, r, t: shadow_text(p, r, Qt.AlignCenter, t, color)

    MARQUEE_SPEED = 32.0   # px/s
    MARQUEE_PAUSE = 1.4    # s at each end

    def _paint_marquee(self, p: QPainter):
        """A title too long for its space slides to show the rest, pauses, and slides
        back; the edges fade where text is hidden."""
        if self._marquee is None:
            return
        strip, rect, overflow = self._marquee
        travel = overflow / self.MARQUEE_SPEED
        period = 2 * (travel + self.MARQUEE_PAUSE)
        t = (time.perf_counter() - self._marquee_t0) % period
        if t < self.MARQUEE_PAUSE:
            f = 0.0
        elif t < self.MARQUEE_PAUSE + travel:
            f = (t - self.MARQUEE_PAUSE) / travel
        elif t < 2 * self.MARQUEE_PAUSE + travel:
            f = 1.0
        else:
            f = 1.0 - (t - 2 * self.MARQUEE_PAUSE - travel) / travel
        f = 0.5 - 0.5 * math.cos(math.pi * f)            # ease in and out of each slide
        offset = f * overflow
        dpr = self.devicePixelRatioF()
        box = QImage(round((rect.width() + 8) * dpr), round((rect.height() + 8) * dpr), QImage.Format_ARGB32_Premultiplied)
        box.setDevicePixelRatio(dpr)
        box.fill(Qt.transparent)
        bp = QPainter(box)
        bp.drawImage(QPointF(4 - offset - 4, 0), strip)
        fade = QLinearGradient(0, 0, rect.width() + 8, 0)
        edge = 12 / (rect.width() + 8)
        fade.setColorAt(0.0, QColor(0, 0, 0, 255 if offset < 1 else 0))
        fade.setColorAt(edge, QColor(0, 0, 0, 255))
        fade.setColorAt(1 - edge, QColor(0, 0, 0, 255))
        fade.setColorAt(1.0, QColor(0, 0, 0, 255 if offset > overflow - 1 else 0))
        bp.setCompositionMode(QPainter.CompositionMode_DestinationIn)
        bp.fillRect(QRectF(0, 0, rect.width() + 8, rect.height() + 8), fade)
        bp.end()
        p.drawImage(QPointF(rect.left() - 4, rect.top() - 4), box)
        QTimer.singleShot(33, self.update)                # keep it moving even when paused

    def _paint_text(self, p: QPainter):
        p.drawImage(0, 0, self._text())
        self._paint_marquee(p)

    def _text(self) -> QImage:
        """Title and time; re-rendered only when the words change (about once a second)."""
        pl = self.player
        hires = pl.hires and pl.path and not self._drop_hover and not pl.error
        title_rect = QRectF(16, self.PAD + self.D + (33 if hires else 38), self.W - 32, 22)
        hires_rect = QRectF(16, title_rect.bottom() - 3, self.W - 32, 12)
        time_rect = QRectF(16, (hires_rect.bottom() if hires else title_rect.bottom()) + 1, self.W - 32, 18)
        tf = ui_font(10.5, QFont.DemiBold)
        if self._drop_hover:
            title = "Drop to play" if self._drop_hover == 1 else f"Drop to play {self._drop_hover} tracks"
        elif pl.error and pl.path:
            title = f"Can't play {Path(pl.path).name}"
        else:
            title = pl.title
        if pl.path:
            t = f"{fmt_time(pl.position())}  /  {fmt_time(pl.duration())}"
        else:
            t = "drag files onto the tray icon"
        aero = pl.style == "aero"
        key = (title, t, pl.style, bool(hires), self.devicePixelRatioF())
        if key == self._text_key and self._text_img is not None:
            return self._text_img
        img = self._layer()
        p = QPainter(img)
        p.setRenderHints(QPainter.Antialiasing | QPainter.TextAntialiasing)
        if hires and pl.style != "turntable":   # small, grey and see-through, under the title
            hf = ui_font(7.5, QFont.DemiBold)
            hf.setLetterSpacing(QFont.AbsoluteSpacing, 0.8)
            p.setFont(hf)
            p.setPen(QColor(235, 235, 240, 110))
            p.drawText(hires_rect, Qt.AlignCenter, "Hi-Res")
        # the title: drawn here when it fits, otherwise it slides (see _paint_marquee)
        t_rect, t_font, t_draw = self._title_spec(bool(hires))
        width = QFontMetricsF(t_font).horizontalAdvance(title)
        if width > t_rect.width():
            if self._marquee_title != (title, pl.style):
                self._marquee_title = (title, pl.style)
                self._marquee_t0 = time.perf_counter()
            dpr = self.devicePixelRatioF()
            strip = QImage(round((width + 8) * dpr), round((t_rect.height() + 8) * dpr), QImage.Format_ARGB32_Premultiplied)
            strip.setDevicePixelRatio(dpr)
            strip.fill(Qt.transparent)
            sp = QPainter(strip)
            sp.setRenderHints(QPainter.Antialiasing | QPainter.TextAntialiasing)
            sp.setFont(t_font)
            t_draw(sp, QRectF(4, 4, width, t_rect.height()), title)
            sp.end()
            self._marquee = (strip, t_rect, width - t_rect.width())
        else:
            self._marquee = None
            self._marquee_title = None
            p.setFont(t_font)
            t_draw(p, t_rect, title)
        p.setFont(tf)
        if pl.style == "turntable":   # the title on the lid, set like a maker's logo
            if hires:
                hf = ui_font(7, QFont.DemiBold)
                hf.setLetterSpacing(QFont.AbsoluteSpacing, 1.2)
                p.setFont(hf)
                p.setPen(QColor(255, 225, 230, 120))
                p.drawText(QRectF(24, 62, self.W - 48, 11), Qt.AlignCenter, "Hi-Res")
            p.setFont(ui_font(8.5, QFont.DemiBold))
            shadow_text(p, QRectF(24, 74 if hires else 66, self.W - 48, 16), Qt.AlignCenter, t, QColor(255, 214, 196, 210))
            p.end()
            self._text_key, self._text_img = key, img
            return img
        if pl.style == "snow":
            p.setFont(ui_font(9, QFont.DemiBold))
            shadow_text(p, time_rect, Qt.AlignCenter, t, QColor(255, 216, 120))
            p.end()
            self._text_key, self._text_img = key, img
            return img
        if pl.style == "glossy":
            glossy_text(p, time_rect, t, ui_font(9, QFont.DemiBold), strength=0.82)
        p.setFont(ui_font(9))
        if aero:
            glow_text(p, time_rect, Qt.AlignCenter, t, QColor(255, 255, 255, 235), QColor(0, 45, 110, 50))
        elif pl.style != "glossy":
            shadow_text(p, time_rect, Qt.AlignCenter, t, QColor(255, 255, 255, 175))
        p.end()
        self._text_key, self._text_img = key, img
        return img

    def _paint_hub_glyph(self, p: QPainter, hub: float, playing: bool):
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(10, 10, 16, 95))
        p.drawEllipse(QPointF(0, 0), hub * 0.92, hub * 0.92)
        p.setBrush(QColor(255, 255, 255, 235))
        u = hub * 0.32
        if playing:
            p.drawRoundedRect(QRectF(-u * 0.95, -u, u * 0.62, u * 2), 1.5, 1.5)
            p.drawRoundedRect(QRectF(u * 0.33, -u, u * 0.62, u * 2), 1.5, 1.5)
        else:
            tri = QPainterPath()
            tri.moveTo(-u * 0.7, -u)
            tri.lineTo(u * 1.05, 0)
            tri.lineTo(-u * 0.7, u)
            tri.closeSubpath()
            p.drawPath(tri)

    def _paint_shuffle_glyph(self, p: QPainter, c: QPointF, on: bool, scale: float = 1.0):
        """Two crossing arrows: bright with a dot under them when shuffle is on, dim when off."""
        pen = QPen(QColor(255, 255, 255, 235 if on else 125), 1.4 if scale >= 1 else 1.1)
        pen.setCapStyle(Qt.RoundCap)
        pen.setJoinStyle(Qt.RoundJoin)
        p.setPen(pen)
        p.setBrush(Qt.NoBrush)
        s = scale
        for y0 in (3.4, -3.4):   # one strand from bottom-left to top-right, the other the opposite way
            path = QPainterPath(QPointF(c.x() - 5.5 * s, c.y() + y0 * s))
            path.lineTo(c.x() - 2.6 * s, c.y() + y0 * s)
            path.cubicTo(QPointF(c.x(), c.y() + y0 * s), QPointF(c.x(), c.y() - y0 * s),
                         QPointF(c.x() + 2.6 * s, c.y() - y0 * s))
            path.lineTo(c.x() + 5.6 * s, c.y() - y0 * s)
            p.drawPath(path)
            tip = QPointF(c.x() + 5.6 * s, c.y() - y0 * s)
            p.drawLine(tip, tip + QPointF(-2.2 * s, -2.2 * s))
            p.drawLine(tip, tip + QPointF(-2.2 * s, 2.2 * s))
        if on:
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(255, 255, 255, 235))
            p.drawEllipse(QPointF(c.x(), c.y() + 8.4 * s), 1.2 * s, 1.2 * s)

    def _paint_pop_glyph(self, p: QPainter, c: QPointF, inward: bool):
        pen = QPen(QColor(255, 255, 255, 220), 1.4)
        pen.setCapStyle(Qt.RoundCap)
        pen.setJoinStyle(Qt.RoundJoin)
        p.setPen(pen)
        p.setBrush(Qt.NoBrush)
        # box with an open top-right corner
        box = QPainterPath()
        box.moveTo(c.x() - 1, c.y() - 5)
        box.lineTo(c.x() - 5, c.y() - 5)
        box.lineTo(c.x() - 5, c.y() + 5)
        box.lineTo(c.x() + 5, c.y() + 5)
        box.lineTo(c.x() + 5, c.y() + 1)
        p.drawPath(box)
        a, b = QPointF(c.x() - 0.5, c.y() + 0.5), QPointF(c.x() + 5.5, c.y() - 5.5)
        tip, tail = (a, b) if inward else (b, a)
        p.drawLine(tail, tip)
        d = 3.2 if inward else -3.2
        p.drawLine(tip, tip + QPointF(0, -d))
        p.drawLine(tip, tip + QPointF(d, 0))


class VolumeHud(QWidget):
    """Ctrl + scroll volume: a slim glass capsule that springs out from the side of
    the card, like the iOS volume bar but glossier. It widens while in use,
    stretches like rubber when pushed past 0 or 100 %, and can be dragged."""

    W, H = 46, 200
    CAP_W, CAP_W_ACTIVE, CAP_H, CAP_TOP = 13.0, 19.0, 142.0, 16.0

    volumeSet = Signal(float)

    def __init__(self, player: Player):
        super().__init__(None, Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint
                         | Qt.NoDropShadowWindowHint | Qt.WindowDoesNotAcceptFocus)
        self.player = player
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setFixedSize(self.W, self.H)
        self.glass = False
        self.glass = prepare_glass_window(int(self.winId()))
        self.backdrop = Backdrop(self)
        self.backdrop.updated.connect(self.update)
        self.card: QWidget | None = None
        self.level = player.volume
        self.appear, self._appear_v, self._appear_to = 0.0, 0.0, 0.0
        self.active = 0.0
        self.stretch, self._stretch_v = 0.0, 0.0
        self._last_input = 0.0
        self._dragging = False
        self._t = 0.0
        self._timer = QTimer(self)
        self._timer.setTimerType(Qt.PreciseTimer)
        self._timer.setInterval(16)
        self._timer.timeout.connect(self._step)

    def poke(self, card: QWidget, overscroll: float = 0.0):
        """Show beside `card` after a volume change; `overscroll` pushes past the ends."""
        self.card = card
        self._last_input = time.monotonic()
        self._appear_to = 1.0
        if overscroll:
            self.stretch = max(-18.0, min(18.0, self.stretch + overscroll * 7))
        if not self.isVisible():
            self.appear, self._appear_v = 0.0, 0.0
            self.level = self.player.volume
            self._place()
            self.show()
            self.raise_()
            if self.glass:
                self.backdrop.start()
        if not self._timer.isActive():
            self._t = time.perf_counter()
            self._timer.start()

    def _place(self):
        card = self.card
        if card is None:
            return
        g = card.frameGeometry()
        screen = card.screen() or QGuiApplication.primaryScreen()
        avail = screen.availableGeometry()
        left = g.left() - self.W - 4
        side = -1 if left >= avail.left() else 1
        x = left if side < 0 else g.right() + 5
        y = g.top() + (g.height() - self.H) // 2
        slide = (1.0 - max(0.0, min(1.2, self.appear))) * 22 * -side   # tucked behind the card's edge
        self.move(QPoint(round(x + slide), y))

    def _step(self):
        now = time.perf_counter()
        dt = min(0.05, now - self._t)
        self._t = now
        idle = time.monotonic() - self._last_input
        if idle > 1.5 and not self._dragging:
            self._appear_to = 0.0
        # springs (slightly underdamped, so it settles with a little bounce)
        self._appear_v += (320 * (self._appear_to - self.appear) - 24 * self._appear_v) * dt
        self.appear += self._appear_v * dt
        self._stretch_v += (-280 * self.stretch - 20 * self._stretch_v) * dt
        self.stretch += self._stretch_v * dt
        busy = idle < 0.45 or self._dragging
        self.active += ((1.0 if busy else 0.0) - self.active) * (1 - math.exp(-dt / 0.12))
        self.level += (self.player.volume - self.level) * (1 - math.exp(-dt / 0.05))
        self._place()
        self.update()
        if self._appear_to == 0.0 and self.appear < 0.02 and abs(self._appear_v) < 0.05:
            self._timer.stop()
            self.backdrop.stop()
            self.hide()

    def _capsule(self) -> QRectF:
        squeeze = 1 - min(abs(self.stretch), 18) / 90       # stretched long, it thins a little
        cw = (self.CAP_W + (self.CAP_W_ACTIVE - self.CAP_W) * self.active) * squeeze
        ch = self.CAP_H + abs(self.stretch)
        top = self.CAP_TOP - (self.stretch if self.stretch > 0 else 0)
        return QRectF(self.W / 2 - cw / 2, top, cw, ch)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHints(QPainter.Antialiasing | QPainter.SmoothPixmapTransform)
        p.setOpacity(max(0.0, min(1.0, self.appear)))
        rect = self._capsule()
        shape = squircle_shape(rect, rect.width() / 2)
        cap = shape(0)
        dpr = self.devicePixelRatioF()
        layer = QImage(round(self.W * dpr), round(self.H * dpr), QImage.Format_ARGB32_Premultiplied)
        layer.setDevicePixelRatio(dpr)
        layer.fill(Qt.transparent)
        lp = QPainter(layer)
        lp.setRenderHints(QPainter.Antialiasing | QPainter.SmoothPixmapTransform)
        fr = self.backdrop.frost_rect() if self.glass and self.backdrop.frost is not None else None
        if fr is not None:
            lp.drawImage(fr, self.backdrop.frost)
        lp.fillRect(self.rect(), QColor(16, 16, 24, 120 if fr is not None else 225))
        # the level: bright glossy fill rising from the bottom
        fill_h = rect.height() * max(0.0, min(1.0, self.level))
        fill = QRectF(rect.left() - 1, rect.bottom() - fill_h, rect.width() + 2, fill_h + 1)
        g = QLinearGradient(rect.left(), 0, rect.right(), 0)
        g.setColorAt(0.0, QColor(255, 255, 255, 252))
        g.setColorAt(0.5, QColor(242, 246, 252, 248))
        g.setColorAt(1.0, QColor(206, 216, 232, 245))
        lp.fillRect(fill, g)
        stripe = QLinearGradient(rect.left(), 0, rect.right(), 0)   # glossy vertical highlight
        stripe.setColorAt(0.0, QColor(255, 255, 255, 0))
        stripe.setColorAt(0.25, QColor(255, 255, 255, 80))
        stripe.setColorAt(0.45, QColor(255, 255, 255, 0))
        lp.fillRect(rect, stripe)
        top = QLinearGradient(0, rect.top(), 0, rect.top() + rect.width() * 1.6)
        top.setColorAt(0.0, QColor(255, 255, 255, 110))
        top.setColorAt(1.0, QColor(255, 255, 255, 0))
        lp.fillRect(rect, top)
        lp.setCompositionMode(QPainter.CompositionMode_DestinationIn)
        lp.setPen(Qt.NoPen)
        mask = QImage(layer.size(), QImage.Format_ARGB32_Premultiplied)
        mask.setDevicePixelRatio(dpr)
        mask.fill(Qt.transparent)
        mp = QPainter(mask)
        mp.setRenderHint(QPainter.Antialiasing)
        mp.setPen(Qt.NoPen)
        mp.setBrush(Qt.black)
        mp.drawPath(cap)
        mp.end()
        lp.drawImage(0, 0, mask)
        lp.end()
        p.drawImage(0, 0, layer)
        paint_glass_edge(p, shape, rim=1.7)
        icon = QPointF(self.W / 2 + 0.5, rect.bottom() + 20)
        paint_speaker(p, icon + QPointF(0, 1), self.level, QColor(0, 0, 0, 110))
        paint_speaker(p, icon, self.level, QColor(255, 255, 255, 240))
        p.end()

    # dragging the bar sets the volume, and pulls it like rubber past the ends
    def _drag_to(self, y: float):
        rect = QRectF(self.W / 2, self.CAP_TOP, 0, self.CAP_H)
        v = (rect.bottom() - y) / rect.height()
        over = v - 1 if v > 1 else v if v < 0 else 0
        self.stretch = max(-18.0, min(18.0, over * self.CAP_H * 0.25))
        self.volumeSet.emit(max(0.0, min(1.0, v)))
        self._last_input = time.monotonic()

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self._dragging = True
            self._drag_to(e.position().y())

    def mouseMoveEvent(self, e):
        if self._dragging:
            self._drag_to(e.position().y())

    def mouseReleaseEvent(self, e):
        self._dragging = False
        self._last_input = time.monotonic()

    def wheelEvent(self, e):
        steps = e.angleDelta().y() / 120.0 or e.pixelDelta().y() / 40.0
        if steps and self.card is not None:
            self.card.volumeWheel.emit(steps)


class DropCatcher(QWidget):
    """An almost invisible window laid over the tray icon while something is being
    dragged, so a file can be dropped straight onto the icon."""

    entered = Signal()
    dropped = Signal(list)

    def __init__(self):
        super().__init__(None, Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint
                         | Qt.WindowDoesNotAcceptFocus | Qt.NoDropShadowWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setAcceptDrops(True)
        self.hot = False

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.fillRect(self.rect(), QColor(0, 0, 0, 1))  # alpha 1 keeps the window hit-testable
        if self.hot:
            r = QRectF(self.rect()).adjusted(1, 1, -1, -1)
            p.setPen(QPen(QColor(255, 255, 255, 200), 1.5))
            p.setBrush(QColor(255, 255, 255, 40))
            p.drawRoundedRect(r, 5, 5)

    def dragEnterEvent(self, e):
        if audio_paths_from_mime(e.mimeData()):
            e.acceptProposedAction()
            self.hot = True
            self.update()
            self.entered.emit()

    def dragLeaveEvent(self, e):
        self.hot = False
        self.update()

    def dropEvent(self, e):
        paths = audio_paths_from_mime(e.mimeData())
        self.hot = False
        if paths:
            e.acceptProposedAction()
            self.dropped.emit(paths)
        self.hide()


# --------------------------------------------------------------------------- app

class DiscTrayApp(QObject):
    def __init__(self, server: QLocalServer, initial: list[str]):
        super().__init__()
        self.cfg = load_config()
        self.server = server
        server.newConnection.connect(self._on_connection)

        self.player = Player()
        self.player.changed.connect(self._on_changed)

        self.panel = DiscCard(self.player, popout=False)
        self.pop = DiscCard(self.player, popout=True)
        self.player.shuffle = bool(self.cfg.get("shuffle", False))
        for card in (self.panel, self.pop):
            card.popToggle.connect(self.toggle_popout)
            card.shuffleToggle.connect(lambda: self.act_shuffle.toggle())
            card.fileDropped.connect(self._on_drop)
        self.panel.deactivated.connect(self._panel_deactivated)
        self._position_timer = QTimer(self)
        self._position_timer.setInterval(10_000)
        self._position_timer.timeout.connect(self._save_position)
        self._position_timer.start()
        app = QApplication.instance()
        app.aboutToQuit.connect(self._save_position)
        app.commitDataRequest.connect(lambda _m: self._save_position())   # Windows signing out / shutting down
        self.pop.deactivated.connect(self._pop_deactivated)
        self._pop_home: QPoint | None = None
        self._pop_from, self._pop_to = QPointF(), QPointF()
        self._pop_anim = QVariantAnimation(self)
        self._pop_anim.valueChanged.connect(self._pop_anim_step)
        self.player.set_volume(self.cfg.get("volume", 1.0))
        self.hud = VolumeHud(self.player)
        self.hud.volumeSet.connect(self._set_volume)
        for card in (self.panel, self.pop):
            card.volumeWheel.connect(lambda steps, c=card: self._volume_wheel(c, steps))
        self._volume_save = QTimer(self)
        self._volume_save.setSingleShot(True)
        self._volume_save.setInterval(800)
        self._volume_save.timeout.connect(self._save_volume)
        self.pop.moved.connect(self._save_pop_pos)
        self._last_hide = 0.0

        self.catcher = DropCatcher()
        self.catcher.entered.connect(lambda: self.show_panel(preview=True))
        self.catcher.dropped.connect(self._on_drop)

        self.tray = QSystemTrayIcon(self.player.icon())
        self.tray.setToolTip(APP_NAME)
        self.tray.activated.connect(self._on_tray)
        self.menu = QMenu()
        self.act_play = self.menu.addAction("Pause", self.player.toggle)
        self.act_prev = self.menu.addAction("Previous track", self.player.previous)
        self.act_next = self.menu.addAction("Next track", self.player.next)
        self.menu.addAction("Open audio files…", self.open_file)
        self.act_pop = self.menu.addAction("Pop out", self.toggle_popout)
        self.player.style = self.cfg.get("style") if self.cfg.get("style") in STYLES else "glassy"
        style_menu = self.menu.addMenu("Style")
        group = QActionGroup(style_menu)
        for key, name in STYLES.items():
            act = style_menu.addAction(name, lambda k=key: self.set_style(k))
            act.setCheckable(True)
            act.setChecked(key == self.player.style)
            group.addAction(act)
        self.refresh_hz = self.cfg.get("refresh_hz") if self.cfg.get("refresh_hz") in REFRESH_RATES else 60
        settings = self.menu.addMenu("Settings")
        self.act_shuffle = QAction("Shuffle", settings, checkable=True)
        self.act_shuffle.setChecked(self.player.shuffle)
        self.act_shuffle.toggled.connect(self.set_shuffle)
        settings.addAction(self.act_shuffle)
        self.act_fs_pause = QAction("Pause while a program is fullscreen", settings, checkable=True)
        self.act_fs_pause.setChecked(self.cfg.get("pause_fullscreen", True))
        self.act_fs_pause.toggled.connect(self.set_fullscreen_pause)
        settings.addAction(self.act_fs_pause)
        self.act_norm = QAction("Volume normalization", settings, checkable=True)
        self.act_norm.setChecked(self.cfg.get("normalize", True))
        self.act_norm.toggled.connect(self.set_normalize)
        settings.addAction(self.act_norm)
        self.act_bouncy = QAction("Bouncy pop-out (throw it around)", settings, checkable=True)
        self.act_bouncy.setChecked(False)   # always starts off; switch it on per session
        self.act_bouncy.toggled.connect(self.set_bouncy)
        settings.addAction(self.act_bouncy)
        self.act_capture = QAction("Show in screenshots && recordings", settings, checkable=True)
        self.act_capture.setChecked(self.cfg.get("capturable", True))
        self.act_capture.toggled.connect(self.set_capturable)
        settings.addAction(self.act_capture)
        self.set_capturable(self.act_capture.isChecked(), save=False)
        self.player.engine.normalize = self.act_norm.isChecked()
        self.pop.bouncy = self.act_bouncy.isChecked()
        settings.addSeparator()
        rate_menu = settings.addMenu("Glass refresh rate")
        rates = QActionGroup(rate_menu)
        for hz in REFRESH_RATES:
            act = rate_menu.addAction(f"{hz} fps", lambda h=hz: self.set_refresh_rate(h))
            act.setCheckable(True)
            act.setChecked(hz == self.refresh_hz)
            rates.addAction(act)
        rate_menu.addSeparator()
        self.refresh_boost = self.cfg.get("refresh_boost", True)
        self.act_boost = QAction("Speed up when something moves behind it", rate_menu, checkable=True)
        self.act_boost.setChecked(self.refresh_boost)
        self.act_boost.toggled.connect(self.set_refresh_boost)
        rate_menu.addAction(self.act_boost)
        for card in (self.panel, self.pop, self.hud):
            card.backdrop.set_rate(self.refresh_hz, self.refresh_boost)
        self.act_startup = QAction("Start with Windows", settings, checkable=True)
        self.act_startup.setChecked(startup_enabled())
        self.act_startup.toggled.connect(self._toggle_startup)
        settings.addAction(self.act_startup)
        self.menu.addSeparator()
        if updater_path().is_file():   # only in installed builds
            self.menu.addAction("Check for updates…", self.check_updates)
        about = self.menu.addAction(f"{APP_NAME} {__version__}")
        about.setEnabled(False)
        self.menu.addAction("Quit", self.quit)
        self.tray.setContextMenu(self.menu)
        self.tray.show()

        # drags over the tray icon
        self._was_down = False
        self._press_outside = False
        self._drag_timer = QTimer(self)
        self._drag_timer.setInterval(40)
        self._drag_timer.timeout.connect(self._poll_drag)
        self._drag_timer.start()
        self._preview_timer = QTimer(self)
        self._preview_timer.setSingleShot(True)
        self._preview_timer.timeout.connect(self._preview_check)

        # Caps Lock peek-through
        self._caps_was = False
        self._caps_t0 = 0.0
        self._caps_before = caps_lock_on()
        self._caps_used = False
        self._caps_ignore_until = 0.0
        self._mouse_was = False
        self._caps_timer = QTimer(self)
        self._caps_timer.setInterval(25)
        self._caps_timer.timeout.connect(self._poll_caps)
        self._caps_timer.start()

        # fullscreen watcher
        self._fs_state = False
        self._fs_seen = False
        self._fs_count = 0
        self._fs_timer = QTimer(self)
        self._fs_timer.setInterval(350)
        self._fs_timer.timeout.connect(self._poll_fullscreen)
        if self.act_fs_pause.isChecked():
            self._fs_timer.start()

        if initial:
            self.player.load_list(initial, animate=False)
        else:
            files = [f for f in self.cfg.get("files") or [self.cfg.get("file")] if f and Path(f).is_file()]
            if files:
                order = self.cfg.get("order") if self.player.shuffle and len(files) == len(self.cfg.get("files") or []) \
                    else None
                self.player.load_list(files, index=min(self.cfg.get("index", 0), len(files) - 1), animate=False,
                                      order=order, pos=self.cfg.get("pos", 0))
                if self.cfg.get("position_ms"):   # pick up where the song was when DiscTray closed
                    self.player.engine.seek(self.cfg["position_ms"])
        if self.cfg.get("popout"):
            self.show_popout()

    # --- tray
    def _on_tray(self, reason):
        if reason == QSystemTrayIcon.Trigger:
            if self._pop_showing():
                # one player only: bring the pop-out over instead of opening the panel
                if self.pop.summoned:
                    self.send_pop_home()
                elif time.monotonic() - self._last_hide > 0.3:
                    self.summon_pop(activate=True)
                return
            if self.panel.isVisible() and not self.panel._slide_hide:
                self.hide_panel()
            elif time.monotonic() - self._last_hide > 0.25:
                self.show_panel(preview=False)

    def _on_changed(self):
        pl = self.player
        self.tray.setIcon(pl.icon())
        self.tray.setToolTip(f"{APP_NAME} — {pl.title}" if pl.path else APP_NAME)
        self.act_play.setText("Pause" if pl.want_playing else "Play")
        self.act_play.setEnabled(bool(pl.path))
        self.act_next.setVisible(len(pl.queue) > 1)
        self.act_prev.setVisible(len(pl.queue) > 1)
        state = {"files": pl.queue, "index": pl.track_index(), "order": pl.order, "pos": pl.index}
        if pl.path and any(self.cfg.get(k) != v for k, v in state.items()):
            self.cfg.pop("file", None)
            self.cfg.update(state)
            save_config(self.cfg)

    def _panel_pos(self) -> QPoint:
        geo = self.tray.geometry()
        anchor = geo.center() if geo.isValid() and not geo.isEmpty() else QCursor.pos()
        screen = QGuiApplication.screenAt(anchor) or QGuiApplication.primaryScreen()
        full, avail = screen.geometry(), screen.availableGeometry()
        w, h, m = self.panel.width(), self.panel.height(), 12
        x = max(avail.left() + m, min(anchor.x() - w // 2, avail.right() - w - m))
        y = max(avail.top() + m, min(anchor.y() - h // 2, avail.bottom() - h - m))
        # sit against whichever edge the taskbar is on
        if avail.left() > full.left():
            x = avail.left() + m
        elif avail.right() < full.right():
            x = avail.right() - w - m
        elif avail.top() > full.top():
            y = avail.top() + m
        else:
            y = avail.bottom() - h - m
        return QPoint(x, y)

    def _pop_showing(self) -> bool:
        return self.pop.isVisible() and not self.pop._slide_hide

    def summon_pop(self, activate: bool):
        """Glide the pop-out to where the tray panel would open; it returns when you
        click elsewhere (or, after a drag over the tray icon, once the drop is done)."""
        if not self.pop.summoned:
            self._pop_home = self.pop.pos()
        self.pop.summoned = True
        self.pop.preview = not activate
        self._ease_pop(self._panel_pos(), POP_GLIDE_MS, QEasingCurve.OutSine)
        self.pop.raise_()
        if activate:
            self.pop.activateWindow()

    def send_pop_home(self):
        if not self.pop.summoned:
            return
        self.pop.summoned = False
        self.pop.preview = False
        home, self._pop_home = self._pop_home, None
        self._last_hide = time.monotonic()
        if home is not None:
            self._ease_pop(home, POP_GLIDE_MS, QEasingCurve.OutSine)

    def _ease_pop(self, target: QPoint, ms: int, curve):
        self.pop._glide.stop()
        self.pop._throw.stop()
        self._pop_from, self._pop_to = QPointF(self.pop.pos()), QPointF(target)
        self._pop_anim.stop()
        _run(self._pop_anim, 0.0, 1.0, ms, curve)

    def _pop_anim_step(self, t):
        f, to = self._pop_from, self._pop_to
        self.pop.move((f + (to - f) * float(t)).toPoint())

    def _pop_deactivated(self):
        if self.pop.summoned and not self.pop.preview:
            self.send_pop_home()

    def show_panel(self, preview: bool):
        if self._pop_showing():
            if not self.pop.summoned:
                self.summon_pop(activate=not preview)
            elif not preview:
                self.pop.preview = False
                self.pop.activateWindow()
            return
        if self.panel.isVisible() and not self.panel._slide_hide:
            if preview:
                return
            self.panel.preview = False
            self.panel.activateWindow()
            return
        self.panel.preview = preview
        self.panel.slide_in(self._panel_pos(), activate=not preview)

    def hide_panel(self):
        self.panel.slide_out()
        self._last_hide = time.monotonic()

    def _panel_deactivated(self):
        if not self.panel.preview:
            self.hide_panel()

    # --- pop-out
    def show_popout(self):
        pos = self.cfg.get("popout_pos")
        target = QPoint(*pos) if pos else None
        if target is None or QGuiApplication.screenAt(target + QPoint(20, 20)) is None:
            avail = QGuiApplication.primaryScreen().availableGeometry()
            target = QPoint(avail.right() - self.pop.width() - 24, avail.bottom() - self.pop.height() - 24)
        self.pop.slide_in(target, activate=False)
        self.panel.popped = True
        self.act_pop.setText("Pop in")
        self.cfg["popout"] = True
        save_config(self.cfg)

    def hide_popout(self):
        self._pop_anim.stop()
        self.pop.summoned = self.pop.preview = False
        self._pop_home = None
        self.pop.slide_out()
        self.panel.popped = False
        self.act_pop.setText("Pop out")
        self.cfg["popout"] = False
        save_config(self.cfg)

    def toggle_popout(self):
        if self.pop.isVisible() and not self.pop._slide_hide:
            self.hide_popout()
        else:
            self.show_popout()
            self.hide_panel()

    def _save_pop_pos(self, pos: QPoint):
        if self.pop.summoned:   # dragged somewhere new while visiting the tray: that's home now
            self.pop.summoned = self.pop.preview = False
            self._pop_home = None
        self.cfg["popout_pos"] = [pos.x(), pos.y()]
        save_config(self.cfg)

    # --- files
    def _previewing(self):
        """The card that a drag over the tray icon brought up, if any."""
        if self.pop.summoned and self.pop.preview:
            return self.pop
        if self.panel.isVisible() and self.panel.preview:
            return self.panel
        return None

    def _on_drop(self, paths: list[str]):
        self.player.load_list(paths)
        if self._previewing() is not None:
            self._preview_timer.start(2600)

    def open_file(self):
        start = str(Path(self.player.path).parent) if self.player.path else str(Path.home() / "Music")
        exts = " ".join(f"*{e}" for e in sorted(AUDIO_EXTS))
        paths, _ = QFileDialog.getOpenFileNames(None, "Choose audio files", start, f"Audio ({exts})")
        if paths:
            self.player.load_list(paths)

    def _on_connection(self):
        sock = self.server.nextPendingConnection()
        if sock is None:
            return

        def read():
            msg = bytes(sock.readAll()).decode("utf-8", "replace")
            if msg.strip() == "quit":   # the updater asks us to close before replacing files
                self.quit()
                return
            files = [f for f in msg.splitlines() if f.strip() and Path(f).is_file()]
            if files:
                self.player.load_list(files)
            else:
                self.show_panel(preview=False)
        sock.readyRead.connect(read)

    # --- drag over tray icon
    def _poll_drag(self):
        down = left_button_down()
        if not down and not self._was_down:
            return   # nothing held: skip the (comparatively slow) tray icon lookup
        pos = QCursor.pos()
        rect = self.tray.geometry()
        valid = rect.isValid() and not rect.isEmpty()
        if down and not self._was_down:
            self._press_outside = not (valid and rect.adjusted(-6, -6, 6, 6).contains(pos))
        if down and self._press_outside and valid and rect.adjusted(-4, -4, 4, 4).contains(pos) \
                and not self.catcher.isVisible():
            self.catcher.setGeometry(rect.adjusted(-3, -3, 3, 3))
            self.catcher.show()
            self.catcher.raise_()
        if not down and self._was_down:
            if self.catcher.isVisible():
                QTimer.singleShot(300, self.catcher.hide)
            if self._previewing() is not None and not self._preview_timer.isActive():
                self._preview_timer.start(900)
        self._was_down = down

    def _preview_check(self):
        card = self._previewing()
        if card is None:
            return
        if left_button_down() or card.geometry().contains(QCursor.pos()):
            self._preview_timer.start(700)
        elif card is self.pop:
            self.send_pop_home()
        else:
            self.hide_panel()

    # --- Caps Lock peek-through
    CAPS_HOLD_S = 0.18   # a quick tap is just Caps Lock; holding it peeks through the cards

    def _poll_caps(self):
        now = time.monotonic()
        down = key_down(VK_CAPITAL)
        mouse = any_mouse_button_down()
        if now < self._caps_ignore_until:   # our own corrective tap
            self._caps_was, self._mouse_was = down, mouse
            return
        cards = [c for c in (self.panel, self.pop) if c.isVisible() and not c._slide_hide]
        if down and not self._caps_was:
            self._caps_t0 = now
            self._caps_used = False
        if down and now - self._caps_t0 >= self.CAPS_HOLD_S:
            for c in cards:
                c.set_ghost(True)
            # "used" = a click landed through a ghosted card
            if mouse and not self._mouse_was:
                pos = QCursor.pos()
                if any(c.ghosted and c.frameGeometry().contains(pos) for c in cards):
                    self._caps_used = True
        if not down and self._caps_was:
            for c in (self.panel, self.pop):
                c.set_ghost(False)
            # the press toggled Caps Lock; if it was only used to click through, undo that
            if self._caps_used and caps_lock_on() != self._caps_before:
                tap_caps_lock()
                self._caps_ignore_until = now + 0.3
            self._caps_used = False
        if not down and now >= self._caps_ignore_until:
            self._caps_before = caps_lock_on()
        self._caps_was, self._mouse_was = down, mouse

    # --- fullscreen
    def _poll_fullscreen(self):
        fs = foreground_is_fullscreen()
        if fs == self._fs_seen:
            self._fs_count += 1
        else:
            self._fs_seen, self._fs_count = fs, 1
        if self._fs_count < 2 or fs == self._fs_state:
            return
        self._fs_state = fs
        if fs and self.player.want_playing:
            self.player.pause(auto=True)
        elif not fs and self.player.auto_paused:
            self.player.play()

    def set_fullscreen_pause(self, on: bool):
        self._fs_state = self._fs_seen = False
        self._fs_count = 0
        if on:
            self._fs_timer.start()
        else:
            self._fs_timer.stop()
            if self.player.auto_paused:   # it was paused for a fullscreen program: carry on
                self.player.play()
        self.cfg["pause_fullscreen"] = on
        save_config(self.cfg)

    # --- misc
    def set_refresh_rate(self, hz: int):
        self.refresh_hz = hz
        self.cfg["refresh_hz"] = hz
        save_config(self.cfg)
        for card in (self.panel, self.pop, self.hud):
            card.backdrop.set_rate(hz, self.refresh_boost)

    def set_refresh_boost(self, on: bool):
        self.refresh_boost = on
        self.cfg["refresh_boost"] = on
        save_config(self.cfg)
        for card in (self.panel, self.pop, self.hud):
            card.backdrop.set_rate(self.refresh_hz, on)

    def _volume_wheel(self, card, steps: float):
        want = self.player.volume + steps / VOLUME_NOTCHES   # one full turn of a typical wheel: silent to full
        self.player.set_volume(want)
        over = want - self.player.volume                 # how far past an end the wheel pushed
        self.hud.poke(card, overscroll=(1.0 if over > 0 else -1.0) * min(1.0, abs(over) * VOLUME_NOTCHES) if over else 0.0)
        self._volume_save.start()

    def _set_volume(self, v: float):
        self.player.set_volume(v)
        self._volume_save.start()

    def _save_volume(self):
        self.cfg["volume"] = round(self.player.volume, 4)
        save_config(self.cfg)

    def set_style(self, key: str):
        self.player.style = key
        for card in (self.panel, self.pop):
            if key in OPAQUE_STYLES:
                card.backdrop.stop()
            elif card.isVisible() and card.glass:
                card.backdrop.start()
            card.reset_sim()
            card._edge_mask = None
            card._lens_serial = -1
            card._backdrop_updated()
        self.cfg["style"] = key
        save_config(self.cfg)
        self.player.changed.emit()

    def set_shuffle(self, on: bool):
        self.player.set_shuffle(on)   # emits changed, which saves the new order
        self.cfg["shuffle"] = on
        save_config(self.cfg)

    def set_normalize(self, on: bool):
        self.player.engine.normalize = on
        self.cfg["normalize"] = on
        save_config(self.cfg)

    def set_capturable(self, on: bool, save: bool = True):
        for w in (self.panel, self.pop, self.hud):
            w.backdrop.set_capturable(on)
        if save:
            self.cfg["capturable"] = on
            save_config(self.cfg)

    def set_bouncy(self, on: bool):
        self.pop.bouncy = on   # not saved: every launch starts with it off

    def check_updates(self):
        import subprocess
        subprocess.Popen([str(updater_path()), "--from-app"], close_fds=True)

    def _toggle_startup(self, on: bool):
        try:
            set_startup(on)
        except OSError:
            self.act_startup.setChecked(startup_enabled())

    def _save_position(self):
        """Remember the spot in the current song (on quit, on Windows shutdown, and
        every few seconds while playing in case DiscTray is closed forcefully)."""
        if not self.player.path:
            return
        pos = int(self.player.position())
        if abs(pos - int(self.cfg.get("position_ms", -10_000))) < 900:
            return
        self.cfg["position_ms"] = pos
        save_config(self.cfg)

    def quit(self):
        self._save_position()
        for card in (self.panel, self.pop, self.hud):
            card.backdrop.stop()
        self.tray.hide()
        self.player.engine.stop()
        QApplication.quit()


def _log_exception(exc_type, exc, tb):
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with open(DATA_DIR / "error.log", "a", encoding="utf-8") as f:
            f.write(time.strftime("\n[%Y-%m-%d %H:%M:%S]\n"))
            traceback.print_exception(exc_type, exc, tb, file=f)
    except Exception:
        pass
    sys.__excepthook__(exc_type, exc, tb)


def selftest(audio: str, out: str) -> int:
    """`DiscTray.exe --selftest <audio> <out.json>`: decode a file without playing it
    and report, so a packaged build can be checked for working audio codecs."""
    app = QCoreApplication(sys.argv)
    engine = AudioEngine()
    engine._wake = lambda: None      # never open the speakers
    result = {"version": __version__, "ok": False, "frames": 0, "error": None}

    def finish():
        job = engine._job
        result["frames"] = int(job.n) if job else 0
        result["ok"] = bool(job and job.complete and job.n > 0)
        Path(out).write_text(json.dumps(result), encoding="utf-8")
        engine.stop()
        app.quit()

    engine.loaded.connect(finish)
    engine.failed.connect(lambda msg: (result.__setitem__("error", msg), finish()))
    QTimer.singleShot(20000, finish)
    engine.load(audio)
    app.exec()
    return 0 if result["ok"] else 1


def main() -> int:
    sys.excepthook = _log_exception
    if len(sys.argv) >= 4 and sys.argv[1] == "--selftest":
        return selftest(sys.argv[2], sys.argv[3])
    initial = [a for a in sys.argv[1:] if Path(a).is_file()]
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    app.setApplicationName(APP_NAME)

    # single instance: hand the file (or a "show") to the running copy
    sock = QLocalSocket()
    sock.connectToServer(SERVER_NAME)
    if sock.waitForConnected(300):
        sock.write(("\n".join(initial) or "show").encode("utf-8"))
        sock.flush()
        sock.waitForBytesWritten(500)
        sock.disconnectFromServer()
        return 0
    QLocalServer.removeServer(SERVER_NAME)
    server = QLocalServer()
    server.listen(SERVER_NAME)

    _app = DiscTrayApp(server, initial)  # noqa: F841 (kept alive for the event loop)
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
