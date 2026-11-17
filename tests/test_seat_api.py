import pytest

from server import main
from test_api_agent import api


def freeze(case_id, seat_id="livermore", mode="offline"):
    response = api("POST", "/analyze", json={"seat_id": seat_id, "case_id": case_id, "mode": mode})
    assert response.status_code == 200, response.text
    return response.json()


def test_catalog_exposes_seats_without_replay_inputs():
    body = api("GET", "/cases?source=examples").json()
    assert {seat["id"] for seat in body["seats"]} == {"livermore", "buffett", "baseline"}
    assert {case["seat_id"] for case in body["cases"]} == {"livermore", "buffett", "baseline"}
    assert all(set(case) == {"seat_id", "id", "label", "symbol", "name", "decision_date", "description"} for case in body["cases"])


@pytest.mark.parametrize("case_id,expected", [
    ("livermore-normal", "completed"), ("livermore-r2", "no_fill"),
    ("livermore-no-candidate", "hold"), ("livermore-z1", "completed"),
    ("livermore-r0", "no_fill"), ("livermore-limit-down", "completed"),
    ("livermore-reduced", "completed"),
])
def test_livermore_executes_its_own_frozen_strategy(case_id, expected):
    result = freeze(case_id)
    assert result["seat"]["id"] == "livermore" and result["mode"] == "offline"
    snapshot = result["evidence"]
    assert all(row["date"] <= snapshot["decision_date"] for row in snapshot["bars"])
    assert "auction_snapshot" not in snapshot and "events" not in result
    assert result["execution_policy"]["max_holding_sessions"] == 1
    assert not result["execution_policy"]["uses_price_thresholds"]
    assert result["plan"]["stop_loss"] is None and result["plan"]["take_profit"] is None
    response = api("POST", "/replay", json={"run_id": result["run_id"]})
    assert response.status_code == 200
    replayed = response.json()
    assert replayed["seat_id"] == "livermore"
    assert replayed["status"] == expected, replayed
    if expected == "completed":
        buy = next(row for row in replayed["events"] if row["type"] == "buy")
        sell = next(row for row in replayed["events"] if row["type"] == "sell")
        assert buy["date"] > snapshot["decision_date"] and sell["date"] > buy["date"]


def test_seat_and_case_are_bound_and_cannot_be_switched_after_freeze():
    assert api("POST", "/analyze", json={"seat_id": "baseline", "case_id": "livermore-normal"}).status_code == 404
    assert api("POST", "/analyze", json={"seat_id": "livermore", "case_id": "normal-fill"}).status_code == 404
    assert api("POST", "/analyze", json={"seat_id": "buffett", "case_id": "normal-fill"}).status_code == 404
    assert api("POST", "/analyze", json={"seat_id": "livermore", "case_id": "livermore-normal", "mode": "ai"}).status_code == 422
    base = freeze("normal-fill", "baseline")
    seat = freeze("livermore-normal")
    assert base["execution_policy"]["max_holding_sessions"] == 3
    assert base["plan"]["stop_loss"] is not None
    assert base["decision_hash"] != seat["decision_hash"]
    assert api("POST", "/replay", json={"run_id": seat["run_id"], "seat_id": "baseline"}).status_code == 422
    assert api("POST", "/replay", json={"run_id": base["run_id"]}).json()["seat_id"] == "baseline"


@pytest.mark.parametrize("seat_id,case_id,expected_dates", [
    ("baseline", "normal-fill", ["2026-10-26", "2026-10-27"]),
    ("baseline", "limit-up-no-fill", ["2026-10-26"]),
    ("baseline", "delayed-exit", ["2026-10-26", "2026-10-27", "2026-10-28", "2026-10-29"]),
    ("livermore", "livermore-z1", ["2026-10-26", "2026-10-27", "2026-10-28"]),
    ("livermore", "livermore-no-candidate", []),
])
def test_replay_bars_are_opt_in_and_bounded_to_observed_sessions(seat_id, case_id, expected_dates, monkeypatch):
    case = main.SEATS[seat_id].cases[case_id]
    for row in case["bars"]:
        if row["date"] > case["decision_date"]:
            monkeypatch.setitem(row, "fixture_private", "not a chart field")
    analysis = freeze(case_id, seat_id)
    assert "replay_bars" not in analysis
    snapshot = analysis["evidence"]
    assert all(row["date"] <= snapshot["decision_date"] for row in snapshot["bars"])

    request = {"run_id": analysis["run_id"]}
    default = api("POST", "/replay", json=request).json()
    assert "replay_bars" not in default
    assert api("POST", "/replay", json={**request, "include_bars": False}).json() == default
    response = api("POST", "/replay", json={**request, "include_bars": True})
    assert response.status_code == 200, response.text
    assert response.headers["Cache-Control"] == "no-store"
    enriched = response.json()
    bars = enriched.pop("replay_bars")
    assert enriched == default  # The display option cannot change fills or outcomes.
    assert [row["date"] for row in bars] == expected_dates
    assert all(row["date"] > snapshot["decision_date"] for row in bars)
    fields = {"date", "open", "high", "low", "close", "volume", "limit_up", "limit_down", "suspended"}
    assert all(set(row) == fields for row in bars)
    assert bars == [{field: row[field] for field in fields} for row in case["bars"] if row["date"] in expected_dates]


def test_replay_bars_remain_unavailable_when_blocked_or_run_is_unknown():
    analysis = freeze("invalid-data", "baseline")
    response = api("POST", "/replay", json={"run_id": analysis["run_id"], "include_bars": True})
    assert response.status_code == 200
    result = response.json()
    assert result["status"] == "blocked" and "replay_bars" not in result
    assert all(row["date"] <= analysis["evidence"]["decision_date"] for row in result["events"])
    unknown = api("POST", "/replay", json={"run_id": "0" * 32, "include_bars": True})
    assert unknown.status_code == 404 and "replay_bars" not in unknown.json()


def test_replay_bars_stay_bound_to_the_frozen_cutoff(monkeypatch):
    analysis = freeze("normal-fill", "baseline")
    case = main.SEATS["baseline"].cases["normal-fill"]
    monkeypatch.setitem(case, "decision_date", "2026-10-22")
    response = api("POST", "/replay", json={"run_id": analysis["run_id"], "include_bars": True})
    assert response.status_code == 409 and "replay_bars" not in response.json()
