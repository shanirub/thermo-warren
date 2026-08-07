"""Stage 2 scaffolding.

Every module is a stub that resolves configuration, prints it, and exits 0.
That is the whole of stage 2's Definition of Done: the same module run two
ways must resolve two different broker hostnames.

Delete each call site as its module gains real behaviour.
"""

import sys

from telemetry.config import settings


def announce(module: str, stage: str) -> None:
    print(f"[{module}] stub -- implemented at {stage}")
    print(f"[{module}] resolved configuration:")
    print(settings.describe())
    sys.stdout.flush()
