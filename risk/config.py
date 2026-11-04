"""Single source of exit parameters, resolved independently of the working directory."""
import json
from pathlib import Path


def load_config():
    return json.loads((Path(__file__).resolve().parents[1] / "baseline.json").read_text())
