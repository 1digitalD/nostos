from __future__ import annotations

import hashlib
import ipaddress
import json
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from typing import Any, cast
from urllib.parse import urljoin, urlparse

import httpx

MAX_GALLERY_PHOTOS = 100
DEFAULT_SCAN_PHOTOS = 8
MAX_SCAN_PHOTOS = 12
MAX_DOWNLOAD_BYTES = 12 * 1024 * 1024
MAX_IMAGE_PIXELS = 30_000_000
MAX_OCR_LINES = 160
MAX_OCR_TEXT_CHARS = 12_000
FETCH_TIMEOUT_SECONDS = 12.0
OCR_TIMEOUT_SECONDS = 25.0
MAX_REDIRECTS = 3
MAX_GALLERY_SECONDS = 60.0

_ALLOWED_IMAGE_HOSTS = (
    "craigslist.org",
    "kijiji.ca",
    "ebayimg.com",
    "realtor.ca",
    "realtor.com",
    "crea.ca",
)

FetchImage = Callable[[str], bytes]
OCRImage = Callable[[bytes], object]


class FloorplanError(RuntimeError):
    """A safe, user-displayable floor-plan analysis error."""


class OCRUnavailableError(FloorplanError):
    """No supported local OCR implementation is available."""


_ROOM_PATTERN = re.compile(
    r"\b(?:primary|master|principal)?\s*(?:bed(?:room)?|bath(?:room)?|kitchen|"
    r"living(?:\s+room)?|dining(?:\s+room)?|den|office|foyer|entry|hall(?:way)?|"
    r"balcony|patio|terrace|closet|ensuite|laundry|utility|storage)\b",
    re.IGNORECASE,
)
_PLAN_PATTERN = re.compile(r"\b(?:floor\s*plan|unit\s*plan|suite\s*plan)\b", re.IGNORECASE)
_DIMENSION_PATTERN = re.compile(
    r"(?P<first>\d{1,3}(?:\.\d{1,2})?(?:\s*['′](?:\s*\d{1,2}(?:\.\d+)?\s*[\"″])?)?)"
    r"\s*(?P<first_unit>ft|feet|foot|m|metres?|meters?)?"
    r"\s*(?:x|×|by)\s*"
    r"(?P<second>\d{1,3}(?:\.\d{1,2})?(?:\s*['′](?:\s*\d{1,2}(?:\.\d+)?\s*[\"″])?)?)"
    r"\s*(?P<second_unit>ft|feet|foot|m|metres?|meters?)?(?=\s|$|[,;])",
    re.IGNORECASE,
)
_AREA_PATTERN = re.compile(
    r"(?P<area>\d{2,4}(?:\.\d{1,2})?)\s*(?P<unit>sq\.?\s*ft|sqft|ft[²2]|m[²2]|sq\.?\s*m)\b",
    re.IGNORECASE,
)
_TOTAL_AREA_PATTERN = re.compile(r"\b(?:total|overall|unit|suite)\b", re.IGNORECASE)


def gallery_fingerprint(urls: Sequence[str]) -> str:
    """Return a stable identity for the full saved gallery, including order."""

    normalized = _normalized_gallery(urls)
    payload = json.dumps(normalized, ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def analyze_photo(
    url: str,
    *,
    fetcher: FetchImage | None = None,
    ocr: OCRImage | None = None,
    _deadline: float | None = None,
) -> dict[str, Any]:
    """Fetch and locally OCR one listing image without deriving listing facts."""

    cleaned_url = _clean_url(url)
    try:
        image = (
            fetcher(cleaned_url)
            if fetcher is not None
            else fetch_image(cleaned_url, deadline=_deadline)
        )
    except FloorplanError as exc:
        return _photo_failure(cleaned_url, "error", str(exc))
    except Exception:
        return _photo_failure(cleaned_url, "error", "The listing image could not be downloaded.")

    if not image:
        return _photo_failure(cleaned_url, "error", "The listing image was empty.")
    if len(image) > MAX_DOWNLOAD_BYTES:
        return _photo_failure(cleaned_url, "error", "The listing image exceeded the size limit.")
    source_hash = hashlib.sha256(image).hexdigest()

    try:
        raw_ocr = (
            ocr(image)
            if ocr is not None
            else local_ocr(image, timeout=_remaining_seconds(_deadline, OCR_TIMEOUT_SECONDS))
        )
        engine, lines = _normalize_ocr_result(raw_ocr)
    except OCRUnavailableError as exc:
        result = _photo_failure(cleaned_url, "unavailable", str(exc))
        result["source_hash"] = source_hash
        return result
    except FloorplanError as exc:
        result = _photo_failure(cleaned_url, "error", str(exc))
        result["source_hash"] = source_hash
        return result
    except Exception:
        result = _photo_failure(cleaned_url, "error", "Local OCR could not read this image.")
        result["source_hash"] = source_hash
        return result

    measurements = _measurements(lines)
    room_labels = _room_labels(lines)
    has_plan_label = any(_PLAN_PATTERN.search(str(line["text"])) for line in lines)
    candidate = bool(
        has_plan_label
        or len(measurements) >= 2
        or (measurements and len(room_labels) >= 2)
    )
    limitations = [
        "OCR text can be incomplete or wrong; compare every label with the original image.",
        "Dimensions are printed text candidates, not verified unit facts.",
        "No total area, room count, geometry, or listing score is inferred.",
    ]
    if not lines:
        limitations.insert(0, "No readable printed text was found in this image.")
    elif not candidate:
        limitations.insert(0, "The readable text did not contain enough plan evidence.")
    return {
        "status": "candidate" if candidate else "not_candidate",
        "candidate": candidate,
        "url": cleaned_url,
        "source_hash": source_hash,
        "ocr_engine": engine,
        "lines": lines,
        "measurements": measurements,
        "room_labels": room_labels,
        "limitations": limitations,
    }


def analyze_gallery(
    urls: Sequence[str],
    *,
    selected_url: str | None = None,
    max_photos: int = DEFAULT_SCAN_PHOTOS,
    fetcher: FetchImage | None = None,
    ocr: OCRImage | None = None,
) -> dict[str, Any]:
    """Analyze a bounded gallery or exactly one user-selected saved photo."""

    gallery = _normalized_gallery(urls)
    gallery_hash = gallery_fingerprint(gallery)
    if not 1 <= max_photos <= MAX_SCAN_PHOTOS:
        raise ValueError(f"max_photos must be between 1 and {MAX_SCAN_PHOTOS}.")
    if selected_url is not None:
        selected = _clean_url(selected_url)
        if selected not in gallery:
            raise ValueError("The selected photo is not in the current saved gallery.")
        scan_urls = [selected]
        scan_scope = "selected"
    else:
        scan_urls = gallery[:max_photos]
        scan_scope = "automatic"

    deadline = time.monotonic() + MAX_GALLERY_SECONDS
    photos: list[dict[str, Any]] = []
    for url in scan_urls:
        if time.monotonic() >= deadline:
            photos.append(
                _photo_failure(url, "error", "This image was skipped after the scan time limit.")
            )
            continue
        photos.append(analyze_photo(url, fetcher=fetcher, ocr=ocr, _deadline=deadline))
    candidate_count = sum(item.get("status") == "candidate" for item in photos)
    readable_count = sum(item.get("status") in {"candidate", "not_candidate"} for item in photos)
    if not gallery:
        status = "empty"
    elif candidate_count:
        status = "candidate"
    elif readable_count:
        status = "not_candidate"
    elif any(item.get("status") == "unavailable" for item in photos):
        status = "unavailable"
    else:
        status = "error"

    limitations = [
        "Only saved listing-gallery images in this explicit scan were analyzed.",
        "A candidate remains unconfirmed until the user selects the applicable image.",
    ]
    if not gallery:
        limitations.insert(0, "This listing has no saved gallery images to analyze.")
    elif len(scan_urls) < len(gallery):
        limitations.append(f"{len(gallery) - len(scan_urls)} saved image(s) were not analyzed.")
    return {
        "status": status,
        "gallery_hash": gallery_hash,
        "gallery_count": len(gallery),
        "scan_scope": scan_scope,
        "selected_url": selected_url,
        "analyzed_count": len(photos),
        "truncated": len(scan_urls) < len(gallery),
        "candidate_count": candidate_count,
        "photos": photos,
        "limitations": limitations,
        "analyzed_at": datetime.now(UTC).isoformat(),
    }


def fetch_image(url: str, *, deadline: float | None = None) -> bytes:
    """Fetch an image while validating every network destination and redirect."""

    current = _clean_url(url)
    timeout = _remaining_seconds(deadline, FETCH_TIMEOUT_SECONDS)
    with httpx.Client(follow_redirects=False, timeout=timeout) as client:
        for redirect_count in range(MAX_REDIRECTS + 1):
            _assert_public_destination(current)
            try:
                with client.stream(
                    "GET",
                    current,
                    headers={
                        "Accept": "image/avif,image/webp,image/png,image/jpeg,image/*;q=0.8",
                        "User-Agent": "Nostos/0.4 local floor-plan review",
                    },
                ) as response:
                    if response.status_code in {301, 302, 303, 307, 308}:
                        location = response.headers.get("location", "").strip()
                        if not location or redirect_count >= MAX_REDIRECTS:
                            raise FloorplanError("The listing image used too many redirects.")
                        current = urljoin(current, location)
                        continue
                    response.raise_for_status()
                    content_type = response.headers.get("content-type", "").split(";", 1)[0]
                    if not content_type.casefold().startswith("image/"):
                        raise FloorplanError("The listing image URL did not return an image.")
                    declared_size = response.headers.get("content-length")
                    if declared_size and int(declared_size) > MAX_DOWNLOAD_BYTES:
                        raise FloorplanError("The listing image exceeded the size limit.")
                    chunks: list[bytes] = []
                    size = 0
                    for chunk in response.iter_bytes():
                        if deadline is not None and time.monotonic() >= deadline:
                            raise FloorplanError("The listing image exceeded the scan time limit.")
                        size += len(chunk)
                        if size > MAX_DOWNLOAD_BYTES:
                            raise FloorplanError("The listing image exceeded the size limit.")
                        chunks.append(chunk)
                    return b"".join(chunks)
            except FloorplanError:
                raise
            except (httpx.HTTPError, ValueError) as exc:
                raise FloorplanError("The listing image could not be downloaded.") from exc
    raise FloorplanError("The listing image could not be downloaded.")


def local_ocr(image: bytes, *, timeout: float = OCR_TIMEOUT_SECONDS) -> dict[str, Any]:
    """Run bounded local OCR via Tesseract or macOS Vision."""

    image_path: Path | None = None
    try:
        image_path = _normalized_image_file(image)
        tesseract = shutil.which("tesseract")
        if tesseract:
            return {
                "engine": "tesseract",
                "lines": _tesseract_lines(tesseract, image_path, timeout=timeout),
            }
        swift = shutil.which("swift") if sys.platform == "darwin" else None
        if swift:
            helper = Path(__file__).with_name("vision_ocr.swift")
            return {
                "engine": "macos_vision",
                "lines": _vision_lines(swift, helper, image_path, timeout=timeout),
            }
        raise OCRUnavailableError(
            "Local OCR is unavailable. Install the floorplans extra and Tesseract, "
            "or run Nostos on macOS with Swift Vision available."
        )
    finally:
        if image_path is not None:
            image_path.unlink(missing_ok=True)


def save_analysis(conn: sqlite3.Connection, listing_id: str, result: Mapping[str, Any]) -> None:
    """Save the latest outcome while retaining the last readable snapshot on failure."""

    gallery_hash = str(result.get("gallery_hash") or "")
    if not re.fullmatch(r"[0-9a-f]{64}", gallery_hash):
        raise ValueError("Analysis is missing a valid gallery hash.")
    latest_json = _bounded_result_json(result)
    usable = result.get("status") in {"candidate", "not_candidate", "empty"}
    snapshot_json = latest_json if usable else None
    now = datetime.now(UTC).isoformat()
    with conn:
        conn.execute(
            """
            INSERT INTO floorplan_analysis(
                listing_id,gallery_hash,result_json,latest_result_json,analyzed_at
            ) VALUES (?,?,?,?,?)
            ON CONFLICT(listing_id) DO UPDATE SET
                gallery_hash=CASE
                    WHEN excluded.result_json IS NOT NULL THEN excluded.gallery_hash
                    ELSE floorplan_analysis.gallery_hash
                END,
                result_json=COALESCE(excluded.result_json,floorplan_analysis.result_json),
                latest_result_json=excluded.latest_result_json,
                analyzed_at=excluded.analyzed_at
            """,
            (listing_id, gallery_hash, snapshot_json, latest_json, now),
        )


def set_floorplan_decision(
    conn: sqlite3.Connection,
    listing_id: str,
    gallery_hash: str,
    decision: str,
    selected_source_hash: str | None = None,
    note: str = "",
) -> None:
    """Record whether the user accepts one image as applicable to the listing."""

    if decision not in {"confirmed", "rejected"}:
        raise ValueError("decision must be 'confirmed' or 'rejected'.")
    if len(note) > 2_000:
        raise ValueError("The floor-plan note must be 2,000 characters or fewer.")
    state = get_floorplan_state(conn, listing_id=listing_id)
    analysis = state["analysis"]
    if analysis is None or analysis.get("gallery_hash") != gallery_hash:
        raise ValueError("The saved gallery changed; run analysis again before deciding.")
    hashes = {
        str(photo.get("source_hash"))
        for photo in cast(list[dict[str, Any]], analysis.get("photos", []))
        if photo.get("source_hash")
    }
    if decision == "confirmed":
        if not selected_source_hash or selected_source_hash not in hashes:
            raise ValueError("Choose an analyzed image from the current gallery.")
    elif selected_source_hash is not None and selected_source_hash not in hashes:
        raise ValueError("Choose an analyzed image from the current gallery.")
    with conn:
        conn.execute(
            """
            INSERT INTO floorplan_decision(
                listing_id,gallery_hash,decision,selected_source_hash,note,decided_at
            ) VALUES (?,?,?,?,?,?)
            ON CONFLICT(listing_id) DO UPDATE SET
                gallery_hash=excluded.gallery_hash,
                decision=excluded.decision,
                selected_source_hash=excluded.selected_source_hash,
                note=excluded.note,
                decided_at=excluded.decided_at
            """,
            (
                listing_id,
                gallery_hash,
                decision,
                selected_source_hash,
                note,
                datetime.now(UTC).isoformat(),
            ),
        )


def get_floorplan_state(conn: sqlite3.Connection, listing_id: str) -> dict[str, Any]:
    """Return the last usable evidence and independent user decision."""

    analysis_row = conn.execute(
        """
        SELECT gallery_hash,result_json,latest_result_json,analyzed_at
        FROM floorplan_analysis WHERE listing_id=?
        """,
        (listing_id,),
    ).fetchone()
    decision_row = conn.execute(
        """
        SELECT gallery_hash,decision,selected_source_hash,note,decided_at
        FROM floorplan_decision WHERE listing_id=?
        """,
        (listing_id,),
    ).fetchone()
    analysis: dict[str, Any] | None = None
    if analysis_row is not None:
        saved_raw = analysis_row["result_json"]
        latest = cast(dict[str, Any], json.loads(str(analysis_row["latest_result_json"])))
        analysis = cast(dict[str, Any], json.loads(str(saved_raw))) if saved_raw else latest
        if saved_raw and latest != analysis:
            analysis["latest_outcome"] = latest
    decision_payload: dict[str, Any] | None = None
    if decision_row is not None:
        decision_payload = dict(decision_row)
        if analysis is None:
            decision_payload["gallery_changed"] = True
            decision_payload["selection_changed"] = decision_row["selected_source_hash"] is not None
        else:
            gallery_changed = str(decision_row["gallery_hash"]) != str(
                analysis.get("gallery_hash")
            )
            current_hashes = {
                str(photo.get("source_hash"))
                for photo in cast(list[dict[str, Any]], analysis.get("photos", []))
                if photo.get("source_hash")
            }
            selected_hash = decision_row["selected_source_hash"]
            decision_payload["gallery_changed"] = gallery_changed
            decision_payload["selection_changed"] = bool(
                selected_hash is not None
                and selected_hash not in current_hashes
            )
    return {"analysis": analysis, "decision": decision_payload}


def _normalized_gallery(urls: Sequence[str]) -> list[str]:
    if isinstance(urls, (str, bytes)):
        raise TypeError("Gallery URLs must be a sequence of URL strings.")
    if len(urls) > MAX_GALLERY_PHOTOS:
        raise ValueError(f"A saved gallery cannot exceed {MAX_GALLERY_PHOTOS} images.")
    result: list[str] = []
    seen: set[str] = set()
    for url in urls:
        cleaned = _clean_url(url)
        if cleaned not in seen:
            seen.add(cleaned)
            result.append(cleaned)
    return result


def _clean_url(url: str) -> str:
    cleaned = str(url).strip()
    parsed = urlparse(cleaned)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Floor-plan images must use an HTTP or HTTPS URL.")
    if parsed.username or parsed.password:
        raise ValueError("Floor-plan image URLs cannot contain credentials.")
    return cleaned


def _assert_public_destination(url: str) -> None:
    parsed = urlparse(_clean_url(url))
    hostname = parsed.hostname
    assert hostname is not None
    try:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError as exc:
        raise FloorplanError("The listing image URL has an invalid port.") from exc
    if port not in {80, 443}:
        raise FloorplanError("The listing image URL must use the standard web port.")
    try:
        addresses = {
            str(item[4][0])
            for item in socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
        }
    except socket.gaierror as exc:
        raise FloorplanError("The listing image host could not be resolved.") from exc
    if not addresses:
        raise FloorplanError("The listing image host could not be resolved.")
    for address in addresses:
        try:
            ip = ipaddress.ip_address(address.split("%", 1)[0])
        except ValueError as exc:
            raise FloorplanError("The listing image host returned an invalid address.") from exc
        if not ip.is_global:
            raise FloorplanError("The listing image host is not a public network address.")
    normalized_host = hostname.rstrip(".").casefold()
    if not any(
        normalized_host == allowed or normalized_host.endswith(f".{allowed}")
        for allowed in _ALLOWED_IMAGE_HOSTS
    ):
        raise FloorplanError("The listing image host is not a supported rental image host.")


def _photo_failure(url: str, status: str, message: str) -> dict[str, Any]:
    return {
        "status": status,
        "candidate": False,
        "url": url,
        "source_hash": None,
        "ocr_engine": None,
        "lines": [],
        "measurements": [],
        "room_labels": [],
        "limitations": [message],
    }


def _normalize_ocr_result(raw: object) -> tuple[str, list[dict[str, Any]]]:
    engine = "injected"
    values: object = raw
    if isinstance(raw, Mapping):
        engine = str(raw.get("engine") or engine)[:80]
        values = raw.get("lines", [])
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
        raise FloorplanError("Local OCR returned an invalid result.")
    lines: list[dict[str, Any]] = []
    total_chars = 0
    for value in values[:MAX_OCR_LINES]:
        if isinstance(value, Mapping):
            text = " ".join(str(value.get("text") or "").split())
            raw_confidence = value.get("confidence", 0.0)
        else:
            text = " ".join(str(value).split())
            raw_confidence = 0.0
        if not text:
            continue
        try:
            confidence = min(1.0, max(0.0, float(raw_confidence)))
        except (TypeError, ValueError):
            confidence = 0.0
        remaining = MAX_OCR_TEXT_CHARS - total_chars
        if remaining <= 0:
            break
        text = text[:remaining]
        total_chars += len(text)
        lines.append({"text": text, "confidence": round(confidence, 3)})
    return engine, lines


def _room_labels(lines: list[dict[str, Any]]) -> list[str]:
    labels: list[str] = []
    seen: set[str] = set()
    for line in lines:
        for match in _ROOM_PATTERN.finditer(str(line["text"])):
            label = " ".join(match.group(0).split())
            key = label.casefold()
            if key not in seen:
                seen.add(key)
                labels.append(label)
    return labels


def _measurements(lines: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for line in lines:
        text = str(line["text"])
        room_match = _ROOM_PATTERN.search(text)
        room_label = " ".join(room_match.group(0).split()) if room_match else None
        confidence = float(line["confidence"])
        for match in _DIMENSION_PATTERN.finditer(text):
            result.append(
                {
                    "kind": "dimensions",
                    "label": room_label or "Unassigned printed dimension",
                    "raw_text": match.group(0).strip(),
                    "evidence_text": text,
                    "value_1": match.group("first").strip(),
                    "value_2": match.group("second").strip(),
                    "unit": (
                        match.group("second_unit") or match.group("first_unit") or "as printed"
                    ).strip(),
                    "confidence": round(confidence, 3),
                }
            )
        for match in _AREA_PATTERN.finditer(text):
            label = (
                "Printed total area"
                if _TOTAL_AREA_PATTERN.search(text)
                else room_label or "Unassigned printed area"
            )
            result.append(
                {
                    "kind": "printed_area",
                    "label": label,
                    "raw_text": match.group(0).strip(),
                    "evidence_text": text,
                    "value": match.group("area"),
                    "unit": match.group("unit").strip(),
                    "confidence": round(confidence, 3),
                }
            )
    return result[:80]


def _normalized_image_file(image: bytes) -> Path:
    try:
        from PIL import Image, ImageOps
    except ImportError as exc:
        raise OCRUnavailableError(
            "Local OCR image checks require the optional floorplans dependency."
        ) from exc
    try:
        with Image.open(BytesIO(image)) as opened:
            width, height = opened.size
            if width <= 0 or height <= 0 or width * height > MAX_IMAGE_PIXELS:
                raise FloorplanError("The listing image exceeded the pixel limit.")
            normalized = ImageOps.exif_transpose(opened).convert("RGB")
            normalized.thumbnail((5_000, 5_000))
            handle = tempfile.NamedTemporaryFile(
                prefix="nostos-floorplan-", suffix=".jpg", delete=False
            )
            path = Path(handle.name)
            handle.close()
            normalized.save(path, format="JPEG", quality=92)
            return path
    except FloorplanError:
        raise
    except Exception as exc:
        raise FloorplanError("The listing image format could not be read safely.") from exc


def _tesseract_lines(
    binary: str, image_path: Path, *, timeout: float
) -> list[dict[str, Any]]:
    try:
        completed = subprocess.run(
            [binary, str(image_path), "stdout", "tsv"],
            check=True,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        raise FloorplanError("Tesseract could not read this image within the time limit.") from exc
    grouped: dict[tuple[str, str, str, str], list[tuple[str, float]]] = {}
    rows = completed.stdout.splitlines()
    for row in rows[1:]:
        columns = row.split("\t", 11)
        if len(columns) != 12 or not columns[11].strip():
            continue
        key = (columns[1], columns[2], columns[3], columns[4])
        try:
            confidence = max(0.0, float(columns[10])) / 100
        except ValueError:
            confidence = 0.0
        grouped.setdefault(key, []).append((columns[11].strip(), confidence))
    return [
        {
            "text": " ".join(word for word, _confidence in words),
            "confidence": sum(confidence for _word, confidence in words) / len(words),
        }
        for words in grouped.values()
    ]


def _vision_lines(
    binary: str, helper: Path, image_path: Path, *, timeout: float
) -> list[dict[str, Any]]:
    try:
        completed = subprocess.run(
            [binary, str(helper), str(image_path)],
            check=True,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        payload = json.loads(completed.stdout)
    except subprocess.TimeoutExpired as exc:
        raise FloorplanError("macOS Vision OCR exceeded the time limit.") from exc
    except (subprocess.SubprocessError, OSError, json.JSONDecodeError) as exc:
        raise FloorplanError("macOS Vision OCR could not read this image.") from exc
    if not isinstance(payload, list):
        raise FloorplanError("macOS Vision OCR returned an invalid result.")
    return cast(list[dict[str, Any]], payload)


def _bounded_result_json(result: Mapping[str, Any]) -> str:
    try:
        encoded = json.dumps(dict(result), ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    except (TypeError, ValueError) as exc:
        raise ValueError("Floor-plan analysis must be JSON-safe.") from exc
    if len(encoded.encode("utf-8")) > 500_000:
        raise ValueError("Floor-plan analysis exceeded the storage limit.")
    return encoded


def _remaining_seconds(deadline: float | None, cap: float) -> float:
    if deadline is None:
        return cap
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise FloorplanError("The floor-plan scan exceeded its time limit.")
    return max(0.1, min(cap, remaining))
