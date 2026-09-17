"""Dedicated durable AMQP subscription in the existing application process."""

import socket
import ssl
import threading
from urllib.parse import urlsplit

from kombu import Connection, Consumer, Exchange, Queue

from app.core.jobs import JobBusy
from app.core.logging import event
from app.notifications.processor import NotificationProcessor
from app.sync.engine import SyncBusy


class NovaConsumer:
    def __init__(self, engine, sessions, settings, manager, metering):
        self.settings, self.manager, self.metering = settings, manager, metering
        self.processor = NotificationProcessor(engine, sessions, settings, manager.gate)
        self.stop = threading.Event()
        self.thread = None
        self.status = "DISABLED"

    def start(self):
        if self.settings.nova_notification_enabled:
            self.status = "CONNECTING"
            self.thread = threading.Thread(target=self.run, name="nova-notifications", daemon=True)
            self.thread.start()

    def close(self):
        self.stop.set()
        if self.thread:
            self.thread.join(timeout=15)

    def callback(self, body, message):
        try:
            result = self.processor.process(body)
        except JobBusy:
            self.stop.wait(0.2)
            message.reject(requeue=True)
            return
        # No ack before database commit. Exceptions cause reconnect/redelivery.
        message.ack()
        if self.settings.metering_enabled and result["status"] in ("APPLIED", "DUPLICATE"):
            self.metering.after_sync()
        if result["reconcile"]:
            try:
                self.manager.trigger()
            except SyncBusy:
                pass  # current poll/next scheduled poll owns reconciliation

    def queue(self):
        # Bind a dedicated durable queue to the existing Nova exchange.
        exchange = Exchange(self.settings.nova_notification_exchange, type="topic", no_declare=True)
        return Queue(
            self.settings.nova_notification_queue,
            exchange=exchange,
            routing_key=self.settings.nova_notification_topic + ".*",
            durable=True,
            auto_delete=False,
        )

    def run(self):
        while not self.stop.is_set():
            try:
                url = self.settings.nova_notification_transport_url.get_secret_value()
                if url.startswith("rabbit://"):
                    url = "amqp://" + url[len("rabbit://") :]
                if urlsplit(url).scheme not in ("amqp", "amqps"):
                    raise ValueError("invalid_notification_transport")
                tls = None
                if urlsplit(url).scheme == "amqps":
                    tls = {"cert_reqs": ssl.CERT_REQUIRED, "server_hostname": urlsplit(url).hostname}
                    if self.settings.nova_notification_ca_cert:
                        tls["ca_certs"] = self.settings.nova_notification_ca_cert
                with Connection(url, ssl=tls, heartbeat=30, connect_timeout=5) as connection:
                    queue = self.queue()
                    with Consumer(
                        connection,
                        queues=[queue],
                        callbacks=[self.callback],
                        accept=["json"],
                        prefetch_count=1,
                    ):
                        self.status = "CONNECTED"
                        event("NOVA_NOTIFICATION_CONNECTED")
                        while not self.stop.is_set():
                            try:
                                connection.drain_events(timeout=1)
                            except socket.timeout:
                                connection.heartbeat_check()
            except Exception:
                self.status = "DISCONNECTED"
                # Never log broker exceptions: their text can contain transport credentials.
                event("NOVA_NOTIFICATION_DISCONNECTED", code="transport_or_processing_error")
                self.stop.wait(5)
        self.status = "STOPPED"
