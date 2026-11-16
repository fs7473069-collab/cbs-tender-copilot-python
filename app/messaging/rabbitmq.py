
import os

import pika
from dotenv import load_dotenv

load_dotenv()

# Analysis request queue. Prefer RABBITMQ_REQUEST_QUEUE; fall back to the
# legacy RABBITMQ_QUEUE name used by existing deployments.
ANALYSIS_REQUEST_QUEUE_ENV = "RABBITMQ_REQUEST_QUEUE"
ANALYSIS_REQUEST_QUEUE_LEGACY_ENV = "RABBITMQ_QUEUE"
ANALYSIS_REQUEST_QUEUE_DEFAULT = "rfp-python-test"


def configured_analysis_request_queue():
    """Queue the analysis consumer listens on. Never used as a response destination."""
    for name in (ANALYSIS_REQUEST_QUEUE_ENV, ANALYSIS_REQUEST_QUEUE_LEGACY_ENV):
        raw = os.getenv(name)
        if isinstance(raw, str) and raw.strip():
            return raw.strip()
    return ANALYSIS_REQUEST_QUEUE_DEFAULT


def create_connection():
    username = os.getenv("RABBITMQ_USERNAME")
    password = os.getenv("RABBITMQ_PASSWORD")
    host = os.getenv("RABBITMQ_HOST")
    port = int(os.getenv("RABBITMQ_PORT", "5672"))
    virtual_host = os.getenv("RABBITMQ_VHOST", "/")

    if not all([username, password, host]):
        raise ValueError(
            "Missing RabbitMQ configuration. "
            "Check RABBITMQ_USERNAME, RABBITMQ_PASSWORD, "
            "and RABBITMQ_HOST in your environment."
        )

    credentials = pika.PlainCredentials(username, password)

    parameters = pika.ConnectionParameters(
        host=host,
        port=port,
        virtual_host=virtual_host,
        credentials=credentials,
        heartbeat=60,
        blocked_connection_timeout=120,
        socket_timeout=10,
        connection_attempts=3,
        retry_delay=2,
    )

    return pika.BlockingConnection(parameters)
