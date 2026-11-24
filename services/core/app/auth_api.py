"""Authentication endpoints and principal resolution (local RBAC)."""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import secrets
import time
from typing import Any
from urllib.parse import urlencode

import httpx
import jwt
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, Field

from .config import Settings
from .security import ROLE_LEVEL, Principal, SecurityStore


class LoginIn(BaseModel):
    username: str
    password: str


class TenantIn(BaseModel):
    tenant_id: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=128)


class UserIn(BaseModel):
    username: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=8, max_length=256)
    tenant_id: str = "team-core"
    role: str = "operator"


class ApiKeyIn(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    tenant_id: str = ""
    role: str = "operator"


def resolve_principal(request: Request, security: SecurityStore,
                      settings: Settings) -> Principal | None:
    api_key = request.headers.get("x-api-key", "").strip()
    if api_key:
        return security.principal_for_api_key(api_key)
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        token = auth.split(" ", 1)[1].strip()
        if token.startswith("cpk_"):
            return security.principal_for_api_key(token)
        return security.principal_for_session(token)
    cookie = request.cookies.get("copilot_session", "")
    if cookie:
        principal = security.principal_for_session(cookie)
        if principal:
            return principal
    trusted_header = (settings.auth_trusted_header or "").strip()
    if trusted_header and settings.auth_trusted_secret:
        username = request.headers.get(trusted_header, "").strip()
        supplied = request.headers.get("x-auth-proxy-secret", "")
        if username and secrets.compare_digest(
            supplied, settings.auth_trusted_secret
        ):
            principal = security.principal_for_username(username)
            if principal:
                return principal
        # A trusted-header deployment must not silently fall back to anonymous
        # or basic auth if the proxy contract is violated.
        if username or supplied:
            return None
    if auth.lower().startswith("basic "):
        try:
            decoded = base64.b64decode(auth.split(" ", 1)[1]).decode("utf-8")
            username, _, password = decoded.partition(":")
        except (binascii.Error, UnicodeDecodeError):
            return None
        if (
            settings.admin_password
            and secrets.compare_digest(username, settings.admin_user)
            and secrets.compare_digest(password, settings.admin_password)
        ):
            return Principal("admin", "admin", "platform", "platform_admin", "basic")
        user = security.authenticate(username, password)
        if user:
            return user
    return None


def current_principal(request: Request) -> Principal:
    principal = getattr(request.state, "principal", None)
    if principal is None:
        raise HTTPException(401, "authentication required")
    return principal


def require_role(min_role: str):
    def dependency(request: Request) -> Principal:
        principal = current_principal(request)
        if principal.level < ROLE_LEVEL[min_role]:
            raise HTTPException(403, f"{min_role} role required")
        return principal
    return dependency


_DISCOVERY_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _sign(payload: dict[str, Any], secret: str) -> str:
    body = _b64(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    sig = hmac.new(secret.encode("utf-8"), body.encode("ascii"),
                   hashlib.sha256).hexdigest()
    return f"{body}.{sig}"


def _verify(token: str, secret: str) -> dict[str, Any] | None:
    try:
        body, sig = token.rsplit(".", 1)
        expected = hmac.new(secret.encode("utf-8"), body.encode("ascii"),
                            hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected):
            return None
        payload = json.loads(_unb64(body))
        if float(payload.get("exp", 0)) < time.time():
            return None
        return payload
    except (ValueError, json.JSONDecodeError, UnicodeDecodeError):
        return None


def _discovery(issuer: str, timeout: float = 8.0) -> dict[str, Any]:
    now = time.time()
    cached = _DISCOVERY_CACHE.get(issuer)
    if cached and now - cached[0] < 300:
        return cached[1]
    url = issuer.rstrip("/") + "/.well-known/openid-configuration"
    with httpx.Client(timeout=timeout) as client:
        response = client.get(url)
        response.raise_for_status()
        doc = response.json()
    _DISCOVERY_CACHE[issuer] = (now, doc)
    return doc


def build_router(security: SecurityStore, settings: Settings) -> APIRouter:
    router = APIRouter()

    def set_session_cookie(response: JSONResponse | RedirectResponse,
                           token: str, request: Request) -> None:
        response.set_cookie(
            "copilot_session", token, httponly=True, samesite="lax",
            max_age=max(1, settings.auth_session_hours) * 3600,
            secure=request.url.scheme == "https", path="/",
        )

    def oidc_ready() -> bool:
        return bool(
            settings.oidc_issuer and settings.oidc_client_id
            and settings.oidc_client_secret and settings.oidc_redirect_uri
            and (settings.oidc_state_secret or settings.oidc_client_secret)
        )

    @router.get("/api/v1/auth/oidc/login")
    def oidc_login(request: Request, next: str = "/") -> Any:  # noqa: A002
        if not oidc_ready():
            raise HTTPException(503, "OIDC is not configured")
        try:
            doc = _discovery(settings.oidc_issuer)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(502, f"OIDC discovery failed: {type(exc).__name__}") from None
        state = secrets.token_urlsafe(24)
        nonce = secrets.token_urlsafe(24)
        target = next if next.startswith("/") and not next.startswith("//") else "/"
        secret = settings.oidc_state_secret or settings.oidc_client_secret
        cookie = _sign({
            "state": state, "nonce": nonce, "next": target,
            "exp": time.time() + 600,
        }, secret)
        params = {
            "response_type": "code",
            "client_id": settings.oidc_client_id,
            "redirect_uri": settings.oidc_redirect_uri,
            "scope": settings.oidc_scopes or "openid profile email",
            "state": state,
            "nonce": nonce,
        }
        response = RedirectResponse(
            doc["authorization_endpoint"] + "?" + urlencode(params),
            status_code=302,
        )
        response.set_cookie(
            "oidc_state", cookie, httponly=True, samesite="lax", max_age=600,
            secure=request.url.scheme == "https", path="/",
        )
        return response

    @router.get("/api/v1/auth/oidc/callback")
    def oidc_callback(request: Request, code: str = "", state: str = "") -> Any:
        if not oidc_ready():
            raise HTTPException(503, "OIDC is not configured")
        secret = settings.oidc_state_secret or settings.oidc_client_secret
        payload = _verify(request.cookies.get("oidc_state", ""), secret)
        if not payload or not state or not secrets.compare_digest(
            str(payload.get("state", "")), state
        ):
            raise HTTPException(400, "invalid OIDC state")
        if not code:
            raise HTTPException(400, "missing authorization code")
        try:
            doc = _discovery(settings.oidc_issuer)
            with httpx.Client(timeout=15.0) as client:
                token_response = client.post(
                    doc["token_endpoint"],
                    data={
                        "grant_type": "authorization_code",
                        "code": code,
                        "redirect_uri": settings.oidc_redirect_uri,
                        "client_id": settings.oidc_client_id,
                        "client_secret": settings.oidc_client_secret,
                    },
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                )
                token_response.raise_for_status()
                tokens = token_response.json()
            id_token = tokens.get("id_token") or ""
            if not id_token:
                raise ValueError("id_token missing")
            jwk_client = jwt.PyJWKClient(doc["jwks_uri"])
            signing_key = jwk_client.get_signing_key_from_jwt(id_token)
            algorithms = doc.get("id_token_signing_alg_values_supported") or ["RS256"]
            claims = jwt.decode(
                id_token, signing_key.key, algorithms=algorithms,
                audience=settings.oidc_client_id, issuer=settings.oidc_issuer,
                options={"require": ["exp", "iat", "aud", "iss"]},
            )
            nonce = str(claims.get("nonce") or "")
            if not nonce or not secrets.compare_digest(
                nonce, str(payload.get("nonce", ""))
            ):
                raise ValueError("nonce mismatch")
        except HTTPException:
            raise
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(401, f"OIDC token validation failed: {type(exc).__name__}") from None

        username = (
            claims.get("preferred_username") or claims.get("email")
            or claims.get("sub") or ""
        )
        principal = security.principal_for_username(str(username))
        if not principal:
            raise HTTPException(
                403, "SSO user is not provisioned in local RBAC; ask an admin to create it"
            )
        token = security.create_session(
            {"user_id": principal.user_id, "tenant_id": principal.tenant_id,
             "role": principal.role},
            settings.auth_session_hours,
        )
        target = str(payload.get("next") or "/")
        if not target.startswith("/") or target.startswith("//"):
            target = "/"
        response = RedirectResponse(target, status_code=303)
        response.delete_cookie("oidc_state", path="/")
        set_session_cookie(response, token, request)
        return response

    @router.get("/login")
    def login_page() -> Any:
        from .render import login_page

        return login_page(settings.auth_enabled, oidc_enabled=oidc_ready())

    @router.post("/login")
    async def login_form(request: Request) -> Any:
        from .render import login_page

        body = (await request.body()).decode("utf-8", errors="replace")
        from urllib.parse import parse_qs

        form = {k: v[0] for k, v in parse_qs(body, keep_blank_values=True).items()}
        principal = security.authenticate(
            form.get("username", ""), form.get("password", "")
        )
        if not principal:
            return login_page(settings.auth_enabled, error="用户名或密码错误")
        token = security.create_session(
            {"user_id": principal.user_id, "tenant_id": principal.tenant_id,
             "role": principal.role},
            settings.auth_session_hours,
        )
        response = RedirectResponse("/", status_code=303)
        set_session_cookie(response, token, request)
        return response

    @router.post("/api/v1/auth/login")
    def api_login(body: LoginIn, request: Request) -> JSONResponse:
        principal = security.authenticate(body.username, body.password)
        if not principal:
            raise HTTPException(401, "invalid username or password")
        token = security.create_session(
            {"user_id": principal.user_id, "tenant_id": principal.tenant_id,
             "role": principal.role},
            settings.auth_session_hours,
        )
        response = JSONResponse({
            "token": token, "tenant_id": principal.tenant_id,
            "role": principal.role, "username": principal.username,
            "expires_in_s": max(1, settings.auth_session_hours) * 3600,
        })
        set_session_cookie(response, token, request)
        return response

    @router.post("/api/v1/auth/logout")
    def logout() -> JSONResponse:
        response = JSONResponse({"status": "ok"})
        response.delete_cookie("copilot_session", path="/")
        return response

    @router.get("/api/v1/auth/me")
    def me(request: Request) -> dict:
        principal = current_principal(request)
        return {
            "user_id": principal.user_id, "username": principal.username,
            "tenant_id": principal.tenant_id, "role": principal.role,
            "kind": principal.kind,
        }

    @router.get("/api/v1/admin/tenants")
    def list_tenants(request: Request) -> dict:
        require_role("platform_admin")(request)
        return {"tenants": security.list_tenants()}

    @router.post("/api/v1/admin/tenants", status_code=201)
    def create_tenant(body: TenantIn, request: Request) -> dict:
        require_role("platform_admin")(request)
        return security.ensure_tenant(body.tenant_id, body.name)

    @router.post("/api/v1/admin/users", status_code=201)
    def create_user(body: UserIn, request: Request) -> dict:
        principal = current_principal(request)
        if principal.role == "tenant_admin":
            if body.tenant_id != principal.tenant_id:
                raise HTTPException(403, "cannot create users in another tenant")
            if body.role == "platform_admin":
                raise HTTPException(403, "cannot create platform admin")
        elif principal.role != "platform_admin":
            raise HTTPException(403, "tenant_admin role required")
        try:
            return security.create_user(
                body.username, body.password, body.tenant_id, body.role
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc))

    @router.post("/api/v1/admin/api-keys", status_code=201)
    def create_api_key(body: ApiKeyIn, request: Request) -> dict:
        principal = current_principal(request)
        tenant = body.tenant_id or principal.tenant_id
        if principal.role == "tenant_admin":
            if tenant != principal.tenant_id:
                raise HTTPException(403, "cannot create keys for another tenant")
        elif principal.role != "platform_admin":
            raise HTTPException(403, "tenant_admin role required")
        if body.role not in ROLE_LEVEL or body.role == "platform_admin":
            raise HTTPException(400, "invalid role for tenant API key")
        return security.create_api_key(tenant, body.role, body.name)

    return router


__all__ = ["build_router", "resolve_principal", "current_principal", "require_role"]
