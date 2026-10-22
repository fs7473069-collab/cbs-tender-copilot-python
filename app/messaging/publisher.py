import json
import pika

from app.messaging.rabbitmq import create_connection

RESPONSE_QUEUE = "rfp_docs_response"


def publish_message(message):
    connection = create_connection()

    try:
        channel = connection.channel()

        channel.queue_declare(
            queue=RESPONSE_QUEUE,
            durable=True,
        )

        channel.confirm_delivery()

        confirmed = channel.basic_publish(
            exchange="",
            routing_key=RESPONSE_QUEUE,
            body=json.dumps(message, ensure_ascii=False),
            properties=pika.BasicProperties(
                content_type="application/json",
                content_encoding="utf-8",
                delivery_mode=2,
            ),
            mandatory=True,
        )

        print(f"Publish result: {confirmed!r}")
        print(f"Publish call completed for queue: {RESPONSE_QUEUE}")

    except pika.exceptions.UnroutableError as exc:
        print(f"Message could not be routed: {exc}")
        raise

    except pika.exceptions.NackError as exc:
        print(f"RabbitMQ rejected the message: {exc}")
        raise

    finally:
        if connection.is_open:
            connection.close()
