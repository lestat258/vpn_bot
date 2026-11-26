"""Install metadata and public, generated branding assets. No offline data cache."""
import functools
import struct
import zlib
from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse, Response

pwa_router = APIRouter(tags=["pwa"])
SIZES = (180, 192, 512)
SHIELD = ((.5,.17),(.77,.27),(.75,.57),(.69,.72),(.5,.84),(.31,.72),(.25,.57),(.23,.27))


def inside(x, y):
    hit = False
    for i, (a,b) in enumerate(SHIELD):
        c,d = SHIELD[i-1]
        if (b > y) != (d > y) and x < (c-a)*(y-b)/(d-b)+a:
            hit = not hit
    return hit


def pixel(x,y):
    color = (13,23,42)
    if inside(x,y):
        color = (35,108,222)
        # A light lock silhouette, with a dark keyhole.
        arc = ((x-.5)**2+(y-.43)**2)**.5
        if ((.078 < arc < .115 and y <= .45) or
            (.385 <= x <= .42 and .43 <= y <= .51) or
            (.58 <= x <= .615 and .43 <= y <= .51) or
            (.35 <= x <= .65 and .48 <= y <= .65)):
            color = (238,246,255)
        if (x-.5)**2+(y-.545)**2 < .022**2 or (.49 <= x <= .51 and .545 <= y <= .595):
            color = (13,23,42)
    return color


def chunk(kind,data):
    return struct.pack(">I",len(data))+kind+data+struct.pack(">I",zlib.crc32(kind+data)&0xffffffff)


@functools.lru_cache(maxsize=3)
def icon_png(size):
    if size not in SIZES:
        raise ValueError("Unsupported icon size")
    rows = bytearray()
    for y in range(size):
        rows.append(0)
        for x in range(size):
            rows.extend(pixel((x+.5)/size,(y+.5)/size))
    header = struct.pack(">IIBBBBB",size,size,8,2,0,0,0)
    return b"\x89PNG\r\n\x1a\n"+chunk(b"IHDR",header)+chunk(b"IDAT",zlib.compress(rows))+chunk(b"IEND",b"")


@pwa_router.get("/admin-manifest.webmanifest", include_in_schema=False)
def manifest():
    return JSONResponse({
        "id": "/", "name": "VPN4US Admin", "short_name": "VPN4US",
        "start_url": "/dashboard", "scope": "/", "display": "standalone",
        "lang": "ru", "background_color": "#0d1117", "theme_color": "#0d1117",
        "icons": [{"src": f"/admin-icon/{size}.png", "sizes": f"{size}x{size}",
                   "type": "image/png", "purpose": "any"} for size in (192,512)]
    }, media_type="application/manifest+json", headers={"Cache-Control":"no-cache"})


@pwa_router.get("/admin-icon/{size}.png", include_in_schema=False)
def icon(size: int):
    if size not in SIZES:
        raise HTTPException(status_code=404)
    return Response(icon_png(size), media_type="image/png",
                    headers={"Cache-Control":"public, max-age=86400"})


@pwa_router.get("/favicon.ico", include_in_schema=False)
def favicon():
    # Redirect to a real PNG with the correct media type.
    from fastapi.responses import RedirectResponse
    return RedirectResponse("/admin-icon/192.png")
