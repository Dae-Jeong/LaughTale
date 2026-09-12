"""외부 카탈로그 스키마 (Laughtale 캐싱 실험).

조립된 불변 객체를 외부 응답으로 **명시적으로** 변환합니다. 이 변환과 뒤이은
JSON 직렬화가 STAGE_SERIALIZE이며, D1에 따라 캐시 hit에도 남습니다.
따라서 캐시 이득 상한에서 제외합니다.
"""

from pydantic import BaseModel, ConfigDict

from catalog_hub.core.assembled import (
    AssembledAttribute,
    AssembledCatalog,
    AssembledCategory,
    AssembledItem,
)


class AttributeData(BaseModel):
    model_config = ConfigDict(frozen=True)

    code: str
    label: str
    value_num: int


class ItemData(BaseModel):
    model_config = ConfigDict(frozen=True)

    code: str
    label: str
    magnitude: int
    attributes: list[AttributeData]
    related_codes: list[str]


class CategoryData(BaseModel):
    model_config = ConfigDict(frozen=True)

    code: str
    label: str
    children: list[CategoryData]
    items: list[ItemData]


class CatalogData(BaseModel):
    model_config = ConfigDict(frozen=True)

    release_id: int
    release_label: str
    lang: str
    category_count: int
    item_count: int
    roots: list[CategoryData]


def to_attribute_data(source: AssembledAttribute) -> AttributeData:
    return AttributeData(
        code=source.code, label=source.label, value_num=source.value_num
    )


def to_item_data(source: AssembledItem) -> ItemData:
    return ItemData(
        code=source.code,
        label=source.label,
        magnitude=source.magnitude,
        attributes=[to_attribute_data(row) for row in source.attributes],
        related_codes=list(source.related_codes),
    )


def to_category_data(source: AssembledCategory) -> CategoryData:
    return CategoryData(
        code=source.code,
        label=source.label,
        children=[to_category_data(child) for child in source.children],
        items=[to_item_data(row) for row in source.items],
    )


def to_catalog_data(source: AssembledCatalog) -> CatalogData:
    """조립 결과를 응답 스키마로 변환합니다."""
    return CatalogData(
        release_id=source.release_id,
        release_label=source.release_label,
        lang=source.lang,
        category_count=source.category_count,
        item_count=source.item_count,
        roots=[to_category_data(node) for node in source.roots],
    )
