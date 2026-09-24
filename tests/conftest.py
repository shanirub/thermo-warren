"""Test environment setup.

config.py instantiates Settings at import time, and rabbitmq_host, _user and
_password have no defaults -- deliberately, so a forgotten compose override
fails by name rather than quietly falling back to localhost. The consequence is
that the environment must be populated before telemetry.config is first
imported, which is why this runs at module scope rather than in a fixture.

setdefault, not assignment: a real .env or a real environment wins, so these
values only apply when nothing else supplied them.
"""

import os

os.environ.setdefault("RABBITMQ_HOST", "localhost")
os.environ.setdefault("RABBITMQ_USER", "test")
os.environ.setdefault("RABBITMQ_PASSWORD", "test")
os.environ.setdefault("LOG_LEVEL", "WARNING")

# Stage 11. Unlike the three above this field HAS a default -- an empty string,
# so that topology, publisher and consumer_observe can start without a token
# they never use. That makes it a different trap rather than no trap: with
# nothing set here the suite would read a real token out of a developer's .env
# and pass, then fail on a fresh clone where consumer_store.main() bails with
# EXIT_UNCONFIGURED before run_consumer is ever reached. A value that is
# obviously not a credential keeps the tests independent of the machine.
os.environ.setdefault("INFLUXDB_TOKEN", "test-token-not-a-real-credential")
