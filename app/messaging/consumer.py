
import json
import os

from dotenv import load_dotenv
from pika.exceptions import AMQPError

from app.messaging.rabbitmq import create_connection
from app.storage.s3_client import download_file
from pipeline import process_document
from app.response.response_builder import build_completed_response
from app.messaging.publisher import publish_message


load_dotenv()

QUEUE = os.getenv("RABBITMQ_QUEUE", "rfp-python-test")
DOWNLOAD_DIR = os.path.join("data", "s3_test")


def callback(ch, method, properties, body):
    try:
        message = json.loads(body)

        print("\nRFP message received:")

        print(json.dumps(message, indent=2))

        documents = message.get("documents", [])

        if not documents:
            raise ValueError("Request contains no documents")

        # Process the first document in the request.
        document = documents[0]

        bucket = document["bucket"]
        key = document["key"]
        filename = os.path.basename(document["filename"])

        os.makedirs(DOWNLOAD_DIR, exist_ok=True)
        local_path = os.path.join(DOWNLOAD_DIR, filename)

        print("\nDownloading document from S3...")

        download_file(
            bucket=bucket,
            key=key,
            local_path=local_path,
        )

        print(f"Document downloaded to: {local_path}")

        print("\nProcessing document with the existing pipeline...")

        requirements = process_document(local_path)

        print(f"Extracted {len(requirements)} requirements")

        response = build_completed_response(
            request_message=message,
            requirements=requirements,
        )

        print("\nPublishing completion response to the test queue...")

        publish_message(response)

        print("Completion response published to the test response queue.")

        # Acknowledge only after publishing succeeds.
        if ch.is_open:
            ch.basic_ack(delivery_tag=method.delivery_tag)
            print("Request acknowledged successfully.")
        else:
            print(
                "RabbitMQ channel closed before acknowledgement. "
                "The request may be redelivered."
            )

    except Exception as exc:
        print(f"\nRequest processing failed: {exc}")

        # A closed channel cannot acknowledge or reject the message.
        if not ch.is_open:
            print(
                "RabbitMQ channel is closed. "
                "No acknowledgement or rejection was attempted."
            )
            return

        try:
            if method.redelivered:
                print(
                    "Request was already redelivered. "
                    "Rejecting without requeue."
                )
                ch.basic_nack(
                    delivery_tag=method.delivery_tag,
                    requeue=False,
                )
            else:
                print("Requeuing request for one retry.")
                ch.basic_nack(
                    delivery_tag=method.delivery_tag,
                    requeue=True,
                )

        except AMQPError as nack_error:
            print(
                "Could not reject/requeue the request because "
                f"RabbitMQ reported an error: {nack_error}"
            )


def start_consumer():
    connection = create_connection()

    try:
        channel = connection.channel()

        channel.basic_qos(prefetch_count=1)

        channel.basic_consume(
            queue=QUEUE,
            on_message_callback=callback,
            auto_ack=False,
        )

        print(f"Connected. Waiting for messages from '{QUEUE}'...")
        print("Press Ctrl+C to stop the consumer.")

        channel.start_consuming()

    except KeyboardInterrupt:
        print("\nConsumer stopped by user.")

    finally:
        if connection.is_open:
            connection.close()


if __name__ == "__main__":
    start_consumer()


def test_closed_channel_does_not_acknowledge_or_reject():
    channel = make_channel()
    channel.is_open = False

    method = Mock()
    method.delivery_tag = 13
    method.redelivered = False

    with patch(
        "app.messaging.consumer.download_file",
        side_effect=RuntimeError("Simulated failure"),
    ):
        callback(
            channel,
            method,
            Mock(),
            json.dumps(make_message()).encode("utf-8"),
        )

    channel.basic_ack.assert_not_called()
    channel.basic_nack.assert_not_called()

def test_closed_channel_does_not_acknowledge_or_reject():
    channel = make_channel()
    channel.is_open = False

    method = Mock()
    method.delivery_tag = 13
    method.redelivered = False

    with patch(
        "app.messaging.consumer.download_file",
        side_effect=RuntimeError("Simulated failure"),
    ):
        callback(
            channel,
            method,
            Mock(),
            json.dumps(make_message()).encode("utf-8"),
        )

    channel.basic_ack.assert_not_called()
    channel.basic_nack.assert_not_called()

def test_closed_channel_does_not_acknowledge_or_reject():
    channel = make_channel()
    channel.is_open = False

    method = Mock()
    method.delivery_tag = 13
    method.redelivered = False

    with patch(
        "app.messaging.consumer.download_file",
        side_effect=RuntimeError("Simulated failure"),
    ):
        callback(
            channel,
            method,
            Mock(),
            json.dumps(make_message()).encode("utf-8"),
        )

    channel.basic_ack.assert_not_called()
    channel.basic_nack.assert_not_called()

