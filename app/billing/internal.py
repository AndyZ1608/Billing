"""Small internal view over existing inventory, usage and rated-charge contracts."""

from collections import defaultdict
from decimal import Decimal, localcontext
from types import SimpleNamespace
from uuid import UUID

from sqlalchemy import select

from app.metering.math import quantity, rounded, seconds, utc
from app.metering.policy import load_metering_policy
from app.metering.query import usage_view
from app.metering.registry import allocations
from app.models import ChargeRecord, Instance, Observation, Project, StatePeriod, Volume, utcnow
from app.openstack.diagnostics import diagnostics
from app.pricing.resolver import PricingResolver
from app.rating.money import cost, money
from app.rating.query import clipped

DIMENSIONS = {
    "compute.vcpu": "cpu",
    "compute.ram": "ram",
    "compute.root_disk": "ssd",
    "compute.ephemeral_disk": "ssd",
    "storage.volume_capacity": "ssd",
}


def bucket():
    return dict(
        usage={k: Decimal(0) for k in ("vcpu_hours", "ram_gib_hours", "ssd_gib_hours")},
        cost={k: Decimal(0) for k in ("cpu", "ram", "ssd", "total")},
        rated_cost={k: Decimal(0) for k in ("cpu", "ram", "ssd", "total")},
        estimated_cost={k: Decimal(0) for k in ("cpu", "ram", "ssd", "total")},
        unrated_segments=0,
        estimated_segments=0,
        rated_segments=0,
    )


def add(target, dimension, usage, amount, status):
    key = {"cpu": "vcpu_hours", "ram": "ram_gib_hours", "ssd": "ssd_gib_hours"}[dimension]
    target["usage"][key] += usage
    if amount is None:
        target["unrated_segments"] += 1
    else:
        target["cost"][dimension] += amount
        target["cost"]["total"] += amount
        kind = "estimated_cost" if status == "ESTIMATED" else "rated_cost"
        target[kind][dimension] += amount
        target[kind]["total"] += amount
        target["estimated_segments" if status == "ESTIMATED" else "rated_segments"] += 1


def report(db, settings, start, end, project_id=None, instance_id=None, now=None, trace=False):
    now = utc(now or utcnow())
    start, end = utc(start), utc(end)
    cutoff = min(end, now)
    cloud = settings.openstack_cloud_id
    project_query = select(Project).where(Project.cloud_id == cloud)
    instance_query = select(Instance).where(Instance.cloud_id == cloud)
    volume_query = select(Volume).where(Volume.cloud_id == cloud)
    if project_id:
        project_query = project_query.where(Project.project_id == project_id)
        instance_query = instance_query.where(Instance.project_id == project_id)
        volume_query = volume_query.where(Volume.project_id == project_id)
    projects = {p.project_id: p for p in db.scalars(project_query)}
    instances = {i.instance_id: i for i in db.scalars(instance_query)}
    volumes = list(db.scalars(volume_query))
    if instance_id:
        if instance_id not in instances:
            raise LookupError("Instance not found")
        project_id = instances[instance_id].project_id
    rows, issues, _ = usage_view(
        db, cloud, settings.metering_calculation_version, start, end, project_id=project_id, now=now
    )
    usage_ids = list({r["usage_record_id"] for r in rows if r["usage_record_id"]})
    charges = defaultdict(list)
    for offset in range(0, len(usage_ids), 400):
        for c in db.scalars(
            select(ChargeRecord)
            .where(
                ChargeRecord.usage_record_id.in_(usage_ids[offset : offset + 400]),
                ChargeRecord.status != "SUPERSEDED",
            )
            .order_by(ChargeRecord.rated_period_start)
        ):
            charges[c.usage_record_id].append(c)
    period_ids = list({r["source_state_period_id"] for r in rows})
    source = {}
    for offset in range(0, len(period_ids), 400):
        for p, o in db.execute(
            select(StatePeriod, Observation)
            .join(Observation, StatePeriod.source_observation_id == Observation.observation_id)
            .where(StatePeriod.period_id.in_(period_ids[offset : offset + 400]))
        ):
            source[p.period_id] = (p, o.normalized_payload)
    resolver = PricingResolver(db, cloud)
    project_buckets = {
        pid: {"project_id": pid, "project_name": p.project_name, **bucket()}
        for pid, p in projects.items()
        if project_id is None or pid == project_id
    }
    vm_buckets = {
        iid: {"instance_id": iid, "instance_name": i.instance_name, "project_id": i.project_id, **bucket()}
        for iid, i in instances.items()
        if (project_id is None or i.project_id == project_id) and (instance_id is None or iid == instance_id)
    }
    result = bucket()
    segments = []
    rates = defaultdict(set)
    with localcontext() as ctx:
        ctx.prec = 50
        for usage in rows:
            dimension = DIMENSIONS.get(usage["meter_name"])
            if dimension is None:
                continue
            owner = usage["resource_id"] if usage["resource_type"] == "INSTANCE" else None
            if usage["resource_type"] == "VOLUME":
                p, payload = source[usage["source_state_period_id"]]
                attached = {a["instance_id"] for a in payload.get("attachments", [])}
                if len(attached) == 1:
                    candidate = UUID(next(iter(attached)))
                    if candidate in instances and instances[candidate].project_id == usage["project_id"]:
                        owner = candidate
            if instance_id and owner != instance_id:
                continue
            pieces = []
            if usage["status"] == "PROVISIONAL":
                points = resolver.boundaries(SimpleNamespace(**usage))
                for a, b in zip(points, points[1:]):
                    price = resolver.resolve(usage["project_id"], usage["meter_name"], a, usage["unit"])
                    reason = price.get("unrated_reason")
                    if price.get("currency") not in (None, "VND"):
                        reason = "NON_VND_PRICE"
                    amount = None if reason else cost(usage["allocated_quantity"], a, b, price["unit_price"])
                    pieces.append(
                        (
                            a,
                            b,
                            amount,
                            "ESTIMATED" if amount is not None else "UNRATED",
                            price.get("unit_price"),
                            None,
                            reason,
                        )
                    )
            else:
                cursor = utc(usage["period_start"])
                stop = utc(usage["period_end"])
                for charge in charges[usage["usage_record_id"]]:
                    a, b = (
                        max(cursor, utc(charge.rated_period_start)),
                        min(stop, utc(charge.rated_period_end)),
                    )
                    if a >= b:
                        continue
                    if a > cursor:
                        pieces.append((cursor, a, None, "UNRATED", None, None, "PENDING_RATING"))
                    _, _, amount = clipped(charge, a, b)
                    reason = charge.unrated_reason
                    if charge.currency not in (None, "VND"):
                        amount, reason = None, "NON_VND_PRICE"
                    pieces.append((a, b, amount, charge.status, charge.unit_price, charge.id, reason))
                    cursor = b
                if cursor < stop:
                    pieces.append((cursor, stop, None, "UNRATED", None, None, "PENDING_RATING"))
            for a, b, amount, status, price, charge_id, reason in pieces:
                used = quantity(usage["allocated_quantity"], a, b)
                for target in (result, project_buckets[usage["project_id"]]):
                    add(target, dimension, used, amount, status)
                if owner in vm_buckets:
                    add(vm_buckets[owner], dimension, used, amount, status)
                if price is not None:
                    rates[dimension].add(price)
                if trace:
                    period = source[usage["source_state_period_id"]][0]
                    segments.append(
                        dict(
                            resource_type=usage["resource_type"],
                            resource_id=usage["resource_id"],
                            attributed_instance_id=owner,
                            source_state_period_id=period.period_id,
                            usage_record_id=usage["usage_record_id"],
                            charge_record_id=charge_id,
                            start=a,
                            end=b,
                            state=period.state,
                            history_confidence=period.history_confidence,
                            meter=usage["meter_name"],
                            allocated_quantity=usage["allocated_quantity"],
                            duration_seconds=seconds(a, b),
                            hours=seconds(a, b) / Decimal(3600),
                            usage=rounded(used),
                            unit=usage["unit"],
                            unit_price=price,
                            amount=amount,
                            status=status,
                            reason=reason,
                        )
                    )
        policy = load_metering_policy(settings.metering_policy_path)
        current = {
            pid: dict(
                instance_count=0,
                active_vm_count=0,
                vcpu_count=Decimal(0),
                ram_gb=Decimal(0),
                nova_disk_gib=Decimal(0),
                cinder_volume_gib=Decimal(0),
                total_billable_ssd_gib=Decimal(0),
            )
            for pid in project_buckets
        }
        for iid, vm in instances.items():
            if vm.project_id not in current:
                continue
            alive = (
                not vm.is_missing
                and not vm.deleted_at_openstack
                and vm.status not in ("DELETED", "SOFT_DELETED")
            )
            pairs, _ = allocations(
                SimpleNamespace(
                    resource_type="INSTANCE",
                    state=vm.status,
                    vcpus=vm.vcpus,
                    ram_mb=vm.ram_mb,
                    root_disk_gb=vm.root_disk_gb,
                    ephemeral_disk_gb=vm.ephemeral_disk_gb,
                ),
                policy,
            )
            allocated = {m.name: q for m, q in pairs} if alive else {}
            cpu = allocated.get("compute.vcpu", Decimal(0))
            ram = allocated.get("compute.ram", Decimal(0))
            disk = allocated.get("compute.root_disk", Decimal(0)) + allocated.get(
                "compute.ephemeral_disk", Decimal(0)
            )
            p = current[vm.project_id]
            p["instance_count"] += alive
            p["active_vm_count"] += alive and vm.status == "ACTIVE"
            p["vcpu_count"] += cpu
            p["ram_gb"] += ram
            p["nova_disk_gib"] += disk
            if iid in vm_buckets:
                vm_buckets[iid]["current"] = dict(
                    status=vm.status,
                    present=bool(alive),
                    vcpus=vm.vcpus,
                    ram_gib=vm.ram_gb,
                    local_root_gib=vm.root_disk_gb,
                    ephemeral_gib=vm.ephemeral_disk_gb,
                    nova_disk_gib=disk,
                    cinder_volume_gib=Decimal(0),
                    total_billable_ssd_gib=disk,
                    boot_source=vm.boot_source,
                    flavor_id=vm.flavor_id,
                    flavor_name=vm.flavor_name,
                    host=vm.host,
                    availability_zone=vm.availability_zone,
                    created_at=vm.created_at_openstack,
                    first_seen=vm.first_seen_at,
                    last_seen=vm.last_seen_at,
                    deleted_at=vm.deleted_at_openstack or vm.deleted_confirmed_at,
                    quality_issues=vm.quality_issues,
                    attached_volumes=[],
                )
        for volume in volumes:
            if volume.project_id not in current:
                continue
            size = (
                Decimal(volume.size_gb or 0)
                if not volume.is_missing and volume.status in policy.volume.counted_states
                else Decimal(0)
            )
            owners = {a["instance_id"] for a in volume.attachments}
            if policy.volume.active_attachment_only and not any(
                UUID(owner) in instances
                and instances[UUID(owner)].project_id == volume.project_id
                and instances[UUID(owner)].status == "ACTIVE"
                and not instances[UUID(owner)].is_missing
                for owner in owners
            ):
                size = Decimal(0)
            current[volume.project_id]["cinder_volume_gib"] += size
            for owner_text in owners:
                owner = UUID(owner_text)
                if owner not in vm_buckets or instances[owner].project_id != volume.project_id:
                    continue
                vm = vm_buckets[owner]["current"]
                vm["attached_volumes"].append(
                    dict(
                        volume_id=volume.volume_id,
                        name=volume.volume_name,
                        size_gib=volume.size_gb,
                        volume_type=volume.volume_type,
                        bootable=volume.bootable,
                        status=volume.status,
                        shared=len(owners) > 1,
                    )
                )
                if len(owners) == 1:
                    vm["cinder_volume_gib"] += size
                    vm["total_billable_ssd_gib"] += size
        for pid, p in current.items():
            p["total_billable_ssd_gib"] = p["nova_disk_gib"] + p["cinder_volume_gib"]
            project_buckets[pid]["current"] = p
        for target in [result, *project_buckets.values(), *vm_buckets.values()]:
            target["usage"] = {k: rounded(v) for k, v in target["usage"].items()}
            for key in ("cost", "rated_cost", "estimated_cost"):
                target[key] = {k: money(v) for k, v in target[key].items()}
        result["current"] = {
            k: sum((p[k] for p in current.values()), Decimal(0))
            for k in (
                "instance_count",
                "active_vm_count",
                "vcpu_count",
                "ram_gb",
                "nova_disk_gib",
                "cinder_volume_gib",
                "total_billable_ssd_gib",
            )
        }
    health = diagnostics(db, settings)
    quality = health["data_quality_status"]
    if quality == "HEALTHY" and (issues or result["unrated_segments"]):
        quality = "PARTIAL"
    result.update(
        projects=list(project_buckets.values()),
        instances=list(vm_buckets.values()),
        trace=segments,
        period_start=start,
        period_end=end,
        as_of=cutoff,
        currency="VND",
        data_quality_status=quality,
        estimated=bool(result["estimated_segments"]),
        cost_complete=not issues and not result["unrated_segments"] and quality == "HEALTHY",
        pricing=dict(
            cpu_per_vcpu_hour=settings.price_cpu_per_vcpu_hour,
            ram_per_gib_hour=settings.price_ram_per_gib_hour,
            ssd_per_gib_hour=settings.price_ssd_per_gib_hour,
            currency="VND",
        ),
        actual_unit_prices={k: sorted(v) for k, v in rates.items()},
        quality_issues=issues,
        diagnostics=health,
        projects_with_billing_usage=sum(
            any(v for v in p["usage"].values()) for p in project_buckets.values()
        ),
    )
    return result
