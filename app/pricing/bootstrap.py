"""Explicit development bootstrap only. TEST prices, never production rate recommendations."""

from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select

from app.metering.registry import METERS
from app.models import PriceBook, PriceBookVersion, PriceRule, Product, ProjectAssignment
from app.pricing.service import create, transition

TEST_PRICES = ("0", "1000", "200", "10", "10", "0", "20")
TEST_CODES = (
    "COMPUTE_INSTANCE",
    "COMPUTE_VCPU",
    "COMPUTE_RAM",
    "COMPUTE_ROOT_DISK",
    "COMPUTE_EPHEMERAL",
    "CINDER_VOLUME",
    "CINDER_CAPACITY",
)


def seed_demo(db, cloud, actor="bootstrap"):
    existing = db.scalar(select(PriceBook).where(PriceBook.code == "POC-VND"))
    if existing:
        return existing
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
                    description="TEST catalog product",
                    meter_name=meter.name,
                    unit=meter.unit,
                    service_category="compute" if meter.resource_type == "INSTANCE" else "storage",
                ),
                cloud,
                actor,
            )
        products.append(product)
    book = create(
        db,
        PriceBook,
        dict(
            code="POC-VND",
            name="POC TEST VND rates",
            description="Development example only. Not production prices.",
            currency="VND",
        ),
        cloud,
        actor,
    )
    version = create(
        db,
        PriceBookVersion,
        dict(price_book_id=book.id, version="test-v1", effective_from=datetime(2000, 1, 1, tzinfo=UTC)),
        cloud,
        actor,
    )
    for product, price in zip(products, TEST_PRICES):
        create(
            db,
            PriceRule,
            dict(
                price_book_version_id=version.id,
                product_id=product.id,
                unit_price=Decimal(price),
                billing_unit=product.unit,
            ),
            cloud,
            actor,
        )
    transition(db, PriceBookVersion, version.id, "activate", cloud, actor)
    if (
        db.scalar(
            select(ProjectAssignment.id)
            .where(
                ProjectAssignment.cloud_id == cloud,
                ProjectAssignment.project_id.is_(None),
                ProjectAssignment.retired_at.is_(None),
            )
            .limit(1)
        )
        is None
    ):
        create(
            db,
            ProjectAssignment,
            dict(price_book_id=book.id, project_id=None, effective_from=datetime(2000, 1, 1, tzinfo=UTC)),
            cloud,
            actor,
        )
    return book
