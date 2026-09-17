import secrets
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.api.routes import DB, Limit, Offset
from app.core.jobs import JobBusy
from app.metering.math import utc
from app.models import (
    PriceBook,
    PriceBookVersion,
    PriceRule,
    PricingAudit,
    Product,
    ProjectAssignment,
    ProjectOverride,
)
from app.pricing.service import PricingError, create, require, transition

router = APIRouter(prefix="/api/v1/pricing", tags=["Pricing administration"])


def row(value):
    return {c.key: getattr(value, c.key) for c in value.__table__.columns}


def response(value):
    return JSONResponse(
        jsonable_encoder(
            value,
            custom_encoder={
                Decimal: lambda v: format(v, "f"),
                datetime: lambda v: utc(v).isoformat(),
                UUID: str,
            },
        )
    )


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


Code = Annotated[str, Field(pattern=r"^[A-Z0-9][A-Z0-9_-]*$", max_length=64)]
Name = Annotated[str, Field(min_length=1, max_length=255)]
Price = Annotated[Decimal, Field(ge=0, max_digits=24, decimal_places=8)]


class ProductInput(Input):
    code: Code
    name: Name
    description: str = Field(default="", max_length=2000)
    meter_name: str = Field(max_length=64)
    unit: str = Field(max_length=30)
    service_category: Literal["compute", "storage"]
    enabled: bool = True


class BookInput(Input):
    code: Code
    name: Name
    description: str = Field(default="", max_length=2000)
    currency: Literal["VND", "USD"]
    enabled: bool = True


class EffectiveInput(Input):
    effective_from: AwareDatetime
    effective_to: AwareDatetime | None = None

    @model_validator(mode="after")
    def bounds(self):
        if self.effective_to and self.effective_from >= self.effective_to:
            raise ValueError("Effective period must be positive: [from,to)")
        return self


class VersionInput(EffectiveInput):
    version: str = Field(min_length=1, max_length=64)


class RuleInput(Input):
    product_id: UUID
    unit_price: Price
    billing_unit: str = Field(max_length=30)
    minimum_quantity: Literal[0] = 0
    rounding_mode: Literal["HALF_EVEN"] = "HALF_EVEN"
    enabled: bool = True

    @field_validator("unit_price", mode="before")
    @classmethod
    def decimal_string(cls, value):
        if isinstance(value, float):
            raise ValueError("Send prices as decimal strings, not JSON floats")
        return value


class AssignmentInput(EffectiveInput):
    project_id: UUID | None = None
    price_book_id: UUID


class OverrideInput(EffectiveInput):
    project_id: UUID
    product_id: UUID
    unit_price: Price
    currency: Literal["VND", "USD"]
    reason: str = Field(min_length=1, max_length=2000)

    @field_validator("unit_price", mode="before")
    @classmethod
    def decimal_string(cls, value):
        if isinstance(value, float):
            raise ValueError("Send prices as decimal strings, not JSON floats")
        return value


def admin(request: Request):
    if request.headers.get("content-type", "").split(";")[0] != "application/json":
        raise HTTPException(415, "Administrative writes require application/json")
    if request.headers.get("x-pricing-admin") != "true":
        raise HTTPException(403, "Pricing administrator action requires X-Pricing-Admin: true")
    configured = request.app.state.settings.pricing_admin_token.get_secret_value()
    supplied = request.headers.get("authorization", "")
    if configured and not secrets.compare_digest(supplied, "Bearer " + configured):
        raise HTTPException(403, "Pricing administrator token required")
    actor = request.headers.get("x-audit-actor", "local-admin")
    if len(actor) > 100 or not actor or any(not (c.isalnum() or c in "._@- ") for c in actor):
        raise HTTPException(422, "Use a short actor label with letters, digits, spaces, . _ @ or -")
    return actor


Admin = Annotated[str, Depends(admin)]


def mutation(request, actor, fn):
    try:
        with request.app.state.rating.lock.held(), request.app.state.sessions.begin() as db:
            result = fn(db, request.app.state.settings.openstack_cloud_id, actor)
            value = row(result)
        return response(value)
    except JobBusy as exc:
        raise HTTPException(409, str(exc)) from None
    except PricingError as exc:
        raise HTTPException(422, str(exc)) from None
    except IntegrityError:
        raise HTTPException(
            409, "Duplicate or overlapping configuration, invalid reference, or immutable pricing record"
        ) from None


def listing(db, model, limit, offset, conditions=()):
    query = select(model).where(*conditions)
    total = db.scalar(select(func.count()).select_from(query.subquery()))
    return response(
        {
            "items": [
                row(r)
                for r in db.scalars(query.order_by(model.created_at, model.id).limit(limit).offset(offset))
            ],
            "total": total,
            "limit": limit,
            "offset": offset,
        }
    )


def register_catalog(path, model, schema):
    def get_list(db: DB, limit: Limit = 100, offset: Offset = 0):
        return listing(db, model, limit, offset)

    def get_one(identity: UUID, db: DB):
        try:
            return response(row(require(db, model, identity)))
        except PricingError:
            raise HTTPException(404, "Record not found") from None

    def post_one(body, request: Request, actor: Admin):
        return mutation(
            request, actor, lambda db, cloud, actor: create(db, model, body.model_dump(), cloud, actor)
        )

    post_one.__annotations__["body"] = schema
    router.add_api_route(path, get_list, methods=["GET"], name="list_" + model.__tablename__)
    router.add_api_route(path + "/{identity}", get_one, methods=["GET"], name="get_" + model.__tablename__)
    router.add_api_route(
        path, post_one, methods=["POST"], status_code=201, name="create_" + model.__tablename__
    )


register_catalog("/products", Product, ProductInput)
register_catalog("/price-books", PriceBook, BookInput)


@router.post("/price-books/{identity}/versions")
def add_version(identity: UUID, body: VersionInput, request: Request, actor: Admin):
    return mutation(
        request,
        actor,
        lambda db, cloud, actor: create(
            db, PriceBookVersion, {**body.model_dump(), "price_book_id": identity}, cloud, actor
        ),
    )


@router.get("/price-books/{identity}/versions")
def versions(identity: UUID, db: DB, limit: Limit = 100, offset: Offset = 0):
    return listing(db, PriceBookVersion, limit, offset, (PriceBookVersion.price_book_id == identity,))


@router.post("/price-book-versions/{identity}/rules")
def add_rule(identity: UUID, body: RuleInput, request: Request, actor: Admin):
    return mutation(
        request,
        actor,
        lambda db, cloud, actor: create(
            db, PriceRule, {**body.model_dump(), "price_book_version_id": identity}, cloud, actor
        ),
    )


@router.get("/price-book-versions/{identity}/rules")
def rules(identity: UUID, db: DB, limit: Limit = 100, offset: Offset = 0):
    return listing(db, PriceRule, limit, offset, (PriceRule.price_book_version_id == identity,))


@router.post("/price-book-versions/{identity}/{action}")
def version_action(identity: UUID, action: Literal["activate", "retire"], request: Request, actor: Admin):
    return mutation(
        request,
        actor,
        lambda db, cloud, actor: transition(db, PriceBookVersion, identity, action, cloud, actor),
    )


def register_project(path, model, schema):
    def get_list(request: Request, db: DB, limit: Limit = 100, offset: Offset = 0):
        return listing(
            db, model, limit, offset, (model.cloud_id == request.app.state.settings.openstack_cloud_id,)
        )

    def post_one(body, request: Request, actor: Admin):
        return mutation(
            request, actor, lambda db, cloud, actor: create(db, model, body.model_dump(), cloud, actor)
        )

    post_one.__annotations__["body"] = schema

    def retire(identity: UUID, request: Request, actor: Admin):
        return mutation(
            request, actor, lambda db, cloud, actor: transition(db, model, identity, "retire", cloud, actor)
        )

    router.add_api_route(path, get_list, methods=["GET"], name="list_" + model.__tablename__)
    router.add_api_route(path, post_one, methods=["POST"], name="create_" + model.__tablename__)
    router.add_api_route(
        path + "/{identity}/retire", retire, methods=["POST"], name="retire_" + model.__tablename__
    )


register_project("/project-assignments", ProjectAssignment, AssignmentInput)
register_project("/project-overrides", ProjectOverride, OverrideInput)


@router.get("/audit")
def audit_rows(request: Request, db: DB, limit: Limit = 100, offset: Offset = 0):
    return listing(
        db,
        PricingAudit,
        limit,
        offset,
        (PricingAudit.cloud_id == request.app.state.settings.openstack_cloud_id,),
    )


@router.get("/admin/config")
def admin_config(request: Request):
    return {
        "token_required": bool(request.app.state.settings.pricing_admin_token.get_secret_value()),
        "mode": "trusted POC administrator"
        if not request.app.state.settings.pricing_admin_token.get_secret_value()
        else "administrator token",
        "source_usage_status": "FINAL",
    }
