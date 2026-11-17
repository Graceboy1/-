from copy import deepcopy

from server import main
from test_api_agent import api


def freeze(case_id, seat_id):
    response = api("POST", "/analyze", json={"case_id": case_id, "seat_id": seat_id, "mode": "offline"})
    assert response.status_code == 200, response.text
    return response.json()


def test_replay_rejects_changes_to_predecision_evidence(monkeypatch):
    run = freeze("normal-fill", "baseline")
    case = deepcopy(main.SEATS["baseline"].cases["normal-fill"])
    case["bars"][0]["volume"] += 1
    monkeypatch.setitem(main.SEATS["baseline"].cases, "normal-fill", case)
    response = api("POST", "/replay", json={"run_id": run["run_id"], "include_bars": True})
    assert response.status_code == 409 and "replay_bars" not in response.json()


def test_postdecision_changes_do_not_change_frozen_evidence(monkeypatch):
    run = freeze("normal-fill", "baseline")
    case = deepcopy(main.SEATS["baseline"].cases["normal-fill"])
    case["bars"][-1]["volume"] += 1
    monkeypatch.setitem(main.SEATS["baseline"].cases, "normal-fill", case)
    assert api("POST", "/replay", json={"run_id": run["run_id"]}).status_code == 200


def test_financial_assessment_is_frozen_not_silently_revised(monkeypatch):
    seat = main.SEATS["buffett"]
    case_id = next(iter(seat.cases))
    run = freeze(case_id, "buffett")
    case = deepcopy(seat.cases[case_id])
    case["business_assessment"]["note"] += "已修订"
    monkeypatch.setitem(seat.cases, case_id, case)
    response = api("POST", "/replay", json={"run_id": run["run_id"]})
    assert response.status_code == 409
