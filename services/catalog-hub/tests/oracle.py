"""독립 기대값 oracle (Laughtale 캐싱 실험).

서비스 구현의 repository·조립 함수·캐시를 재사용하지 않습니다. 같은 합성 DB를
**raw SQL로 직접 읽어** 기대값을 따로 만듭니다. 그래야 캐시 경로와 직조회 경로가
함께 틀려도 검출됩니다 (설계서 R6 보완분).

theme-catalog의 독립 oracle 패턴을 재사용했습니다 — 구현과 다른 경로로 같은 답을
구성하고, 추가로 고정 기대값 표와 대조합니다.
"""

import os

import asyncpg

# 서비스가 쓰는 SQLAlchemy URL과 달리 raw asyncpg DSN입니다. 경로 자체를 분리합니다.
# 기본은 호스트에서 5433으로 직접 붙는 경로입니다. 클러스터 안(Step 3 관측 Pod)에서는
# Service 이름으로 붙어야 하므로 환경변수로 바꿉니다. 경로만 다르고 같은 합성 DB입니다.
ORACLE_DSN = os.environ.get(
    "ORACLE_DSN", "postgresql://thready:thready@127.0.0.1:5433/catalog_hub_lab"
)


async def fetch_expected_shape(dsn: str = ORACLE_DSN) -> dict[str, object]:
    """published release의 규모를 구현과 무관하게 셉니다."""
    connection = await asyncpg.connect(dsn)
    try:
        release_id = await connection.fetchval(
            "SELECT id FROM release WHERE published IS TRUE"
        )
        categories = await connection.fetchval(
            "SELECT count(*) FROM category WHERE release_id = $1", release_id
        )
        items = await connection.fetchval(
            "SELECT count(*) FROM item WHERE release_id = $1", release_id
        )
        roots = await connection.fetchval(
            "SELECT count(*) FROM category WHERE release_id = $1 AND parent_id IS NULL",
            release_id,
        )
        return {
            "release_id": release_id,
            "category_count": categories,
            "item_count": items,
            "root_count": roots,
        }
    finally:
        await connection.close()


async def fetch_expected_labels(
    lang: str, limit: int = 20, dsn: str = ORACLE_DSN
) -> dict[str, str]:
    """분류 코드 → 해당 언어 라벨입니다. 구현의 i18n 치환과 대조합니다."""
    connection = await asyncpg.connect(dsn)
    try:
        release_id = await connection.fetchval(
            "SELECT id FROM release WHERE published IS TRUE"
        )
        rows = await connection.fetch(
            """
            SELECT c.code, t.label
            FROM category c
            JOIN translation t
              ON t.release_id = c.release_id
             AND t.entity_kind = 'category'
             AND t.entity_id = c.id
             AND t.lang = $2
            WHERE c.release_id = $1
            ORDER BY c.id
            LIMIT $3
            """,
            release_id,
            lang,
            limit,
        )
        return {row["code"]: row["label"] for row in rows}
    finally:
        await connection.close()


async def fetch_expected_item(
    code: str, lang: str, dsn: str = ORACLE_DSN
) -> dict[str, object]:
    """항목 하나의 기대값입니다. 속성 수·교차참조 수까지 독립적으로 셉니다."""
    connection = await asyncpg.connect(dsn)
    try:
        release_id = await connection.fetchval(
            "SELECT id FROM release WHERE published IS TRUE"
        )
        row = await connection.fetchrow(
            """
            SELECT i.id, i.code, i.magnitude, t.label
            FROM item i
            LEFT JOIN translation t
              ON t.release_id = i.release_id
             AND t.entity_kind = 'item'
             AND t.entity_id = i.id
             AND t.lang = $2
            WHERE i.release_id = $1 AND i.code = $3
            """,
            release_id,
            lang,
            code,
        )
        if row is None:
            raise AssertionError(f"oracle has no item {code}")
        attributes = await connection.fetchval(
            "SELECT count(*) FROM attribute WHERE release_id = $1 AND item_id = $2",
            release_id,
            row["id"],
        )
        links = await connection.fetchval(
            "SELECT count(*) FROM item_link WHERE release_id = $1 AND source_id = $2",
            release_id,
            row["id"],
        )
        return {
            "code": row["code"],
            "label": row["label"],
            "magnitude": row["magnitude"],
            "attribute_count": attributes,
            "related_count": links,
        }
    finally:
        await connection.close()


def flatten_categories(roots: list[dict]) -> dict[str, dict]:
    """응답 트리를 코드 → 노드로 평탄화합니다. 구현 헬퍼를 쓰지 않습니다."""
    flat: dict[str, dict] = {}

    def walk(node: dict) -> None:
        flat[node["code"]] = node
        for child in node["children"]:
            walk(child)

    for root in roots:
        walk(root)
    return flat


def flatten_items(roots: list[dict]) -> dict[str, dict]:
    """응답 트리의 모든 항목을 코드 → 항목으로 모읍니다."""
    flat: dict[str, dict] = {}

    def walk(node: dict) -> None:
        for item in node["items"]:
            flat[item["code"]] = item
        for child in node["children"]:
            walk(child)

    for root in roots:
        walk(root)
    return flat
