"""release 전환 헬퍼 (Laughtale 캐싱 실험).

D7 민감도 측정과 Step 3 전파 관측이 **실제 release 전환을 일으켜야** 하므로,
전환·되돌리기를 여기에 둡니다. 조회 경로(repository·조립·캐시)는 쓰지 않고 raw
asyncpg로 DB를 직접 바꿉니다 — 구현이 틀려도 전환 자체는 독립적으로 일어납니다.

phub 계약을 모사합니다: published는 동시에 하나이고, 기존 release를 제자리 수정하지
않고 **successor**를 새로 만들어 전환합니다.

시험·측정 도구 전용입니다. 서비스 요청 경로에서 호출하지 않습니다.
"""

import os

import asyncpg

# 서비스가 쓰는 SQLAlchemy URL과 달리 raw asyncpg DSN입니다. 경로 자체를 분리합니다.
# 기본은 호스트에서 5433으로 직접 붙는 경로입니다. 클러스터 안(Step 3 관측 Pod)에서는
# Service 이름으로 붙어야 하므로 환경변수로 바꿉니다. 경로만 다르고 같은 합성 DB입니다.
TRANSITION_DSN = os.environ.get(
    "ORACLE_DSN", "postgresql://thready:thready@127.0.0.1:5433/catalog_hub_lab"
)


async def publish_successor_release(
    label_suffix: str, dsn: str = TRANSITION_DSN
) -> tuple[int, int]:
    """새 release를 발행해 전환을 일으킵니다. 시험·측정 전용입니다.

    phub 계약을 모사합니다: published는 동시에 하나이고, 기존 release를 제자리
    수정하지 않고 **successor**를 새로 만들어 전환합니다. 한 transaction에서
    수행하므로 published가 둘인 순간이 없습니다.

    반환: (옛 release_id, 새 release_id)
    """
    connection = await asyncpg.connect(dsn)
    try:
        async with connection.transaction():
            old_id = await connection.fetchval(
                "SELECT id FROM release WHERE published IS TRUE"
            )
            new_id = await connection.fetchval("SELECT max(id) + 1 FROM release")
            await connection.execute(
                "UPDATE release SET published = FALSE WHERE id = $1", old_id
            )
            await connection.execute(
                "INSERT INTO release (id, label, published, created_at)"
                " VALUES ($1, $2, TRUE, now())",
                new_id,
                f"rel-successor-{label_suffix}",
            )
            # successor는 옛 release의 행을 복사합니다. id가 명시 할당이므로
            # 테이블별 최대 id만큼 밀어 새 id를 만듭니다. 자기참조 컬럼(parent_id,
            # category_id 등)도 같은 폭으로 밀어야 관계가 보존됩니다.
            self_refs = {
                "category": ("parent_id",),
                "item": ("category_id",),
                "attribute": ("item_id",),
                "item_link": ("source_id", "target_id"),
                "translation": ("entity_id",),
            }
            offsets: dict[str, int] = {}
            for table in ("category", "item", "attribute", "item_link", "translation"):
                offsets[table] = await connection.fetchval(
                    f"SELECT coalesce(max(id), 0) FROM {table}"  # noqa: S608
                )

            # 참조 대상 테이블의 offset을 써야 관계가 맞습니다.
            ref_source = {
                "parent_id": "category",
                "category_id": "category",
                "item_id": "item",
                "source_id": "item",
                "target_id": "item",
            }
            for table in ("category", "item", "attribute", "item_link", "translation"):
                columns = await connection.fetch(
                    "SELECT column_name FROM information_schema.columns"
                    " WHERE table_name = $1 ORDER BY ordinal_position",
                    table,
                )
                names = [row["column_name"] for row in columns]
                shift = offsets[table]
                projected: list[str] = []
                for name in names:
                    if name == "id":
                        projected.append(f"id + {shift}")
                    elif name == "release_id":
                        projected.append("$1")
                    elif name in self_refs.get(table, ()):
                        # entity_id는 entity_kind에 따라 참조 테이블이 달라집니다.
                        if table == "translation":
                            projected.append(
                                "entity_id + CASE entity_kind"
                                f" WHEN 'category' THEN {offsets['category']}"
                                f" WHEN 'item' THEN {offsets['item']}"
                                f" ELSE {offsets['attribute']} END"
                            )
                        else:
                            projected.append(f"{name} + {offsets[ref_source[name]]}")
                    else:
                        projected.append(name)
                await connection.execute(
                    f"INSERT INTO {table} ({', '.join(names)})"  # noqa: S608
                    f" SELECT {', '.join(projected)} FROM {table}"
                    " WHERE release_id = $2 ORDER BY id",
                    new_id,
                    old_id,
                )
            # 새 release의 라벨을 구분 가능하게 바꿉니다.
            await connection.execute(
                "UPDATE translation SET label = 'SUCCESSOR ' || label"
                " WHERE release_id = $1",
                new_id,
            )
        return old_id, new_id
    finally:
        await connection.close()


async def rollback_to_release(
    keep_id: int, drop_id: int, dsn: str = TRANSITION_DSN
) -> None:
    """시험이 만든 successor를 지우고 원래 release를 다시 published로 만듭니다."""
    connection = await asyncpg.connect(dsn)
    try:
        async with connection.transaction():
            await connection.execute("DELETE FROM release WHERE id = $1", drop_id)
            await connection.execute(
                "UPDATE release SET published = TRUE WHERE id = $1", keep_id
            )
    finally:
        await connection.close()
