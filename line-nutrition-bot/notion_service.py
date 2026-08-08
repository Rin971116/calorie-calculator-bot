"""
Notion 資料庫存取。

主表（NOTION_DATABASE_ID）：餐點紀錄 + 每日體重共用。
  欄位：使用者ID(title)、日期時間(date)、食物明細(text)、
        總熱量(number)、總蛋白質(number)、體重(number)
  - 一筆「餐點」紀錄：食物明細/總熱量/總蛋白質有值，體重留空
  - 一筆「體重」紀錄：體重有值，其餘留空

代謝率表（NOTION_BMR_DATABASE_ID）：每位使用者一列，代表「當前代謝率(TDEE)」。
  欄位：使用者ID(title)、代謝率(number)、更新時間(date)
"""
import datetime
import requests
from collections import defaultdict
from config import Config

NOTION_VERSION = "2022-06-28"
BASE = "https://api.notion.com/v1"


def _headers():
    return {
        "Authorization": f"Bearer {Config.NOTION_API_KEY}",
        "Content-Type": "application/json",
        "Notion-Version": NOTION_VERSION,
    }


def _now_local():
    tz = datetime.timezone(datetime.timedelta(hours=Config.TIMEZONE_OFFSET))
    return datetime.datetime.now(tz)


# ===========================================================================
# 主表：寫入
# ===========================================================================
def save_record(user_id, items, total_calories, total_protein):
    """寫入一筆餐點紀錄。"""
    detail = "；".join(
        f"{it['name']} {it.get('calories', 0)}kcal/{it.get('protein', 0)}g"
        for it in items
    )
    now = _now_local()
    payload = {
        "parent": {"database_id": Config.NOTION_DATABASE_ID},
        "properties": {
            "使用者ID": {"title": [{"text": {"content": user_id}}]},
            "日期時間": {"date": {"start": now.isoformat()}},
            "食物明細": {"rich_text": [{"text": {"content": detail[:1900]}}]},
            "總熱量": {"number": total_calories},
            "總蛋白質": {"number": total_protein},
        },
    }
    r = requests.post(f"{BASE}/pages", headers=_headers(), json=payload, timeout=30)
    if r.status_code >= 300:
        print("Notion 餐點寫入失敗：", r.status_code, r.text)
        return False
    return True


def save_weight(user_id, weight_kg):
    """寫入一筆每日體重紀錄。"""
    now = _now_local()
    payload = {
        "parent": {"database_id": Config.NOTION_DATABASE_ID},
        "properties": {
            "使用者ID": {"title": [{"text": {"content": user_id}}]},
            "日期時間": {"date": {"start": now.isoformat()}},
            "體重": {"number": weight_kg},
        },
    }
    r = requests.post(f"{BASE}/pages", headers=_headers(), json=payload, timeout=30)
    if r.status_code >= 300:
        print("Notion 體重寫入失敗：", r.status_code, r.text)
        return False
    return True


# ===========================================================================
# 主表：查詢
# ===========================================================================
def _query_records(user_id, start_iso):
    """查詢某使用者自 start_iso 起的所有主表紀錄。"""
    results = []
    payload = {
        "filter": {
            "and": [
                {"property": "使用者ID", "title": {"equals": user_id}},
                {"property": "日期時間", "date": {"on_or_after": start_iso}},
            ]
        },
        "page_size": 100,
    }
    url = f"{BASE}/databases/{Config.NOTION_DATABASE_ID}/query"
    while True:
        r = requests.post(url, headers=_headers(), json=payload, timeout=30)
        if r.status_code >= 300:
            print("Notion 查詢失敗：", r.status_code, r.text)
            break
        data = r.json()
        results.extend(data.get("results", []))
        if data.get("has_more"):
            payload["start_cursor"] = data["next_cursor"]
        else:
            break
    return results


def _parse_row(page):
    """取出 日期(YYYY-MM-DD)、熱量、蛋白質、體重。"""
    props = page.get("properties", {})
    date_obj = props.get("日期時間", {}).get("date") or {}
    date_start = date_obj.get("start")
    day = date_start[:10] if date_start else None
    cal = props.get("總熱量", {}).get("number") or 0
    pro = props.get("總蛋白質", {}).get("number") or 0
    weight = props.get("體重", {}).get("number")  # 可能為 None
    return day, cal, pro, weight


# ---- 給營養統計用 ----
def get_today_total(user_id):
    now = _now_local()
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    rows = _query_records(user_id, start.isoformat())
    total_cal = total_pro = meals = 0
    for p in rows:
        _, cal, pro, weight = _parse_row(p)
        if cal or pro:  # 只算餐點
            total_cal += cal
            total_pro += pro
            meals += 1
    return {"calories": total_cal, "protein": total_pro, "meals": meals}


def _daily_average(user_id, start_dt):
    rows = _query_records(user_id, start_dt.isoformat())
    per_day_cal = defaultdict(int)
    per_day_pro = defaultdict(int)
    for p in rows:
        day, cal, pro, _ = _parse_row(p)
        if day and (cal or pro):
            per_day_cal[day] += cal
            per_day_pro[day] += pro
    days = len(per_day_cal)
    if days == 0:
        return {"calories": 0, "protein": 0, "days": 0}
    return {
        "calories": round(sum(per_day_cal.values()) / days),
        "protein": round(sum(per_day_pro.values()) / days),
        "days": days,
    }


def get_week_average(user_id):
    now = _now_local()
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    start = start - datetime.timedelta(days=start.weekday())
    return _daily_average(user_id, start)


def get_month_average(user_id):
    now = _now_local()
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return _daily_average(user_id, start)


# ---- 給 BMR / 熱量盈餘用：每日攝取與每日體重 ----
def get_daily_intake_map(user_id, days_back=9):
    """
    回傳 {YYYY-MM-DD: 當日總攝取熱量} —— 用於 BMR 與熱量盈餘。
    多取幾天（預設 9）以便算「前一日」。
    """
    now = _now_local()
    start = (now - datetime.timedelta(days=days_back)).replace(
        hour=0, minute=0, second=0, microsecond=0)
    rows = _query_records(user_id, start.isoformat())
    intake = defaultdict(int)
    for p in rows:
        day, cal, _, _ = _parse_row(p)
        if day and cal:
            intake[day] += cal
    return dict(intake)


def get_daily_weight_map(user_id, days_back=9):
    """
    回傳 {YYYY-MM-DD: 當日體重}。同一天多筆取最後一筆。
    """
    now = _now_local()
    start = (now - datetime.timedelta(days=days_back)).replace(
        hour=0, minute=0, second=0, microsecond=0)
    rows = _query_records(user_id, start.isoformat())
    # 依日期時間排序，確保同日取最後一筆
    parsed = []
    for p in rows:
        props = p.get("properties", {})
        date_obj = props.get("日期時間", {}).get("date") or {}
        date_start = date_obj.get("start")
        weight = props.get("體重", {}).get("number")
        if date_start and weight is not None:
            parsed.append((date_start, date_start[:10], weight))
    parsed.sort(key=lambda x: x[0])
    weight_map = {}
    for _, day, w in parsed:
        weight_map[day] = w  # 後者覆蓋前者 → 當日最後一筆
    return weight_map


# ===========================================================================
# 代謝率表：讀取 / upsert（每位使用者唯一一列）
# ===========================================================================
def _find_bmr_page(user_id):
    payload = {
        "filter": {"property": "使用者ID", "title": {"equals": user_id}},
        "page_size": 1,
    }
    url = f"{BASE}/databases/{Config.NOTION_BMR_DATABASE_ID}/query"
    r = requests.post(url, headers=_headers(), json=payload, timeout=30)
    if r.status_code >= 300:
        print("Notion BMR 查詢失敗：", r.status_code, r.text)
        return None
    results = r.json().get("results", [])
    return results[0] if results else None


def get_current_bmr(user_id):
    """回傳當前代謝率數值；無則 None。"""
    page = _find_bmr_page(user_id)
    if not page:
        return None
    return page.get("properties", {}).get("代謝率", {}).get("number")


def upsert_bmr(user_id, bmr_value):
    """更新（或建立）該使用者代謝率表的唯一一列。"""
    now = _now_local()
    props = {
        "使用者ID": {"title": [{"text": {"content": user_id}}]},
        "代謝率": {"number": round(bmr_value)},
        "更新時間": {"date": {"start": now.isoformat()}},
    }
    page = _find_bmr_page(user_id)
    if page:
        r = requests.patch(
            f"{BASE}/pages/{page['id']}",
            headers=_headers(), json={"properties": props}, timeout=30,
        )
    else:
        r = requests.post(
            f"{BASE}/pages",
            headers=_headers(),
            json={"parent": {"database_id": Config.NOTION_BMR_DATABASE_ID},
                  "properties": props},
            timeout=30,
        )
    if r.status_code >= 300:
        print("Notion BMR 更新失敗：", r.status_code, r.text)
        return False
    return True
