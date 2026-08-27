"""Tests for address_from_postingbody free-text extraction."""

from __future__ import annotations

import unittest

from nostos.normalize.address import (
    AddressNormalizationTokens,
    address_from_postingbody,
)

POSTING_TOKENS = AddressNormalizationTokens(
    city_province_postal_tokens=frozenset(),
    city_directional_prefixes=frozenset(),
)


def _address_from_postingbody(posting_body: str | None) -> str:
    return address_from_postingbody(posting_body, tokens=POSTING_TOKENS)


class AddressFromPostingBodyTests(unittest.TestCase):
    def test_explicit_address_label(self) -> None:
        body = """
2 Bedroom & 2 Full Bathroom Suite at Downtown Wall Centre

Address:1328 W Pender St, Vancouver, BC V6E 4T1

This newly re-painted suite features...
"""
        self.assertEqual(
            _address_from_postingbody(body),
            "1328 W Pender St, Vancouver, BC V6E 4T1",
        )

    def test_explicit_address_with_comma(self) -> None:
        body = "Address: 5xx-1050 Burrard St, Vancouver, V6Z 2S3"
        self.assertEqual(
            _address_from_postingbody(body),
            "5xx-1050 Burrard St, Vancouver, V6Z 2S3",
        )

    def test_explicit_address_with_period_after_label(self) -> None:
        body = "Address: 4189 Halifax Street, Burnaby"
        self.assertEqual(_address_from_postingbody(body), "4189 Halifax Street, Burnaby")

    def test_implicit_first_street_line(self) -> None:
        body = """
Welcome to this beautiful suite!

126 21st Street West
North Vancouver, BC

This suite has...
"""
        self.assertEqual(_address_from_postingbody(body), "126 21st Street West")

    def test_implicit_avenue(self) -> None:
        body = "Welcome!\n\n1234 4th Avenue\nVancouver\nDetails follow."
        self.assertEqual(_address_from_postingbody(body), "1234 4th Avenue")

    def test_implicit_boulevard(self) -> None:
        body = "2400 W Boulevard\nVancouver"
        self.assertEqual(_address_from_postingbody(body), "2400 W Boulevard")

    def test_no_address_returns_empty(self) -> None:
        body = "Beautiful suite with view, 2 bed 2 bath, available now."
        self.assertEqual(_address_from_postingbody(body), "")

    def test_address_label_but_bogus_value(self) -> None:
        body = "Address: please contact\nBeautiful suite"
        self.assertEqual(_address_from_postingbody(body), "")

    def test_empty_input(self) -> None:
        self.assertEqual(_address_from_postingbody(""), "")
        self.assertEqual(_address_from_postingbody(None), "")

    def test_label_present_with_low_value_skipped_then_implicit_used(self) -> None:
        body = "Address: tbd\n\n126 21st Street West"
        self.assertEqual(_address_from_postingbody(body), "126 21st Street West")

    def test_label_takes_priority_over_implicit(self) -> None:
        body = "Welcome to 999 Other Street!\n\nAddress: 1234 Main St"
        self.assertEqual(_address_from_postingbody(body), "1234 Main St")

    def test_skips_lines_without_leading_digit(self) -> None:
        body = "Welcome\n\nSuite overview\n\n126 Main Street\nMore"
        self.assertEqual(_address_from_postingbody(body), "126 Main Street")

    def test_skips_lines_without_street_type(self) -> None:
        body = "2 bedrooms\n1189 sq ft\n126 Main Street"
        self.assertEqual(_address_from_postingbody(body), "126 Main Street")

    def test_drive_avenue_lane(self) -> None:
        self.assertEqual(_address_from_postingbody("500 Smith Drive"), "500 Smith Drive")
        self.assertEqual(_address_from_postingbody("789 Pine Lane"), "789 Pine Lane")

    def test_case_insensitive_street_type(self) -> None:
        body = "ADDRESS: 1234 MAIN STREET"
        self.assertEqual(_address_from_postingbody(body), "1234 MAIN STREET")
