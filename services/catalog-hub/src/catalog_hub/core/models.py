"""합성 카탈로그 스키마 (Laughtale 캐싱 실험).

조립형 조회를 재현하기 위한 **합성** 관계형 모델입니다. 실제 회사 데이터·스키마·
도메인 용어를 복제하지 않았고, 값은 전부 seed 고정 생성물입니다.

재현하려는 구조적 성질만 가져왔습니다:
  - 트리형 분류(부모-자식)와 교차 참조로 다회 쿼리가 필요한 조립
  - 다국어 라벨이 별도 테이블에 있어 i18n 치환 단계가 생김
  - release가 불변이고 동시에 published 하나 — 캐시 키에 넣을 수 있음
  - 읽기 편중 (쓰기는 release 발행뿐)
"""

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Release(Base):
    """불변 release입니다. 동시에 published는 하나만 존재합니다."""

    __tablename__ = "release"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    label: Mapped[str] = mapped_column(String(64), nullable=False)
    published: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        # 동시에 published 하나라는 계약을 DB가 강제합니다.
        Index(
            "uq_release_single_published",
            "published",
            unique=True,
            postgresql_where=published.is_(True),
        ),
    )


class Category(Base):
    """트리형 분류 노드입니다. 부모-자식으로 그래프 조립이 필요합니다."""

    __tablename__ = "category"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    release_id: Mapped[int] = mapped_column(
        ForeignKey("release.id", ondelete="CASCADE"), nullable=False
    )
    code: Mapped[str] = mapped_column(String(64), nullable=False)
    parent_id: Mapped[int | None] = mapped_column(
        ForeignKey("category.id", ondelete="CASCADE"), nullable=True
    )
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    children: Mapped[list[Category]] = relationship(
        back_populates="parent", cascade="all, delete-orphan"
    )
    parent: Mapped[Category | None] = relationship(
        back_populates="children", remote_side=[id]
    )

    __table_args__ = (
        UniqueConstraint("release_id", "code", name="uq_category_release_code"),
        Index("ix_category_release", "release_id"),
        Index("ix_category_parent", "parent_id"),
    )


class Item(Base):
    """분류에 속한 항목입니다. 조회 응답의 잎 노드가 됩니다."""

    __tablename__ = "item"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    release_id: Mapped[int] = mapped_column(
        ForeignKey("release.id", ondelete="CASCADE"), nullable=False
    )
    category_id: Mapped[int] = mapped_column(
        ForeignKey("category.id", ondelete="CASCADE"), nullable=False
    )
    code: Mapped[str] = mapped_column(String(64), nullable=False)
    magnitude: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    payload: Mapped[str] = mapped_column(Text, nullable=False, default="")

    __table_args__ = (
        UniqueConstraint("release_id", "code", name="uq_item_release_code"),
        Index("ix_item_release_category", "release_id", "category_id"),
    )


class Attribute(Base):
    """항목에 딸린 속성입니다. 조립 시 추가 쿼리를 발생시킵니다."""

    __tablename__ = "attribute"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    release_id: Mapped[int] = mapped_column(
        ForeignKey("release.id", ondelete="CASCADE"), nullable=False
    )
    item_id: Mapped[int] = mapped_column(
        ForeignKey("item.id", ondelete="CASCADE"), nullable=False
    )
    code: Mapped[str] = mapped_column(String(64), nullable=False)
    value_num: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    __table_args__ = (Index("ix_attribute_release_item", "release_id", "item_id"),)


class ItemLink(Base):
    """항목 간 교차 참조입니다. 트리와 별개의 그래프 쿼리를 만듭니다."""

    __tablename__ = "item_link"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    release_id: Mapped[int] = mapped_column(
        ForeignKey("release.id", ondelete="CASCADE"), nullable=False
    )
    source_id: Mapped[int] = mapped_column(
        ForeignKey("item.id", ondelete="CASCADE"), nullable=False
    )
    target_id: Mapped[int] = mapped_column(
        ForeignKey("item.id", ondelete="CASCADE"), nullable=False
    )
    relation: Mapped[str] = mapped_column(String(32), nullable=False)

    __table_args__ = (Index("ix_item_link_release_source", "release_id", "source_id"),)


class Translation(Base):
    """다국어 라벨입니다. 조립 후 i18n 치환 단계를 만듭니다.

    `entity_kind`는 category/item/attribute를 구분합니다.
    """

    __tablename__ = "translation"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    release_id: Mapped[int] = mapped_column(
        ForeignKey("release.id", ondelete="CASCADE"), nullable=False
    )
    entity_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    entity_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    lang: Mapped[str] = mapped_column(String(8), nullable=False)
    label: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "release_id",
            "entity_kind",
            "entity_id",
            "lang",
            name="uq_translation_entity_lang",
        ),
        Index("ix_translation_lookup", "release_id", "entity_kind", "lang"),
    )
