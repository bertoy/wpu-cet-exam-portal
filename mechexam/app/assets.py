"""Cache-proof asset serving.

The single worst failure of the previous build was a host (PythonAnywhere)
serving an *old* copy of app.css from its own static mapping, which left the
administration pages rendering unstyled while the rest of the app looked fine.
No amount of redeploying fixed it because the stale copy lived in a layer the
application could not reach.

This module removes that failure mode by construction:

*   Stylesheets and scripts are served by Flask itself, from a URL that
    contains a hash of the file's own bytes -- ``/asset/9f2a1c.../css/app.css``.
    A host or browser cache can only ever hold a URL that matches content it
    actually received, so "stale" is not a reachable state.
*   The hashed response is marked ``immutable``, so caching is still
    aggressive; it is the *URL* that changes when the file does.
*   Templates call ``asset('css/app.css')``.  Nothing references ``/static/``
    for app CSS or JS, so a host static mapping cannot intercept it.
*   In debug, hashes are recomputed per request; in production they are
    computed once at boot and refreshed if the file's mtime moves, so editing
    a file on the server takes effect without a restart.
"""
from __future__ import annotations

import hashlib
import mimetypes
from pathlib import Path

from flask import Blueprint, abort, current_app, make_response

bp = Blueprint("assets", __name__)

_cache: dict[str, tuple[float, str]] = {}


def _asset_root() -> Path:
    return Path(current_app.root_path) / "static"


def _safe_path(relpath: str) -> Path:
    root = _asset_root().resolve()
    target = (root / relpath).resolve()
    if root != target and root not in target.parents:
        abort(404)
    return target


def digest(relpath: str) -> str:
    """Short content hash for ``relpath``, recomputed when the file changes."""
    target = _safe_path(relpath)
    if not target.is_file():
        return "missing"
    mtime = target.stat().st_mtime
    hit = _cache.get(relpath)
    if hit and hit[0] == mtime:
        return hit[1]
    h = hashlib.sha256(target.read_bytes()).hexdigest()[:16]
    _cache[relpath] = (mtime, h)
    return h


def asset_url(relpath: str) -> str:
    """URL for an app asset, carrying its own content hash."""
    relpath = relpath.lstrip("/")
    return f"/asset/{digest(relpath)}/{relpath}"


@bp.route("/asset/<string:token>/<path:relpath>")
def serve(token: str, relpath: str):
    target = _safe_path(relpath)
    if not target.is_file():
        abort(404)

    current = digest(relpath)
    body = target.read_bytes()
    mime = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
    if target.suffix == ".woff2":
        mime = "font/woff2"
    resp = make_response(body)
    resp.headers["Content-Type"] = mime
    resp.headers["ETag"] = f'"{current}"'
    resp.headers["X-Asset-Digest"] = current

    # Vendored libraries live in a version-named directory and their files are
    # fetched by relative URL from inside their own stylesheet, so the token in
    # the request belongs to the stylesheet rather than to the font.  Those are
    # immutable by name, so cache them hard regardless of the token -- without
    # this, every page load would re-fetch ~600 KB of maths fonts.
    is_vendor = relpath.startswith("vendor/")

    if token == current or is_vendor:
        resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    else:
        # Someone is holding an old URL.  Serve the current file but forbid
        # caching, so the next page load picks up the correct hashed URL.
        resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return resp


def register(app):
    app.register_blueprint(bp)
    app.jinja_env.globals["asset"] = asset_url
    app.jinja_env.globals["asset_digest"] = digest
