
import json
import pika

from app.messaging.rabbitmq import create_connection

RESPONSE_QUEUE = "rfp_docs_response1"


def publish_message(message):
    connection = create_connection()

    try:
        channel = connection.channel()

        channel.queue_declare(
            queue=RESPONSE_QUEUE,
            durable=True,
        )

        # Confirm that RabbitMQ accepts the message.
        channel.confirm_delivery()

        # Serialize the complete message into UTF-8 bytes.
        body = json.dumps(
            message,
            ensure_ascii=False,
        ).encode("utf-8")

        # Verify the message before publishing.
        print("Queue:", RESPONSE_QUEUE)
        print("Publishing bytes:", len(body))

        requirements = (
            message.get("result", {}).get("requirements", [])
        )
        print("Requirements before publish:", len(requirements))

        # Pika 1.4.x waits for publisher confirms inside basic_publish
        # when confirm_delivery() is enabled. On success it returns None.
        # Broker rejection is signaled by NackError / UnroutableError.
        channel.basic_publish(
            exchange="",
            routing_key=RESPONSE_QUEUE,
            body=body,
            properties=pika.BasicProperties(
                content_type="application/json",
                content_encoding="utf-8",
                delivery_mode=2,
            ),
            mandatory=True,
        )

        print("RabbitMQ publisher confirmation received.")
        print("Publish call completed for queue:", RESPONSE_QUEUE)

    except pika.exceptions.UnroutableError as exc:
        print("Message could not be routed:", exc)
        raise

    except pika.exceptions.NackError as exc:
        print("RabbitMQ rejected the message:", exc)
        raise

    finally:
        if connection.is_open:
            connection.close()


def publish_to_queue(message, queue):
    """Confirmed, persistent, mandatory publish to a durable named queue.

    Used by the export worker; analysis keeps using publish_message.
    """
    if not isinstance(queue, str) or not queue.strip():
        raise ValueError("A response queue name is required")

    connection = create_connection()

    try:
        channel = connection.channel()
        channel.queue_declare(queue=queue, durable=True)
        channel.confirm_delivery()

        body = json.dumps(message, ensure_ascii=False).encode("utf-8")
        print("Queue:", queue)
        print("Publishing bytes:", len(body))

        channel.basic_publish(
            exchange="",
            routing_key=queue,
            body=body,
            properties=pika.BasicProperties(
                content_type="application/json",
                content_encoding="utf-8",
                delivery_mode=2,
            ),
            mandatory=True,
        )
        print("RabbitMQ publisher confirmation received.")

    except pika.exceptions.UnroutableError as exc:
        print("Message could not be routed:", exc)
        raise

    except pika.exceptions.NackError as exc:
        print("RabbitMQ rejected the message:", exc)
        raise

    finally:
        if connection.is_open:
            connection.close()
