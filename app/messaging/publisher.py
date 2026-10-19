
import json
import pika

from app.messaging.rabbitmq import create_connection


RESPONSE_QUEUE = "rfp_docs_response"


def publish_message(message):
    connection = create_connection()

    try:
        channel = connection.channel()

        # Create the test queue if it does not exist.
        channel.queue_declare(
            queue=RESPONSE_QUEUE,
            durable=True,
        )

        # Publish directly to the test queue using RabbitMQ's default exchange.
        channel.basic_publish(
            exchange="",
            routing_key=RESPONSE_QUEUE,
            body=json.dumps(message, ensure_ascii=False),
            properties=pika.BasicProperties(
                content_type="application/json",
                content_encoding="utf-8",
                delivery_mode=2,
            ),
        )

    finally:
        connection.close()