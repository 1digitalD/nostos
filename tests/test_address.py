"""Tests for nostos.normalize.address primitives."""

from __future__ import annotations

import unittest

from nostos.normalize.address import (
    AddressNormalizationTokens,
    addresses_match,
    match_key,
    normalize_address,
    sig_token,
)

BC_ADDRESS_TOKENS = AddressNormalizationTokens(
    city_province_postal_tokens=frozenset(
        {
            "bc",
            "b.c",
            "ab",
            "alberta",
            "on",
            "ontario",
            "qc",
            "quebec",
            "mb",
            "manitoba",
            "sk",
            "saskatchewan",
            "vancouver",
            "burnaby",
            "richmond",
            "surrey",
            "langley",
            "coquitlam",
            "newwestminster",
            "newwest",
            "delta",
            "maple ridge",
            "port coquitlam",
            "poco",
            "white rock",
            "port moody",
            "kingsway",
            "north vancouver",
            "west vancouver",
            "seattle",
            "bellevue",
            "tacoma",
            "portland",
            "everett",
            "victoria",
            "kelowna",
            "kamloops",
            "nanaimo",
            "abbotsford",
            "chilliwack",
            "hope",
            "squamish",
            "whistler",
        }
    ),
    city_directional_prefixes=frozenset({"north", "west"}),
)


def _sig_token(address: str | None) -> str:
    return sig_token(address, tokens=BC_ADDRESS_TOKENS)


def _match_key(address: str | None) -> str:
    return match_key(address, tokens=BC_ADDRESS_TOKENS)


def _addresses_match(a: str | None, b: str | None) -> bool:
    return addresses_match(a, b, tokens=BC_ADDRESS_TOKENS)


class NormalizeAddressTests(unittest.TestCase):
    def test_full_address(self) -> None:
        address = normalize_address("1188 Richards Street", "Vancouver", "BC", "V6A 1Y7")
        self.assertEqual(address, "1188 Richards Street, Vancouver, BC, V6A 1Y7")

    def test_street_only(self) -> None:
        self.assertEqual(normalize_address("1328 West Pender Street"), "1328 West Pender Street")

    def test_city_province_only(self) -> None:
        self.assertEqual(normalize_address(None, "Vancouver", "BC"), "Vancouver, BC")

    def test_all_empty_returns_empty_string_not_none(self) -> None:
        self.assertEqual(normalize_address("", None, "  ", ""), "")

    def test_drops_junk_values(self) -> None:
        address = normalize_address("Foo", "n/a", "BC", "unknown")
        self.assertEqual(address, "Foo, BC")

    def test_collapses_whitespace(self) -> None:
        address = normalize_address("1328  West   Pender  Street")
        self.assertEqual(address, "1328 West Pender Street")

    def test_strips_trailing_punctuation(self) -> None:
        address = normalize_address("1328 West Pender Street.")
        self.assertEqual(address, "1328 West Pender Street")


class SigTokenTests(unittest.TestCase):
    """Tests for sig_token() — aggressive street-type stripping for cross-source dedupe."""

    def test_strips_street_type_in_middle(self) -> None:
        self.assertEqual(_sig_token("1310 Richards Street Vancouver BC"), "1310 richards")

    def test_collapses_st_vs_street(self) -> None:
        self.assertEqual(
            _sig_token("1310 Richards Street Vancouver BC"),
            _sig_token("1310 Richards St Vancouver BC"),
        )

    def test_collapses_with_or_without_city_province(self) -> None:
        self.assertEqual(
            _sig_token("1328 W Pender St, Vancouver, BC V6E 4T1"),
            _sig_token("1328 West Pender Street"),
        )

    def test_collapses_with_partial_postal(self) -> None:
        self.assertEqual(
            _sig_token("1328 W Pender St V6E 4T1"),
            _sig_token("1328 West Pender Street"),
        )

    def test_strips_us_zip(self) -> None:
        self.assertEqual(_sig_token("500 Smith Drive, Seattle 98101"), "500 smith")

    def test_strips_canadian_postal(self) -> None:
        self.assertEqual(_sig_token("500 Smith Drive V6E 4T1"), "500 smith")

    def test_preserves_directional(self) -> None:
        self.assertEqual(_sig_token("315 1st Street East, North Vancouver"), "315 1st east")

    def test_preserves_directional_east_vs_west(self) -> None:
        self.assertNotEqual(
            _sig_token("315 1st Street East, North Vancouver"),
            _sig_token("315 1st Street West, North Vancouver"),
        )

    def test_strips_avenue(self) -> None:
        self.assertEqual(_sig_token("1234 4th Avenue Vancouver BC"), "1234 4th")

    def test_strips_drive(self) -> None:
        self.assertEqual(_sig_token("500 Smith Drive, Vancouver"), "500 smith")

    def test_strips_boulevard(self) -> None:
        self.assertEqual(_sig_token("2400 W Boulevard, Vancouver"), "2400 west")

    def test_expands_w_to_west(self) -> None:
        self.assertEqual(_sig_token("1328 W Pender St"), _sig_token("1328 West Pender St"))

    def test_expands_e_to_east(self) -> None:
        self.assertEqual(_sig_token("315 1st Ave E"), _sig_token("315 1st Ave East"))

    def test_does_not_expand_letter_in_word(self) -> None:
        self.assertEqual(_sig_token("Wall Centre, Vancouver"), "wall centre")

    def test_strips_burnaby(self) -> None:
        self.assertEqual(_sig_token("4189 Halifax Street, Burnaby"), "4189 halifax")

    def test_no_street_type(self) -> None:
        self.assertEqual(_sig_token("Vancouver BC"), "")

    def test_empty(self) -> None:
        self.assertEqual(_sig_token(""), "")
        self.assertEqual(_sig_token(None), "")


class MatchKeyTests(unittest.TestCase):
    def test_known_cases(self) -> None:
        self.assertEqual(_match_key("1328 West Pender Street"), "1328 west pender")
        self.assertEqual(_match_key("1328 W Pender St"), "1328 west pender")
        self.assertEqual(_match_key("1328 W Pender St"), _match_key("1328 West Pender Street"))
        self.assertEqual(_match_key("315 1st Street East"), "315 1st street east")
        self.assertEqual(_match_key("Vancouver, BC"), "vancouver bc")

    def test_strips_trailing_street_type(self) -> None:
        self.assertEqual(_match_key("1188 Richards Street"), _match_key("1188 Richards St"))

    def test_keeps_directionals(self) -> None:
        self.assertNotEqual(
            _match_key("1328 W Pender Street"),
            _match_key("1328 E Pender Street"),
        )

    def test_handles_special_chars(self) -> None:
        self.assertEqual(
            _match_key("1011-66 Cordova Street (corner of X & Y)"),
            "1011-66 cordova street corner of x y",
        )
        self.assertNotIn("&", _match_key("X & Y"))
        self.assertNotIn("'", _match_key("King's Way"))

    def test_empty_input(self) -> None:
        self.assertEqual(_match_key(""), "")
        self.assertEqual(_match_key(None), "")


class AddressesMatchTests(unittest.TestCase):
    """Real-world match examples from cron runs."""

    def test_abbrev_vs_full_street_type(self) -> None:
        self.assertTrue(
            _addresses_match(
                "1188 Richards Street",
                "1188 Richards St, Vancouver, BC",
            )
        )

    def test_directional_preserved_west(self) -> None:
        self.assertTrue(
            _addresses_match(
                "1328 W Pender Street",
                "1328 W Pender St, Vancouver, BC V6E 4T1",
            )
        )

    def test_w_abbrev_matches_west_full(self) -> None:
        self.assertTrue(_addresses_match("1328 W Pender St", "1328 West Pender Street"))
        self.assertEqual(_sig_token("1328 W Pender St"), _sig_token("1328 West Pender Street"))

    def test_different_street_numbers_no_match(self) -> None:
        self.assertFalse(_addresses_match("1328 W Pender Street", "1011 W Pender Street"))

    def test_east_vs_west_no_match(self) -> None:
        self.assertFalse(_addresses_match("315 1st Street East", "315 1st Street West"))

    def test_partial_overlap_halifax(self) -> None:
        self.assertTrue(_addresses_match("4189 Halifax St", "4189 Halifax Street, Burnaby"))

    def test_city_only_loose(self) -> None:
        self.assertTrue(_addresses_match("Vancouver, BC", "Vancouver, BC"))

    def test_canonical_more_specific_than_card(self) -> None:
        self.assertTrue(
            _addresses_match(
                "1328 West Pender Street, Vancouver, BC V6E 4T1",
                "Vancouver, BC",
            )
        )
