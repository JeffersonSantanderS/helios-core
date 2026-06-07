import sys
from pathlib import Path

# Ensure the project root (containing the helios/ package) is in sys.path.
# pytest's pythonpath setting in pyproject.toml sometimes doesn't take effect
# depending on the pytest version and venv configuration. This is a reliable fallback.
_project_root = str(Path(__file__).parent)
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)