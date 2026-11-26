"""Authenticated read-only dashboard."""
import asyncio
import time
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from .auth import check_auth
from dashboard_metrics import dashboard_data, local_metrics, probe_panels

dashboard_router = APIRouter(tags=["dashboard"])
templates = Jinja2Templates(directory="/opt/vpn-bot/templates")
DB = "/opt/vpn-bot/data.db"
HEADERS = {"Cache-Control":"no-store","Referrer-Policy":"no-referrer"}
_panel_lock=asyncio.Lock()
_panel_cache=None
_panel_until=0.0


@dashboard_router.get("/dashboard",response_class=HTMLResponse)
async def dashboard(request: Request):
    if not check_auth(request):
        return RedirectResponse("/",status_code=303,headers=HEADERS)
    context, metrics = await asyncio.gather(asyncio.to_thread(dashboard_data,DB),asyncio.to_thread(local_metrics))
    return templates.TemplateResponse("dashboard.html",{"request":request,**context,"metrics":metrics},headers=HEADERS)


@dashboard_router.get("/dashboard/live")
async def dashboard_live(request: Request):
    if not check_auth(request):
        return JSONResponse({"detail":"Unauthorized"},status_code=401,headers=HEADERS)
    return JSONResponse(await asyncio.to_thread(local_metrics),headers=HEADERS)


@dashboard_router.get("/dashboard/panels")
async def dashboard_panels(request: Request):
    global _panel_cache,_panel_until
    if not check_auth(request):
        return JSONResponse({"detail":"Unauthorized"},status_code=401,headers=HEADERS)
    async with _panel_lock:
        if _panel_cache is None or time.monotonic()>=_panel_until:
            try:
                _panel_cache=await probe_panels(DB)
            except Exception:
                _panel_cache=None
                return JSONResponse({"detail":"Unavailable"},status_code=503,headers=HEADERS)
            _panel_until=time.monotonic()+30
        return JSONResponse(_panel_cache,headers=HEADERS)
