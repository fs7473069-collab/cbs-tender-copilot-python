import os

import pika
from dotenv import load_dotenv

load_dotenv()


def create_connection():
    credentials = pika.PlainCredentials(
        os.getenv("RABBITMQ_USERNAME"),
        os.getenv("RABBITMQ_PASSWORD"),
    )

    parameters = pika.ConnectionParameters(
        host=os.getenv("RABBITMQ_HOST"),
        port=int(os.getenv("RABBITMQ_PORT", "5672")),
        virtual_host=os.getenv("RABBITMQ_VHOST", "/"),
        credentials=credentials,
        ssl_options=None,
    )

    return pika.BlockingConnection(parameters)