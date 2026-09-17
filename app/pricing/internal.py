"""Explicit internal rate bootstrap; never silently rewrites an active price definition."""

from decimal import Decimal

from sqlalchemy import select

from app.metering.math import utc
from app.metering.registry import METERS
from app.models import PriceBook, PriceBookVersion, PriceRule, Product, ProjectAssignment
from app.pricing.bootstrap import TEST_CODES
from app.pricing.service import PricingError, create, transition


def internal_rates(settings):
    return {
        "compute.instance": Decimal(0),
        "compute.vcpu": settings.price_cpu_per_vcpu_hour,
        "compute.ram": settings.price_ram_per_gib_hour,
        "compute.root_disk": settings.price_ssd_per_gib_hour,
        "compute.ephemeral_disk": settings.price_ssd_per_gib_hour,
        "storage.volume": Decimal(0),
        "storage.volume_capacity": settings.price_ssd_per_gib_hour,
    }


def seed_internal(db, settings, start, actor="internal-bootstrap"):
    start = utc(start)
    cloud = settings.openstack_cloud_id
    prices = internal_rates(settings)
    products = []
    for meter, code in zip(METERS, TEST_CODES):
        product = db.scalar(select(Product).where(Product.meter_name == meter.name))
        if product is None:
            product = create(
                db,
                Product,
                dict(
                    code=code,
                    name=code.replace("_", " "),
                    meter_name=meter.name,
                    unit=meter.unit,
                    service_category="compute" if meter.resource_type == "INSTANCE" else "storage",
                ),
                cloud,
                actor,
            )
        if not product.enabled or product.unit != meter.unit:
            raise PricingError("Existing product is disabled or has a different unit")
        products.append(product)
    book = db.scalar(select(PriceBook).where(PriceBook.code == "INTERNAL-VND"))
    if book is None:
        book = create(
            db,
            PriceBook,
            dict(code="INTERNAL-VND", name="Internal CPU / RAM / SSD", currency=settings.billing_currency),
            cloud,
            actor,
        )
    if not book.enabled or book.currency != "VND":
        raise PricingError("INTERNAL-VND must be enabled and denominated in VND")
    versions = list(
        db.scalars(
            select(PriceBookVersion).where(
                PriceBookVersion.price_book_id == book.id, PriceBookVersion.status == "ACTIVE"
            )
        )
    )
    applicable = [
        v
        for v in versions
        if utc(v.effective_from) <= start and (v.effective_to is None or start < utc(v.effective_to))
    ]
    if applicable:
        rules = {
            r.product_id: r.unit_price
            for r in db.scalars(
                select(PriceRule).where(
                    PriceRule.price_book_version_id == applicable[0].id, PriceRule.enabled.is_(True)
                )
            )
        }
        if any(rules.get(p.id) != prices[p.meter_name] for p in products):
            raise PricingError(
                "INTERNAL-VND already has different active rates. "
                "Create a dated replacement in Pricing; active rates are immutable."
            )
    else:
        if any(v.effective_to is None or utc(v.effective_to) > start for v in versions):
            raise PricingError("Requested internal rates overlap an existing version")
        v = create(
            db,
            PriceBookVersion,
            dict(
                price_book_id=book.id,
                version="internal-" + start.strftime("%Y%m%dT%H%M%S"),
                effective_from=start,
            ),
            cloud,
            actor,
        )
        for p in products:
            create(
                db,
                PriceRule,
                dict(
                    price_book_version_id=v.id,
                    product_id=p.id,
                    unit_price=prices[p.meter_name],
                    billing_unit=p.unit,
                ),
                cloud,
                actor,
            )
        transition(db, PriceBookVersion, v.id, "activate", cloud, actor)
    defaults = list(
        db.scalars(
            select(ProjectAssignment).where(
                ProjectAssignment.cloud_id == cloud,
                ProjectAssignment.project_id.is_(None),
                ProjectAssignment.retired_at.is_(None),
            )
        )
    )
    covering = [
        a
        for a in defaults
        if utc(a.effective_from) <= start and (a.effective_to is None or start < utc(a.effective_to))
    ]
    if len(covering) == 1 and covering[0].price_book_id == book.id and covering[0].effective_to is None:
        return book
    for a in defaults:
        if a.effective_to is not None and utc(a.effective_to) <= start:
            continue
        if utc(a.effective_from) >= start and a.price_book_id != book.id:
            raise PricingError("A future default assignment exists. Resolve it explicitly in Pricing first.")
        prior_from, prior_book = utc(a.effective_from), a.price_book_id
        transition(db, ProjectAssignment, a.id, "retire", cloud, actor)
        if prior_from < start:
            create(
                db,
                ProjectAssignment,
                dict(
                    project_id=None, price_book_id=prior_book, effective_from=prior_from, effective_to=start
                ),
                cloud,
                actor,
            )
    create(
        db,
        ProjectAssignment,
        dict(project_id=None, price_book_id=book.id, effective_from=start),
        cloud,
        actor,
    )
    return book
