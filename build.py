"""Build the Windows release:

    python build.py

produces, in dist/:
    DiscTray/                         the app (folder build, starts fast)
    DiscTray-<version>-win64.zip      that folder zipped: what the updater downloads
    DiscTrayUpdater.exe               the per-user installer / updater (C#, compiled with Windows' own csc.exe)
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DIST, BUILD = ROOT / "dist", ROOT / "build"
ICON = ROOT / "assets" / "disctray.ico"
PUBLISHER = "ASQUALL-hub"


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


def version_resource(ver: str, name: str, description: str) -> Path:
    """A PyInstaller version file, so the exe says what it is under Properties > Details."""
    nums = (tuple(int(n) for n in re.findall(r"\d+", ver)) + (0, 0, 0, 0))[:4]
    path = BUILD / f"{name}-version.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = {"CompanyName": PUBLISHER, "FileDescription": description, "FileVersion": ver,
              "InternalName": name, "LegalCopyright": PUBLISHER, "OriginalFilename": f"{name}.exe",
              "ProductName": "DiscTray", "ProductVersion": ver}
    strings = ", ".join(f"StringStruct({k!r}, {v!r})" for k, v in fields.items())
    path.write_text(
        f"VSVersionInfo(ffi=FixedFileInfo(filevers={nums}, prodvers={nums}, mask=0x3f, flags=0x0, OS=0x40004,"
        f" fileType=0x1, subtype=0x0, date=(0, 0)), kids=[StringFileInfo([StringTable('040904B0', [{strings}])]),"
        f" VarFileInfo([VarStruct('Translation', [1033, 1200])])])\n", encoding="utf-8")
    return path


def build_updater(ver: str) -> Path:
    """Compile updater/Updater.cs with the C# compiler that comes with Windows (.NET Framework 4)."""
    csc = Path(os.environ["WINDIR"]) / "Microsoft.NET" / "Framework64" / "v4.0.30319" / "csc.exe"
    gac = Path(os.environ["WINDIR"]) / "Microsoft.NET" / "assembly" / "GAC_MSIL"
    refs = ["System.dll", "System.Core.dll", "System.Drawing.dll", "System.Windows.Forms.dll",
            "System.Web.Extensions.dll", "Microsoft.CSharp.dll"]
    for lib in ("System.IO.Compression", "System.IO.Compression.FileSystem"):   # not next to csc.exe, only in the GAC
        refs.append(str(next((gac / lib).glob(f"v4.0_*/{lib}.dll"))))
    nums = ".".join(((re.findall(r"\d+", ver)) + ["0"] * 4)[:4])
    info = BUILD / "updater" / "AssemblyInfo.cs"
    info.parent.mkdir(parents=True, exist_ok=True)
    info.write_text("using System.Reflection;\n" + "".join(f'[assembly: {k}("{v}")]\n' for k, v in (
        ("AssemblyTitle", "DiscTray Updater"), ("AssemblyDescription", "Installs and updates DiscTray for this user"),
        ("AssemblyCompany", PUBLISHER), ("AssemblyProduct", "DiscTray"), ("AssemblyCopyright", PUBLISHER),
        ("AssemblyVersion", nums), ("AssemblyFileVersion", nums), ("AssemblyInformationalVersion", ver))),
        encoding="utf-8")
    out = DIST / "DiscTrayUpdater.exe"
    subprocess.run([str(csc), "/nologo", "/target:winexe", "/platform:anycpu", "/optimize+", f"/out:{out}",
                    f"/win32icon:{ICON}", f"/win32manifest:{ROOT / 'updater' / 'app.manifest'}",
                    *(f"/r:{r}" for r in refs), str(ROOT / "updater" / "Updater.cs"), str(info)],
                   cwd=ROOT, check=True)
    return out


def main() -> None:
    ver = version()
    if not ICON.is_file():
        make_icon()
    shutil.rmtree(DIST, ignore_errors=True)

    pyinstaller("--windowed", "--name", "DiscTray", "--icon", str(ICON),
                "--workpath", str(BUILD / "app"), "--specpath", str(BUILD),
                "--version-file", str(version_resource(ver, "DiscTray", "DiscTray music player")),
                "--exclude-module", "tkinter",
                str(ROOT / "DiscTray.pyw"))
    archive = DIST / f"DiscTray-{ver}-win64.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for f in sorted((DIST / "DiscTray").rglob("*")):
            if f.is_file():
                z.write(f, f.relative_to(DIST / "DiscTray"))

    build_updater(ver)

    print(f"\nDiscTray {ver}:")
    for f in (archive, DIST / "DiscTrayUpdater.exe"):
        print(f"  {f.relative_to(ROOT)}  {f.stat().st_size / 1e6:.1f} MB")


if __name__ == "__main__":
    main()
