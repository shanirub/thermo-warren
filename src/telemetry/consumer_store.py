"""Durable-path consumer.

Manual acknowledgment, bounded prefetch, dead-letter exchange attached. Parses
and acknowledges from stage 6; the InfluxDB write is inserted between parse and
acknowledgment at stage 11.

Implemented at stage 6, extended at stages 7-9 and 11.
"""

from telemetry.stub import announce


def main() -> None:
    announce("consumer_store", "stage 6")


if __name__ == "__main__":
    main()
