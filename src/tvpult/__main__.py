"""`python -m tvpult` — used by the desktop shortcut (pythonw -m tvpult app: no console window)."""

from .cli import main

raise SystemExit(main())
