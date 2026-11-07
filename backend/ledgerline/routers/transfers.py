"""Transfer endpoint and transaction lookup."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Header, Response, status

from ..config import Settings
from ..db import Database
from ..deps import (
    current_principal,
    enforce_money_rate_limit,
    get_db,
    get_settings,
    require_customer,
)
from ..models import TransferRequest
from ..services import accounts_service, money_service
from ..services.accounts_service import Principal
from .accounts import _money_response

router = APIRouter(prefix="/api", tags=["transfers"])


@router.post("/transfers", status_code=status.HTTP_201_CREATED)
def transfer(
    payload: TransferRequest,
    response: Response,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    db: Database = Depends(get_db),
    settings: Settings = Depends(get_settings),
    principal: Principal = Depends(require_customer),
    _: None = Depends(enforce_money_rate_limit),
) -> dict:
    key = money_service.validate_idempotency_key(idempotency_key)
    result, replayed = money_service.transfer(
        db,
        settings,
        principal,
        from_account_id=payload.from_account_id,
        to_account_number=payload.to_account_number,
        raw_amount=payload.amount,
        description=payload.description,
        idempotency_key=key,
    )
    return _money_response(response, result, replayed)


@router.get("/transactions/{transaction_id}")
def get_transaction(
    transaction_id: str,
    db: Database = Depends(get_db),
    principal: Principal = Depends(current_principal),
) -> dict:
    return {"transaction": accounts_service.transaction_detail(db, principal, transaction_id)}
