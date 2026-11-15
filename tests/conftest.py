import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
os.environ["DATABASE_URL"] = f"sqlite:///{(ROOT / 'var' / 'test_api.db').as_posix()}"
os.environ["SKYQ_CONTROL_ENABLED"] = "false"
os.environ["SKYQ_CONTROL_TOKEN"] = ""
