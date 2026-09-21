"""Single resolution point for every host, port and credential in the project.

No module anywhere else may contain a literal hostname, port or password. That
rule is what allows one codebase to run both inside the compose network and
directly on the host: only the values change, never the code.

Resolution order (pydantic-settings default, highest priority first):
    1. process environment  -- what compose injects per service
    2. .env file            -- the host-mode values, gitignored
    3. field default        -- only where a default is genuinely safe

Fields with no default are required. A missing one raises at import time rather
than at connect time, so the failure names the variable instead of surfacing
later as a confusing connection error.

Scope note: this file holds what *differs between run modes* -- endpoints,
credentials, and each process's own behaviour. Topology names and the queue
arguments the broker enforces live in `topology_spec.py` instead, because they
must be identical in every mode and a silent mismatch there produces no error
anywhere. See that module's docstring for the rule.
"""

from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        # Later stages add keys to .env before this class grows fields for them,
        # so unknown keys are ignored rather than fatal. Cost: a typo'd key in
        # .env is silently inert. Switch to "forbid" if that trade stops paying.
        extra="ignore",
    )

    # --- RabbitMQ connection ---------------------------------------------
    # No default: compose must override this explicitly, and a forgotten
    # override fails loudly instead of quietly falling back to localhost.
    rabbitmq_host: str

    # Defaults are the IANA/protocol standard ports, safe to assume.
    rabbitmq_amqp_port: int = 5672
    rabbitmq_mqtt_port: int = 1883
    rabbitmq_management_port: int = 15672

    rabbitmq_user: str
    rabbitmq_password: str
    rabbitmq_vhost: str = "/"

    # Retry is owned by the connecting module, not by pika -- see
    # topology.connect(). The compose healthcheck is liveness only, and host
    # mode has no healthcheck gate at all, so a bounded retry is worth having.
    rabbitmq_connect_attempts: int = 5
    rabbitmq_connect_retry_delay: float = 2.0

    # --- This process's own behaviour --------------------------------------
    # Literal rather than str: a typo'd level fails at startup naming the field,
    # instead of surfacing later as a confusing logging error.
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"

    # --- Publisher (stage 5) ------------------------------------------------
    # Same broker as above, connected to over MQTT instead of AMQP -- reuses
    # rabbitmq_host, rabbitmq_mqtt_port, rabbitmq_user and rabbitmq_password.

    # Stage 17's firmware must connect with a *different* client id: two MQTT
    # clients sharing one id cause the broker to disconnect the first one, and
    # the resulting flapping is hard to diagnose from the log alone.
    mqtt_client_id: str = "telemetry-sim"

    # The "device" field in the payload, and an InfluxDB tag from stage 11.
    device_id: str = "sim-01"

    # Steady-state publish rate. Corruption and burst are CLI flags instead of
    # config fields -- they are hand-run experiments for stages 7 and 9, and a
    # flag is unreachable from a normal `docker compose up` in a way a config
    # field is not.
    publish_interval_seconds: float = 1.0

    # --- Consumers (stage 6) ------------------------------------------------
    # How many messages the broker may have in flight, unacknowledged, to
    # consumer_store at once. Two meanings, and the second is the one that
    # bites later: it is also the at-least-once duplicate window, because an
    # unclean crash redelivers every unacknowledged message. 10 duplicates is
    # what stage 11 has to decide what to do about.
    #
    # 10 rather than 1 because a cap of 1 is unobservable -- "unacked pinned at
    # 1" looks identical whether basic_qos was called or not, so stage 6's DoD
    # check would pass without proving anything. 10 is reachable in ~20s with
    # --ack-delay 2 against the 1 Hz publisher, with no backlog to set up first.
    #
    # consumer_observe deliberately has no equivalent: it uses automatic
    # acknowledgment, and basic_qos is ignored on such a channel.
    consumer_prefetch_count: int = 10

    # --- Later stages ------------------------------------------------------
    # Topology names and queue arguments              -> topology_spec.py
    # InfluxDB url / org / bucket / token             -> stage 10

    @property
    def amqp_url(self) -> str:
        """AMQP URI with the password masked -- for logging, not for connecting."""
        return (
            f"amqp://{self.rabbitmq_user}:***@"
            f"{self.rabbitmq_host}:{self.rabbitmq_amqp_port}{self.rabbitmq_vhost}"
        )

    def describe(self) -> str:
        """Human-readable resolved configuration, with secrets masked."""
        lines = [
            f"  rabbitmq_host        = {self.rabbitmq_host}",
            f"  rabbitmq_amqp_port   = {self.rabbitmq_amqp_port}",
            f"  rabbitmq_mqtt_port   = {self.rabbitmq_mqtt_port}",
            f"  rabbitmq_user        = {self.rabbitmq_user}",
            f"  rabbitmq_password    = {'*' * 8 if self.rabbitmq_password else '(empty)'}",
            f"  rabbitmq_vhost       = {self.rabbitmq_vhost}",
            f"  amqp_url             = {self.amqp_url}",
            f"  connect_attempts     = {self.rabbitmq_connect_attempts}",
            f"  connect_retry_delay  = {self.rabbitmq_connect_retry_delay}s",
            f"  log_level            = {self.log_level}",
            f"  mqtt_client_id       = {self.mqtt_client_id}",
            f"  device_id            = {self.device_id}",
            f"  publish_interval_s   = {self.publish_interval_seconds}",
            f"  consumer_prefetch    = {self.consumer_prefetch_count}",
        ]
        return "\n".join(lines)


settings = Settings()
