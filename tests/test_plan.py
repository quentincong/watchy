"""Watchy 2.0 plan contract: strict parsing, validation, freshness (Phase 1)."""

from datetime import date

import pytest

from tests.fixtures_v2 import ADVICE_WITH_BLOCK, VALID_BLOCK, make_plan
from watchy.plan import (
    BLOCK_END,
    BLOCK_START,
    PlanFreshness,
    PlanStatus,
    direction,
    is_bullish_buy_plan,
    normalize_decision,
    parse_plan_block,
    plan_freshness,
    strip_plan_block,
    validate_plan,
)


def _block(**replace):
    text = VALID_BLOCK
    for label, value in replace.items():
        label = label.replace("_", "-")
        lines = []
        for line in text.splitlines():
            if line.lower().startswith(label.lower() + ":"):
                if value is None:
                    continue
                line = f"{line.split(':', 1)[0]}: {value}"
            lines.append(line)
        text = "\n".join(lines)
    return text


class TestParseBlock:
    def test_valid_block(self):
        parsed = parse_plan_block(ADVICE_WITH_BLOCK)
        assert parsed.found and parsed.errors == []
        assert parsed.fields["buy_zone_low"] == 121.0
        assert parsed.fields["take_profit_price"] is None
        assert parsed.fields["trim_condition"] == ""
        assert parsed.fields["dont_do"] == "do not chase above 125."

    def test_missing_block(self):
        parsed = parse_plan_block("Decision: BUY\nno plan here")
        assert not parsed.found
        assert parsed.errors == ["weekly plan block missing"]

    def test_unterminated_block(self):
        parsed = parse_plan_block(VALID_BLOCK.replace(BLOCK_END, ""))
        assert "weekly plan block not terminated" in parsed.errors

    @pytest.mark.parametrize("value", ["about 121", "121-123", "$121 or so", "-5", "0", "inf", "1e3"])
    def test_non_plain_numbers_rejected(self, value):
        parsed = parse_plan_block(_block(Buy_Zone_Low=value))
        assert any(e.startswith("buy_zone_low") for e in parsed.errors)
        assert parsed.fields["buy_zone_low"] is None

    @pytest.mark.parametrize("value,expected", [("$121.50", 121.5), ("1,215.25", 1215.25), ("121", 121.0)])
    def test_plain_number_forms(self, value, expected):
        parsed = parse_plan_block(_block(Buy_Zone_Low=value))
        assert parsed.fields["buy_zone_low"] == expected

    def test_missing_field(self):
        parsed = parse_plan_block(_block(Chase_Ceiling=None))
        assert "missing plan field: chase_ceiling" in parsed.errors

    def test_duplicate_and_unknown_fields(self):
        text = VALID_BLOCK.replace(BLOCK_END, "Thesis: again\nMood: great\n" + BLOCK_END)
        parsed = parse_plan_block(text)
        assert "duplicate plan field: thesis" in parsed.errors
        assert any(e.startswith("unknown plan field") for e in parsed.errors)

    def test_strip_block_keeps_detail(self):
        stripped = strip_plan_block(ADVICE_WITH_BLOCK)
        assert BLOCK_START not in stripped and "Chase-Ceiling" not in stripped
        assert "accumulation" in stripped


class TestValidate:
    def test_valid_plan_is_active(self):
        plan = make_plan()
        assert plan.status == PlanStatus.ACTIVE.value
        assert plan.validation_errors == []

    def test_parse_errors_invalidate(self):
        plan = validate_plan(make_plan(), parse_errors=["missing plan field: x"])
        assert plan.status == PlanStatus.INVALID.value

    @pytest.mark.parametrize("overrides,error", [
        ({"decision": "MAYBE"}, "invalid decision"),
        ({"urgency": "SOON"}, "invalid urgency"),
        ({"decision": "HOLD", "urgency": "HIGH"}, "HOLD must carry LOW"),
        ({"buy_zone_low": 124.0, "buy_zone_high": 123.0}, "buy_zone_low above"),
        ({"buy_zone_high": 126.0}, "buy_zone_high above chase_ceiling"),
        ({"invalidation_level": 121.5}, "invalidation_level not below buy zone"),
        ({"buy_zone_high": None}, "buy zone needs both"),
        ({"resistance_low": 136.0}, "resistance_low above"),
        ({"chase_ceiling": None}, "requires a buy zone and chase_ceiling"),
        ({"chase_ceiling": 500.0}, "implausibly far"),
        ({"input_price": None}, "input price"),
        ({"input_price_ts": ""}, "timestamp missing"),
        ({"thesis": ""}, "thesis required"),
        ({"dont_do": " "}, "dont_do required"),
        ({"invalidation_level": None, "invalidation_condition": ""}, "invalidation_level or"),
        ({"expires_after_session": "2026-09-20"}, "before valid_from"),
        ({"valid_from_session": "next monday"}, "malformed"),
        ({"decision": "TRIM", "urgency": "LOW", "trim_condition": ""}, "TRIM requires"),
        ({"buy_zone_low": float("nan")}, "positive finite"),
    ])
    def test_invalid(self, overrides, error):
        plan = make_plan(**overrides)
        assert plan.status == PlanStatus.INVALID.value
        assert any(error in e for e in plan.validation_errors), plan.validation_errors

    def test_nullable_levels_allowed_for_hold(self):
        plan = make_plan(
            decision="HOLD", urgency="LOW", buy_zone_low=None, buy_zone_high=None,
            chase_ceiling=None, resistance_low=None, resistance_high=None,
        )
        assert plan.status == PlanStatus.ACTIVE.value
        assert not is_bullish_buy_plan(plan)

    def test_zone_without_chase_is_warning_not_error(self):
        plan = make_plan(decision="WATCH", urgency="LOW", chase_ceiling=None)
        assert plan.status == PlanStatus.ACTIVE.value
        assert plan.validation_warnings
        assert not is_bullish_buy_plan(plan)


class TestFreshness:
    def test_active_within_week(self):
        assert plan_freshness(make_plan(), date(2026, 9, 23)) == PlanFreshness.ACTIVE

    def test_expired_after_last_session(self):
        # A failed next-week refresh must not silently extend the old plan.
        assert plan_freshness(make_plan(), date(2026, 9, 28)) == PlanFreshness.EXPIRED

    def test_not_yet_valid(self):
        assert plan_freshness(make_plan(), date(2026, 9, 18)) == PlanFreshness.NOT_YET_VALID

    def test_missing_and_invalid(self):
        assert plan_freshness(None, date(2026, 9, 23)) == PlanFreshness.MISSING
        bad = make_plan(decision="MAYBE")
        assert plan_freshness(bad, date(2026, 9, 23)) == PlanFreshness.INVALID

    def test_deactivated_is_invalid(self):
        plan = make_plan()
        plan.status = PlanStatus.DEACTIVATED.value
        assert plan_freshness(plan, date(2026, 9, 23)) == PlanFreshness.INVALID


class TestDecision:
    def test_hold_on_unheld_is_watch(self):
        assert normalize_decision("HOLD", held=False) == "WATCH"

    def test_hold_on_held_stays_hold(self):
        assert normalize_decision("HOLD", held=True) == "HOLD"
        assert normalize_decision("HOLD", held=None) == "HOLD"

    def test_garbage_is_empty(self):
        assert normalize_decision("STRONG BUY!!", held=True) == ""
        assert normalize_decision("", held=True) == ""

    def test_direction(self):
        assert direction("ADD") == "bullish"
        assert direction("TRIM") == "bearish"
        assert direction("WATCH") == "neutral"
        assert direction("") == ""
