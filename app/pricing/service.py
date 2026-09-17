import json

from sqlalchemy import select

from app.metering.math import utc
from app.metering.registry import BY_NAME
from app.models import (
    PriceBook,
    PriceBookVersion,
    PriceRule,
    PricingAudit,
    Product,
    Project,
    ProjectAssignment,
    ProjectOverride,
    utcnow,
)


class PricingError(ValueError):
    pass


def snapshot(row):
    return json.loads(json.dumps({c.key: getattr(row, c.key) for c in row.__table__.columns}, default=str))


def audit(db, cloud, action, row, actor, before=None):
    db.flush()
    db.add(
        PricingAudit(
            cloud_id=cloud,
            action=action,
            entity_type=row.__tablename__,
            entity_id=row.id,
            before=before,
            after=snapshot(row),
            actor=actor,
        )
    )


def require(db, model, identity):
    row = db.get(model, identity)
    if row is None:
        raise PricingError(f"{model.__tablename__} record not found")
    return row


def intersects(a, b):
    return (a.effective_to is None or utc(b.effective_from) < utc(a.effective_to)) and (
        b.effective_to is None or utc(a.effective_from) < utc(b.effective_to)
    )


def create(db, model, data, cloud, actor):
    values = dict(data)
    if model is Product:
        meter = BY_NAME.get(values["meter_name"])
        if not meter or values["unit"] != meter.unit:
            raise PricingError("Product must map exactly to a Phase 2 meter and its unit")
        expected = "compute" if meter.resource_type == "INSTANCE" else "storage"
        if values["service_category"] != expected:
            raise PricingError(f"Service category must be {expected}")
    elif model is PriceBookVersion:
        require(db, PriceBook, values["price_book_id"])
        values["created_by"] = actor
    elif model is PriceRule:
        version = require(db, PriceBookVersion, values["price_book_version_id"])
        product = require(db, Product, values["product_id"])
        if version.status != "DRAFT":
            raise PricingError("Active/retired rules are immutable; create a new draft version")
        if values["billing_unit"] != product.unit:
            raise PricingError("Billing unit must exactly match the product unit")
    elif model in (ProjectAssignment, ProjectOverride):
        values["cloud_id"] = cloud
        if values.get("project_id") is not None and db.get(Project, (cloud, values["project_id"])) is None:
            raise PricingError("Project not found in the configured cloud")
        if model is ProjectAssignment:
            require(db, PriceBook, values["price_book_id"])
        else:
            require(db, Product, values["product_id"])
        row = model(**values)
        query = select(model).where(
            model.cloud_id == cloud, model.project_id == values.get("project_id"), model.retired_at.is_(None)
        )
        if model is ProjectOverride:
            query = query.where(model.product_id == values["product_id"])
        if any(intersects(row, other) for other in db.scalars(query)):
            raise PricingError("Effective period overlaps existing pricing; retire and replace explicitly")
    row = model(**values)
    db.add(row)
    audit(db, cloud, "CREATE", row, actor)
    return row


def transition(db, model, identity, action, cloud, actor):
    row = require(db, model, identity)
    if hasattr(row, "cloud_id") and row.cloud_id != cloud:
        raise PricingError("Record is outside the configured cloud")
    before = snapshot(row)
    if model is PriceBookVersion:
        if action == "activate":
            if row.status != "DRAFT":
                raise PricingError("Only a draft version can be activated")
            if not db.scalar(
                select(PriceRule.id)
                .where(PriceRule.price_book_version_id == row.id, PriceRule.enabled.is_(True))
                .limit(1)
            ):
                raise PricingError("Add at least one enabled price rule before activation")
            others = db.scalars(
                select(PriceBookVersion).where(
                    PriceBookVersion.price_book_id == row.price_book_id, PriceBookVersion.status == "ACTIVE"
                )
            )
            if any(intersects(row, other) for other in others):
                raise PricingError("Active price version effective periods overlap")
            row.status, row.activated_at = "ACTIVE", utcnow()
        elif action == "retire" and row.status == "ACTIVE":
            row.status = "RETIRED"
        else:
            raise PricingError("Only an active version can be retired")
    else:
        if action != "retire" or row.retired_at:
            raise PricingError("Only a current assignment/override can be retired")
        row.retired_at = row.updated_at = utcnow()
    audit(db, cloud, action.upper(), row, actor, before)
    return row
