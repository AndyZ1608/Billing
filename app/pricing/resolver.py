"""A run-local immutable pricing snapshot. No inventory or SDK dependencies."""

from collections import defaultdict

from sqlalchemy import select

from app.metering.math import utc
from app.models import PriceBook, PriceBookVersion, PriceRule, Product, ProjectAssignment, ProjectOverride


def applies(row, point):
    return utc(row.effective_from) <= point and (row.effective_to is None or point < utc(row.effective_to))


class PricingResolver:
    def __init__(self, db, cloud):
        self.products = {
            p.meter_name: p for p in db.scalars(select(Product).where(Product.enabled.is_(True)))
        }
        self.books = {p.id: p for p in db.scalars(select(PriceBook).where(PriceBook.enabled.is_(True)))}
        self.versions = defaultdict(list)
        for row in db.scalars(select(PriceBookVersion).where(PriceBookVersion.status == "ACTIVE")):
            self.versions[row.price_book_id].append(row)
        self.rules = {
            (r.price_book_version_id, r.product_id): r
            for r in db.scalars(select(PriceRule).where(PriceRule.enabled.is_(True)))
        }
        self.assignments = defaultdict(list)
        for row in db.scalars(
            select(ProjectAssignment).where(
                ProjectAssignment.cloud_id == cloud, ProjectAssignment.retired_at.is_(None)
            )
        ):
            self.assignments[row.project_id].append(row)
        self.overrides = defaultdict(list)
        for row in db.scalars(
            select(ProjectOverride).where(
                ProjectOverride.cloud_id == cloud, ProjectOverride.retired_at.is_(None)
            )
        ):
            self.overrides[(row.project_id, row.product_id)].append(row)

    def boundaries(self, usage):
        start, end = utc(usage.period_start), utc(usage.period_end)
        product = self.products.get(usage.meter_name)
        assignments = self.assignments[usage.project_id] + self.assignments[None]
        rows = list(assignments)
        for assignment in assignments:
            rows.extend(self.versions[assignment.price_book_id])
        if product:
            rows.extend(self.overrides[(usage.project_id, product.id)])
        points = {start, end}
        for row in rows:
            for point in (row.effective_from, row.effective_to):
                if point and start < utc(point) < end:
                    points.add(utc(point))
        return sorted(points)

    def resolve(self, project, meter, point, unit):
        point = utc(point)
        result = {}

        def fail(reason):
            return {**result, "unrated_reason": reason}

        product = self.products.get(meter)
        if product is None:
            return fail("NO_PRODUCT")
        result.update(
            product_id=product.id, product_code=product.code, service_category=product.service_category
        )
        if unit != product.unit:
            return fail("INVALID_UNIT")
        specific = [r for r in self.assignments[project] if applies(r, point)]
        chosen = specific or [r for r in self.assignments[None] if applies(r, point)]
        overrides = [r for r in self.overrides[(project, product.id)] if applies(r, point)]
        if len(chosen) > 1 or len(overrides) > 1:
            return fail("OVERLAPPING_PRICE_CONFIG")
        assignment = chosen[0] if chosen else None
        book = self.books.get(assignment.price_book_id) if assignment else None
        if assignment:
            result["assignment_id"] = assignment.id
        if book:
            result.update(price_book_id=book.id, currency=book.currency)
        if overrides:
            override = overrides[0]
            result.update(
                project_price_override_id=override.id,
                pricing_source="PROJECT_OVERRIDE",
                currency=override.currency,
                pricing_effective_from=override.effective_from,
                pricing_effective_to=override.effective_to,
            )
            if book and book.currency != override.currency:
                return fail("CURRENCY_MISMATCH")
            result["unit_price"] = override.unit_price
        else:
            if book is None:
                return fail("NO_PRICE_BOOK")
            result["pricing_source"] = "PROJECT_PRICE_BOOK" if specific else "DEFAULT_PRICE_BOOK"
            versions = [r for r in self.versions[book.id] if applies(r, point)]
            if len(versions) > 1:
                return fail("OVERLAPPING_PRICE_CONFIG")
            if not versions:
                return fail("PRICE_GAP")
            version = versions[0]
            result.update(
                price_book_version_id=version.id,
                price_book_version=version.version,
                pricing_effective_from=version.effective_from,
                pricing_effective_to=version.effective_to,
            )
            rule = self.rules.get((version.id, product.id))
            if rule is None:
                return fail("NO_PRICE_RULE")
            result["price_rule_id"] = rule.id
            if rule.billing_unit != unit or rule.minimum_quantity != 0 or rule.rounding_mode != "HALF_EVEN":
                return fail("INVALID_PRICE")
            result["unit_price"] = rule.unit_price
        if result["unit_price"] is None or not result["unit_price"].is_finite() or result["unit_price"] < 0:
            result.pop("unit_price", None)
            return fail("INVALID_PRICE")
        if result["currency"] not in ("VND", "USD"):
            return fail("INVALID_CURRENCY")
        return result
