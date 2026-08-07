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
"""

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

    # --- Later stages ------------------------------------------------------
    # Topology names (exchange, queues, binding key)  -> stage 4
    # InfluxDB url / org / bucket / token             -> stage 10
    # Publisher rate and corruption controls          -> stage 5

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
        ]
        return "\n".join(lines)


settings = Settings()
