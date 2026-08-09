"""
基礎代謝率（BMR，此處近似為每日總消耗 TDEE）估算 + 熱量盈餘計算。

── BMR 估算邏輯（使用者指定的「區間夾擠法」）──────────────────────────
前提：某天的體重變化，反映的是「前一天」攝取與消耗的落差；
      每日消耗在此近似為代謝率 BMR。

對過去 7 天內、每一天 d：
  需要 weight(d)、weight(d-1)（判斷體重升降）與 intake(d-1)（前一日總攝取）。
  - 若 weight(d) < weight(d-1)（變輕）→ 前一天 消耗>攝取 → BMR > intake(d-1)
        得到一個「下界」x = intake(d-1)
  - 若 weight(d) > weight(d-1)（變重）→ 前一天 消耗<攝取 → BMR < intake(d-1)
        得到一個「上界」y = intake(d-1)
  - 體重持平或任一資料缺失 → 該天跳過

統計時：
  lower = max(所有下界 x)   # 最緊的下界
  upper = min(所有上界 y)   # 最緊的上界
  BMR ≈ (lower + upper) / 2

例外處理：
  - 有效比較天數 < BMR_MIN_DAYS → 資料不足
  - lower > upper（區間矛盾，多因水分波動）→ 不給數字，請使用者累積更多天
  - 只有下界 → 只能推估「BMR 大於 lower」
  - 只有上界 → 只能推估「BMR 小於 upper」
"""
import datetime
from config import Config
import notion_service


def _prev_day(day_str):
    d = datetime.date.fromisoformat(day_str)
    return (d - datetime.timedelta(days=1)).isoformat()


def compute_week_bmr(user_id):
    """
    回傳 dict，status 為下列之一：
      "ok"            → value：估算的 BMR
      "lower_only"    → lower：只能推估「大於」此值
      "upper_only"    → upper：只能推估「小於」此值
      "insufficient"  → days：目前有效天數（< 最低要求）
      "contradiction" → lower/upper：矛盾區間
    另含 detail 供顯示。
    """
    weight_map = notion_service.get_daily_weight_map(user_id, days_back=9)
    intake_map = notion_service.get_daily_intake_map(user_id, days_back=9)

    # 只看過去 7 天（含今天）的日期
    tz = datetime.timezone(datetime.timedelta(hours=Config.TIMEZONE_OFFSET))
    today = datetime.datetime.now(tz).date()
    recent_days = {(today - datetime.timedelta(days=i)).isoformat()
                   for i in range(0, 7)}

    lowers = []  # 下界 x（BMR 大於這些值）
    uppers = []  # 上界 y（BMR 小於這些值）
    valid = 0

    for day in sorted(recent_days):
        prev = _prev_day(day)
        # 需要今天與昨天的體重、以及昨天的攝取
        if day not in weight_map or prev not in weight_map:
            continue
        if prev not in intake_map:
            continue

        w_today = weight_map[day]
        w_prev = weight_map[prev]
        prev_intake = intake_map[prev]

        if w_today < w_prev:          # 變輕 → BMR > prev_intake
            lowers.append(prev_intake)
            valid += 1
        elif w_today > w_prev:        # 變重 → BMR < prev_intake
            uppers.append(prev_intake)
            valid += 1
        # 持平則跳過

    # 有效天數不足
    if valid < Config.BMR_MIN_DAYS:
        return {"status": "insufficient", "days": valid,
                "need": Config.BMR_MIN_DAYS}

    has_lower = len(lowers) > 0
    has_upper = len(uppers) > 0

    if has_lower and has_upper:
        lower = max(lowers)
        upper = min(uppers)
        if lower > upper:
            return {"status": "contradiction",
                    "lower": round(lower), "upper": round(upper)}
        return {"status": "ok",
                "value": round((lower + upper) / 2),
                "lower": round(lower), "upper": round(upper)}
    if has_lower:
        return {"status": "lower_only", "lower": round(max(lowers))}
    return {"status": "upper_only", "upper": round(min(uppers))}


# ---------------------------------------------------------------------------
# 熱量盈餘：當日攝取 − 當前代謝率
# 缺資料時明確回報缺哪一項（依使用者需求：需檢查 當日攝取/當日體重/當前代謝率）
# ---------------------------------------------------------------------------
def compute_today_surplus(user_id):
    """
    回傳 dict：
      status="ok"      → intake / bmr / surplus
      status="missing" → missing：缺少的項目清單（中文）
    """
    tz = datetime.timezone(datetime.timedelta(hours=Config.TIMEZONE_OFFSET))
    today = datetime.datetime.now(tz).date().isoformat()

    intake_map = notion_service.get_daily_intake_map(user_id, days_back=1)
    weight_map = notion_service.get_daily_weight_map(user_id, days_back=1)
    bmr = notion_service.get_current_bmr(user_id)

    missing = []
    today_intake = intake_map.get(today)
    if not today_intake:
        missing.append("今日的餐點熱量紀錄")
    if today not in weight_map:
        missing.append("今日的體重紀錄")
    if bmr is None:
        missing.append("當前的每日平均消耗熱量（請先執行一次『計算每日平均消耗熱量』）")

    if missing:
        return {"status": "missing", "missing": missing}

    surplus = today_intake - bmr
    return {"status": "ok",
            "intake": today_intake, "bmr": bmr, "surplus": surplus}
