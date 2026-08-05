"""Put the repo root on sys.path so `shared.*` and `services.*` both import.

Only the repo root is added, deliberately. Adding the service directory too
would make `app.pipeline` and `services.validation_pipeline.app.pipeline`
two distinct module objects with separate class identities, and isinstance
checks across them would silently fail.
"""

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
