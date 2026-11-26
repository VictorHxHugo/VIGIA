"""Request and response models for authentication endpoints."""

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.auth.models import UserRole


class LoginRequest(BaseModel):
    """Credentials submitted to create an authenticated session."""

    model_config = ConfigDict(extra="forbid")

    login: str = Field(max_length=254)
    password: str = Field(min_length=1, max_length=1024, repr=False)


class AuthUserResponse(BaseModel):
    """Public identity fields returned after authentication."""

    model_config = ConfigDict(extra="forbid")

    user_id: UUID
    role: UserRole


__all__ = ["AuthUserResponse", "LoginRequest"]
