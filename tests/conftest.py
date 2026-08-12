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
