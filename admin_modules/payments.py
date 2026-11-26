"""Authenticated journal and explicit, read-only external status check."""
import asyncio
import hashlib
import hmac
import secrets
import logging
import time
from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from payment_admin_view import load_journal
from payment_admin_check import check_order
from payment_receipt_resend import resend_order
from .auth import check_auth

payments_router = APIRouter(tags=["payments"])
templates = Jinja2Templates(directory="/opt/vpn-bot/templates")
DB = "/opt/vpn-bot/data.db"
_check_lock = asyncio.Lock()
_resend_until = 0.0
_csrf_key = secrets.token_bytes(32)
# Avoid logging receipt URLs containing taxpayer identifiers in this process.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


def csrf_token(request):
    session = request.cookies.get("session_id", "")
    return hmac.new(_csrf_key, session.encode(), hashlib.sha256).hexdigest()


async def render_journal(request, selected="all", page="1", result=None):
    context = await asyncio.to_thread(load_journal, DB, selected, page)
    response = templates.TemplateResponse("payments.html", {
        "request":request, **context, "csrf":csrf_token(request), "check_result":result
    })
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


@payments_router.get("/payments", response_class=HTMLResponse)
async def payments_page(request: Request):
    if not check_auth(request):
        return RedirectResponse("/")
    return await render_journal(request, request.query_params.get("filter","all"),
                                request.query_params.get("page","1"))


@payments_router.post("/payments/{order_id}/check", response_class=HTMLResponse)
async def check_payment(request: Request, order_id: int):
    if not check_auth(request):
        return RedirectResponse("/", status_code=303)
    form = await request.form()
    submitted = form.get("csrf", "")
    if not isinstance(submitted,str) or not hmac.compare_digest(submitted.encode(),csrf_token(request).encode()):
        raise HTTPException(status_code=403, detail="Invalid session confirmation")
    if _check_lock.locked():
        raise HTTPException(status_code=429, detail="Another check is running")
    async with _check_lock:
        result = await check_order(DB,order_id)
    return await render_journal(request, form.get("filter","all"), form.get("page","1"), result)


@payments_router.post("/payments/{order_id}/resend", response_class=HTMLResponse)
async def resend_payment(request: Request, order_id: int):
    global _resend_until
    if not check_auth(request):
        return RedirectResponse("/", status_code=303)
    form = await request.form()
    submitted = form.get("csrf", "")
    if not isinstance(submitted,str) or not hmac.compare_digest(submitted.encode(),csrf_token(request).encode()):
        raise HTTPException(status_code=403, detail="Invalid session confirmation")
    if form.get("confirm") != "yes":
        raise HTTPException(status_code=400, detail="Explicit confirmation required")
    if _check_lock.locked() or time.monotonic() < _resend_until:
        raise HTTPException(status_code=429, detail="Повторная отправка временно ограничена")
    async with _check_lock:
        _resend_until = time.monotonic() + 60
        try:
            result = await resend_order(DB, order_id)
        finally:
            _resend_until = time.monotonic() + 60
    return await render_journal(request, form.get("filter","all"), form.get("page","1"), result)
