import secrets
from urllib.parse import urlsplit

from fastapi import Depends, HTTPException, Request
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


def same_origin(request: Request) -> None:
    # Browsers attach Basic credentials to cross-site form posts too, but always send Origin with a POST.
    # No Origin (curl, the helpdesk) is not a browser, so there is no ambient credential to abuse.
    origin = request.headers.get("origin")
    if origin is not None and urlsplit(origin).netloc != request.headers.get("host"):
        raise HTTPException(403, "cross-site request")
