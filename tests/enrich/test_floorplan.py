from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

import nostos.enrich.floorplan as floorplan_module
from nostos.enrich.floorplan import (
    FloorplanError,
    OCRUnavailableError,
    analyze_gallery,
    analyze_photo,
    fetch_image,
    gallery_fingerprint,
    get_floorplan_state,
    local_ocr,
    save_analysis,
    set_floorplan_decision,
)
from nostos.store.db import apply_migrations, connect
from nostos.store.repo import ListingRepo


def _plan_ocr(_image: bytes) -> dict[str, Any]:
    return {
        "engine": "fixture",
        "lines": [
            {"text": "BEDROOM 10' 6\" x 12' 0\"", "confidence": 0.82},
            {"text": "LIVING ROOM 14 ft x 11 ft", "confidence": 0.88},
            {"text": "TOTAL 812 SQ FT", "confidence": 0.94},
        ],
    }


def test_analyze_photo_labels_candidate_without_promoting_listing_facts() -> None:
    result = analyze_photo(
        "https://images.example.test/plan.jpg",
        fetcher=lambda _url: b"fixture-image",
        ocr=_plan_ocr,
    )

    assert result["status"] == "candidate"
    assert result["candidate"] is True
    assert result["ocr_engine"] == "fixture"
    assert {item["label"].casefold() for item in result["measurements"]} == {
        "bedroom",
        "living room",
        "printed total area",
    }
    total = next(item for item in result["measurements"] if item["label"] == "Printed total area")
    assert total["raw_text"] == "812 SQ FT"
    assert "area_sqft" not in result
    assert "room_count" not in result


def test_dimension_parser_preserves_unassigned_text_without_cross_line_room_guess() -> None:
    result = analyze_photo(
        "https://images.example.test/plan.jpg",
        fetcher=lambda _url: b"fixture-image",
        ocr=lambda _image: [
            {"text": "BEDROOM", "confidence": 0.9},
            {"text": "10' x 12'", "confidence": 0.7},
            {"text": "KITCHEN 8' 6\" x 9' 2\"", "confidence": 0.8},
        ],
    )

    assert result["measurements"][0]["label"] == "Unassigned printed dimension"
    assert result["measurements"][0]["evidence_text"] == "10' x 12'"
    assert result["measurements"][1]["label"].casefold() == "kitchen"


def test_gallery_scan_is_bounded_but_selected_photo_can_be_beyond_first_eight() -> None:
    urls = [f"https://images.example.test/{index}.jpg" for index in range(10)]
    fetched: list[str] = []

    def fetch(url: str) -> bytes:
        fetched.append(url)
        return url.encode()

    automatic = analyze_gallery(urls, fetcher=fetch, ocr=lambda _image: [])
    assert automatic["gallery_hash"] == gallery_fingerprint(urls)
    assert automatic["analyzed_count"] == 8
    assert automatic["truncated"] is True
    assert fetched == urls[:8]

    fetched.clear()
    selected = analyze_gallery(urls, selected_url=urls[9], fetcher=fetch, ocr=_plan_ocr)
    assert selected["gallery_hash"] == automatic["gallery_hash"]
    assert selected["scan_scope"] == "selected"
    assert selected["analyzed_count"] == 1
    assert selected["photos"][0]["url"] == urls[9]
    assert fetched == [urls[9]]

    with pytest.raises(ValueError, match="not in the current saved gallery"):
        analyze_gallery(
            urls,
            selected_url="https://images.example.test/not-saved.jpg",
            fetcher=fetch,
            ocr=_plan_ocr,
        )


def test_empty_fetch_ocr_unavailable_and_ocr_failure_are_useful_outcomes() -> None:
    url = "https://images.example.test/plan.jpg"
    empty = analyze_gallery([])
    assert empty["status"] == "empty"
    assert empty["photos"] == []

    failed_fetch = analyze_photo(url, fetcher=lambda _url: b"", ocr=_plan_ocr)
    assert failed_fetch["status"] == "error"
    assert "empty" in failed_fetch["limitations"][0].casefold()

    def unavailable(_image: bytes) -> object:
        raise OCRUnavailableError("Local OCR is unavailable for this fixture.")

    missing = analyze_photo(url, fetcher=lambda _url: b"image", ocr=unavailable)
    assert missing["status"] == "unavailable"
    assert missing["source_hash"]

    def broken(_image: bytes) -> object:
        raise FloorplanError("Local OCR timed out.")

    failure = analyze_photo(url, fetcher=lambda _url: b"image", ocr=broken)
    assert failure["status"] == "error"
    assert failure["limitations"] == ["Local OCR timed out."]


def test_fetch_rejects_private_destination_before_request() -> None:
    with pytest.raises(FloorplanError, match="not a public network address"):
        fetch_image("http://127.0.0.1/floor-plan.jpg")


def test_tesseract_adapter_keeps_words_on_the_same_printed_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tsv = "\n".join(
        [
            "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext",
            "5\t1\t1\t1\t1\t1\t0\t0\t10\t10\t90\tBEDROOM",
            "5\t1\t1\t1\t1\t2\t0\t0\t10\t10\t80\t10",
            "5\t1\t1\t1\t1\t3\t0\t0\t10\t10\t85\tx",
            "5\t1\t1\t1\t1\t4\t0\t0\t10\t10\t80\t12",
        ]
    )
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0, stdout=tsv, stderr=""),
    )

    lines = floorplan_module._tesseract_lines("tesseract", Path("fixture.jpg"), timeout=1)

    assert lines == [{"text": "BEDROOM 10 x 12", "confidence": pytest.approx(0.8375)}]


def test_persistence_keeps_decision_and_last_usable_snapshot_on_failure(tmp_path: Path) -> None:
    db_path = tmp_path / "nostos.db"
    url = "https://images.example.test/plan.jpg"
    usable = analyze_gallery([url], fetcher=lambda _url: b"plan-v1", ocr=_plan_ocr)
    selected_hash = usable["photos"][0]["source_hash"]

    with connect(db_path) as conn:
        apply_migrations(conn)
        ListingRepo(conn).ensure_listing("listing-1")
        save_analysis(conn, "listing-1", usable)
        set_floorplan_decision(
            conn,
            "listing-1",
            gallery_hash=usable["gallery_hash"],
            selected_source_hash=selected_hash,
            decision="confirmed",
            note="Bedroom label may read 10'6 by 12'0.",
        )
        initial = get_floorplan_state(conn, "listing-1")

        failed = analyze_gallery(
            [url],
            fetcher=lambda _url: b"plan-v1",
            ocr=lambda _image: (_ for _ in ()).throw(OCRUnavailableError("OCR offline.")),
        )
        save_analysis(conn, "listing-1", failed)
        retained = get_floorplan_state(conn, "listing-1")

    assert initial["decision"]["decision"] == "confirmed"
    assert initial["decision"]["note"].startswith("Bedroom")
    assert retained["analysis"]["status"] == "candidate"
    assert retained["analysis"]["latest_outcome"]["status"] == "unavailable"
    assert retained["decision"]["selection_changed"] is False


def test_reanalysis_flags_a_selected_image_that_changed(tmp_path: Path) -> None:
    db_path = tmp_path / "nostos.db"
    first_url = "https://images.example.test/one.jpg"
    first = analyze_gallery([first_url], fetcher=lambda _url: b"first", ocr=_plan_ocr)
    with connect(db_path) as conn:
        apply_migrations(conn)
        ListingRepo(conn).ensure_listing("listing-1")
        save_analysis(conn, "listing-1", first)
        set_floorplan_decision(
            conn,
            "listing-1",
            gallery_hash=first["gallery_hash"],
            selected_source_hash=first["photos"][0]["source_hash"],
            decision="rejected",
            note="This belongs to another unit.",
        )
        changed = analyze_gallery(
            ["https://images.example.test/two.jpg"],
            fetcher=lambda _url: b"second",
            ocr=_plan_ocr,
        )
        save_analysis(conn, "listing-1", changed)
        state = get_floorplan_state(conn, "listing-1")

    assert state["decision"]["gallery_changed"] is True
    assert state["decision"]["selection_changed"] is True


@pytest.mark.skipif(
    not shutil.which("tesseract") and not (sys.platform == "darwin" and shutil.which("swift")),
    reason="No supported local OCR backend is installed.",
)
def test_installed_local_ocr_reads_a_real_generated_image(tmp_path: Path) -> None:
    image_module = pytest.importorskip("PIL.Image")
    draw_module = pytest.importorskip("PIL.ImageDraw")
    font_module = pytest.importorskip("PIL.ImageFont")
    font_path = Path("/System/Library/Fonts/Supplemental/Arial.ttf")
    if not font_path.exists():
        pytest.skip("A deterministic large system font is unavailable.")
    image = image_module.new("RGB", (1400, 500), "white")
    draw = draw_module.Draw(image)
    font = font_module.truetype(str(font_path), 92)
    draw.text((70, 80), "BEDROOM", fill="black", font=font)
    draw.text((70, 235), "10 ft x 12 ft", fill="black", font=font)
    fixture = tmp_path / "floorplan-text.png"
    image.save(fixture)

    result = local_ocr(fixture.read_bytes())
    text = " ".join(str(line["text"]) for line in result["lines"]).upper()
    assert "BEDROOM" in text
    assert "10" in text and "12" in text
