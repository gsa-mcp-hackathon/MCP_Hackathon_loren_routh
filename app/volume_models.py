"""Serve APS derivative packages hosted in a Unity Catalog Volume.

This is the SCALABLE storage path: packages produced by RevitExtractor.exe are
uploaded to a UC Volume (one source of truth, reachable by Databricks and this
backend). The browser viewer cannot fetch Volume paths directly, so this module
proxies them over HTTP with Range support — exactly what the APS viewer needs to
stream SVF (it issues many small byte-range reads of .pf/.pack/.svf files).

Additive and parallel: the existing local /models mount and the Genie path are
left untouched. The viewer can load from here OR from local files; flip one URL.

Auth reuses the backend's existing DATABRICKS_HOST / DATABRICKS_TOKEN (the same
credentials db.py uses for SQL). No new secrets.
"""
import os
import urllib.parse

import requests
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response

router = APIRouter()

# Root of the hosted packages inside the Volume. A viewer path of
# "mech/Resource/3D View/.../{3D}.svf" maps to VOLUME_ROOT + "/mech/Resource/...".
VOLUME_ROOT = "/Volumes/users/loren_routh/mcp_hackathon_data/aps_package"

# Databricks Files API: GET /api/2.0/fs/files{absolute_volume_path}
_FILES_API_PREFIX = "/api/2.0/fs/files"


def _host():
    host = os.environ.get("DATABRICKS_HOST")
    if not host:
        raise HTTPException(
            status_code=503,
            detail="DATABRICKS_HOST is not set; cannot reach the Volume.",
        )
    return host.rstrip("/")


def _token():
    token = os.getenv("DATABRICKS_TOKEN")
    if not token:
        raise HTTPException(
            status_code=503,
            detail="DATABRICKS_TOKEN is not loaded; cannot reach the Volume.",
        )
    return token


def _files_api_url(volume_path):
    # volume_path is an absolute Volume path beginning with /Volumes/...
    # The Files API wants real '/' path separators; only other reserved
    # characters (e.g. spaces in "3D View") may be percent-encoded. Leaving
    # the default safe='/' here is deliberate — encoding slashes to %2F makes
    # the API 404.
    return _host() + _FILES_API_PREFIX + urllib.parse.quote(volume_path, safe="/")


@router.get("/dbx-models/{package_path:path}")
def serve_package_file(package_path: str, request: Request):
    """Stream one file from the hosted package, preserving Range semantics.

    package_path is everything after /dbx-models/, e.g.
    "mech/Resource/3D View/54872479-.../{3D}.svf". It is resolved beneath
    VOLUME_ROOT; any attempt to escape the root (via "..") is rejected.
    """
    # Normalize and contain the path: no traversal outside the package root.
    decoded = urllib.parse.unquote(package_path)
    if ".." in decoded.split("/"):
        raise HTTPException(status_code=400, detail="Invalid path.")
    volume_path = f"{VOLUME_ROOT}/{decoded}"

    # Forward only a Range header if the viewer sent one; everything else is
    # our own request to the Files API.
    upstream_headers = {"Authorization": "Bearer " + _token()}
    range_header = request.headers.get("range")
    if range_header:
        upstream_headers["Range"] = range_header

    try:
        upstream = requests.get(
            _files_api_url(volume_path),
            headers=upstream_headers,
            timeout=60,
        )
    except requests.RequestException as exc:
        # Surface a real failure, not a fake success.
        raise HTTPException(
            status_code=502, detail="Volume file fetch failed."
        ) from exc

    if upstream.status_code not in (200, 206):
        # 404 -> file not in the package; anything else -> upstream problem.
        raise HTTPException(
            status_code=upstream.status_code if upstream.status_code == 404 else 502,
            detail="Volume file unavailable.",
        )

    # Pass through the bytes and the headers the viewer relies on for streaming.
    passthrough = {}
    for name in ("Content-Type", "Content-Range", "Accept-Ranges", "Content-Length"):
        if name in upstream.headers:
            passthrough[name] = upstream.headers[name]
    passthrough.setdefault("Accept-Ranges", "bytes")

    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        headers=passthrough,
    )
