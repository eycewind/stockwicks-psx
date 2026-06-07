# app/forms/account.py
from fastapi import Form
from pydantic import BaseModel, EmailStr, validator
from typing import Optional

class UpdateAccountForm(BaseModel):
    first_name: str
    last_name: str
    email: EmailStr
    username: str
    password: Optional[str] = None
    confirm_password: Optional[str] = None

    @validator("confirm_password", always=True)
    def passwords_match(cls, confirm_password, values):
        password = values.get("password")

        if not password and not confirm_password:
            return confirm_password  # Allow skipping password change

        if password:
            if not confirm_password:
                raise ValueError("Please confirm your new password.")
            if confirm_password != password:
                raise ValueError("Passwords do not match.")
        return confirm_password

    @classmethod
    def as_form(
        cls,
        first_name: str = Form(...),
        last_name: str = Form(...),
        email: EmailStr = Form(...),
        username: str = Form(...),
        password: Optional[str] = Form(None),
        confirm_password: Optional[str] = Form(None),
    ):
        return cls(
            first_name=first_name,
            last_name=last_name,
            email=email,
            username=username,
            password=password,
            confirm_password=confirm_password
        )
