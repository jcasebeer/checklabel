"""Unit tests for the decision core — every pass/fail rule lives here."""
from app import config
from app.verifier import (
    _header_all_caps,
    _parse_abv,
    best_brand_check,
    check_abv,
    check_brand,
    check_warning,
    decide,
)

WARNING = config.GOVERNMENT_WARNING


def good_warning(**overrides) -> dict:
    gw = {"present": True, "text_verbatim": WARNING,
          "header_all_caps": True, "appears_bold": True}
    gw.update(overrides)
    return gw


# --- Brand -------------------------------------------------------------------
class TestCheckBrand:
    def test_exact_match_passes(self):
        assert check_brand("Stone's Throw", "Stone's Throw").status == "pass"

    def test_case_and_punctuation_normalized(self):
        # The Dave Morrison case from the brief: same brand, different casing.
        assert check_brand("Stone's Throw", "STONE'S THROW").status == "pass"
        assert check_brand("Stone's Throw", "Stones Throw").status == "pass"

    def test_near_miss_is_flagged_for_review(self):
        c = check_brand("Stone's Throw", "Stone's Thraw")
        assert c.status == "warn"

    def test_brand_within_larger_lockup_is_flagged_for_review(self):
        # Real case from the TTB registry: COLA brand "TX", label prints
        # "TX Experimental Series Vino De Naranja Barrel".
        c = check_brand("TX", "TX Experimental Series Vino De Naranja Barrel")
        assert c.status == "warn"
        # ...and the reverse direction (label shorter than application).
        assert check_brand("Stone's Throw Cellars", "STONE'S THROW").status == "warn"
        # Substring without a word boundary is NOT containment.
        assert check_brand("TX", "NEXTXEN VODKA").status == "fail"

    def test_different_brand_fails(self):
        assert check_brand("Stone's Throw", "Harbor Light").status == "fail"

    def test_missing_brand_fails(self):
        c = check_brand("Stone's Throw", None)
        assert c.status == "fail"
        assert c.found is None


class TestBestBrandCheck:
    """TTB 'brand name' may be the producer OR the product name — the check
    must consider every name printed on the label."""

    def test_registered_brand_among_candidates_passes(self):
        # Real registry case: brand NEW BELGIUM, big label text FAT TIRE.
        extracted = {"brand_name": "FAT TIRE",
                     "name_candidates": ["FAT TIRE", "NEW BELGIUM", "Amber Ale"]}
        assert best_brand_check("New Belgium", extracted).status == "pass"

    def test_primary_brand_still_wins_ties(self):
        extracted = {"brand_name": "Stone's Throw", "name_candidates": ["Stone's Throw"]}
        c = best_brand_check("Stone's Throw", extracted)
        assert c.status == "pass"
        assert c.found == "Stone's Throw"

    def test_no_candidate_matches_fails(self):
        extracted = {"brand_name": "FAT TIRE", "name_candidates": ["Amber Ale"]}
        assert best_brand_check("Harbor Light", extracted).status == "fail"

    def test_missing_everything_fails(self):
        assert best_brand_check("X", {"brand_name": None}).status == "fail"
        assert best_brand_check("X", {}).status == "fail"

    def test_containment_via_candidate_warns(self):
        extracted = {"brand_name": "Queen of the Coast IPA",
                     "name_candidates": ["Pizza Port Brewing Co. Carlsbad"]}
        assert best_brand_check("Pizza Port Brewing Co.", extracted).status == "warn"


# --- ABV -----------------------------------------------------------------------
class TestCheckAbv:
    def test_exact_match_passes(self):
        assert check_abv(13.5, 13.5).status == "pass"

    def test_mismatch_fails(self):
        assert check_abv(13.5, 14.0).status == "fail"

    def test_tolerance_is_configurable(self, monkeypatch):
        monkeypatch.setattr(config, "ABV_TOLERANCE", 0.5)
        assert check_abv(13.5, 13.9).status == "pass"
        assert check_abv(13.5, 14.5).status == "fail"

    def test_not_on_label_fails(self):
        assert check_abv(13.5, None).status == "fail"

    def test_no_expected_value_is_skipped_not_blocking(self):
        c = check_abv(None, 13.5)
        assert c.status == "skipped"

    def test_zero_abv_is_a_real_reading(self):
        assert check_abv(0.0, 0.0).status == "pass"


class TestParseAbv:
    def test_variants(self):
        assert _parse_abv(None) is None
        assert _parse_abv(13.5) == 13.5
        assert _parse_abv(13) == 13.0
        assert _parse_abv("13.5% ALC/VOL") == 13.5
        assert _parse_abv("ALC. 45% BY VOL (90 PROOF)") == 45.0
        assert _parse_abv("no numbers here") is None
        assert _parse_abv(0) == 0.0


# --- Government warning --------------------------------------------------------
class TestCheckWarning:
    def test_perfect_warning_passes(self):
        c = check_warning(good_warning())
        assert c.status == "pass"

    def test_missing_warning_fails(self):
        assert check_warning({"present": False}).status == "fail"
        assert check_warning({}).status == "fail"

    def test_wording_mismatch_fails(self):
        gw = good_warning(text_verbatim=WARNING.replace("birth defects", "issues"))
        c = check_warning(gw)
        assert c.status == "fail"
        assert "wording" in c.detail

    def test_title_case_header_fails_even_if_model_says_caps(self):
        # The Jenny Park case: "Government Warning" in title case must be
        # rejected. The code decides from the transcription, so a wrong
        # header_all_caps boolean from the model cannot mask it.
        text = WARNING.replace("GOVERNMENT WARNING:", "Government Warning:")
        c = check_warning(good_warning(text_verbatim=text, header_all_caps=True))
        assert c.status == "fail"
        assert "capitals" in c.detail

    def test_model_caps_flag_used_when_header_not_in_text(self):
        # Header can't be located → fall back to the model's judgment.
        gw = good_warning(text_verbatim="(1) According to the Surgeon General...",
                          header_all_caps=False)
        c = check_warning(gw)
        assert "capitals" in c.detail

    def test_unconfirmed_bold_needs_review(self):
        c = check_warning(good_warning(appears_bold=None))
        assert c.status == "warn"

    def test_not_bold_fails(self):
        c = check_warning(good_warning(appears_bold=False))
        assert c.status == "fail"
        assert "bold" in c.detail

    def test_whitespace_normalization(self):
        # Line breaks on the physical label must not fail the wording check.
        wrapped = WARNING.replace(" impairs ", "\nimpairs ").replace(" (2) ", "\n(2) ")
        assert check_warning(good_warning(text_verbatim=wrapped)).status == "pass"

    def test_tight_kerning_spacing_is_not_a_wording_mismatch(self):
        # Real TTB-approved label (ALVIDES): prints "GOVERNMENT WARNING:(1)"
        # with no space after the colon. Spacing is typography, not wording.
        tight = WARNING.replace("WARNING: (1)", "WARNING:(1)").replace(". (2) ", ".(2) ")
        assert check_warning(good_warning(text_verbatim=tight)).status == "pass"

    def test_missing_punctuation_still_fails(self):
        no_colon = WARNING.replace("GOVERNMENT WARNING:", "GOVERNMENT WARNING", 1)
        assert check_warning(good_warning(text_verbatim=no_colon)).status == "fail"


class TestHeaderAllCaps:
    def test_detection(self):
        assert _header_all_caps("GOVERNMENT WARNING: (1) ...") is True
        assert _header_all_caps("Government Warning: (1) ...") is False
        assert _header_all_caps("GOVERNMENT  WARNING : (1) ...") is True  # odd spacing
        assert _header_all_caps("(1) According to...") is None
        assert _header_all_caps("") is None


# --- Overall decision ------------------------------------------------------------
class TestDecide:
    def extracted(self, **overrides) -> dict:
        d = {"brand_name": "Stone's Throw", "alcohol_content_text": "13.5% ALC/VOL",
             "abv_percent": 13.5, "government_warning": good_warning()}
        d.update(overrides)
        return d

    def test_all_good_passes(self):
        assert decide(self.extracted(), "Stone's Throw", 13.5).overall == "pass"

    def test_skipped_abv_does_not_block_approval(self):
        # Application listed no ABV → still "Approved", not "Needs review".
        assert decide(self.extracted(), "Stone's Throw", None).overall == "pass"

    def test_any_fail_fails_overall(self):
        assert decide(self.extracted(brand_name="Other"), "Stone's Throw", 13.5).overall == "fail"

    def test_warn_beats_pass(self):
        r = decide(self.extracted(government_warning=good_warning(appears_bold=None)),
                   "Stone's Throw", 13.5)
        assert r.overall == "warn"

    def test_zero_abv_percent_not_shadowed_by_text(self):
        # abv_percent of 0 is falsy but real — must not fall through to
        # parsing the alcohol_content_text.
        r = decide(self.extracted(abv_percent=0.0, alcohol_content_text="45% before dealcoholization"),
                   "Stone's Throw", 0.0)
        abv_check = next(c for c in r.checks if c.field == "Alcohol content")
        assert abv_check.status == "pass"
