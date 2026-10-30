# Double-click to start DiscTray without a console window.
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from disctray import main

sys.exit(main())
