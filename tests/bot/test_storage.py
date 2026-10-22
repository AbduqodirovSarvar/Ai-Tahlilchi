"""SqliteStorage: contract of an aiogram FSM storage plus persistence across restarts."""

import sqlite3
import tempfile
from collections.abc import AsyncIterator
from contextlib import closing
from pathlib import Path

import pytest
from aiogram.exceptions import DataNotDictLikeError
from aiogram.fsm.state import State
from aiogram.fsm.storage.base import StorageKey
from hypothesis import given, settings
from hypothesis import strategies as st

from app.bot.storage import FSM_DB_NAME, SqliteStorage, fsm_db_path

KEY = StorageKey(bot_id=1, chat_id=10, user_id=20)
OTHER = StorageKey(bot_id=1, chat_id=10, user_id=21)


@pytest.fixture
async def storage(tmp_path: Path) -> AsyncIterator[SqliteStorage]:
    store = SqliteStorage(tmp_path / "sub" / FSM_DB_NAME)
    yield store
    await store.close()


async def test_empty_by_default(storage: SqliteStorage) -> None:
    assert await storage.get_state(KEY) is None
    assert await storage.get_data(KEY) == {}


async def test_state_and_data_roundtrip_independent(storage: SqliteStorage) -> None:
    await storage.set_state(KEY, "AnalysisFlow:dates")
    await storage.set_data(KEY, {"draft": {"a": [1, 2]}, "name": "Oʻzbekiston"})
    assert await storage.get_state(KEY) == "AnalysisFlow:dates"
    assert await storage.get_data(KEY) == {"draft": {"a": [1, 2]}, "name": "Oʻzbekiston"}
    await storage.set_state(KEY, None)
    assert await storage.get_state(KEY) is None
    assert (await storage.get_data(KEY))["name"] == "Oʻzbekiston"


async def test_state_accepts_state_objects(storage: SqliteStorage) -> None:
    await storage.set_state(KEY, State("step", group_name="Flow"))
    assert await storage.get_state(KEY) == "Flow:step"


async def test_users_are_isolated(storage: SqliteStorage) -> None:
    await storage.set_data(KEY, {"x": 1})
    assert await storage.get_data(OTHER) == {}


async def test_get_value_default(storage: SqliteStorage) -> None:
    await storage.set_data(KEY, {"x": [1]})
    assert await storage.get_value(KEY, "x") == [1]
    assert await storage.get_value(KEY, "missing", "d") == "d"


async def test_update_data_merges(storage: SqliteStorage) -> None:
    await storage.set_data(KEY, {"a": 1})
    assert await storage.update_data(KEY, {"b": 2}) == {"a": 1, "b": 2}


async def test_rejects_non_dict_data(storage: SqliteStorage) -> None:
    with pytest.raises(DataNotDictLikeError):
        await storage.set_data(KEY, [1])  # type: ignore[arg-type]


async def test_cleared_record_leaves_no_row(storage: SqliteStorage, tmp_path: Path) -> None:
    await storage.set_state(KEY, "S:x")
    await storage.set_state(KEY, None)
    await storage.set_data(KEY, {})
    with closing(sqlite3.connect(tmp_path / "sub" / FSM_DB_NAME)) as db:
        assert db.execute("SELECT count(*) FROM fsm").fetchone() == (0,)


async def test_survives_restart(tmp_path: Path) -> None:
    path = tmp_path / FSM_DB_NAME
    first = SqliteStorage(path)
    await first.set_state(KEY, "S:x")
    await first.set_data(KEY, {"draft": {"v": 1}})
    await first.close()

    second = SqliteStorage(path)
    try:
        assert await second.get_state(KEY) == "S:x"
        assert await second.get_data(KEY) == {"draft": {"v": 1}}
    finally:
        await second.close()


@pytest.mark.parametrize("payload", ["{not json", "[1, 2]"])
async def test_corrupt_data_reads_as_empty(
    storage: SqliteStorage, tmp_path: Path, payload: str
) -> None:
    await storage.set_state(KEY, "S:x")
    with closing(sqlite3.connect(tmp_path / "sub" / FSM_DB_NAME)) as db:
        db.execute("UPDATE fsm SET data = ?", (payload,))
        db.commit()
    assert await storage.get_data(KEY) == {}
    assert await storage.get_state(KEY) == "S:x"


async def test_close_is_idempotent_and_reopens(storage: SqliteStorage) -> None:
    await storage.close()
    await storage.close()
    await storage.set_data(KEY, {"a": 1})
    assert await storage.get_data(KEY) == {"a": 1}


def test_fsm_db_sits_next_to_the_application_db() -> None:
    assert fsm_db_path(Path("/data/app.db")) == Path("/data") / FSM_DB_NAME


JSON_LEAVES = st.none() | st.booleans() | st.integers(-(2**53), 2**53) | st.text(max_size=20)


@settings(max_examples=40, deadline=None)
@given(
    st.dictionaries(
        st.text(min_size=1, max_size=8),
        st.recursive(JSON_LEAVES, lambda inner: st.lists(inner, max_size=3), max_leaves=6),
        min_size=1,
        max_size=4,
    )
)
async def test_json_data_roundtrips(data: dict[str, object]) -> None:
    with tempfile.TemporaryDirectory() as directory:
        store = SqliteStorage(Path(directory) / FSM_DB_NAME)
        try:
            await store.set_data(KEY, data)
            assert await store.get_data(KEY) == data
        finally:
            await store.close()
