"""Storage layer & projects — pytest suite (split from the original monolithic runner)."""

from __future__ import annotations

from datetime import date
from pathlib import Path
import json


def test_local_backend_list_files_returns_posix_keys(tmp: Path):
    from storage.backends import LocalBackend

    backend = LocalBackend(tmp)
    key = "users/local_user/test_proj/imports/custom/manual.json"
    backend.write_bytes(key, b"{}")

    assert backend.list_files("users/local_user/test_proj/imports/") == [key]


def test_storage_jsonl_roundtrip(tmp: Path):
    """JSONL write → read preserves all records."""
    import storage
    path = tmp / "test.jsonl"
    records = [
        {"date": "2023-01-01", "entries": [{"ticker": "AAPL", "amount": 10}]},
        {"date": "2023-01-02", "entries": [{"ticker": "MSFT", "amount": 5}]},
    ]
    storage.write_jsonl(path, records)
    loaded = storage.read_jsonl(path)
    assert len(loaded) == 2
    assert loaded[0]["date"] == "2023-01-01"
    assert loaded[1]["entries"][0]["ticker"] == "MSFT"


def test_storage_append_jsonl(tmp: Path):
    """Appending to JSONL adds new records without touching existing ones."""
    import storage
    path = tmp / "append.jsonl"
    storage.write_jsonl(path, [{"date": "2023-01-01", "x": 1}])
    storage.append_jsonl(path, {"date": "2023-01-02", "x": 2})
    records = storage.read_jsonl(path)
    assert len(records) == 2
    assert records[1]["x"] == 2


def test_storage_balance(tmp: Path):
    """Balance save → load preserves values, strips near-zero entries."""
    import storage
    balance = {"AAPL": {"amount": 10.0, "avg_price": 125.0}, "PLN": {"amount": 5000.0, "avg_price": 0.0}, "MSFT": {"amount": 1e-12, "avg_price": 0.0}}
    storage.save_balance(balance)
    loaded = storage.load_balance()
    assert loaded["AAPL"]["amount"] == 10.0
    assert loaded["PLN"]["amount"] == 5000.0
    assert "MSFT" not in loaded, "Near-zero holding should be stripped"


def test_price_cache_write_read(tmp: Path):
    """Price cache write → read returns same data for a given ticker/year."""
    import storage
    prices = {"2023-01-03": 125.07, "2023-01-04": 126.36}
    storage.save_price_year("AAPL", 2023, prices)
    loaded = storage.load_price_year("AAPL", 2023)
    assert loaded["2023-01-03"] == 125.07
    assert storage.has_price_year("AAPL", 2023)
    assert not storage.has_price_year("AAPL", 2022)


def test_invalidate_portfolio_from_plain_json_layout(tmp: Path):
    """REGRESSION: cache invalidation must not depend on orjson byte layout.

    The old implementation sliced bytes [9:19], which only works for orjson's
    compact output; json.dumps puts a space after the colon.
    """
    import storage, json as _json

    path = storage.portfolio_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    # Deliberately written with stdlib json (spaced layout, like no-orjson envs)
    path.write_text(
        _json.dumps({"date": "2024-01-01", "assets": [], "total_value": 1.0}) + "\n" +
        _json.dumps({"date": "2024-02-01", "assets": [], "total_value": 2.0}) + "\n" +
        _json.dumps({"date": "2024-03-01", "assets": [], "total_value": 3.0}) + "\n",
        encoding="utf-8",
    )

    storage.invalidate_portfolio_from("2024-02-01")

    kept = [r["date"] for r in storage.read_jsonl(path)]
    assert kept == ["2024-01-01"], \
        f"Snapshots on/after 2024-02-01 must be removed, kept: {kept}"


def test_invalidate_portfolio(tmp: Path):
    """invalidate_portfolio_from removes snapshots on/after the given date."""
    import storage

    snapshots = [
        {"date": "2023-01-01", "total_value": 100},
        {"date": "2023-01-02", "total_value": 110},
        {"date": "2023-01-03", "total_value": 120},
        {"date": "2023-01-04", "total_value": 130},
    ]
    storage.save_portfolio(snapshots)
    storage.invalidate_portfolio_from("2023-01-03")
    kept = storage.load_portfolio()
    assert len(kept) == 2
    assert kept[-1]["date"] == "2023-01-02"


def test_storage_loads_prices_range(tmp: Path):
    """load_prices_range merges multiple year files correctly."""
    import storage

    storage.save_price_year("AAPL", 2022, {"2022-12-30": 129.93})
    storage.save_price_year("AAPL", 2023, {"2023-01-03": 125.07})

    prices = storage.load_prices_range("AAPL", date(2022, 12, 1), date(2023, 1, 31))
    assert "2022-12-30" in prices
    assert "2023-01-03" in prices


def test_create_and_list_projects(tmp: Path):
    """create_project + list_projects returns sorted project names."""
    import storage

    storage.create_project("alpha")
    storage.create_project("gamma")
    storage.create_project("beta")

    projects = storage.list_projects()
    assert "alpha" in projects
    assert "beta" in projects
    assert "gamma" in projects
    assert projects == sorted(projects)


def test_rename_project(tmp: Path):
    """rename_project preserves project data under new name."""
    import storage

    storage.create_project("old_name")
    storage.set_current_project("old_name")
    storage.save_balance({"AAPL": {"amount": 10.0, "avg_price": 150.0}})

    storage.rename_project("old_name", "new_name")

    projects = storage.list_projects()
    assert "new_name" in projects
    assert "old_name" not in projects

    storage.set_current_project("new_name")
    bal = storage.load_balance()
    assert bal["AAPL"]["amount"] == 10.0


def test_delete_project(tmp: Path):
    """delete_project removes project directory and registry entry."""
    import storage

    storage.create_project("to_delete")
    assert "to_delete" in storage.list_projects()

    storage.delete_project("to_delete")
    assert "to_delete" not in storage.list_projects()


def test_benchmark_save_load_roundtrip(tmp: Path):
    """save_benchmarks → load_benchmarks roundtrip preserves data."""
    import storage

    data = [
        {"date": "2023-01-03", "SXRV.DE": 5000.0, "I500.DE": 4800.0},
        {"date": "2023-01-04", "SXRV.DE": 5050.0, "I500.DE": 4820.0},
    ]
    storage.save_benchmarks("PLN", data)
    loaded = storage.load_benchmarks("PLN")
    assert loaded is not None
    assert len(loaded) == 2
    assert loaded[0]["SXRV.DE"] == 5000.0
    assert loaded[1]["I500.DE"] == 4820.0


def test_benchmark_load_returns_none_when_missing(tmp: Path):
    """load_benchmarks returns None when no cache file exists."""
    import storage
    assert storage.load_benchmarks("USD") is None


def test_list_users_default(tmp: Path):
    """list_users returns local_user when no users.json exists."""
    import storage
    users = storage.list_users()
    assert "local_user" in users


def test_create_user(tmp: Path):
    """create_user adds user to users.json and creates directory."""
    import storage
    import os

    storage.create_user("testuser")
    users = storage.list_users()
    assert "testuser" in users
    assert "local_user" in users

    # Check directory was created
    user_dir = storage.USERS_ROOT / "testuser"
    assert user_dir.exists()
    assert user_dir.is_dir()


def test_create_user_duplicate_raises(tmp: Path):
    """Creating duplicate user raises ValueError."""
    import storage

    storage.create_user("testuser")
    try:
        storage.create_user("testuser")
        assert False, "Expected ValueError"
    except ValueError as e:
        assert "already exists" in str(e)


def test_get_user_by_key(tmp: Path):
    """get_user_by_key returns user_name for valid key, None for invalid."""
    import storage
    import json

    # Check default user key
    user_name = storage.get_user_by_key("a1b2c3d4-e5f6-7890-abcd-ef1234567890")
    assert user_name == "local_user"

    # Invalid key returns None
    assert storage.get_user_by_key("invalid-key") is None


def test_set_current_user_by_key(tmp: Path):
    """set_current_user_by_key sets current user correctly."""
    import storage

    storage.set_current_user_by_key("a1b2c3d4-e5f6-7890-abcd-ef1234567890")
    assert storage.current_user() == "local_user"

    # Invalid key falls back to default
    storage.set_current_user_by_key("invalid-key")
    assert storage.current_user() == "local_user"


def test_project_isolation_per_user(tmp: Path):
    """Projects are isolated per user - different users can have same project name."""
    import storage

    # Create two users
    storage.create_user("user1")
    storage.create_user("user2")

    # Create same project name for both users
    storage.set_current_user("user1")
    storage.create_project("my_portfolio")
    storage.save_balance({"AAPL": {"amount": 10.0, "avg_price": 150.0}})

    storage.set_current_user("user2")
    storage.create_project("my_portfolio")
    storage.save_balance({"MSFT": {"amount": 5.0, "avg_price": 300.0}})

    # Verify isolation
    storage.set_current_user("user1")
    storage.set_current_project("my_portfolio")
    bal1 = storage.load_balance()
    assert bal1["AAPL"]["amount"] == 10.0
    assert "MSFT" not in bal1

    storage.set_current_user("user2")
    storage.set_current_project("my_portfolio")
    bal2 = storage.load_balance()
    assert bal2["MSFT"]["amount"] == 5.0
    assert "AAPL" not in bal2


def test_list_projects_per_user(tmp: Path):
    """list_projects returns only projects for current user."""
    import storage

    storage.create_user("user1")
    storage.create_user("user2")

    storage.set_current_user("user1")
    storage.create_project("project_a")
    storage.create_project("project_b")

    storage.set_current_user("user2")
    storage.create_project("project_c")

    storage.set_current_user("user1")
    projects = storage.list_projects()
    assert set(projects) == {"project_a", "project_b"}

    storage.set_current_user("user2")
    projects = storage.list_projects()
    assert projects == ["project_c"]
