from __future__ import annotations

from typing import Annotated, Any, ClassVar

from pydantic import BaseModel, ConfigDict, StringConstraints

NonEmptyStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]

ListingId = NonEmptyStr
Signature = NonEmptyStr


class Identity(BaseModel):
    model_config = ConfigDict(frozen=True)

    listing_id: ListingId
    source: NonEmptyStr
    source_id: NonEmptyStr
    url: NonEmptyStr
    signature: Signature

    _DIRECTIONAL_ALIASES: ClassVar[dict[str, str]] = {
        "n": "north",
        "s": "south",
        "e": "east",
        "w": "west",
        "ne": "northeast",
        "nw": "northwest",
        "se": "southeast",
        "sw": "southwest",
    }
    _DROP_TOKENS: ClassVar[set[str]] = {
        "vancouver",
        "bc",
        "ca",
        "canada",
    }
    _STREET_TYPE_TOKENS: ClassVar[set[str]] = {
        "st",
        "street",
        "ave",
        "avenue",
        "rd",
        "road",
        "dr",
        "drive",
        "blvd",
        "boulevard",
        "way",
        "lane",
        "ln",
        "court",
        "ct",
    }
    _TOKEN_PUNCTUATION: ClassVar[str] = ",.;:/\\|()[]{}!?#+&*'\""
    _TOKEN_TRANSLATION: ClassVar[dict[int, str]] = str.maketrans(
        {char: " " for char in _TOKEN_PUNCTUATION}
    )
    _SIG_MAX_TOKENS: ClassVar[int] = 6
    _PRICE_BUCKET_SIZE: ClassVar[int] = 25

    @classmethod
    def compute_signature(
        cls,
        *,
        title: str,
        price: int | str | float | None,
        address: str | None = None,
    ) -> Signature:
        token_source = address.strip() if address and address.strip() else title
        tokens = cls._signature_tokens(token_source)
        prefix = " ".join(tokens[: cls._SIG_MAX_TOKENS]) if tokens else "listing"
        bucket = cls._price_bucket(cls._coerce_int_for_signature(price))
        return f"{prefix}|{bucket}"

    @classmethod
    def _signature_tokens(cls, text: str) -> list[str]:
        normalized = text.lower().translate(cls._TOKEN_TRANSLATION)
        tokens: list[str] = []
        for token in normalized.split():
            expanded = cls._DIRECTIONAL_ALIASES.get(token, token)
            compact = expanded.replace("-", "")
            if expanded in cls._DROP_TOKENS or expanded in cls._STREET_TYPE_TOKENS:
                continue
            if cls._is_postal_token(compact):
                continue
            tokens.append(expanded)
        return tokens

    @classmethod
    def _price_bucket(cls, price: int) -> int:
        return int(round(price / cls._PRICE_BUCKET_SIZE) * cls._PRICE_BUCKET_SIZE)

    @staticmethod
    def _coerce_int_for_signature(value: Any) -> int:
        if value is None or value == "":
            return 0
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _is_postal_token(token: str) -> bool:
        if len(token) == 6:
            return (
                token[0].isalpha()
                and token[1].isdigit()
                and token[2].isalpha()
                and token[3].isdigit()
                and token[4].isalpha()
                and token[5].isdigit()
            )
        if len(token) == 3:
            return (
                token[0].isalpha()
                and token[1].isdigit()
                and token[2].isalpha()
            ) or (
                token[0].isdigit()
                and token[1].isalpha()
                and token[2].isdigit()
            )
        return False
