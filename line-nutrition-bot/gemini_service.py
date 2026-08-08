"""
呼叫 Gemini：
  1) analyze_image()      — 辨識食物，找出需與使用者確認的項目
  2) estimate_nutrition() — 依最終食物清單估算各項與總計的熱量/蛋白質

強化：
  - 圖片過大時自動壓縮（省流量、加快辨識）
  - Gemini 偶爾回非 JSON 時自動重試一次
"""
import io
import json
import google.generativeai as genai
from PIL import Image
from config import Config

genai.configure(api_key=Config.GEMINI_API_KEY)

MAX_EDGE = 1024  # 圖片最長邊壓到此像素以內


def _get_model():
    return genai.GenerativeModel(Config.GEMINI_MODEL)


def compress_image(image_bytes):
    """把過大圖片縮小並轉為 JPEG，回傳 (bytes, mime)。失敗則原樣回傳。"""
    try:
        img = Image.open(io.BytesIO(image_bytes))
        img = img.convert("RGB")
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
    """呼叫 Gemini 並解析 JSON；失敗自動重試一次。"""
    model = _get_model()
    last_err = None
    for attempt in range(2):
        try:
            resp = model.generate_content(parts)
            return _extract_json(resp.text)
        except (ValueError, json.JSONDecodeError) as e:
            last_err = e
            print(f"Gemini JSON 解析失敗（第 {attempt + 1} 次），重試中：", e)
    raise last_err


# ---------------------------------------------------------------------------
ANALYZE_PROMPT = """你是一位營養師助理。請辨識這張餐點照片中的所有食物。

規則：
1. 列出你能辨識的每一項食物。
2. 對於「無法確定種類」的食物（例如白色方塊可能是豆腐/起司/魚板；折起來的蛋可能是純煎蛋/蛋餅/包餡蛋），
   不要自己猜，要把它放進 questions，並提供 2~4 個可能選項讓使用者選。
3. 份量請以照片目測估計（例如「約 2 片」「約 150 克」）。

請「只」回傳以下 JSON 格式，不要有其他文字：
{
  "items": [
    {"name": "食物名稱", "portion": "目測份量", "certain": true}
  ],
  "questions": [
    {
      "item_ref": "照片中該食物的簡短描述（例如：白色方塊）",
      "question": "要問使用者的問題",
      "options": ["選項1", "選項2", "選項3"]
    }
  ]
}
若沒有任何需要確認的項目，questions 請回傳空陣列 []。"""


def analyze_image(image_bytes, mime_type="image/jpeg"):
    data, mime = compress_image(image_bytes)
    return _generate_json([
        ANALYZE_PROMPT,
        {"mime_type": mime, "data": data},
    ])


# ---------------------------------------------------------------------------
ESTIMATE_PROMPT_TEMPLATE = """你是一位營養師。以下是一份餐點的最終食物清單（已與使用者確認）：

{food_list}

請估算每一項的熱量(kcal)與蛋白質(克)，並計算總計。數值以合理估計即可，取整數。

請「只」回傳以下 JSON 格式，不要有其他文字：
{{
  "items": [
    {{"name": "食物名稱", "portion": "份量", "calories": 整數, "protein": 整數}}
  ],
  "total_calories": 整數,
  "total_protein": 整數
}}"""


def estimate_nutrition(food_list):
    lines = []
    for f in food_list:
        portion = f.get("portion", "")
        lines.append(f"- {f['name']}（{portion}）" if portion else f"- {f['name']}")
    prompt = ESTIMATE_PROMPT_TEMPLATE.format(food_list="\n".join(lines))
    return _generate_json(prompt)
