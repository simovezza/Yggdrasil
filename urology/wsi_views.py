"""Urology WSI tile, metadata, and file serving API views."""

import io
import logging
import mimetypes
import os
import tempfile
import threading
from collections import OrderedDict

from django.contrib.auth.decorators import login_required
from django.http import Http404, HttpResponse, JsonResponse
from django.views.decorators.http import require_http_methods

from common.file_access import authorize_file_read, open_binary, streaming_response
from common.models import FileRegistry

from .wsi_reader import get_wsi_metadata, get_wsi_thumbnail, get_wsi_tile

logger = logging.getLogger(__name__)

# Local slide disk cache to prevent downloading multi-megabyte slides from MinIO S3 on every tile
WSI_CACHE_DIR = os.path.join(tempfile.gettempdir(), "ygg_wsi_cache")
os.makedirs(WSI_CACHE_DIR, exist_ok=True)

# In-memory LRU tile cache for sub-millisecond tile serving
_TILE_CACHE = OrderedDict()
_TILE_CACHE_LOCK = threading.Lock()
_MAX_TILE_CACHE_SIZE = 2048


def _get_cached_tile(key: str):
    with _TILE_CACHE_LOCK:
        if key in _TILE_CACHE:
            _TILE_CACHE.move_to_end(key)
            return _TILE_CACHE[key]
    return None


def _set_cached_tile(key: str, data: bytes):
    with _TILE_CACHE_LOCK:
        _TILE_CACHE[key] = data
        _TILE_CACHE.move_to_end(key)
        if len(_TILE_CACHE) > _MAX_TILE_CACHE_SIZE:
            _TILE_CACHE.popitem(last=False)


def _get_local_slide_path(file_obj) -> str:
    """Retrieve or cache the slide file locally on disk."""
    safe_hash = file_obj.file_hash or f"file_{file_obj.id}"
    local_path = os.path.join(WSI_CACHE_DIR, f"{safe_hash}.tif")
    if not os.path.exists(local_path):
        body, _ = open_binary(file_obj.file_path)
        tmp_path = f"{local_path}.{os.getpid()}.tmp"
        with open(tmp_path, "wb") as f_out:
            while True:
                chunk = body.read(1024 * 1024)
                if not chunk:
                    break
                f_out.write(chunk)
        os.replace(tmp_path, local_path)
    return local_path


def _get_authorized_file(request, file_id: int):
    file_obj = FileRegistry.objects.filter(id=file_id, domain="urology").first()
    if not file_obj:
        raise Http404("File not found")

    allowed, error, status = authorize_file_read(request.user, file_obj, "urology")
    if not allowed:
        logger.warning(
            "User %s denied access to urology file %s (%s)",
            request.user.id,
            file_id,
            error,
        )
        return None, JsonResponse({"error": error}, status=status)
    return file_obj, None


@login_required
@require_http_methods(["GET"])
def wsi_metadata_api(request, file_id: int):
    """Return JSON metadata describing the WSI pyramidal levels and physical calibration."""
    file_obj, error_resp = _get_authorized_file(request, file_id)
    if error_resp:
        return error_resp

    try:
        slide_path = _get_local_slide_path(file_obj)
        with open(slide_path, "rb") as fp:
            metadata = get_wsi_metadata(fp, cache_key=f"urology_wsi_{file_id}_{file_obj.file_hash}")
        metadata["fileId"] = file_obj.id
        metadata["filename"] = file_obj.metadata.get("original_filename", "") or ""
        return JsonResponse(metadata)
    except Exception as exc:
        logger.exception("Error extracting WSI metadata for file %s: %s", file_id, exc)
        return JsonResponse({"error": str(exc)}, status=500)


@login_required
@require_http_methods(["GET"])
def wsi_tile_api(request, file_id: int, level: int, col: int, row: int):
    """Serve a single tile at (level, col, row) with memory and disk caching."""
    file_obj, error_resp = _get_authorized_file(request, file_id)
    if error_resp:
        return error_resp

    is_png = request.path.endswith(".png") or request.GET.get("format") == "png"
    img_format = "PNG" if is_png else "JPEG"
    content_type = "image/png" if is_png else "image/jpeg"

    etag = f'"{file_obj.file_hash}_{level}_{col}_{row}"'
    if request.headers.get("If-None-Match") == etag:
        return HttpResponse(status=304)

    cache_key = f"{file_obj.file_hash}_{level}_{col}_{row}_{img_format}"
    cached_bytes = _get_cached_tile(cache_key)
    if cached_bytes:
        response = HttpResponse(cached_bytes, content_type=content_type)
        response["ETag"] = etag
        response["Cache-Control"] = "public, max-age=604800, immutable"
        return response

    try:
        slide_path = _get_local_slide_path(file_obj)
        with open(slide_path, "rb") as fp:
            tile_bytes = get_wsi_tile(
                fp, level=level, col=col, row=row, tile_size=256, image_format=img_format
            )

        _set_cached_tile(cache_key, tile_bytes)

        response = HttpResponse(tile_bytes, content_type=content_type)
        response["ETag"] = etag
        response["Cache-Control"] = "public, max-age=604800, immutable"
        return response
    except Exception as exc:
        logger.exception("Error reading WSI tile (%s, %s, %s) for file %s: %s", level, col, row, file_id, exc)
        return JsonResponse({"error": str(exc)}, status=500)


@login_required
@require_http_methods(["GET"])
def wsi_thumbnail_api(request, file_id: int):
    """Serve slide overview thumbnail for the floating minimap navigator."""
    file_obj, error_resp = _get_authorized_file(request, file_id)
    if error_resp:
        return error_resp

    etag = f'"{file_obj.file_hash}_thumb"'
    if request.headers.get("If-None-Match") == etag:
        return HttpResponse(status=304)

    try:
        slide_path = _get_local_slide_path(file_obj)
        with open(slide_path, "rb") as fp:
            thumb_bytes = get_wsi_thumbnail(fp, max_dim=512, image_format="JPEG")

        response = HttpResponse(thumb_bytes, content_type="image/jpeg")
        response["ETag"] = etag
        response["Cache-Control"] = "public, max-age=86400"
        return response
    except Exception as exc:
        logger.exception("Error generating thumbnail for file %s: %s", file_id, exc)
        return JsonResponse({"error": str(exc)}, status=500)


@login_required
@require_http_methods(["GET"])
def serve_file(request, file_id: int, filename: str = None, bundle_key: str = None):
    """Stream a Urology FileRegistry entry (e.g. NIfTI MRI volume) for Cornerstone3D."""
    del filename
    if bundle_key:
        raise Http404("Urology files do not use bundles")

    file_obj, error_resp = _get_authorized_file(request, file_id)
    if error_resp:
        return error_resp

    content_type = None
    original_name = file_obj.metadata.get("original_filename") or ""
    if original_name.endswith(".nii.gz") or (file_obj.file_path and file_obj.file_path.endswith(".gz")):
        content_type = "application/gzip"
    else:
        content_type = mimetypes.guess_type(original_name)[0] or "application/octet-stream"

    return streaming_response(
        path_or_key=file_obj.file_path,
        content_type=content_type,
        filename=original_name or "file.nii.gz",
        as_attachment=False,
    )
