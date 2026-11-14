"""Named public-domain choices from the case; selection is not connectivity.

These presets limit the ordinary open-web research to selected domains. They
do not assert that a website is reachable or that an official API is connected.
"""
from .retrieval import SOURCES
from .source_policy import source_metadata


CASE_SOURCES = [
    ("eis", "ЕИС Закупки", "zakupki.gov.ru", "public_records"),
    ("gisp", "ГИСП", "gisp.gov.ru", "industry"),
    ("sber_ast", "Сбербанк-АСТ", "sberbank-ast.ru", "platforms"),
    ("rts", "РТС-тендер", "rts-tender.ru", "platforms"),
    ("roseltorg", "Росэлторг", "roseltorg.ru", "platforms"),
    ("etp_gpb", "ЭТП ГПБ", "etpgpb.ru", "platforms"),
    ("zakaz_rf", "Заказ РФ", "etp.zakazrf.ru", "platforms"),
    ("tek_torg", "ТЭК-Торг", "tektorg.ru", "platforms"),
    ("fabrikant", "Фабрикант", "fabrikant.ru", "platforms"),
    ("rad", "Российский аукционный дом", "lot-online.ru", "platforms"),
    ("berezka", "ЕАТ «Берёзка»", "agregatoreat.ru", "platforms"),
    ("mos_suppliers", "Портал поставщиков Москвы", "zakupki.mos.ru", "platforms"),
    ("etm", "ЭТМ", "etm.ru", "retail"),
    ("chipdip", "Чип и Дип", "chipdip.ru", "retail"),
    ("yandex_market", "Яндекс Маркет", "market.yandex.ru", "platforms"),
    ("ozon", "Ozon", "ozon.ru", "platforms"),
    ("vseinstrumenti", "ВсеИнструменты.ру", "vseinstrumenti.ru", "retail"),
    ("komus", "Комус", "komus.ru", "retail"),
]


def source_presets():
    """Preserve legacy IDs/connected flags while adding selectable case sites."""
    from .source_policy import GROUPS
    original = {row["id"]: row for row in SOURCES}
    result, seen = [], set()
    for identifier, name, domain, group in CASE_SOURCES:
        old = original.get(identifier, {})
        metadata = source_metadata(domain)
        result.append({**{k: v for k, v in old.items() if k != "search"},
            "id": identifier, "name": name, "domain": domain,
            "url": old.get("url") or "https://" + domain + "/",
            "kind": old.get("kind", "reference"),
            "description": "Поиск открытых страниц выбранного сайта. Доступность проверяется во время исследования.",
            **metadata, "source_group": group, "source_group_label": GROUPS[group],
            "source_group_basis": "Источник из справочника площадок",
            "connected": old.get("kind") in {"catalog", "supplier"},
            "selectable": True, "case_source": True})
        seen.add(identifier)
    for row in SOURCES:
        if row["id"] not in seen:
            result.append({**{k: v for k, v in row.items() if k != "search"},
                **source_metadata(row["domain"]),
                "connected": row["kind"] in {"catalog", "supplier"}, "selectable": True,
                "case_source": False})
    # The case names KORUS but supplies no unambiguous domain. Do not guess a
    # company or silently scope research to an unrelated namesake.
    result.append({"id": "korus", "name": "КОРУС", "domain": "", "url": "",
        "kind": "reference", "connected": False, "selectable": False, "case_source": True,
        "description": "В исходном списке не указан сайт. После уточнения добавьте его домен.",
        "source_group": "retail", "source_group_label": GROUPS["retail"],
        "source_group_basis": "Домен источника не установлен"})
    return result
