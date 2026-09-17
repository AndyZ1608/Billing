import json
from datetime import timedelta
from decimal import Decimal
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from app.models import ProcessedNotification, StatePeriod
from app.notifications.normalize import normalize
from app.notifications.processor import NotificationProcessor
from tests.test_internal_billing import rate, setup_vm
from tests.test_lifecycle_phase2 import T


def active_policy(h):
    h.settings.metering_policy_path = "config/metering.yaml"
    h.settings.metering_calculation_version = "meter-v2"
    h.settings.billing_policy_path = "config/billing.yaml"


def payload(h, at, state, old=None, event_id=None, kind="instance.update"):
    vm = h.fake.data["instances"][0]

    def obj(data):
        return {"nova_object.namespace": "nova", "nova_object.version": "1.0", "nova_object.data": data}

    data = {"uuid": vm["id"], "tenant_id": vm["project_id"], "state": state}
    if old:
        data["state_update"] = obj({"state": state, "old_state": old})
    return {
        "message_id": event_id or str(uuid4()),
        "timestamp": at.isoformat(),
        "event_type": kind,
        "payload": obj(data),
    }


def processor(h):
    return NotificationProcessor(h.engine, h.sessions, h.settings, h.manager.gate)


def send(h, at, state, old=None):
    message = payload(h, at, state, old)
    result = processor(h).process(
        {"oslo.message": json.dumps(message)}, received_at=at + timedelta(seconds=2)
    )
    assert result["status"] == "APPLIED", result
    return message


def test_active_only_golden_notifications(history):
    h = history
    active_policy(h)
    setup_vm(h, 4, 8, 50)
    send(h, T + timedelta(hours=2, minutes=15), "stopped", "active")
    send(h, T + timedelta(hours=5, minutes=30), "active", "stopped")
    last = send(h, T + timedelta(hours=8), "stopped", "active")
    result = rate(h, T + timedelta(hours=8))
    assert result["cost"] == dict(
        cpu=Decimal(190000), ram=Decimal(418000), ssd=Decimal(118750), total=Decimal(726750)
    )
    assert result["usage"]["vcpu_hours"] == Decimal(19)
    assert processor(h).process(last, received_at=T + timedelta(hours=9))["status"] == "DUPLICATE"
    with h.sessions() as db:
        assert db.scalar(select(func.count()).select_from(ProcessedNotification)) == 3
        assert db.scalar(select(func.count()).select_from(StatePeriod)) == 4
        rows = list(db.scalars(select(StatePeriod).order_by(StatePeriod.valid_from)))
        assert rows[1].effective_time_source == "NOVA_NOTIFICATION"
    again = rate(h, T + timedelta(hours=8))
    assert again["cost"] == result["cost"]


def test_shutoff_ten_hours_is_zero(history):
    h = history
    active_policy(h)
    h.fake.data["instances"][0]["status"] = "SHUTOFF"
    setup_vm(h, 4, 8, 50)
    result = rate(h, T + timedelta(hours=10))
    assert not any(result["usage"].values())
    assert result["cost"]["total"] == 0


def test_exact_seconds_and_shutoff_query(history):
    h = history
    active_policy(h)
    setup_vm(h, 4, 8, 50)
    end = T + timedelta(hours=1, seconds=32)
    send(h, end, "stopped", "active")
    send(h, T + timedelta(hours=10), "active", "stopped")
    result = rate(h, T + timedelta(hours=10))
    assert result["cost"]["cpu"] == Decimal("40355.55555556")
    assert result["usage"]["vcpu_hours"] == Decimal("4.035555555556")
    with h.sessions() as db:
        row = db.scalars(select(StatePeriod).order_by(StatePeriod.valid_from)).first()
        from app.metering.math import utc

        assert utc(row.valid_to) == end
        assert utc(row.closed_at) == end + timedelta(seconds=2)


def test_late_event_does_not_rewrite_closed_history(history):
    h = history
    active_policy(h)
    setup_vm(h, 4, 8, 50)
    send(h, T + timedelta(hours=2), "stopped", "active")
    late = payload(h, T + timedelta(hours=1), "active")
    assert processor(h).process(late, received_at=T + timedelta(hours=3))["status"] == "QUARANTINED"
    with h.sessions() as db:
        assert db.scalar(select(func.count()).select_from(StatePeriod)) == 2
        assert (
            db.scalars(select(ProcessedNotification).where(ProcessedNotification.status == "QUARANTINED"))
            .one()
            .issue_code
            == "OUT_OF_ORDER_EVENT"
        )
    # Authoritative polling reconciles the present, not fabricated earlier history.
    h.fake.data["instances"][0]["status"] = "ACTIVE"
    h.sync_at(T + timedelta(hours=4))
    with h.sessions() as db:
        assert db.scalar(select(func.count()).select_from(StatePeriod)) == 3


def test_notification_unsupported_payload_and_missing_timestamp(history):
    h = history
    setup_vm(h, 4, 8, 50)
    message = payload(h, T + timedelta(hours=1), "stopped")
    message.pop("timestamp")
    assert processor(h).process(message)["status"] == "QUARANTINED"
    message = payload(h, T + timedelta(hours=1), "stopped", kind="instance.power_off.start")
    assert normalize(message) is None


def test_active_attached_volume_only(history):
    h = history
    active_policy(h)
    vm = h.fake.data["instances"][0]
    vol = h.fake.data["volumes"][0]
    vm["image"] = ""
    vm["flavor"].update(vcpus=4, ram=8192, disk=50, ephemeral=0)
    vol.update(size=50, attachments=[{"server_id": vm["id"], "device": "/dev/vda"}])
    h.sync_at(T)
    send(h, T + timedelta(hours=1), "stopped", "active")
    h.fake.data["instances"][0]["status"] = "SHUTOFF"
    vol["size"] = 60
    h.sync_at(T + timedelta(hours=2))
    result = rate(h, T + timedelta(hours=2))
    assert result["cost"]["ssd"] == Decimal(25000)
    assert result["cost"]["total"] == Decimal(153000)


def test_consumer_acks_after_commit_and_busy_requeues(history):
    from app.notifications.consumer import NovaConsumer

    h = history
    setup_vm(h, 4, 8, 50)
    consumer = NovaConsumer(h.engine, h.sessions, h.settings, h.manager, MagicMock())
    message = MagicMock()
    body = payload(h, T + timedelta(hours=1), "stopped", "active")
    with h.manager.gate:
        consumer.callback(body, message)
    message.reject.assert_called_once_with(requeue=True)
    message.ack.assert_not_called()
    consumer.callback(body, message)
    message.ack.assert_called_once()
    with h.sessions() as db:
        assert db.scalar(select(func.count()).select_from(ProcessedNotification)) == 1


@pytest.mark.parametrize(
    "state",
    [
        "SHUTOFF",
        "PAUSED",
        "SUSPENDED",
        "RESCUE",
        "SHELVED",
        "SHELVED_OFFLOADED",
        "ERROR",
        "BUILD",
        "VERIFY_RESIZE",
        "UNRECOGNIZED",
    ],
)
def test_all_non_active_states_have_zero_usage(history, state):
    h = history
    active_policy(h)
    h.fake.data["instances"][0]["status"] = state
    setup_vm(h, 4, 8, 50)
    result = rate(h, T + timedelta(hours=10))
    assert result["cost"]["total"] == 0
    assert not any(result["usage"].values())
    if state == "UNRECOGNIZED":
        assert result["quality_issues"]


def test_one_hour_active_nine_hours_stopped(history):
    h = history
    active_policy(h)
    setup_vm(h, 4, 8, 50)
    send(h, T + timedelta(hours=1), "stopped", "active")
    send(h, T + timedelta(hours=10), "active", "stopped")
    result = rate(h, T + timedelta(hours=10))
    assert result["usage"]["vcpu_hours"] == 4
    assert result["cost"]["total"] == Decimal(153000)


def test_notification_resize_and_delete(history):
    h = history
    active_policy(h)
    setup_vm(h, 2, 4, 20)
    message = payload(h, T + timedelta(hours=2), "active", kind="instance.resize.end")
    message["payload"]["nova_object.data"]["flavor"] = {
        "nova_object.namespace": "nova",
        "nova_object.version": "1.4",
        "nova_object.data": {
            "flavorid": "resized",
            "vcpus": 4,
            "memory_mb": 8192,
            "root_gb": 20,
            "ephemeral_gb": 0,
        },
    }
    assert processor(h).process(message, T + timedelta(hours=2, seconds=3))["status"] == "APPLIED"
    send(h, T + timedelta(hours=5), "deleted")
    result = rate(h, T + timedelta(hours=9))
    assert result["cost"]["total"] == Decimal(562000)


def test_transport_secret_not_exposed_by_diagnostics(history):
    from fastapi.testclient import TestClient
    from pydantic import SecretStr

    from app.main import create_app

    h = history
    h.settings.nova_notification_transport_url = SecretStr("amqp://billing:do-not-expose@broker/vhost")
    setup_vm(h, 4, 8, 50)
    with TestClient(create_app(h.settings, h.engine, lambda: h.fake)) as client:
        for path in ["/api/v1/diagnostics/openstack", "/api/v1/diagnostics/notifications", "/api/v1/health"]:
            response = client.get(path)
            assert response.status_code == 200
            assert "do-not-expose" not in response.text
            assert "amqp://" not in response.text


def test_dedicated_kombu_subscription_does_not_consume_other_queue(history):
    from kombu import Connection, Consumer, Exchange, Producer, Queue

    from app.notifications.consumer import NovaConsumer

    h = history
    setup_vm(h, 4, 8, 50)
    worker = NovaConsumer(h.engine, h.sessions, h.settings, h.manager, MagicMock())
    body = payload(h, T + timedelta(hours=1), "stopped")
    with Connection("memory://") as connection:
        exchange = Exchange(h.settings.nova_notification_exchange, type="topic")
        exchange(connection).declare()
        own = worker.queue()(connection)
        other = Queue(
            "unrelated-" + str(uuid4()),
            exchange=exchange,
            routing_key=h.settings.nova_notification_topic + ".*",
        )(connection)
        own.declare()
        other.declare()
        with Consumer(
            connection, queues=[own], callbacks=[worker.callback], accept=["json"], prefetch_count=1
        ):
            Producer(connection).publish(
                {"oslo.message": json.dumps(body)},
                exchange=exchange,
                routing_key=h.settings.nova_notification_topic + ".info",
                serializer="json",
            )
            connection.drain_events(timeout=1)
        unrelated = other.get(no_ack=True)
        assert unrelated is not None
        assert own.get(no_ack=True) is None
    with h.sessions() as db:
        assert db.scalar(select(func.count()).select_from(ProcessedNotification)) == 1
