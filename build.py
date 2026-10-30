"""Build the Windows release:

    python build.py

produces, in dist/:
    DiscTray/                         the app (folder build, starts fast)
    DiscTray-<version>-win64.zip      that folder zipped: what the updater downloads
    DiscTrayUpdater.exe               the per-user installer / updater
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DIST, BUILD = ROOT / "dist", ROOT / "build"
ICON = ROOT / "assets" / "disctray.ico"


def version() -> str:
    return re.search(r'__version__ = "([^"]+)"', (ROOT / "disctray.py").read_text(encoding="utf-8")).group(1)


def make_icon() -> None:
    """Render the app's record (no cover art) into a multi-size .ico."""
    from io import BytesIO

    from PIL import Image
    from PySide6.QtCore import QBuffer, QIODevice
    from PySide6.QtWidgets import QApplication

    sys.path.insert(0, str(ROOT))
    import disctray

    app = QApplication.instance() or QApplication([])
    pm = disctray.render_disc(None, 256, seed=262)
    buf = QBuffer()
    buf.open(QIODevice.WriteOnly)
    pm.toImage().save(buf, "PNG")
    img = Image.open(BytesIO(bytes(buf.data())))
    ICON.parent.mkdir(exist_ok=True)
    img.save(ICON, sizes=[(16, 16), (20, 20), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    del app


def pyinstaller(*args: str) -> None:
    subprocess.run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
                    "--distpath", str(DIST), *args], cwd=ROOT, check=True)


def main() -> None:
    ver = version()
    if not ICON.is_file():
        make_icon()
    shutil.rmtree(DIST, ignore_errors=True)

    pyinstaller("--windowed", "--name", "DiscTray", "--icon", str(ICON),
                "--workpath", str(BUILD / "app"), "--specpath", str(BUILD),
                "--exclude-module", "tkinter",
                str(ROOT / "DiscTray.pyw"))
    archive = DIST / f"DiscTray-{ver}-win64.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for f in sorted((DIST / "DiscTray").rglob("*")):
            if f.is_file():
                z.write(f, f.relative_to(DIST / "DiscTray"))

    pyinstaller("--onefile", "--windowed", "--name", "DiscTrayUpdater", "--icon", str(ICON),
                "--workpath", str(BUILD / "updater"), "--specpath", str(BUILD),
                "--add-data", f"{ICON};assets",
                str(ROOT / "updater.py"))

    print(f"\nDiscTray {ver}:")
    for f in (archive, DIST / "DiscTrayUpdater.exe"):
        print(f"  {f.relative_to(ROOT)}  {f.stat().st_size / 1e6:.1f} MB")


if __name__ == "__main__":
    main()
