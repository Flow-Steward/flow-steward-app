from __future__ import annotations

from collections import Counter

from core.application.management.query_models import (
    ExtensionMarketplaceFacetBucketView,
    ExtensionMarketplaceFacetsView,
    ExtensionMarketplaceFilterInput,
    ExtensionMarketplaceItemView,
)

from .service_support import _normalize_lower, _normalize_text


class ExtensionMarketplaceFilter:
    """Filter, order, paginate, and aggregate marketplace projections."""

    def matches(
        self, item: ExtensionMarketplaceItemView, query: ExtensionMarketplaceFilterInput
    ) -> bool:
        return (
            self._matches_search(item=item, query=query)
            and self._matches_identity(item=item, query=query)
            and self._matches_runtime(item=item, query=query)
            and self._matches_distribution(item=item, query=query)
            and self._matches_commerce(item=item, query=query)
        )

    @staticmethod
    def _values(raw: list[str]) -> set[str]:
        return {_normalize_lower(value) for value in raw if _normalize_lower(value)}

    @staticmethod
    def _matches_search(
        *, item: ExtensionMarketplaceItemView, query: ExtensionMarketplaceFilterInput
    ) -> bool:
        q_text = _normalize_lower(query.q)
        if not q_text:
            return True
        haystack = " ".join(
            [
                item.extension_id,
                item.display_name,
                item.description,
                item.vendor,
                item.category,
                item.channel,
                item.origin,
                item.source_label,
                " ".join(item.tags),
            ]
        ).lower()
        return all(token in haystack for token in q_text.split() if token)

    def _matches_identity(
        self, *, item: ExtensionMarketplaceItemView, query: ExtensionMarketplaceFilterInput
    ) -> bool:
        categories = self._values(query.category)
        vendors = self._values(query.vendor)
        tags = self._values(query.tags)
        return (
            (not categories or _normalize_lower(item.category) in categories)
            and (not vendors or _normalize_lower(item.vendor) in vendors)
            and (not tags or bool({_normalize_lower(tag) for tag in item.tags} & tags))
        )

    def _matches_runtime(
        self, *, item: ExtensionMarketplaceItemView, query: ExtensionMarketplaceFilterInput
    ) -> bool:
        contains = self._values(query.contains)
        channels = self._values(query.channel)
        execution_modes = self._values(query.execution_mode)
        if contains and not contains.issubset({_normalize_lower(kind) for kind in item.contains}):
            return False
        if channels and _normalize_lower(item.channel) not in channels:
            return False
        if execution_modes and not (
            {_normalize_lower(mode) for mode in item.execution_modes} & execution_modes
        ):
            return False
        if query.allows_self_hosted is not None and bool(item.allows_self_hosted) != bool(
            query.allows_self_hosted
        ):
            return False
        return not (
            query.requires_connected_control_plane is not None
            and bool(item.requires_connected_control_plane)
            != bool(query.requires_connected_control_plane)
        )

    def _matches_distribution(
        self, *, item: ExtensionMarketplaceItemView, query: ExtensionMarketplaceFilterInput
    ) -> bool:
        import_sources = self._values(query.import_source)
        compatibility = self._values(query.compatibility)
        origins = self._values(query.origin)
        if import_sources and not (
            {_normalize_lower(source) for source in item.import_sources} & import_sources
        ):
            return False
        if compatibility and _normalize_lower(item.compatibility) not in compatibility:
            return False
        return not (origins and _normalize_lower(item.origin) not in origins)

    def _matches_commerce(
        self, *, item: ExtensionMarketplaceItemView, query: ExtensionMarketplaceFilterInput
    ) -> bool:
        billing_modes = self._values(query.billing_mode)
        plan_ids = self._values(query.plan_id)
        if billing_modes and _normalize_lower(item.billing_mode) not in billing_modes:
            return False
        if plan_ids and _normalize_lower(item.plan_id) not in plan_ids:
            return False
        return not (query.installed is not None and bool(item.installed) != bool(query.installed))

    @staticmethod
    def sort(items: list[ExtensionMarketplaceItemView], sort: str) -> None:
        normalized = _normalize_lower(sort) or "updated_desc"
        if normalized == "updated_asc":
            items.sort(key=lambda row: (_normalize_text(row.updated_at), row.extension_id))
        elif normalized == "name_asc":
            items.sort(key=lambda row: (_normalize_lower(row.display_name), row.extension_id))
        elif normalized == "name_desc":
            items.sort(
                key=lambda row: (_normalize_lower(row.display_name), row.extension_id),
                reverse=True,
            )
        else:
            items.sort(
                key=lambda row: (_normalize_text(row.updated_at), row.extension_id), reverse=True
            )

    def facets(self, items: list[ExtensionMarketplaceItemView]) -> ExtensionMarketplaceFacetsView:
        counters = {name: Counter() for name in self._facet_names()}
        for item in items:
            self._count_scalar_facets(counters=counters, item=item)
            self._count_list_facets(counters=counters, item=item)
        return ExtensionMarketplaceFacetsView(
            category=self._buckets(counters["category"]),
            tags=self._buckets(counters["tags"]),
            vendor=self._buckets(counters["vendor"]),
            channel=self._buckets(counters["channel"]),
            execution_mode=self._buckets(counters["execution_mode"]),
            compatibility=self._buckets(counters["compatibility"]),
            origin=self._buckets(counters["origin"]),
            contains=self._buckets(counters["contains"]),
            billing_mode=self._buckets(counters["billing_mode"]),
            plan_id=self._buckets(counters["plan_id"]),
            import_source=self._buckets(counters["import_source"]),
        )

    @staticmethod
    def _facet_names() -> tuple[str, ...]:
        return (
            "category",
            "tags",
            "vendor",
            "channel",
            "execution_mode",
            "compatibility",
            "origin",
            "contains",
            "billing_mode",
            "plan_id",
            "import_source",
        )

    @staticmethod
    def _count_scalar_facets(
        *, counters: dict[str, Counter], item: ExtensionMarketplaceItemView
    ) -> None:
        for name, value in (
            ("category", item.category),
            ("vendor", item.vendor),
            ("channel", item.channel),
            ("compatibility", item.compatibility),
            ("origin", item.origin),
            ("billing_mode", item.billing_mode),
            ("plan_id", item.plan_id),
        ):
            if value:
                counters[name][value] += 1

    @staticmethod
    def _count_list_facets(
        *, counters: dict[str, Counter], item: ExtensionMarketplaceItemView
    ) -> None:
        for name, values in (
            ("tags", item.tags),
            ("execution_mode", item.execution_modes),
            ("contains", item.contains),
            ("import_source", item.import_sources),
        ):
            for value in values:
                if value:
                    counters[name][value] += 1

    @staticmethod
    def _buckets(counter: Counter) -> list[ExtensionMarketplaceFacetBucketView]:
        rows = sorted(counter.items(), key=lambda pair: (-pair[1], pair[0]))
        return [
            ExtensionMarketplaceFacetBucketView(value=value, count=count) for value, count in rows
        ]


__all__ = ["ExtensionMarketplaceFilter"]
