# DiscTray

A tray-only music player for Windows. Drop an audio file on its tray icon and it plays, on a spinning record under glass.

- **Lives in the tray.** No taskbar button and no main window. Click the icon for the player; it closes when you click away.
- **Drop and play.** Drag a file onto the tray icon (or the player) and the old track eases out while the new one eases in. Drop several files (or a folder) to get a playlist that loops; drop one file to go back to looping just that.
- **Turntable feel.** Pausing winds the record down, so the speed and the pitch drop together. Playing spins it back up. The audio engine is sample-accurate, so there are no clicks or pops.
- **Pop out.** The pop-out button gives you a draggable, always-on-top copy that glides with your mouse.
- **Gets out of the way.**
  - It pauses while another program is truly fullscreen and resumes afterwards.
  - Hold **Caps Lock** and the player fades to a ghost you can click straight through. If you only held it to click through, Caps Lock is switched back for you.
- **Volume.** Hold **Ctrl** and scroll over the player for an iOS-style glass volume bar.
- **Even loudness.** Volume normalization (on by default) measures every track the way streaming services do (EBU R128 / BS.1770) and plays them all at -14 LUFS, without clipping.
- **Hi-Res.** Lossless files (FLAC, WAV, AIFF, ALAC...) get a small *Hi-Res* tag under the title.
- **Throw it.** With *Bouncy pop-out* on, fling the pop-out and it keeps its momentum and bounces off the screen edges.
- **Settings** (tray menu → Settings): volume normalization, bouncy pop-out, glass refresh rate, start with Windows.
- **Styles** (tray menu → Style):
  - **Glassy:** Liquid Glass-style squircles with live blur and edge refraction.
  - **Glossy Glass:** a glass record and glossy lettering.
  - **Frutiger Aero.**
  - **Snow Globe:** snow with physics. Shake the pop-out!
  - **Liquid:** water that sloshes when you move it.

## Install

1. Download **`DiscTrayUpdater.exe`** from the [latest release](https://github.com/ASQUALL-hub/disctray/releases/latest).
2. Run it. It installs the newest version for your user only (no admin rights) to `%LOCALAPPDATA%\Programs\DiscTray`, adds a Start Menu shortcut and starts DiscTray.

The exe isn't code-signed, so Windows SmartScreen may warn the first time: choose **More info → Run anyway**.

**Updating:** tray menu → **Check for updates…**, or run the updater again.
**Uninstalling:** *Settings → Apps → DiscTray → Uninstall*, or `DiscTrayUpdater.exe --uninstall`.

New tray icons go into the hidden **^** area on Windows 11. You can drop files onto the **^** arrow, or pin DiscTray under *Settings → Personalization → Taskbar → Other system tray icons* to drop straight onto it.

## Run from source

```bash
pip install -r requirements.txt
pythonw DiscTray.pyw
```

Settings and the last playlist are stored in `%APPDATA%\DiscTray`.

## Build a release

```bash
pip install pyinstaller pillow
python build.py
```

This writes `dist/DiscTray-<version>-win64.zip` (what the updater downloads) and `dist/DiscTrayUpdater.exe`. To publish, bump `__version__` in `disctray.py`, build, then:

```bash
gh release create v<version> dist/DiscTray-<version>-win64.zip dist/DiscTrayUpdater.exe
```

The updater always installs the release marked *latest*.
