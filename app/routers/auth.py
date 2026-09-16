"""Authentication endpoints: signup, signin, refresh, and current-user."""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.auth import (
    create_access_token,
    create_refresh_token,
    get_current_user,
    get_user_from_refresh,
)
from app.database import get_db
from app.models import User, _user_id, log_event
from app.schemas import SigninRequest, SignupRequest, TokenResponse, UserOut

router = APIRouter(prefix="/auth", tags=["auth"])


def _normalize_email(email: str) -> str:
    return email.strip().lower()


@router.post("/signup", response_model=TokenResponse, status_code=status.HTTP_201_CREATED)
def signup(payload: SignupRequest, db: Session = Depends(get_db)):
    email = _normalize_email(payload.email)
    if "@" not in email or "." not in email.split("@")[-1]:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "invalid email address")
    existing = db.query(User).filter(User.email == email).first()
    if existing:
        raise HTTPException(status.HTTP_409_CONFLICT, "email already registered")

    user = User(id=_user_id(email), email=email)
    user.set_password(payload.password)
    db.add(user)
    log_event(db, "user_signup", channel="rest", email=email)
    db.commit()
    db.refresh(user)

    return TokenResponse(
        access_token=create_access_token(user),
        refresh_token=create_refresh_token(user),
    )


@router.post("/signin", response_model=TokenResponse)
def signin(payload: SigninRequest, db: Session = Depends(get_db)):
    email = _normalize_email(payload.email)
    user = db.query(User).filter(User.email == email).first()
    if not user or not user.check_password(payload.password):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid email or password")
    log_event(db, "user_signin", channel="rest", email=email)
    db.commit()
    return TokenResponse(
        access_token=create_access_token(user),
        refresh_token=create_refresh_token(user),
    )


@router.post("/refresh", response_model=TokenResponse)
def refresh(user: User = Depends(get_user_from_refresh)):
    return TokenResponse(
        access_token=create_access_token(user),
        refresh_token=create_refresh_token(user),
    )


@router.get("/me", response_model=UserOut)
def me(user: User = Depends(get_current_user)):
    return UserOut(**user.to_dict())