import hashlib
import hmac
import time
from functools import wraps

from aiohttp import web

from config import SECRET_KEY, WEB_PASSWORD, WEB_USERNAME

SESSION_MAX_AGE = 7 * 24 * 3600


def _sign(value: str) -> str:
    return hmac.new(
        SECRET_KEY.encode("utf-8"), value.encode("utf-8"), hashlib.sha256
    ).hexdigest()


def create_session_token() -> str:
    """Create a short signed, stateless admin session token."""
    expiry = str(int(time.time()) + SESSION_MAX_AGE)
    return f"{expiry}.{_sign(expiry)}"


def is_valid_session(token: str) -> bool:
    """Validate a cookie token without ever raising on malformed input."""
    if not isinstance(token, str):
        return False
    try:
        expiry, signature = token.split(".", 1)
        expiry_int = int(expiry)
    except (AttributeError, TypeError, ValueError, OverflowError):
        return False
    if expiry_int <= int(time.time()):
        return False
    expected = _sign(expiry)
    return hmac.compare_digest(signature, expected)


def check_credentials(username: str, password: str) -> bool:
    # compare_digest requires values of matching types and avoids timing leaks.
    return hmac.compare_digest(str(username or ""), WEB_USERNAME) and hmac.compare_digest(
        str(password or ""), WEB_PASSWORD
    )


def login_required(handler):
    """Redirect unauthenticated panel requests to the login page."""

    @wraps(handler)
    async def wrapper(request):
        if not is_valid_session(request.cookies.get("session", "")):
            raise web.HTTPFound("/login")
        return await handler(request)

    return wrapper
