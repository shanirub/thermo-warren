"""One-shot topology declarer.

Declares the exchange bindings, both queues, the dead-letter exchange and the
dead-letter queue. Must complete before anything publishes: a topic exchange
with no matching binding discards messages silently.

Implemented at stage 4.
"""

from telemetry.stub import announce


def main() -> None:
    announce("topology", "stage 4")


if __name__ == "__main__":
    main()
