"""Authenticated read-only payment journal."""
import asyncio
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from payment_admin_view import load_journal
from .auth import check_auth

payments_router = APIRouter(tags=["payments"])
templates = Jinja2Templates(directory="/opt/vpn-bot/templates")

@payments_router.get("/payments", response_class=HTMLResponse)
async def payments_page(request: Request):
    if not check_auth(request):
        return RedirectResponse("/")
    context = await asyncio.to_thread(load_journal, "/opt/vpn-bot/data.db",
                                      request.query_params.get("filter","all"),
                                      request.query_params.get("page","1"))
    response = templates.TemplateResponse("payments.html", {"request":request, **context})
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response
