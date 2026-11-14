"""The chosen product profile uses every eligible quote, never a picked trio."""
from __future__ import annotations

from decimal import Decimal, InvalidOperation, localcontext

from .engine import _supplier_name, _verified_identity

TARGET_OFFERS = 3
CV_LIMIT = 33
PROFILE = "Три сопоставимых предложения по позиции, CV ≤ 33%"


def target_group(result):
    offers = [offer for offer in result.get("offers", [])
              if offer.get("status") == "included" and not offer.get("duplicate_of")
              and not offer.get("source_issues")]
    eligible, prices = [], []
    for offer in offers:
        try:
            price = Decimal(str(offer.get("normalized_price")))
        except (InvalidOperation, ValueError, TypeError):
            continue
        if price.is_finite() and price > 0:
            eligible.append(offer)
            prices.append(price)
    identities = {_verified_identity(o) or _supplier_name(o) for o in eligible} - {None, ""}
    independent = result.get("calculation", {}).get("independent_suppliers", len(identities))
    with localcontext() as context:
        context.prec = 36
        mean = sum(prices) / len(prices) if prices else None
        variance = sum((p - mean) ** 2 for p in prices) / (len(prices) - 1) if len(prices) > 1 else None
        cv = variance.sqrt() / mean * 100 if variance is not None and mean else None
    ready = (len(eligible) >= TARGET_OFFERS and independent >= TARGET_OFFERS
             and cv is not None and cv <= CV_LIMIT
             and result.get("calculation", {}).get("status") == "ready")
    if len(eligible) < TARGET_OFFERS:
        reason = f"Полностью сопоставимых предложений: {len(eligible)} из {TARGET_OFFERS}."
    elif independent < TARGET_OFFERS:
        reason = f"Установленных разных поставщиков: {independent} из {TARGET_OFFERS}."
    elif cv is None or cv > CV_LIMIT:
        reason = "Разброс всех сопоставимых предложений превышает CV 33%; причины различий требуют проверки."
    elif not ready:
        reason = result.get("calculation", {}).get("reason") or "Расчёт по позиции требует подтверждения."
    else:
        reason = "Выбранный профиль выполнен по всем сопоставимым предложениям; юридическая обоснованность не заявляется."
    return {"required": TARGET_OFFERS, "count": len(eligible), "ready": ready,
            "offer_ids": [o["id"] for o in eligible], "cv": round(float(cv), 8) if cv is not None else None,
            "cv_limit": CV_LIMIT, "reason": reason,
            "manual_count": sum(bool(o.get("manual_acceptance_fields") or o.get("manual_confirmations")) for o in eligible),
            "independent_suppliers": independent, "profile": PROFILE,
            "selected_values": [{"offer_id": o["id"], "price": float(p)} for o, p in zip(eligible, prices)]}


def annotate_target(result, *, enforce=False):
    group = target_group(result)
    result["target_group"] = group
    if enforce:
        calculation = result.setdefault("calculation", {})
        calculation.update(profile=PROFILE, cv_limit=CV_LIMIT, candidate_cv=group["cv"],
                           selected_offer_ids=list(group["offer_ids"]),
                           selected_values=list(group["selected_values"]))
        if not group["ready"]:
            calculation.update(status="insufficient_data", unit_price=None, total=None, reason=group["reason"])
        calculation["rule"] = "Выбранный профиль продукта: минимум три разных поставщика по каждой позиции; CV = выборочное стандартное отклонение / среднее × 100 ≤ 33%. Учитываются все сопоставимые предложения. Это не юридическая норма."
    return result
