import sys
from pathlib import Path

# Keep the skill directory clean: importing research.py must not create __pycache__.
sys.dont_write_bytecode = True

SCRIPTS = Path(__file__).resolve().parents[1] / "skills" / "deep-research" / "scripts"
sys.path.insert(0, str(SCRIPTS))
