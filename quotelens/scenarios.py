"""Saved real public evidence and explicitly synthetic teaching scenarios."""
from __future__ import annotations

from copy import deepcopy
import base64
import json
from pathlib import Path

from .engine import parse_request


DATA = Path(__file__).resolve().parent.parent / "data"
OSC_DESCRIPTION = "Осциллограф цифровой, 4 канала, полоса не менее 100 МГц"


def _offer(identifier, price=100, supplier=None, attributes=None, unit="шт", pack_size=1, tax="included"):
    """Construct test data. These reserved domains never claim market evidence."""
    supplier = supplier or identifier
    attrs = {"model": "SYN-" + identifier.upper(), "channels": 4, "bandwidth_mhz": 100}
    if attributes is not None:
        attrs = deepcopy(attributes)
    url = f"https://{supplier}.example.invalid/synthetic/{identifier}"
    evidence = {"price": {"quote": f"СИНТЕТИКА: цена {price} RUB за {unit}", "url": url},
                "tax": {"quote": "СИНТЕТИКА: НДС включён" if tax == "included" else "СИНТЕТИКА: налог не установлен", "url": url},
                "pack_size": {"quote": f"СИНТЕТИКА: {pack_size} единиц потребности в коробке/упаковке", "url": url}}
    evidence.update({"attributes." + key: {"quote": f"СИНТЕТИКА: {key} = {value}", "url": url} for key, value in attrs.items() if value is not None})
    return {"id": "synthetic-" + identifier, "title": "Синтетический пример " + str(attrs.get("model", identifier)),
            "supplier": "Учебный поставщик " + supplier.upper() + " (синтетика)",
            "domain": f"{supplier}.example.invalid", "url": url,
            "retrieved_at": "2026-11-11T00:00:00Z", "price": price, "unit": unit,
            "pack_size": pack_size, "currency": "RUB", "tax": tax, "delivery": None,
            "attributes": attrs, "evidence": evidence, "provenance": "synthetic",
            "data_note": "Полностью выдуманные учебные данные. Дата служебная; рыночного получения не было."}


def _synthetic(request, offers):
    return {"request": request, "offers": offers, "source_statuses": [{"name": "Синтетические данные",
            "domain": "example.invalid", "status": "synthetic",
            "message": "Учебные цены и поставщики придуманы; поиск рынка не проводился."}],
            "kind": "synthetic", "captured_at": None}


def _build_synthetic():
    osc = parse_request(OSC_DESCRIPTION, 3)
    comparable = [_offer("a", 69000), _offer("b", 72000), _offer("c", 75000)]
    wrong_channels = _offer("two-channels", 30000, attributes={"model": "SYN-TWO", "channels": 2, "bandwidth_mhz": 100})
    wrong_band = _offer("low-band", 32000, attributes={"model": "SYN-LOW", "channels": 4, "bandwidth_mhz": 50})
    duplicate = _offer("a-mirror", 68000, supplier="a", attributes=comparable[0]["attributes"])
    mismatch = _offer("mismatch", 20000, attributes={"model": "АКИП-4131/1В", "channels": 4, "bandwidth_mhz": 100})
    mismatch["title"] = "Синтетическая ошибочная карточка ADS-2031"
    listing = _offer("listing", 25000, attributes={"is_listing": True, "channels": 4, "bandwidth_mhz": 100})
    missing = _offer("no-band", 100, attributes={"channels": 4, "bandwidth_mhz": None})
    no_tax = _offer("no-tax", 110, tax="unknown")
    no_price = _offer("no-price", None)
    paper = parse_request("Бумага офисная A4, плотность 80 г/м², 500 листов в пачке", 100)
    paper_attrs = {"format": "A4", "density_gsm": 80, "sheets_per_pack": 500}
    papers = [_offer("paper-a", 300, attributes=paper_attrs, unit="пачка", pack_size=500),
              _offer("paper-b", 1500, attributes=paper_attrs, unit="коробка", pack_size=5),
              _offer("paper-c", 640, attributes=paper_attrs, unit="коробка", pack_size=2),
              _offer("paper-wrong", 250, attributes={**paper_attrs, "density_gsm": 70}, unit="пачка", pack_size=500),
              _offer("paper-unknown-pack", 1700, attributes=paper_attrs, unit="коробка", pack_size=None)]
    service = parse_request("Уборка помещений, цена за м²", 100)
    services = [_offer("clean-" + name, price, attributes={"service_type": "уборка помещений"}, unit="м²")
                for name, price in [("a", 50), ("b", 55), ("c", 60)]]
    outliers = [_offer("iqr-" + str(index), price) for index, price in enumerate([100, 101, 102, 103, 104, 105, 1000], 1)]
    return {
        "synthetic-comparable": _synthetic(osc, comparable + [wrong_channels, wrong_band, duplicate, mismatch, listing]),
        "synthetic-missing": _synthetic(osc, [missing, no_tax, no_price]),
        "synthetic-paper": _synthetic(paper, papers),
        "synthetic-service": _synthetic(service, services),
        "synthetic-outlier": _synthetic(osc, outliers),
        "synthetic-one": _synthetic(osc, [_offer("only", 100)]),
    }


_SYNTHETIC = _build_synthetic()
_CATALOG = [
    {"id": "real-oscilloscopes", "title": "Реальный снимок — осциллографы",
     "description": "АКИП-4131/1В у трёх продавцов и более дешёвый несовместимый ADS-2031. Реальные страницы получены 11.11.2026; цены могли измениться.",
     "kind": "real_snapshot", "expected": "3 сопоставимых предложения по 69 065 ₽; ADS-2031 исключён по каналам, полосе и модели. Доставка неизвестна.", "category": "oscilloscope"},
    {"id": "synthetic-comparable", "title": "Синтетика — сопоставимость и дубликаты",
     "description": "Выдуманные предложения: три подходящих, две ошибки характеристик, дубликат, неверная модель и страница списка.",
     "kind": "synthetic", "expected": "3 включены; средняя цена 72 000 ₽, сумма для 3 шт. 216 000 ₽. Остальные исключены.", "category": "oscilloscope"},
    {"id": "synthetic-missing", "title": "Синтетика — неизвестные данные",
     "description": "Выдуманные карточки без обязательной полосы, с неизвестным налогом и без цены.",
     "kind": "synthetic", "expected": "Все предложения требуют уточнения; расчёт недостаточен, итог и CV отсутствуют.", "category": "oscilloscope"},
    {"id": "synthetic-paper", "title": "Синтетика — бумага и упаковки",
     "description": "Выдуманная бумага A4 80 г/м², 500 листов в пачке: цена за пачку и коробку, другая плотность и неизвестная упаковка.",
     "kind": "synthetic", "expected": "Цены принятых: 300, 300 и 320 ₽ за пачку; 100 пачек = 30 666,67 ₽. Неверная плотность исключена, неизвестная коробка требует уточнения.", "category": "paper"},
    {"id": "synthetic-service", "title": "Синтетика — базовая услуга",
     "description": "Выдуманные одинаковые услуги уборки с ценой за м². Расширенные условия обслуживания не моделируются.",
     "kind": "synthetic", "expected": "3 предложения; средняя цена 55 ₽/м², сумма для 100 м² — 5 500 ₽.", "category": "service"},
    {"id": "synthetic-outlier", "title": "Синтетика — высокая цена и IQR",
     "description": "Семь выдуманных сопоставимых цен; одна намного выше остальных. Диагностика не удаляет наблюдения.",
     "kind": "synthetic", "expected": "Все 7 цен включены. IQR отмечает высокую цену, расчёт и чувствительность сохраняют её влияние.", "category": "oscilloscope"},
    {"id": "synthetic-one", "title": "Синтетика — один поставщик",
     "description": "Одно выдуманное подходящее предложение; это не достаточный набор по профилю из трёх поставщиков.",
     "kind": "synthetic", "expected": "n = 1, выборочное отклонение и CV отсутствуют, итог не рассчитан.", "category": "oscilloscope"},
]


def list_scenarios() -> list[dict]:
    return deepcopy(_CATALOG)


def attach_real_images(offers: list[dict]) -> list[dict]:
    """Attach captured actual product photos, keeping their source provenance."""
    manifest = DATA / "images/manifest.json"
    if not manifest.exists():
        return offers
    images = json.loads(manifest.read_text())
    def key(value):
        return str(value).upper().replace("АКИП", "AKIP").replace("В", "B").replace(" ", "")
    for offer in offers:
        if offer.get("provenance") == "synthetic":
            continue
        model = key(offer.get("attributes", {}).get("model", ""))
        photo = next((i for i in images if key(i["model"]) == model), None)
        if photo:
            file = DATA / "images" / photo["file"]
            offer["image"] = "data:" + photo["mime"] + ";base64," + base64.b64encode(file.read_bytes()).decode()
            offer["image_url"] = photo["url"]
            offer["image_source_url"] = photo["source_page"]
    return offers


def load_scenario(identifier: str) -> dict:
    if identifier == "real-oscilloscopes":
        data = json.loads((DATA / "real-oscilloscopes.json").read_text(encoding="utf-8"))
        data["request"] = parse_request(data.pop("request_description"), data.pop("quantity"))
        attach_real_images(data["offers"])
        return data
    if identifier in _SYNTHETIC:
        return deepcopy(_SYNTHETIC[identifier])
    raise ValueError("Неизвестный сценарий.")
