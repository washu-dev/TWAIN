import sys
from pathlib import Path

# Ensure the api/ directory is on the path regardless of where pytest is invoked
sys.path.insert(0, str(Path(__file__).parent))
