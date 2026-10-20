"""Launch the local HTML collection panel without importing the old CLI."""
from pathlib import Path
from dashboard.web import main

if __name__ == '__main__':
    raise SystemExit(main(Path(__file__).resolve().parent))
