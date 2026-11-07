"""Account read endpoints plus deposit/withdraw."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Header, Query, Request, Response, status

from ..config import Settings
from ..db import Database
from ..deps import (
    current_principal,
    enforce_money_rate_limit,
    get_db,
    get_settings,
    require_customer,
)
from ..models import DepositRequest, WithdrawRequest
from ..services import accounts_service, money_service
from ..services.accounts_service import Principal

router = APIRouter(prefix="/api", tags=["accounts"])


@router.get("/accounts")
def list_accounts(
    db: Database = Depends(get_db),
    principal: Principal = Depends(current_principal),
) -> dict:
    return {"accounts": accounts_service.list_accounts(db, principal)}


@router.get("/accounts/{account_id}")
def get_account(
    account_id: str,
    db: Database = Depends(get_db),
    principal: Principal = Depends(current_principal),
) -> dict:
    return {"account": accounts_service.get_account(db, principal, account_id)}


@router.get("/accounts/{account_id}/statement")
def get_statement(
    account_id: str,
    limit: int = Query(default=accounts_service.DEFAULT_PAGE_SIZE, ge=1, le=accounts_service.MAX_PAGE_SIZE),
    before_id: int | None = Query(default=None, ge=1),
    db: Database = Depends(get_db),
    principal: Principal = Depends(current_principal),
) -> dict:
    return accounts_service.statement(
        db, principal, account_id=account_id, limit=limit, before_id=before_id
    )


def _money_response(response: Response, payload: dict, replayed: bool) -> dict:
    if replayed:
        response.status_code = status.HTTP_200_OK
        response.headers["Idempotent-Replay"] = "true"
    else:
        response.status_code = status.HTTP_201_CREATED
        response.headers["Idempotent-Replay"] = "false"
        transaction_id = payload.get("transaction", {}).get("id")
        if transaction_id:
            response.headers["Location"] = f"/api/transactions/{transaction_id}"
    return {**payload, "replayed": replayed}


@router.post("/accounts/{account_id}/deposit", status_code=status.HTTP_201_CREATED)
def deposit(
    account_id: str,
    payload: DepositRequest,
    request: Request,
    response: Response,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    db: Database = Depends(get_db),
    settings: Settings = Depends(get_settings),
    principal: Principal = Depends(require_customer),
    _: None = Depends(enforce_money_rate_limit),
) -> dict:
    key = money_service.validate_idempotency_key(idempotency_key)
    result, replayed = money_service.deposit(
        db,
        settings,
        principal,
        account_id=account_id,
        raw_amount=payload.amount,
        description=payload.description,
        idempotency_key=key,
    )
    return _money_response(response, result, replayed)


@router.post("/accounts/{account_id}/withdraw", status_code=status.HTTP_201_CREATED)
def withdraw(
    account_id: str,
    payload: WithdrawRequest,
    request: Request,
    response: Response,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    db: Database = Depends(get_db),
    settings: Settings = Depends(get_settings),
    principal: Principal = Depends(require_customer),
    _: None = Depends(enforce_money_rate_limit),
) -> dict:
    key = money_service.validate_idempotency_key(idempotency_key)
    result, replayed = money_service.withdraw(
        db,
        settings,
        principal,
        account_id=account_id,
        raw_amount=payload.amount,
        description=payload.description,
        idempotency_key=key,
    )
    return _money_response(response, result, replayed)
