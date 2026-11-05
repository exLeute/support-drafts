import secrets

from fastapi import Depends, HTTPException
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from app import settings

basic = HTTPBasic(auto_error=False)


def operator_auth(credentials: HTTPBasicCredentials | None = Depends(basic)) -> None:
    if not settings.OPERATOR_PASSWORD:
        return
    if credentials is None or not secrets.compare_digest(
        credentials.password.encode(), settings.OPERATOR_PASSWORD.encode()
    ):
        raise HTTPException(401, headers={"WWW-Authenticate": "Basic"})
