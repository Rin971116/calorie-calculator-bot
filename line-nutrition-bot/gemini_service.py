"""
呼叫 Gemini：
  1) analyze_image()       — 辨識照片中「所有」品項，回傳清單（名稱/數量/量詞）
  2) apply_corrections()   — 依使用者的自由文字更正，回傳更新後的品項清單
  3) estimate_nutrition()  — 依最終確認的品項清單，估算各項與總計的熱量/蛋白質

流程改為「列點核對」：先列出所有品項給使用者確認，使用者可自由文字更正
（改名稱、改數量、新增、刪除），確認無誤後才估算與存檔。

強化：
  - 圖片過大自動壓縮
  - Gemini 偶爾回非 JSON 時自動重試一次
"""
import io
import json
import google.generativeai as genai
from PIL import Image
from config import Config

genai.configure(api_key=Config.GEMINI_API_KEY)

MAX_EDGE = 1024


def _get_model():
    return genai.GenerativeModel(Config.GEMINI_MODEL)


def compress_image(image_bytes):
    try:
        img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        w, h = img.size
        if max(w, h) > MAX_EDGE:
            scale = MAX_EDGE / max(w, h)
            img = img.resize((int(w * scale), int(h * scale)))
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=85)
        return buf.getvalue(), "image/jpeg"
    except Exception as e:
        print("圖片壓縮失敗，使用原圖：", e)
        return image_bytes, "image/jpeg"


def _extract_json(text):
    text = text.strip()
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("非 JSON 格式：" + text[:200])
    return json.loads(text[start:end + 1])


def _generate_json(parts):
    model = _get_model()
    last_err = None
    for attempt in range(2):
        try:
            resp = model.generate_content(parts)
            return _extract_json(resp.text)
        except (ValueError, json.JSONDecodeError) as e:
            last_err = e
            print(f"Gemini JSON 解析失敗（第 {attempt + 1} 次），重試：", e)
    raise last_err


# ---------------------------------------------------------------------------
# 1) 辨識照片：列出所有品項
# ---------------------------------------------------------------------------
ANALYZE_PROMPT = """你是一位專業營養師助理。請仔細辨識這張餐點照片中的「每一項」食物，不要遺漏任何主菜、配菜、主食、水果、飲料。

要求：
1. 盡可能具體辨識（例如分辨「煎豆腐」「煎雞胸」「歐姆蛋」，而非籠統的「白色方塊」）。
2. 若真的無法確定某項的種類，name 就填你最可能的猜測，並在後面加註「(不確定)」。
3. 每項都要估計數量與量詞（片/塊/份/顆/碗/杯等）。
4. 沙拉、拼盤等可視為一項（例如「生菜沙拉」），不需拆到每片菜葉。

請「只」回傳以下 JSON，不要有其他文字：
{
  "items": [
    {"name": "食物名稱", "quantity": 數字, "unit": "量詞"}
  ]
}"""


def analyze_image(image_bytes, mime_type="image/jpeg"):
    data, mime = compress_image(image_bytes)
    result = _generate_json([ANALYZE_PROMPT, {"mime_type": mime, "data": data}])
    return result.get("items", [])


# ---------------------------------------------------------------------------
# 2) 套用使用者的自由文字更正
# ---------------------------------------------------------------------------
CORRECTION_PROMPT_TEMPLATE = """你是一位協助校對食物清單的助理。以下是目前的品項清單（JSON）：

{current_items}

使用者提出以下更正指示（自由文字，可能包含改名稱、改數量、新增品項、刪除品項，且可能一次多項）：
「{user_text}」

請依指示更新清單。規則：
- 「第N項」指清單中第 N 個品項（從 1 開始）。
- 改名稱：更新該項 name。
- 改數量：更新該項 quantity（與 unit，如有提到）。
- 新增：在清單末端加入新品項。
- 刪除：移除該項。
- 沒被提到的品項保持不變。

請「只」回傳更新後的完整清單 JSON，不要有其他文字：
{{
  "items": [
    {{"name": "食物名稱", "quantity": 數字, "unit": "量詞"}}
  ]
}}"""


def apply_corrections(current_items, user_text):
    prompt = CORRECTION_PROMPT_TEMPLATE.format(
        current_items=json.dumps(current_items, ensure_ascii=False),
        user_text=user_text,
    )
    result = _generate_json(prompt)
    return result.get("items", [])


# ---------------------------------------------------------------------------
# 3) 估算營養
# ---------------------------------------------------------------------------
ESTIMATE_PROMPT_TEMPLATE = """你是一位營養師。以下是一份餐點的最終品項清單（已與使用者核對確認）：

{food_list}

請估算每一項的熱量(kcal)與蛋白質(克)，並計算總計。數值以合理估計即可，取整數。

請「只」回傳以下 JSON，不要有其他文字：
{{
  "items": [
    {{"name": "食物名稱", "portion": "數量與量詞", "calories": 整數, "protein": 整數}}
  ],
  "total_calories": 整數,
  "total_protein": 整數
}}"""


def estimate_nutrition(items):
    lines = []
    for f in items:
        qty = f.get("quantity", "")
        unit = f.get("unit", "")
        portion = f"{qty}{unit}".strip()
        lines.append(f"- {f['name']}（{portion}）" if portion else f"- {f['name']}")
    prompt = ESTIMATE_PROMPT_TEMPLATE.format(food_list="\n".join(lines))
    return _generate_json(prompt)
