"""
Notion 資料庫存取（三表架構）。

餐點表（NOTION_DATABASE_ID）：只存餐點
  欄位：使用者ID(title)、暱稱(text)、日期時間(date)、食物明細(text)、
        總熱量(number)、總蛋白質(number)

體重表（NOTION_WEIGHT_DATABASE_ID）：只存體重，同一人同一天覆蓋為最新一筆
  欄位：使用者ID(title)、暱稱(text)、日期(date，只到日)、體重(number)

每日平均消耗熱量表（NOTION_BMR_DATABASE_ID）：每位使用者一列，代表當前每日平均消耗熱量(TDEE)
  欄位：使用者ID(title)、暱稱(text)、每日平均消耗熱量(number)、更新時間(date)
  （註：環境變數名沿用 NOTION_BMR_DATABASE_ID，內部變數仍用 bmr，僅顯示與欄位名正名）

多人分開：所有查詢都以「使用者ID equals 某人 LINE User ID」篩選，天然分開。
自動清除：purge_old_records() 刪除超過保留天數的餐點與體重（互動時順手呼叫）。
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


def _today_str():
    return _now_local().date().isoformat()


def _title_prop(user_id):
    return {"title": [{"text": {"content": user_id}}]}


def _text_prop(value):
    return {"rich_text": [{"text": {"content": (value or "")[:1900]}}]}


# ===========================================================================
# 餐點表
# ===========================================================================
def save_record(user_id, items, total_calories, total_protein, nickname=""):
    detail = "；".join(
        f"{it['name']} {it.get('calories', 0)}kcal/{it.get('protein', 0)}g"
        for it in items
    )
    payload = {
        "parent": {"database_id": Config.NOTION_DATABASE_ID},
        "properties": {
            "使用者ID": _title_prop(user_id),
            "暱稱": _text_prop(nickname),
            "日期時間": {"date": {"start": _now_local().isoformat()}},
            "食物明細": _text_prop(detail),
            "總熱量": {"number": total_calories},
            "總蛋白質": {"number": total_protein},
        },
    }
    r = requests.post(f"{BASE}/pages", headers=_headers(), json=payload, timeout=30)
    if r.status_code >= 300:
        print("Notion 餐點寫入失敗：", r.status_code, r.text)
        return False
    return True


def _query(database_id, filter_obj, page_size=100):
    """通用查詢，自動翻頁。"""
    results = []
    payload = {"page_size": page_size}
    if filter_obj:
        payload["filter"] = filter_obj
    url = f"{BASE}/databases/{database_id}/query"
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


def _query_meals(user_id, start_iso):
    return _query(Config.NOTION_DATABASE_ID, {
        "and": [
            {"property": "使用者ID", "title": {"equals": user_id}},
            {"property": "日期時間", "date": {"on_or_after": start_iso}},
        ]
    })


def _parse_meal(page):
    props = page.get("properties", {})
    date_obj = props.get("日期時間", {}).get("date") or {}
    date_start = date_obj.get("start")
    day = date_start[:10] if date_start else None
    cal = props.get("總熱量", {}).get("number") or 0
    pro = props.get("總蛋白質", {}).get("number") or 0
    return day, cal, pro


# ---- 營養統計 ----
def get_today_total(user_id):
    start = _now_local().replace(hour=0, minute=0, second=0, microsecond=0)
    rows = _query_meals(user_id, start.isoformat())
    total_cal = total_pro = meals = 0
    for p in rows:
        _, cal, pro = _parse_meal(p)
        total_cal += cal
        total_pro += pro
        meals += 1
    return {"calories": total_cal, "protein": total_pro, "meals": meals}


def _daily_average(user_id, start_dt):
    rows = _query_meals(user_id, start_dt.isoformat())
    per_day_cal = defaultdict(int)
    per_day_pro = defaultdict(int)
    for p in rows:
        day, cal, pro = _parse_meal(p)
        if day:
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


def get_daily_intake_map(user_id, days_back=9):
    start = (_now_local() - datetime.timedelta(days=days_back)).replace(
        hour=0, minute=0, second=0, microsecond=0)
    rows = _query_meals(user_id, start.isoformat())
    intake = defaultdict(int)
    for p in rows:
        day, cal, _ = _parse_meal(p)
        if day and cal:
            intake[day] += cal
    return dict(intake)


# ===========================================================================
# 體重表（同日覆蓋）
# ===========================================================================
def _find_weight_page(user_id, day_str):
    """找某人某天的體重紀錄頁（用於覆蓋）。"""
    rows = _query(Config.NOTION_WEIGHT_DATABASE_ID, {
        "and": [
            {"property": "使用者ID", "title": {"equals": user_id}},
            {"property": "日期", "date": {"equals": day_str}},
        ]
    }, page_size=1)
    return rows[0] if rows else None


def save_weight(user_id, weight_kg, nickname=""):
    """
    寫入體重：同一人同一天已有紀錄就『更新覆蓋』，否則新增。
    回傳 True/False。
    """
    day = _today_str()
    props = {
        "使用者ID": _title_prop(user_id),
        "暱稱": _text_prop(nickname),
        "日期": {"date": {"start": day}},
        "體重": {"number": weight_kg},
    }
    existing = _find_weight_page(user_id, day)
    if existing:
        r = requests.patch(f"{BASE}/pages/{existing['id']}",
                           headers=_headers(), json={"properties": props}, timeout=30)
    else:
        r = requests.post(f"{BASE}/pages", headers=_headers(),
                          json={"parent": {"database_id": Config.NOTION_WEIGHT_DATABASE_ID},
                                "properties": props}, timeout=30)
    if r.status_code >= 300:
        print("Notion 體重寫入失敗：", r.status_code, r.text)
        return False
    return True


def get_daily_weight_map(user_id, days_back=9):
    """回傳 {YYYY-MM-DD: 體重}。體重表已保證同日單筆。"""
    start = (_now_local().date() - datetime.timedelta(days=days_back)).isoformat()
    rows = _query(Config.NOTION_WEIGHT_DATABASE_ID, {
        "and": [
            {"property": "使用者ID", "title": {"equals": user_id}},
            {"property": "日期", "date": {"on_or_after": start}},
        ]
    })
    weight_map = {}
    for p in rows:
        props = p.get("properties", {})
        date_obj = props.get("日期", {}).get("date") or {}
        d = date_obj.get("start")
        w = props.get("體重", {}).get("number")
        if d and w is not None:
            weight_map[d[:10]] = w
    return weight_map


# ===========================================================================
# 代謝率表（每人一列，upsert）
# ===========================================================================
def _find_bmr_page(user_id):
    rows = _query(Config.NOTION_BMR_DATABASE_ID,
                  {"property": "使用者ID", "title": {"equals": user_id}}, page_size=1)
    return rows[0] if rows else None


def get_current_bmr(user_id):
    page = _find_bmr_page(user_id)
    if not page:
        return None
    return page.get("properties", {}).get("每日平均消耗熱量", {}).get("number")


def upsert_bmr(user_id, bmr_value, nickname=""):
    props = {
        "使用者ID": _title_prop(user_id),
        "暱稱": _text_prop(nickname),
        "每日平均消耗熱量": {"number": round(bmr_value)},
        "更新時間": {"date": {"start": _now_local().isoformat()}},
    }
    page = _find_bmr_page(user_id)
    if page:
        r = requests.patch(f"{BASE}/pages/{page['id']}",
                           headers=_headers(), json={"properties": props}, timeout=30)
    else:
        r = requests.post(f"{BASE}/pages", headers=_headers(),
                          json={"parent": {"database_id": Config.NOTION_BMR_DATABASE_ID},
                                "properties": props}, timeout=30)
    if r.status_code >= 300:
        print("Notion BMR 更新失敗：", r.status_code, r.text)
        return False
    return True


# ===========================================================================
# 蛋白質目標表（每人一列，存加權數）
# ===========================================================================
def _find_protein_page(user_id):
    rows = _query(Config.NOTION_PROTEIN_DATABASE_ID,
                  {"property": "使用者ID", "title": {"equals": user_id}}, page_size=1)
    return rows[0] if rows else None


def get_protein_factor(user_id):
    """回傳使用者的蛋白質加權數；未設定則 None。"""
    page = _find_protein_page(user_id)
    if not page:
        return None
    return page.get("properties", {}).get("加權數", {}).get("number")


def upsert_protein_factor(user_id, factor, nickname=""):
    props = {
        "使用者ID": _title_prop(user_id),
        "暱稱": _text_prop(nickname),
        "加權數": {"number": factor},
        "更新時間": {"date": {"start": _now_local().isoformat()}},
    }
    page = _find_protein_page(user_id)
    if page:
        r = requests.patch(f"{BASE}/pages/{page['id']}",
                           headers=_headers(), json={"properties": props}, timeout=30)
    else:
        r = requests.post(f"{BASE}/pages", headers=_headers(),
                          json={"parent": {"database_id": Config.NOTION_PROTEIN_DATABASE_ID},
                                "properties": props}, timeout=30)
    if r.status_code >= 300:
        print("Notion 蛋白質目標更新失敗：", r.status_code, r.text)
        return False
    return True


def get_latest_weight(user_id, lookback_days=90):
    """
    取使用者最近一次的體重紀錄，回傳 (weight, date_str) 或 (None, None)。
    往回找 lookback_days 天內最新的一筆。
    """
    start = (_now_local().date() - datetime.timedelta(days=lookback_days)).isoformat()
    rows = _query(Config.NOTION_WEIGHT_DATABASE_ID, {
        "and": [
            {"property": "使用者ID", "title": {"equals": user_id}},
            {"property": "日期", "date": {"on_or_after": start}},
        ]
    })
    latest_day = None
    latest_weight = None
    for p in rows:
        props = p.get("properties", {})
        date_obj = props.get("日期", {}).get("date") or {}
        d = date_obj.get("start")
        w = props.get("體重", {}).get("number")
        if d and w is not None:
            day = d[:10]
            if latest_day is None or day > latest_day:
                latest_day = day
                latest_weight = w
    return latest_weight, latest_day


# ===========================================================================
# 自動清除：刪除超過保留天數的餐點與體重
# ===========================================================================
def _archive_page(page_id):
    """Notion 沒有真正刪除 API，將頁面 archived=true 即等同移除。"""
    r = requests.patch(f"{BASE}/pages/{page_id}",
                       headers=_headers(), json={"archived": True}, timeout=30)
    return r.status_code < 300


def purge_old_records():
    """
    刪除所有使用者超過 DATA_RETENTION_DAYS 的餐點與體重紀錄。
    回傳實際刪除筆數。互動時順手呼叫（app.py 內有一天最多一次的節流）。
    """
    cutoff_date = _now_local().date() - datetime.timedelta(days=Config.DATA_RETENTION_DAYS)
    cutoff_iso = cutoff_date.isoformat()
    deleted = 0

    # 餐點：日期時間 before cutoff
    old_meals = _query(Config.NOTION_DATABASE_ID,
                       {"property": "日期時間", "date": {"before": cutoff_iso}})
    for p in old_meals:
        if _archive_page(p["id"]):
            deleted += 1

    # 體重：日期 before cutoff
    old_weights = _query(Config.NOTION_WEIGHT_DATABASE_ID,
                         {"property": "日期", "date": {"before": cutoff_iso}})
    for p in old_weights:
        if _archive_page(p["id"]):
            deleted += 1

    if deleted:
        print(f"自動清除：已移除 {deleted} 筆超過 {Config.DATA_RETENTION_DAYS} 天的紀錄")
    return deleted
