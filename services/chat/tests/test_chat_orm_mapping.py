import pytest
from sqlalchemy import DateTime, DefaultClause, inspect

from chat_service.models.base import Base, CreatedAtMixin
from chat_service.models.chat import Conversation, Member, Message, User, metadata


@pytest.mark.parametrize(
    "model,table,keys",
    [
        (User, "users", ["id"]),
        (Conversation, "conversations", ["id"]),
        (Member, "members", ["conversation_id", "user_id"]),
        (Message, "messages", ["id"]),
    ],
)
def test_models_share_metadata_without_implicit_relationships(
    model, table: str, keys: list[str]
) -> None:
    mapper = inspect(model)
    assert mapper.local_table is metadata.tables[f"chat.{table}"]
    assert metadata is Base.metadata
    assert [column.name for column in mapper.primary_key] == keys
    assert not mapper.relationships


def test_timestamp_is_opt_in_without_schema_expansion() -> None:
    assert issubclass(Message, CreatedAtMixin)
    assert len(metadata.tables) == 4
    for model in (User, Conversation, Member, Message):
        columns = inspect(model).local_table.c
        assert "updated_at" not in columns
        assert ("created_at" in columns) is (model is Message)
    created_at = inspect(Message).columns.created_at
    assert isinstance(created_at.type, DateTime)
    assert created_at.type.timezone is True
    assert created_at.nullable is False
    assert isinstance(created_at.server_default, DefaultClause)
    assert str(created_at.server_default.arg) == "CURRENT_TIMESTAMP"
