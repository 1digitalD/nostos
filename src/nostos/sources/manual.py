"""User supplied listing facts, preserved as replayable source records."""
from __future__ import annotations

from collections.abc import Iterator
from decimal import Decimal
from typing import Any

from nostos.context import SearchContext
from nostos.model import Absence, Area, Identity, Listing, Money, Observed, Origin, Place
from nostos.model.source_record import SourceRecord, SourceRecordRef
from nostos.sources.base import Capabilities, Liveness


class ManualSource:
    name = "manual"
    capabilities = Capabilities()

    def discover(self, ctx: SearchContext) -> Iterator[SourceRecord]:
        return iter(())

    def fetch_detail(self, rec: SourceRecord) -> SourceRecord:
        return rec

    def check_liveness(self, rec: SourceRecord) -> Liveness:
        return Liveness.OK

    def to_listing(self, rec: SourceRecord, ctx: SearchContext) -> Listing:
        data = rec.payload
        assert isinstance(data, dict)

        def observed(value: Any) -> Any:
            if value is None or value == "":
                return Absence.NOT_STATED
            return Observed(value=value, origin=Origin.USER, confidence=1,
                            evidence="Entered by user", observed_at=rec.fetched_at)

        rent = data.get("rent")
        size = data.get("area")
        return Listing(
            identity=Identity(listing_id=f"manual:{rec.source_id}", source="manual",
                              source_id=rec.source_id, url=rec.url, signature=f"manual:{rec.url}"),
            place=Place(raw_address=str(data.get("address") or "") or None),
            rent=observed(Money(amount=Decimal(str(rent)), currency=ctx.citypack.locale.currency,
                                period="month") if rent is not None else None),
            beds=observed(data.get("beds")), baths=observed(data.get("baths")),
            area=observed(Area(value=float(size), unit=ctx.citypack.locale.area_unit)
                          if size is not None else None),
            floor=observed(data.get("floor")), parking=observed(data.get("parking")),
            furnishing=observed(data.get("furnishing")), photos=[],
            attributes={key: observed(data.get(key)) for key in (
                "title", "description", "available_date", "lease_months", "total_monthly"
            )},
            raw_ref=SourceRecordRef(source=rec.source, source_id=rec.source_id,
                                   url=rec.url, content_hash=rec.content_hash,
                                   fetched_at=rec.fetched_at),
            schema_version=1,
        )
