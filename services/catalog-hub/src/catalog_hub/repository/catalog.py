"""조립형 조회 (Laughtale 캐싱 실험).

**인위적으로 느리게 만들지 않습니다.** sleep·전수 스캔·불필요한 재파싱이 없고,
모든 쿼리는 인덱스를 쓰며 N+1을 피하려 한 번에 모아 읽습니다. 여기서 드는 비용은
"다회 쿼리 + 파이썬 트리 조립 + i18n 치환"이라는 구조 자체의 비용입니다.

구간 귀속:
  - `current_release`는 STAGE_RELEASE (D4 — 캐시 hit에도 남습니다)
  - 본문 쿼리 다회는 STAGE_DB
  - 트리 조립과 i18n 치환은 STAGE_ASSEMBLE
"""

from collections import defaultdict

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from catalog_hub.core.assembled import (
    AssembledAttribute,
    AssembledCatalog,
    AssembledCategory,
    AssembledItem,
)
from catalog_hub.core.models import (
    Attribute,
    Category,
    Item,
    ItemLink,
    Release,
    Translation,
)
from catalog_hub.core.stage_timer import (
    STAGE_ASSEMBLE,
    STAGE_DB,
    STAGE_RELEASE,
    StageTimer,
)

# 본문 쿼리 횟수입니다. 조립 1회당 실제로 나가는 SELECT 수와 일치해야 합니다.
BODY_QUERY_COUNT = 5


class ReleaseNotFoundError(LookupError):
    """published release가 없습니다. 빈 DB이거나 발행 전입니다."""


class CatalogRepository:
    """합성 카탈로그의 조립형 조회입니다."""

    __slots__ = ("session", "timer")

    def __init__(self, session: AsyncSession, timer: StageTimer) -> None:
        self.session = session
        self.timer = timer

    async def current_release(self) -> Release:
        """현재 published release를 조회합니다.

        D4: 요청 시점 DB 조회입니다. 폴링·이벤트를 새로 만들지 않습니다.
        전파 지연은 최대 1요청이지만, **매 요청 쿼리 1회가 남습니다.**
        캐시 hit에도 이 비용은 사라지지 않으므로 별도 구간으로 잽니다.
        """
        result = await self.session.execute(
            select(Release).where(Release.published.is_(True))
        )
        release = result.scalar_one_or_none()
        self.timer.mark(STAGE_RELEASE)
        if release is None:
            raise ReleaseNotFoundError("no published release")
        return release

    async def assemble_catalog(self, release: Release, lang: str) -> AssembledCatalog:
        """한 release·lang의 전체 카탈로그를 조립합니다.

        쿼리 5회로 필요한 행을 모두 읽은 뒤(STAGE_DB), 파이썬에서 트리를 조립하고
        i18n을 치환합니다(STAGE_ASSEMBLE). 두 구간을 분리해 재야 캐시 이득 상한을
        올바르게 계산할 수 있습니다.
        """
        release_id = release.id

        categories = (
            (
                await self.session.execute(
                    select(Category)
                    .where(Category.release_id == release_id)
                    .order_by(Category.sort_order, Category.id)
                )
            )
            .scalars()
            .all()
        )
        items = (
            (
                await self.session.execute(
                    select(Item)
                    .where(Item.release_id == release_id)
                    .order_by(Item.category_id, Item.id)
                )
            )
            .scalars()
            .all()
        )
        attributes = (
            (
                await self.session.execute(
                    select(Attribute)
                    .where(Attribute.release_id == release_id)
                    .order_by(Attribute.item_id, Attribute.id)
                )
            )
            .scalars()
            .all()
        )
        links = (
            (
                await self.session.execute(
                    select(ItemLink).where(ItemLink.release_id == release_id)
                )
            )
            .scalars()
            .all()
        )
        translations = (
            (
                await self.session.execute(
                    select(Translation).where(
                        Translation.release_id == release_id,
                        Translation.lang == lang,
                    )
                )
            )
            .scalars()
            .all()
        )
        self.timer.mark(STAGE_DB)

        labels: dict[tuple[str, int], str] = {
            (row.entity_kind, row.entity_id): row.label for row in translations
        }

        item_codes: dict[int, str] = {row.id: row.code for row in items}
        attributes_by_item: dict[int, list[AssembledAttribute]] = defaultdict(list)
        for row in attributes:
            attributes_by_item[row.item_id].append(
                AssembledAttribute(
                    code=row.code,
                    label=labels.get(("attribute", row.id), row.code),
                    value_num=row.value_num,
                )
            )

        related_by_item: dict[int, list[str]] = defaultdict(list)
        for row in links:
            target_code = item_codes.get(row.target_id)
            if target_code is not None:
                related_by_item[row.source_id].append(target_code)

        items_by_category: dict[int, list[AssembledItem]] = defaultdict(list)
        for row in items:
            items_by_category[row.category_id].append(
                AssembledItem(
                    code=row.code,
                    label=labels.get(("item", row.id), row.code),
                    magnitude=row.magnitude,
                    attributes=tuple(attributes_by_item.get(row.id, ())),
                    related_codes=tuple(related_by_item.get(row.id, ())),
                )
            )

        children_by_parent: dict[int | None, list[Category]] = defaultdict(list)
        for row in categories:
            children_by_parent[row.parent_id].append(row)

        def build(node: Category) -> AssembledCategory:
            """부모에서 자식으로 내려가며 트리를 만듭니다."""
            return AssembledCategory(
                code=node.code,
                label=labels.get(("category", node.id), node.code),
                children=tuple(
                    build(child) for child in children_by_parent.get(node.id, ())
                ),
                items=tuple(items_by_category.get(node.id, ())),
            )

        roots = tuple(build(node) for node in children_by_parent.get(None, ()))
        assembled = AssembledCatalog(
            release_id=release_id,
            release_label=release.label,
            lang=lang,
            roots=roots,
            category_count=len(categories),
            item_count=len(items),
        )
        self.timer.mark(STAGE_ASSEMBLE)
        return assembled
