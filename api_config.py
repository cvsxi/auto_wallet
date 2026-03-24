from __future__ import annotations

import os


# This module intentionally reads the key from the environment.
# Do not hardcode secrets here before publishing the repository.
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
