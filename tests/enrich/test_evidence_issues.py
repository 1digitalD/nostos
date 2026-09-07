from nostos.enrich.issues import saved_evidence_issues
from nostos.model import Origin
from tests.web.test_query_status import _listing, _obs


def test_office_is_not_silently_accepted_as_second_bedroom() -> None:
    listing = _listing(description="1-Bedroom + Second Room/Home Office", beds=2)
    issues = saved_evidence_issues(listing)
    assert [issue.field for issue in issues] == ["beds"]
    assert issues[0].evidence == "1-Bedroom"


def test_conflicting_bedroom_claims_are_preserved() -> None:
    issues = saved_evidence_issues(_listing(description="1BR apartment. Two bedroom layout."))
    assert issues[0].field == "beds"
    assert "1BR" in issues[0].evidence
    assert "Two bedroom" in issues[0].evidence


def test_rooms_with_shared_facilities_are_not_unit_facts() -> None:
    issues = saved_evidence_issues(
        _listing(description="Private Rooms for Rent - $700 Each. Shared Kitchen & Bathroom.")
    )
    assert issues[0].field == "full_unit"


def test_consistent_bedrooms_plus_den_do_not_trigger_conflict() -> None:
    assert saved_evidence_issues(_listing(description="2 bedrooms + den")) == ()


def test_explicit_bedroom_correction_resolves_only_that_conflict() -> None:
    listing = _listing(description="1-Bedroom + Second Room/Home Office. Private Rooms for Rent.")
    listing = listing.model_copy(
        update={"beds": _obs(1.0).model_copy(update={"origin": Origin.USER})}
    )
    assert [issue.field for issue in saved_evidence_issues(listing)] == ["full_unit"]
