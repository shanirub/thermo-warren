"""Software publisher standing in for the MCU.

Emits the payload contract at ~1 Hz over MQTT at QoS 1, with controls for rate,
periodic malformed output, and burst publishing. Defines the contract the
stage 17 firmware must satisfy.

Implemented at stage 5.
"""

from telemetry.stub import announce


def main() -> None:
    announce("publisher", "stage 5")


if __name__ == "__main__":
    main()
