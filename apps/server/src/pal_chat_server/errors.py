from __future__ import annotations

from uuid import uuid4

from fastapi import Request
from fastapi.responses import JSONResponse


class AppError(Exception):
    def __init__(
        self,
        *,
        code: str,
        status_code: int,
        message: str,
        details: dict[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code
        self.message = message
        self.details = details or {}


def build_error_response(
    *,
    request_id: str,
    status_code: int,
    code: str,
    message: str,
    details: dict[str, object] | None = None,
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={
            "error": {
                "code": code,
                "message": message,
                "request_id": request_id,
                "details": details or {},
            }
        },
    )


async def app_error_handler(_: Request, exc: AppError) -> JSONResponse:
    return build_error_response(
        request_id=str(uuid4()),
        status_code=exc.status_code,
        code=exc.code,
        message=exc.message,
        details=exc.details,
    )

