"""
一次性腳本：建立並上架 LINE Rich Menu（圖文選單），讓 6 個常用功能
常駐在聊天室輸入框下方。完全使用 LINE 官方功能，免費。

會自動用 Pillow 產生選單圖片，你不需要自備圖檔。

執行方式（設好 .env 後）：
    python rich_menu_setup.py

若要移除選單，執行：
    python rich_menu_setup.py --delete
"""
import io
import sys
import requests
from PIL import Image, ImageDraw, ImageFont
from config import Config

LINE_API = "https://api.line.me/v2/bot"
LINE_DATA_API = "https://api-data.line.me/v2/bot"

WIDTH, HEIGHT = 2500, 1686
COLS, ROWS = 3, 2
CELL_W = WIDTH // COLS
CELL_H = HEIGHT // ROWS

# (顯示文字, 送出的指令文字) — 指令需與 app.py 的關鍵字一致
BUTTONS = [
    ("今日總計", "今日"),
    ("本週平均", "本週"),
    ("本月平均", "本月"),
    ("上傳體重", "上傳體重"),
    ("計算代謝率", "計算基礎代謝率"),
    ("熱量盈餘", "熱量盈餘"),
]

COLORS = ["#2E7D32", "#388E3C", "#43A047",
          "#00897B", "#00796B", "#00695C"]


def _headers(json=True):
    h = {"Authorization": f"Bearer {Config.LINE_CHANNEL_ACCESS_TOKEN}"}
    if json:
        h["Content-Type"] = "application/json"
    return h


def _load_font(size):
    for path in [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    ]:
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            continue
    return ImageFont.load_default()


def _hex(c):
    c = c.lstrip("#")
    return tuple(int(c[i:i + 2], 16) for i in (0, 2, 4))


def build_menu_image():
    img = Image.new("RGB", (WIDTH, HEIGHT), (255, 255, 255))
    draw = ImageDraw.Draw(img)
    font = _load_font(90)
    for i, (label, _) in enumerate(BUTTONS):
        col, row = i % COLS, i // COLS
        x0, y0 = col * CELL_W, row * CELL_H
        x1, y1 = x0 + CELL_W, y0 + CELL_H
        draw.rectangle([x0 + 8, y0 + 8, x1 - 8, y1 - 8], fill=_hex(COLORS[i]))
        bbox = draw.textbbox((0, 0), label, font=font)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        draw.text((x0 + (CELL_W - tw) / 2, y0 + (CELL_H - th) / 2 - 20),
                  label, fill=(255, 255, 255), font=font)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=90)
    return buf.getvalue()


def build_areas():
    areas = []
    for i, (_, cmd) in enumerate(BUTTONS):
        col, row = i % COLS, i // COLS
        areas.append({
            "bounds": {"x": col * CELL_W, "y": row * CELL_H,
                       "width": CELL_W, "height": CELL_H},
            "action": {"type": "message", "text": cmd},
        })
    return areas


def delete_all():
    r = requests.get(f"{LINE_API}/richmenu/list", headers=_headers())
    for m in r.json().get("richmenus", []):
        requests.delete(f"{LINE_API}/richmenu/{m['richMenuId']}",
                        headers=_headers())
        print("已刪除選單：", m["richMenuId"])


def setup():
    # 1) 建立選單結構
    body = {
        "size": {"width": WIDTH, "height": HEIGHT},
        "selected": True,
        "name": "營養Bot主選單",
        "chatBarText": "功能選單",
        "areas": build_areas(),
    }
    r = requests.post(f"{LINE_API}/richmenu", headers=_headers(), json=body)
    r.raise_for_status()
    menu_id = r.json()["richMenuId"]
    print("已建立選單：", menu_id)

    # 2) 上傳圖片
    img_bytes = build_menu_image()
    r = requests.post(
        f"{LINE_DATA_API}/richmenu/{menu_id}/content",
        headers={"Authorization": f"Bearer {Config.LINE_CHANNEL_ACCESS_TOKEN}",
                 "Content-Type": "image/jpeg"},
        data=img_bytes,
    )
    r.raise_for_status()
    print("已上傳選單圖片")

    # 3) 設為所有使用者的預設選單
    r = requests.post(f"{LINE_API}/user/all/richmenu/{menu_id}",
                      headers=_headers())
    r.raise_for_status()
    print("已設為預設選單，完成！")


if __name__ == "__main__":
    Config.validate()
    if "--delete" in sys.argv:
        delete_all()
    else:
        delete_all()  # 先清舊的，避免重複
        setup()
