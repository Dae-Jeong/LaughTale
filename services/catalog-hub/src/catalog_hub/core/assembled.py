"""조립된 응답 객체 (D1 — 캐싱 단위).

캐싱 단위는 **조립 완료된 불변 객체**입니다. 직렬화 JSON은 캐싱하지 않습니다.
따라서 캐시 hit에도 직렬화는 남으며, 캐시 이득 상한은 `DB + 조립 + i18n`입니다.

이 타입들은 DB·HTTP를 알지 못합니다. repository가 만들고 router가 응답으로 바꿉니다.
"""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class AssembledAttribute:
    """i18n 치환이 끝난 속성입니다."""

    code: str
    label: str
    value_num: int


@dataclass(frozen=True, slots=True)
class AssembledItem:
    """i18n 치환이 끝난 항목입니다. 교차 참조 코드를 함께 갖습니다."""

    code: str
    label: str
    magnitude: int
    attributes: tuple[AssembledAttribute, ...]
    related_codes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class AssembledCategory:
    """트리 노드입니다. 자식 분류와 항목을 모두 품습니다."""

    code: str
    label: str
    children: tuple[AssembledCategory, ...]
    items: tuple[AssembledItem, ...]


@dataclass(frozen=True, slots=True)
class AssembledCatalog:
    """한 (release, endpoint, lang)의 조립 결과입니다. 이것이 캐시 값입니다."""

    release_id: int
    release_label: str
    lang: str
    roots: tuple[AssembledCategory, ...]
    category_count: int
    item_count: int


@dataclass(frozen=True, slots=True)
class CacheKey:
    """캐시 키입니다. release_id를 포함하므로 stale이 발생하지 않습니다 (D4).

    release는 불변이고 동시에 published 하나이므로, release가 바뀌면 키 전체가
    자연히 무효가 됩니다. TTL·부분 무효화·경합 설계가 필요 없습니다.
    """

    release_id: int
    endpoint: str
    lang: str
