"""Request/response models.

``extra="forbid"`` everywhere: an attacker who adds ``"role": "ADMIN"`` to a
registration payload gets a 422 instead of a silently ignored field (and, in
worse codebases, a silently *applied* field).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)


class RegisterRequest(StrictModel):
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=1, max_length=200)
    full_name: str = Field(min_length=2, max_length=120)


class LoginRequest(StrictModel):
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=1, max_length=200)


class RefreshRequest(StrictModel):
    refresh_token: str = Field(min_length=20, max_length=200)


class LogoutRequest(StrictModel):
    refresh_token: str = Field(min_length=20, max_length=200)


class ChangePasswordRequest(StrictModel):
    current_password: str = Field(min_length=1, max_length=200)
    new_password: str = Field(min_length=1, max_length=200)


class CreateAccountRequest(StrictModel):
    owner_email: str = Field(min_length=3, max_length=254)
    currency: str = Field(min_length=3, max_length=3)
    name: str = Field(min_length=2, max_length=80)


class FreezeRequest(StrictModel):
    frozen: bool


class DepositRequest(StrictModel):
    amount: str = Field(min_length=1, max_length=40)
    description: str | None = Field(default=None, max_length=200)


class WithdrawRequest(StrictModel):
    amount: str = Field(min_length=1, max_length=40)
    description: str | None = Field(default=None, max_length=200)


class TransferRequest(StrictModel):
    from_account_id: str = Field(min_length=8, max_length=64)
    to_account_number: str = Field(min_length=8, max_length=32)
    amount: str = Field(min_length=1, max_length=40)
    description: str | None = Field(default=None, max_length=200)


class CreateStaffUserRequest(StrictModel):
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=1, max_length=200)
    full_name: str = Field(min_length=2, max_length=120)
    # Only staff roles can be created through this endpoint, and only by an
    # existing ADMIN. Customer accounts are self-service via /api/auth/register.
    role: Literal["ADMIN", "AUDITOR"]
