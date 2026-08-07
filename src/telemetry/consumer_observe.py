"""Observation-path consumer.

Reads the bounded, deliberately lossy queue that has no dead-letter exchange.
Logs only.

Implemented at stage 6.
"""

from telemetry.stub import announce


def main() -> None:
    announce("consumer_observe", "stage 6")


if __name__ == "__main__":
    main()
