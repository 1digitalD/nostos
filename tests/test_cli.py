from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

import nostos.cli as cli_module
from nostos.config.profile import Profile, load_profile
from nostos.context import SearchContext, load_search_context
from nostos.model import Absence, Identity, Listing, Money, Observed, Origin, Place, SourceRecord
from nostos.sources.base import Capabilities, Liveness


@pytest.mark.parametrize(
    ("argv",),
    [
        (["--help"],),
        (["init", "--help"],),
        (["watch", "--help"],),
        (["rank", "--help"],),
        (["list", "--help"],),
        (["explain", "--help"],),
    ],
)
def test_help_commands_exit_zero(argv: list[str]) -> None:
    runner = CliRunner()
    result = runner.invoke(cli_module.app, argv)
    assert result.exit_code == 0
    assert "Examples:" in result.stdout


def test_init_non_interactive_writes_profile_and_refuses_to_clobber(tmp_path: Path) -> None:
    citypack_path = _write_citypack(tmp_path / "citypack.yaml", source_name="stub")
    profile_path = tmp_path / "profile.yaml"
    runner = CliRunner()

    result = runner.invoke(
        cli_module.app,
        [
            "init",
            "--non-interactive",
            "--citypack",
            str(citypack_path),
            "--profile",
            str(profile_path),
            "--city",
            "vancouver",
            "--max-rent",
            "3400",
            "--beds",
            "2",
            "--laundry",
            "nice-to-have",
            "--pets",
            "avoid",
            "--source",
            "stub",
        ],
    )
    assert result.exit_code == 0, result.output
    assert profile_path.exists()

    profile = load_profile(profile_path)
    assert isinstance(profile, Profile)
    assert profile.city == "vancouver"
    assert profile.hard.rent is not None
    assert profile.hard.rent.max == pytest.approx(3400)
    laundry_weight = profile.weights["laundry.in_suite"]
    pets_weight = profile.weights["pets.allowed"]
    assert isinstance(laundry_weight, float)
    assert isinstance(pets_weight, float)
    assert laundry_weight > 0
    assert pets_weight < 0
    load_search_context(citypack_path=citypack_path, profile_path=profile_path)

    original = profile_path.read_text(encoding="utf-8")
    second_result = runner.invoke(
        cli_module.app,
        [
            "init",
            "--non-interactive",
            "--citypack",
            str(citypack_path),
            "--profile",
            str(profile_path),
            "--city",
            "vancouver",
            "--max-rent",
            "3000",
            "--beds",
            "1",
            "--laundry",
            "deal-breaker",
            "--pets",
            "prefer",
            "--source",
            "stub",
        ],
    )
    assert second_result.exit_code != 0
    assert "--force" in second_result.output
    assert profile_path.read_text(encoding="utf-8") == original


def test_init_missing_required_values_fails_fast_without_prompt(tmp_path: Path) -> None:
    citypack_path = _write_citypack(tmp_path / "citypack.yaml", source_name="stub")
    profile_path = tmp_path / "profile.yaml"
    runner = CliRunner()

    result = runner.invoke(
        cli_module.app,
        [
            "init",
            "--non-interactive",
            "--citypack",
            str(citypack_path),
            "--profile",
            str(profile_path),
            "--city",
            "vancouver",
        ],
    )
    assert result.exit_code != 0
    joined = result.output
    assert "Missing required values" in joined
    assert "nostos init --non-interactive" in joined


def test_watch_list_and_explain_use_store_without_network(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_stub_source(monkeypatch)

    citypack_path = _write_citypack(tmp_path / "citypack.yaml", source_name="stub")
    profile_path = _write_profile(
        tmp_path / "profile-positive.yaml",
        laundry_weight=10,
        source_name="stub",
    )
    db_path = tmp_path / "nostos.db"
    runner = CliRunner()

    watch_result = runner.invoke(
        cli_module.app,
        [
            "watch",
            "--citypack",
            str(citypack_path),
            "--profile",
            str(profile_path),
            "--db",
            str(db_path),
            "--source",
            "stub",
            "--yes",
        ],
    )
    assert watch_result.exit_code == 0, watch_result.output
    assert "run_id=" in watch_result.stdout

    with sqlite3.connect(db_path) as conn:
        run_count = conn.execute("SELECT COUNT(*) FROM run").fetchone()
        score_count = conn.execute("SELECT COUNT(*) FROM score").fetchone()
    assert run_count is not None
    assert score_count is not None
    assert int(run_count[0]) == 1
    assert int(score_count[0]) == 2

    list_result = runner.invoke(
        cli_module.app,
        [
            "list",
            "--profile",
            str(profile_path),
            "--db",
            str(db_path),
            "--limit",
            "2",
        ],
    )
    assert list_result.exit_code == 0, list_result.output
    listed_ids = _parse_listing_ids(list_result.stdout)
    assert listed_ids[:2] == ["stub:with-laundry", "stub:no-laundry"]

    explain_result = runner.invoke(
        cli_module.app,
        [
            "explain",
            "stub:with-laundry",
            "--profile",
            str(profile_path),
            "--db",
            str(db_path),
        ],
    )
    assert explain_result.exit_code == 0, explain_result.output
    assert "Overall score:" in explain_result.stdout
    assert "In-suite laundry" in explain_result.stdout


def test_profile_flip_changes_rank_order_for_same_fixture_set(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R1 ship-gate thesis: profile rubric changes ranking order."""
    _install_stub_source(monkeypatch)
    citypack_path = _write_citypack(tmp_path / "citypack.yaml", source_name="stub")
    profile_positive = _write_profile(
        tmp_path / "profile-positive.yaml",
        laundry_weight=10,
        source_name="stub",
    )
    profile_negative = _write_profile(
        tmp_path / "profile-negative.yaml",
        laundry_weight=-10,
        source_name="stub",
    )
    db_path = tmp_path / "nostos.db"
    runner = CliRunner()

    watch_result = runner.invoke(
        cli_module.app,
        [
            "watch",
            "--citypack",
            str(citypack_path),
            "--profile",
            str(profile_positive),
            "--db",
            str(db_path),
            "--source",
            "stub",
            "--yes",
        ],
    )
    assert watch_result.exit_code == 0, watch_result.output

    rank_positive = runner.invoke(
        cli_module.app,
        [
            "rank",
            "--citypack",
            str(citypack_path),
            "--profile",
            str(profile_positive),
            "--db",
            str(db_path),
        ],
    )
    assert rank_positive.exit_code == 0, rank_positive.output
    positive_list = runner.invoke(
        cli_module.app,
        ["list", "--profile", str(profile_positive), "--db", str(db_path), "--limit", "2"],
    )
    assert positive_list.exit_code == 0, positive_list.output
    positive_order = _parse_listing_ids(positive_list.stdout)

    rank_negative = runner.invoke(
        cli_module.app,
        [
            "rank",
            "--citypack",
            str(citypack_path),
            "--profile",
            str(profile_negative),
            "--db",
            str(db_path),
        ],
    )
    assert rank_negative.exit_code == 0, rank_negative.output
    negative_list = runner.invoke(
        cli_module.app,
        ["list", "--profile", str(profile_negative), "--db", str(db_path), "--limit", "2"],
    )
    assert negative_list.exit_code == 0, negative_list.output
    negative_order = _parse_listing_ids(negative_list.stdout)

    assert positive_order[:2] == ["stub:with-laundry", "stub:no-laundry"]
    assert negative_order[:2] == ["stub:no-laundry", "stub:with-laundry"]
    assert positive_order[:2] != negative_order[:2]


def _install_stub_source(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        cli_module,
        "SOURCE_FACTORIES",
        {
            "stub": lambda: StubSource(
                records=(
                    _record("with-laundry", in_suite_laundry=True),
                    _record("no-laundry", in_suite_laundry=False),
                )
            ),
        },
    )


def _record(source_id: str, *, in_suite_laundry: bool) -> SourceRecord:
    now = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
    title = "Bright suite with in-unit laundry" if in_suite_laundry else "Quiet suite"
    description = (
        "Includes in-suite laundry and private washer dryer."
        if in_suite_laundry
        else "Shared laundry room in building."
    )
    return SourceRecord(
        source="stub",
        source_id=source_id,
        url=f"https://example.test/stub/{source_id}",
        content_hash=f"hash-{source_id}",
        fetched_at=now,
        payload={
            "title": title,
            "description": description,
            "address": f"{source_id} Example Street",
            "rent": 2500,
            "beds": 2.0,
            "baths": 1.0,
            "in_suite_laundry": in_suite_laundry,
        },
    )


class StubSource:
    name = "stub"
    capabilities = Capabilities(
        requires_credentials=False,
        supports_detail_fetch=False,
        requires_browser=False,
        rate_limit_per_minute=60.0,
    )

    def __init__(self, *, records: tuple[SourceRecord, ...]) -> None:
        self._records = records

    def discover(self, _: SearchContext) -> Iterator[SourceRecord]:
        yield from self._records

    def fetch_detail(self, rec: SourceRecord) -> SourceRecord:
        return rec

    def check_liveness(self, _: SourceRecord) -> Liveness:
        return Liveness.OK

    def to_listing(self, rec: SourceRecord, _: SearchContext) -> Listing:
        payload = _mapping(rec.payload)
        observed_at = rec.fetched_at
        attributes: dict[str, Any] = {
            "title": _text_observed(str(payload["title"]), observed_at, evidence="stub title"),
            "description": _text_observed(
                str(payload["description"]),
                observed_at,
                evidence="stub description",
            ),
            "in_suite_laundry": Observed[bool](
                value=bool(payload["in_suite_laundry"]),
                origin=Origin.SOURCE_FIELD,
                confidence=1.0,
                evidence="stub laundry flag",
                observed_at=observed_at,
            ),
        }
        return Listing(
            identity=Identity(
                listing_id=f"stub:{rec.source_id}",
                source="stub",
                source_id=rec.source_id,
                url=rec.url,
                signature=f"sig:stub:{rec.source_id}",
            ),
            place=Place(
                raw_address=str(payload["address"]),
                structured=None,
                point=None,
                area_key=None,
            ),
            rent=Observed[Money](
                value=Money(amount=Decimal(str(payload["rent"])), currency="CAD", period="month"),
                origin=Origin.SOURCE_FIELD,
                confidence=1.0,
                evidence="stub rent",
                observed_at=observed_at,
            ),
            beds=Observed[float](
                value=_coerce_float(payload["beds"]),
                origin=Origin.SOURCE_FIELD,
                confidence=1.0,
                evidence="stub beds",
                observed_at=observed_at,
            ),
            baths=Observed[float](
                value=_coerce_float(payload["baths"]),
                origin=Origin.SOURCE_FIELD,
                confidence=1.0,
                evidence="stub baths",
                observed_at=observed_at,
            ),
            area=Absence.NOT_STATED,
            floor=Absence.NOT_STATED,
            parking=Absence.NOT_STATED,
            furnishing=Absence.NOT_STATED,
            photos=[],
            attributes=attributes,
            raw_ref=rec.to_ref(),
            schema_version=1,
        )


def _text_observed(value: str, observed_at: datetime, *, evidence: str) -> Observed[str]:
    return Observed[str](
        value=value,
        origin=Origin.SOURCE_FIELD,
        confidence=1.0,
        evidence=evidence,
        observed_at=observed_at,
    )


def _mapping(value: object) -> dict[str, object]:
    if isinstance(value, dict):
        return value
    raise AssertionError("expected mapping payload")


def _coerce_float(value: object) -> float:
    if isinstance(value, bool):
        raise AssertionError("expected numeric value, got bool")
    if isinstance(value, int | float):
        return float(value)
    raise AssertionError(f"expected numeric value, got {type(value)!r}")


def _write_citypack(path: Path, *, source_name: str) -> Path:
    payload = {
        "name": "vancouver",
        "locale": {
            "language": "en-CA",
            "timezone": "America/Vancouver",
            "currency": "CAD",
            "area_unit": "sqft",
        },
        "areas": [
            {
                "key": "kits_beach",
                "label": "Kitsilano",
                "keywords": ["kitsilano"],
                "bbox": [49.262, -123.190, 49.278, -123.145],
            }
        ],
        "sources": {
            source_name: {"enabled": True, "load_bearing": False},
        },
        "address": {
            "directional": {"w": "west"},
            "strip_tokens": ["vancouver"],
            "region_tokens": ["bc"],
        },
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


def _write_profile(path: Path, *, laundry_weight: float, source_name: str) -> Path:
    payload = {
        "city": "vancouver",
        "hard": {
            "rent": {"max": 3200, "currency": "CAD"},
            "beds": {"eq": 2},
            "exclude": [],
        },
        "weights": {"laundry.in_suite": laundry_weight},
        "proximity": [],
        "avoid_areas": [],
        "confidence": {"unverified_penalty": 0},
        "sources": {source_name: "on"},
        "notify": [],
        "schedule": "0 */6 * * *",
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


def _parse_listing_ids(output: str) -> list[str]:
    listing_ids: list[str] = []
    for line in output.splitlines():
        if line.startswith("listing_id="):
            listing_ids.append(line.split("=", maxsplit=1)[1].split("\t", maxsplit=1)[0])
    return listing_ids
