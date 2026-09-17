import hashlib
import json
import threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from time import monotonic
from uuid import uuid4

from sqlalchemy import select, text

from app.core.logging import event
from app.lifecycle.engine import LifecycleBatch, LifecycleError, retain_known_allocation
from app.metering.math import utc
from app.models import Cloud, Instance, Observation, Project, SyncRun, Volume, utcnow
from app.openstack.client import ConfigurationError, OpenStackClient
from app.openstack.normalize import normalize_instance, normalize_project, normalize_volume


class SyncBusy(Exception):
    pass


def error_code(exc):
    if isinstance(exc, ConfigurationError):
        return "configuration_missing"
    status = getattr(exc, "status_code", None) or getattr(exc, "http_status", None)
    if status in (401, 403, 404, 408, 429, 500, 502, 503, 504):
        return f"http_{status}"
    if "timeout" in type(exc).__name__.lower():
        return "api_timeout"
    return "service_error"


def snapshot(values):
    # This is an allowlisted normalized observation, never an SDK to_dict dump.
    return json.loads(
        json.dumps(values, default=lambda v: utc(v).isoformat() if isinstance(v, datetime) else str(v))
    )


def ensure_cloud(db, settings):
    cloud = db.get(Cloud, settings.openstack_cloud_id)
    if cloud is None:
        cloud = Cloud(
            cloud_id=settings.openstack_cloud_id,
            name=settings.openstack_cloud_name,
            region=settings.os_region_name,
        )
        db.add(cloud)
        db.flush()
    cloud.name = settings.openstack_cloud_name
    cloud.region = settings.os_region_name
    return cloud


class SyncManager:
    """One scheduler + worker. A PostgreSQL session lock also excludes other processes."""

    def __init__(self, engine, sessions, settings, client_factory=None, clock=None):
        self.engine, self.sessions, self.settings = engine, sessions, settings
        self.client_factory = client_factory or (lambda: OpenStackClient(settings))
        self.clock = clock or (lambda: utcnow())
        self.gate = threading.Lock()
        self.stop_event = threading.Event()
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="inventory-sync")
        self.scheduler = None
        self.future = None
        self.on_completed = None
        digest = hashlib.sha256(str(settings.openstack_cloud_id).encode()).digest()[:8]
        self.lock_key = int.from_bytes(digest, "big", signed=True)

    def start(self):
        self.scheduler = threading.Thread(target=self._schedule, daemon=True, name="sync-scheduler")
        self.scheduler.start()

    def _schedule(self):
        while not self.stop_event.is_set():
            try:
                self.trigger()
            except SyncBusy:
                pass
            except Exception:
                event("sync_schedule_failed", code="database_or_scheduler_error")
            self.stop_event.wait(self.settings.sync_interval_seconds)

    def close(self):
        self.stop_event.set()
        if self.scheduler:
            self.scheduler.join()
        self.executor.shutdown(wait=True)

    def _release(self, connection):
        try:
            if connection:
                try:
                    connection.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": self.lock_key})
                    connection.commit()
                except Exception:
                    # Never return a potentially locked physical connection to the pool.
                    connection.invalidate()
                finally:
                    connection.close()
        finally:
            self.gate.release()

    def trigger(self, *, wait=False):
        if not self.gate.acquire(blocking=False):
            raise SyncBusy()
        connection = None
        try:
            if self.engine.dialect.name == "postgresql":
                connection = self.engine.connect()
                acquired = connection.scalar(
                    text("SELECT pg_try_advisory_lock(:key)"), {"key": self.lock_key}
                )
                connection.commit()
                if not acquired:
                    connection.close()
                    connection = None
                    raise SyncBusy()
            with self.sessions.begin() as db:
                ensure_cloud(db, self.settings)
                # Only the owner of the cloud lock can recover interrupted runs.
                for old in db.scalars(
                    select(SyncRun).where(
                        SyncRun.cloud_id == self.settings.openstack_cloud_id, SyncRun.status == "RUNNING"
                    )
                ):
                    old.status, old.finished_at = "FAILED", self.clock()
                    old.errors = [{"service": "sync", "code": "interrupted_previous_run"}]
                run_id = uuid4()
                db.add(
                    SyncRun(
                        sync_run_id=run_id, cloud_id=self.settings.openstack_cloud_id, started_at=self.clock()
                    )
                )
            self.future = self.executor.submit(self._execute, run_id, connection)
        except Exception:
            self._release(connection)
            raise
        if wait:
            self.future.result()
        return run_id

    def _execute(self, run_id, connection):
        started = monotonic()
        client = None
        event("sync_started", sync_run_id=run_id)
        try:
            client = self.client_factory()
            try:
                client.authenticate()
            except Exception as exc:
                self._finish(run_id, "FAILED", [{"service": "keystone", "code": error_code(exc)}], {}, False)
                return
            event("OPENSTACK_CONNECTION_OK", sync_run_id=run_id)
            errors, services = [], {}
            cache = {}

            def flavor_lookup(key):
                if key not in cache:
                    try:
                        cache[key] = client.flavor(key)
                    except Exception as exc:
                        cache[key] = exc
                if isinstance(cache[key], Exception):
                    raise cache[key]
                return cache[key]

            domains = [
                ("keystone", "projects", Project, "project_id", client.projects, normalize_project),
                (
                    "nova",
                    "instances",
                    Instance,
                    "instance_id",
                    client.instances,
                    lambda row: normalize_instance(row, flavor_lookup),
                ),
                ("cinder", "volumes", Volume, "volume_id", client.volumes, normalize_volume),
            ]
            for service, kind, model, id_key, collect, normalize in domains:
                # Exhaust pagination before changing DB. A late-page failure must not
                # advance missing counters or commit an incomplete service snapshot.
                try:
                    raw_rows = list(collect())
                except Exception as exc:
                    code = error_code(exc)
                    if code in ("http_401", "http_403") and service in ("nova", "cinder"):
                        event(
                            "CROSS_PROJECT_PERMISSION_ERROR", sync_run_id=run_id, service=service, code=code
                        )
                    errors.append({"service": service, "code": code})
                    services[service] = {"status": "FAILED", "code": code}
                    event("sync_service_failed", sync_run_id=run_id, service=service, code=code)
                    continue
                rows, invalid, warnings = {}, 0, 0
                for raw in raw_rows:
                    try:
                        row = normalize(raw)
                        key = row[id_key]
                        if key in rows and rows[key] != row:
                            # Conflicting duplicates make reconciliation unsafe.
                            invalid += 1
                            continue
                        rows[key] = row
                        if row.get("quality_issues"):
                            warnings += 1
                            event(
                                "resource_quality_issue",
                                sync_run_id=run_id,
                                service=service,
                                resource_id=key,
                                codes=row["quality_issues"],
                            )
                    except Exception:
                        invalid += 1
                        event(
                            "resource_rejected",
                            sync_run_id=run_id,
                            service=service,
                            code="malformed_resource",
                        )
                try:
                    stats = self._store(run_id, kind, model, id_key, rows, reconcile=invalid == 0)
                except Exception as exc:
                    errors.append(
                        {
                            "service": service,
                            "code": "lifecycle_inconsistency"
                            if isinstance(exc, LifecycleError)
                            else "database_write_failed",
                        }
                    )
                    services[service] = {"status": "FAILED", "code": "database_write_failed"}
                    event(
                        "sync_service_failed",
                        sync_run_id=run_id,
                        service=service,
                        code="database_write_failed",
                    )
                    continue
                if invalid:
                    errors.append(
                        {"service": service, "code": "malformed_or_conflicting_resources", "count": invalid}
                    )
                if warnings:
                    errors.append(
                        {"service": service, "code": "incomplete_resource_dimensions", "count": warnings}
                    )
                services[service] = {
                    "status": "PARTIAL" if invalid or warnings else "SUCCESS",
                    "observed_at": self.clock().isoformat(),
                    **stats,
                    "rejected": invalid,
                    "quality_warnings": warnings,
                    "per_project": dict(Counter(str(row["project_id"]) for row in rows.values())),
                    "scope": "all_projects" if service != "keystone" else "visible_projects",
                }
                event("sync_service_finished", sync_run_id=run_id, service=service, **services[service])
                event(
                    {
                        "keystone": "PROJECT_SYNC_COMPLETE",
                        "nova": "INSTANCE_SYNC_COMPLETE",
                        "cinder": "VOLUME_SYNC_COMPLETE",
                    }[service],
                    sync_run_id=run_id,
                    **services[service],
                )
            successful = sum(value["status"] != "FAILED" for value in services.values())
            status = "SUCCESS" if not errors else "PARTIAL" if successful else "FAILED"
            self._finish(run_id, status, errors, services, True)
        except Exception:
            event("sync_failed", sync_run_id=run_id, code="internal_sync_error")
            try:
                self._finish(
                    run_id, "FAILED", [{"service": "sync", "code": "internal_sync_error"}], {}, False
                )
            except Exception:
                event("sync_status_write_failed", sync_run_id=run_id)
        finally:
            try:
                if client:
                    client.close()
            except Exception:
                event("client_close_failed", sync_run_id=run_id)
            self._release(connection)
            event("sync_finished", sync_run_id=run_id, duration_seconds=round(monotonic() - started, 3))
            if self.on_completed:
                try:
                    self.on_completed()
                except Exception:
                    event(
                        "METERING_FAILED",
                        cloud_id=self.settings.openstack_cloud_id,
                        code="post_sync_metering_failed",
                    )

    def _store(self, run_id, kind, model, id_key, rows, *, reconcile):
        cloud_id, now = self.settings.openstack_cloud_id, self.clock()
        stats = {"discovered": len(rows), "created": 0, "updated": 0, "unchanged": 0, "missing": 0}
        with self.sessions.begin() as db:
            lifecycle = LifecycleBatch(db, cloud_id, kind, run_id, now) if model is not Project else None
            existing = {
                getattr(row, id_key): row
                for row in db.scalars(select(model).where(model.cloud_id == cloud_id))
            }
            if model is not Project:
                project_ids = set(db.scalars(select(Project.project_id).where(Project.cloud_id == cloud_id)))
                for row in rows.values():
                    project_id = row["project_id"]
                    if project_id not in project_ids:
                        db.add(
                            Project(
                                cloud_id=cloud_id,
                                project_id=project_id,
                                project_name="Unknown project",
                                enabled=None,
                                is_placeholder=True,
                            )
                        )
                        project_ids.add(project_id)
                db.flush()
            for key, values in rows.items():
                resource = existing.get(key)
                if model is Instance:
                    retain_known_allocation(resource, values)
                if lifecycle:
                    lifecycle.seen(key, values, was_pending=bool(resource and resource.missing_scans))
                if resource is None:
                    resource = model(cloud_id=cloud_id, first_seen_at=now, **values)
                    db.add(resource)
                    stats["created"] += 1
                else:
                    changed = any(
                        snapshot({field: getattr(resource, field)}) != snapshot({field: value})
                        for field, value in values.items()
                    )
                    for field, value in values.items():
                        setattr(resource, field, value)
                    stats["updated" if changed else "unchanged"] += 1
                resource.last_seen_at = resource.updated_at = now
                resource.missing_scans, resource.missing_since, resource.is_missing = 0, None, False
                if lifecycle:
                    resource.deleted_confirmed_at = now if values.get("deleted_at_openstack") else None
                safe_payload = snapshot(values)
                if model is not Project:
                    resource.raw_payload = safe_payload if self.settings.retain_raw_payload else None
                if not lifecycle:
                    db.add(
                        Observation(
                            cloud_id=cloud_id,
                            sync_run_id=run_id,
                            resource_type=kind,
                            resource_id=key,
                            observed_at=now,
                            event="SEEN",
                            normalized_payload=safe_payload,
                        )
                    )
            if reconcile:
                for key, resource in existing.items():
                    if key in rows or model is Project and resource.is_placeholder:
                        continue
                    resource.missing_scans += 1
                    resource.missing_since = resource.missing_since or now
                    resource.is_missing = resource.missing_scans >= self.settings.missing_scan_threshold
                    resource.updated_at = now
                    stats["missing"] += 1
                    if lifecycle:
                        lifecycle.missing(key, resource)
                    else:
                        db.add(
                            Observation(
                                cloud_id=cloud_id,
                                sync_run_id=run_id,
                                resource_type=kind,
                                resource_id=key,
                                observed_at=now,
                                event="MISSING",
                                normalized_payload={
                                    "missing_scans": resource.missing_scans,
                                    "is_missing": resource.is_missing,
                                },
                            )
                        )
            run = db.get(SyncRun, run_id)
            setattr(run, f"{kind}_found", len(rows))
        return stats

    def _finish(self, run_id, status, errors, services, authenticated):
        with self.sessions.begin() as db:
            now = self.clock()
            run = db.get(SyncRun, run_id)
            run.status, run.finished_at, run.errors, run.services = status, now, errors, services
            cloud = db.get(Cloud, self.settings.openstack_cloud_id)
            cloud.connection_status = "CONNECTED" if authenticated else "FAILED"
            cloud.last_attempt_at = now
            if status == "SUCCESS":
                cloud.last_successful_sync = now
            else:
                cloud.last_failed_sync = now
            old_status = cloud.service_status or {}
            merged = {}
            for service in ("keystone", "nova", "cinder"):
                value = services.get(service, {"status": "FAILED", "code": "sync_unavailable"})
                last_success = old_status.get(service, {}).get("last_success_at")
                if value["status"] == "SUCCESS":
                    last_success = now.isoformat()
                merged[service] = {**value, "last_success_at": last_success}
            cloud.service_status = merged
        event("sync_result", sync_run_id=run_id, status=status, errors=errors)
        event(
            "BILLING_RECONCILIATION_COMPLETE",
            sync_run_id=run_id,
            status=status,
            projects=services.get("keystone", {}).get("discovered"),
            instances=services.get("nova", {}).get("discovered"),
            volumes=services.get("cinder", {}).get("discovered"),
        )
