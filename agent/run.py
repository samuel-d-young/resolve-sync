"""Frozen-app entry point.

PyInstaller needs a real script rather than ``-m resolve_sync``. Everything of
substance lives in resolve_sync.__main__.main(), which is written to survive
having no console (see the notes there).
"""

from resolve_sync.__main__ import main

if __name__ == "__main__":
    main()
