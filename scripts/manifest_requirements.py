#!/usr/bin/env python3
"""Print the integration's runtime requirements from manifest.json, one per line."""

import json
from pathlib import Path

MANIFEST = Path(__file__).resolve().parent.parent / "custom_components" / "alarmdotcom" / "manifest.json"

print("\n".join(json.loads(MANIFEST.read_text(encoding="utf-8"))["requirements"]))
