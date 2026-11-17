from pathlib import Path

import pytest

from receiptdesk.field_visit import inspect, move, register, state
from receiptdesk.ingest import freeze_config, import_file
from receiptdesk.rules import evaluate


FIX = Path(__file__).resolve().parent.parent / 'fixtures'


def prepared(tmp_path, name):
    db = tmp_path / name
    freeze_config(db, FIX / 'field_config.json')
    assert import_file(db, FIX / 'field_events.jsonl')['imported'] == 5
    assert evaluate(db) == {'R1': 'undelivered', 'R2': 'insufficient'}
    assert register(db, 'route-1', FIX / 'field_case.json')['recipients'] == 2
    return db


def test_short_route_late_second_visit(tmp_path):
    db = prepared(tmp_path, 'short.db')
    assert move(db, 'route-1', 'A', 'm1')['minute'] == 1
    a = inspect(db, 'route-1', 'R1', 'v1')
    assert move(db, 'route-1', 'B', 'm2')['minute'] == 9
    b = inspect(db, 'route-1', 'R2', 'v2')
    assert move(db, 'route-1', 'E', 'm3')['minute'] == 16
    assert (a['late_minutes'], b['late_minutes']) == (0, 4)
    assert [x['recipient'] for x in state(db, 'route-1')['inspections']] == ['R1', 'R2']
    assert move(db, 'route-1', 'A', 'm1')['status'] == 'replayed'


def test_prompt_route_reduces_max_lateness(tmp_path):
    db = prepared(tmp_path, 'prompt.db')
    assert move(db, 'route-1', 'B', 'm1')['minute'] == 7
    b = inspect(db, 'route-1', 'R2', 'v1')
    assert move(db, 'route-1', 'A', 'm2')['minute'] == 17
    a = inspect(db, 'route-1', 'R1', 'v2')
    assert move(db, 'route-1', 'E', 'm3')['minute'] == 18
    assert (a['late_minutes'], b['late_minutes']) == (0, 2)
    assert [x['recipient'] for x in state(db, 'route-1')['inspections']] == ['R2', 'R1']


def test_road_and_location_guardrails(tmp_path):
    db = prepared(tmp_path, 'guard.db')
    with pytest.raises(ValueError, match='not_at_recipient_site'):
        inspect(db, 'route-1', 'R1', 'early')
    with pytest.raises(ValueError, match='road_unavailable'):
        move(db, 'route-1', 'E', 'no-road')
    assert state(db, 'route-1')['minute'] == 0


def test_requires_existing_decisions(tmp_path):
    db = tmp_path / 'missing.db'
    freeze_config(db, FIX / 'field_config.json')
    with pytest.raises(ValueError, match='recipient_not_field_eligible'):
        register(db, 'route-1', FIX / 'field_case.json')
