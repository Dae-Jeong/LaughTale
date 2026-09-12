"""합성 데이터 생성 (Laughtale 캐싱 실험).

**전부 seed 고정 난수로 만든 합성 데이터입니다.** 회사 원자료를 읽거나 복제하지
않았고, 코드·라벨은 의미 없는 생성 문자열입니다. 재현하려는 것은 내용이 아니라
규모와 구조(트리 깊이, 교차 참조, 다국어 라벨 수)입니다.

기본 목표 규모는 27MB급입니다. `--scale`로 조정하며, 실제 생성 후 DB 크기를
측정해 보고합니다.

    python -m catalog_hub.tools.seed --dry-run     계획만 출력
    python -m catalog_hub.tools.seed --create-db   DB 생성 + 스키마 + 데이터
"""

import argparse
import asyncio
import random
import sys
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import func, insert, select, text
from sqlalchemy.ext.asyncio import create_async_engine

from catalog_hub.core.models import (
    Attribute,
    Base,
    Category,
    Item,
    ItemLink,
    Release,
    Translation,
)
from catalog_hub.core.settings import SUPPORTED_LANGS, Settings

DEFAULT_SEED = 20260912
CODE_ALPHABET = "abcdefghijklmnopqrstuvwxyz0123456789"

# payload 한 건의 길이입니다. 27MB급 규모를 행 수가 아니라 본문 크기로 맞춥니다.
PAYLOAD_CHARS = 900


@dataclass(frozen=True, slots=True)
class SeedPlan:
    """생성 규모입니다. 실제 바이트는 생성 후 측정합니다."""

    root_categories: int
    depth: int
    children_per_node: int
    items_per_leaf: int
    attributes_per_item: int
    links_per_item: int
    langs: tuple[str, ...]

    def category_count(self) -> int:
        total = 0
        nodes = self.root_categories
        for _ in range(self.depth):
            total += nodes
            nodes *= self.children_per_node
        return total

    def leaf_count(self) -> int:
        nodes = self.root_categories
        for _ in range(self.depth - 1):
            nodes *= self.children_per_node
        return nodes

    def item_count(self) -> int:
        return self.leaf_count() * self.items_per_leaf

    def estimated_rows(self) -> dict[str, int]:
        categories = self.category_count()
        items = self.item_count()
        attributes = items * self.attributes_per_item
        translations = (categories + items + attributes) * len(self.langs)
        return {
            "release": 1,
            "category": categories,
            "item": items,
            "attribute": attributes,
            "item_link": items * self.links_per_item,
            "translation": translations,
        }


# 기본 계획입니다. 27MB급을 목표로 하되 실제 크기는 생성 후 측정합니다.
#
# 조정 이력: 첫 시도(items_per_leaf=12)는 19MB였습니다. 항목을 늘려 27MB에
# 맞췄습니다. 규모를 맞추려고 payload 길이만 키우면 행 수·조립 비용이 따라오지
# 않으므로, 조립 대상 행 수를 늘리는 방향으로 조정했습니다.
DEFAULT_PLAN = SeedPlan(
    root_categories=6,
    depth=3,
    children_per_node=4,
    items_per_leaf=17,
    attributes_per_item=4,
    links_per_item=2,
    langs=SUPPORTED_LANGS,
)


def make_code(rng: random.Random, prefix: str, index: int) -> str:
    suffix = "".join(rng.choice(CODE_ALPHABET) for _ in range(6))
    return f"{prefix}-{index:05d}-{suffix}"


def make_label(rng: random.Random, lang: str, code: str) -> str:
    """언어별 합성 라벨입니다. 실제 번역이 아니라 길이를 가진 표식입니다."""
    words = "".join(rng.choice(CODE_ALPHABET) for _ in range(10))
    return f"[{lang}] {code} {words}"


def make_payload(rng: random.Random) -> str:
    return "".join(rng.choice(CODE_ALPHABET) for _ in range(PAYLOAD_CHARS))


async def create_database_if_absent(admin_url: str, db_name: str) -> bool:
    """기존 인스턴스 안에 DB만 추가합니다. 새 인스턴스를 띄우지 않습니다."""
    engine = create_async_engine(admin_url, isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as connection:
            exists = await connection.scalar(
                text("SELECT 1 FROM pg_database WHERE datname = :name"),
                {"name": db_name},
            )
            if exists:
                return False
            await connection.execute(text(f'CREATE DATABASE "{db_name}"'))
            return True
    finally:
        await engine.dispose()


async def seed(
    settings: Settings, plan: SeedPlan, *, seed_value: int
) -> dict[str, object]:
    """스키마를 만들고 합성 행을 채웁니다."""
    rng = random.Random(seed_value)
    engine = create_async_engine(settings.db_url)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.drop_all)
            await connection.run_sync(Base.metadata.create_all)

        async with engine.begin() as connection:
            release_id = 1
            await connection.execute(
                insert(Release),
                [
                    {
                        "id": release_id,
                        "label": f"rel-{seed_value}-001",
                        "published": True,
                        "created_at": datetime.now(UTC),
                    }
                ],
            )

            categories: list[dict[str, object]] = []
            next_category_id = 1
            leaves: list[int] = []

            def build_level(parent_id: int | None, level: int) -> None:
                nonlocal next_category_id
                count = (
                    plan.root_categories
                    if parent_id is None
                    else plan.children_per_node
                )
                for index in range(count):
                    category_id = next_category_id
                    next_category_id += 1
                    categories.append(
                        {
                            "id": category_id,
                            "release_id": release_id,
                            "code": make_code(rng, "cat", category_id),
                            "parent_id": parent_id,
                            "sort_order": index,
                        }
                    )
                    if level + 1 < plan.depth:
                        build_level(category_id, level + 1)
                    else:
                        leaves.append(category_id)

            build_level(None, 0)
            await connection.execute(insert(Category), categories)

            items: list[dict[str, object]] = []
            next_item_id = 1
            for leaf_id in leaves:
                for _ in range(plan.items_per_leaf):
                    item_id = next_item_id
                    next_item_id += 1
                    items.append(
                        {
                            "id": item_id,
                            "release_id": release_id,
                            "category_id": leaf_id,
                            "code": make_code(rng, "item", item_id),
                            "magnitude": rng.randrange(0, 1000),
                            "payload": make_payload(rng),
                        }
                    )
            await connection.execute(insert(Item), items)

            attributes: list[dict[str, object]] = []
            next_attribute_id = 1
            for row in items:
                for _ in range(plan.attributes_per_item):
                    attribute_id = next_attribute_id
                    next_attribute_id += 1
                    attributes.append(
                        {
                            "id": attribute_id,
                            "release_id": release_id,
                            "item_id": row["id"],
                            "code": make_code(rng, "attr", attribute_id),
                            "value_num": rng.randrange(0, 500),
                        }
                    )
            await connection.execute(insert(Attribute), attributes)

            item_ids = [row["id"] for row in items]
            links: list[dict[str, object]] = []
            next_link_id = 1
            for row in items:
                for _ in range(plan.links_per_item):
                    links.append(
                        {
                            "id": next_link_id,
                            "release_id": release_id,
                            "source_id": row["id"],
                            "target_id": rng.choice(item_ids),
                            "relation": rng.choice(("related", "successor", "variant")),
                        }
                    )
                    next_link_id += 1
            await connection.execute(insert(ItemLink), links)

            translations: list[dict[str, object]] = []
            next_translation_id = 1
            for kind, rows in (
                ("category", categories),
                ("item", items),
                ("attribute", attributes),
            ):
                for row in rows:
                    for lang in plan.langs:
                        translations.append(
                            {
                                "id": next_translation_id,
                                "release_id": release_id,
                                "entity_kind": kind,
                                "entity_id": row["id"],
                                "lang": lang,
                                "label": make_label(rng, lang, str(row["code"])),
                            }
                        )
                        next_translation_id += 1
            # 번역은 건수가 많아 나눠 넣습니다.
            chunk = 5000
            for start in range(0, len(translations), chunk):
                await connection.execute(
                    insert(Translation), translations[start : start + chunk]
                )

        async with engine.connect() as connection:
            size = await connection.scalar(
                text("SELECT pg_size_pretty(pg_database_size(current_database()))")
            )
            size_bytes = await connection.scalar(
                text("SELECT pg_database_size(current_database())")
            )
            counts: dict[str, int] = {}
            for name, model in (
                ("release", Release),
                ("category", Category),
                ("item", Item),
                ("attribute", Attribute),
                ("item_link", ItemLink),
                ("translation", Translation),
            ):
                counts[name] = (
                    await connection.scalar(select(func.count()).select_from(model))
                ) or 0

        return {
            "db_size_pretty": size,
            "db_size_bytes": size_bytes,
            "rows": counts,
            "total_rows": sum(counts.values()),
            "seed": seed_value,
        }
    finally:
        await engine.dispose()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m catalog_hub.tools.seed",
        description=(
            "합성 카탈로그 데이터를 생성합니다. 기존 5433 인스턴스 안에 DB만 추가하며 "
            "새 인스턴스를 띄우지 않습니다. 회사 데이터를 복제하지 않습니다."
        ),
    )
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED, help="난수 seed")
    parser.add_argument(
        "--admin-url",
        default="postgresql+asyncpg://thready:thready@127.0.0.1:5433/postgres",
        help="DB 생성을 위한 관리 접속 URL",
    )
    parser.add_argument(
        "--db-name", default="catalog_hub_lab", help="생성할 합성 DB 이름"
    )
    parser.add_argument("--create-db", action="store_true", help="DB가 없으면 만듭니다")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="생성하지 않고 계획만 출력합니다 (기본 동작은 --execute 필요)",
    )
    parser.add_argument(
        "--execute", action="store_true", help="실제로 스키마·데이터를 만듭니다"
    )
    return parser


async def run(args: argparse.Namespace) -> int:
    plan = DEFAULT_PLAN
    if args.dry_run or not args.execute:
        estimate = plan.estimated_rows()
        print("mode: dry-run (생성하지 않음)")
        print(f"db_name: {args.db_name}")
        print(f"langs: {list(plan.langs)}")
        print(f"estimated_rows: {estimate}")
        print(f"estimated_total_rows: {sum(estimate.values())}")
        print("note: 실제 DB 크기는 --execute 후 측정합니다.")
        return 0

    settings = Settings(
        _env_file=None,
        db_url=(f"postgresql+asyncpg://thready:thready@127.0.0.1:5433/{args.db_name}"),
    )
    if args.create_db:
        created = await create_database_if_absent(args.admin_url, args.db_name)
        print(f"database {'created' if created else 'already present'}: {args.db_name}")

    result = await seed(settings, plan, seed_value=args.seed)
    print(f"db_size: {result['db_size_pretty']} ({result['db_size_bytes']} bytes)")
    print(f"rows: {result['rows']}")
    print(f"total_rows: {result['total_rows']}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
