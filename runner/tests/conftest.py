import sys
from pathlib import Path

# Put the repo root on sys.path so `import runner.*` resolves under pytest.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
