"""
LINE 食物營養紀錄 Bot — 主程式

照片流程（列點核對版）：
  1. 傳食物照片 → 辨識所有品項 → 列點給使用者核對
  2. 使用者自由文字更正（改名稱/改數量/新增/刪除），或按「正確」
  3. 每次更正後重新列點，循環直到使用者確認正確
  4. 確認後 → 估算熱量與蛋白質 → 回傳結果 → 寫入 Notion

其他功能：營養統計、上傳體重、計算基礎代謝率、熱量盈餘。

例外處理：
  - 核對/確認流程 session 遺失（重啟/逾時 15 分鐘）→ 明確告知並請重新上傳照片
  - 體重輸入非數字 → 提示格式
  - BMR / 熱量盈餘缺資料 → 明確回報缺哪一項
"""
import datetime
from flask import Flask, request, abort

from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    Configuration, ApiClient, MessagingApi, MessagingApiBlob,
    ReplyMessageRequest, TextMessage,
    QuickReply, QuickReplyItem, MessageAction,
)
from linebot.v3.webhooks import (
    MessageEvent, TextMessageContent, ImageMessageContent,
)

from config import Config
import gemini_service
import notion_service
import bmr_service
from session import session_store

Config.validate()

# --- 自動清除節流：全服務一天最多實際跑一次 purge ---
_last_purge_day = {"value": None}


def maybe_purge():
    """互動時順手觸發；同一天只實際清一次，避免每則訊息都掃資料庫。"""
    tz = datetime.timezone(datetime.timedelta(hours=Config.TIMEZONE_OFFSET))
    today = datetime.datetime.now(tz).date().isoformat()
    if _last_purge_day["value"] == today:
        return
    _last_purge_day["value"] = today  # 先標記，避免並發重複觸發
    try:
        notion_service.purge_old_records()
    except Exception as e:
        print("自動清除失敗（略過）：", e)

app = Flask(__name__)
line_config = Configuration(access_token=Config.LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(Config.LINE_CHANNEL_SECRET)

# 指令關鍵字
STAT_KEYWORDS = {
    # 今日（新名稱 + 舊指令）
    "今日統計": "today", "今日": "today", "今天": "today",
    # 本週
    "本週統計": "week", "本週": "week", "這週": "week",
    # 本月
    "本月統計": "month", "本月": "month", "這個月": "month",
}
CMD_UPLOAD_WEIGHT = {"上傳今日體重", "上傳體重", "輸入體重", "體重"}
CMD_CALC_BMR = {"計算並設定基礎代謝率", "計算基礎代謝率", "基礎代謝率",
                "計算代謝率", "bmr", "BMR"}
CMD_SURPLUS = {"今日熱量盈餘", "熱量盈餘", "今日盈餘", "計算盈餘"}

# 核對階段：確認正確 / 取消 的關鍵字
CONFIRM_WORDS = {"正確", "對", "ok", "OK", "Ok", "沒問題", "無誤", "正确"}
CANCEL_WORDS = {"取消", "算了", "不用了", "不用", "重傳", "重新上傳", "cancel", "Cancel"}

# 主選單按鈕（顯示＝送出指令）。順序：上傳今日體重、今日熱量盈餘、
# 今日統計、本週統計、本月統計、計算並設定基礎代謝率
MAIN_QUICK = ["上傳今日體重", "今日熱量盈餘", "今日統計",
              "本週統計", "本月統計", "計算並設定基礎代謝率"]
# 核對階段的快捷按鈕
REVIEW_QUICK = ["正確", "取消"]
# 等待輸入時（例如體重）顯示的取消按鈕
CANCEL_QUICK = ["取消"]
# 「是否計入紀錄」的按鈕與關鍵字
SAVE_QUICK = ["是，記錄", "否，只是查詢"]
SAVE_YES_WORDS = {"是，記錄", "是", "記錄", "要", "好", "yes", "Yes"}
SAVE_NO_WORDS = {"否，只是查詢", "否", "不是", "不要", "只是查詢", "查詢", "no", "No"}


# ---------------------------------------------------------------------------
# 回覆工具
# ---------------------------------------------------------------------------
def reply(reply_token, messages):
    with ApiClient(line_config) as api_client:
        MessagingApi(api_client).reply_message(
            ReplyMessageRequest(reply_token=reply_token, messages=messages)
        )


def text_msg(text, quick_options=None):
    qr = None
    if quick_options:
        items = [QuickReplyItem(action=MessageAction(label=o[:20], text=o))
                 for o in quick_options]
        qr = QuickReply(items=items)
    return TextMessage(text=text, quick_reply=qr)


def download_image(message_id):
    with ApiClient(line_config) as api_client:
        return bytes(MessagingApiBlob(api_client).get_message_content(message_id))


def get_nickname(user_id):
    """取得使用者的 LINE 顯示名稱；失敗則回空字串（不影響主流程）。"""
    try:
        with ApiClient(line_config) as api_client:
            profile = MessagingApi(api_client).get_profile(user_id)
            return profile.display_name or ""
    except Exception as e:
        print("取得暱稱失敗（略過）：", e)
        return ""


# ---------------------------------------------------------------------------
# 照片流程：列點核對
# ---------------------------------------------------------------------------
def format_item_list(items):
    """把品項清單排成給使用者核對的文字。"""
    lines = ["🔍 我辨識到以下品項，請幫我核對：\n"]
    for i, it in enumerate(items, 1):
        qty = it.get("quantity", "")
        unit = it.get("unit", "")
        lines.append(f"{i}. {it['name']}：{qty}（{unit}）")
    lines.append(
        "\n若正確，請按下方「正確」或回覆「正確」。\n"
        "若有錯，直接打字告訴我，例如：\n"
        "・「第5項是煎豆腐」\n"
        "・「第1項數量是3」\n"
        "・「新增 味噌湯 1 碗」\n"
        "・「刪掉第3項」"
    )
    return "\n".join(lines)


def start_new_analysis(user_id, image_bytes):
    items = gemini_service.analyze_image(image_bytes)
    if not items:
        return [text_msg("我沒能辨識出這張照片裡的食物 😅 請換一張清楚一點的餐點照片試試。")]
    session_store.set(user_id, {"type": "review", "items": items})
    return [text_msg(format_item_list(items), quick_options=REVIEW_QUICK)]


def handle_review_reply(user_id, text):
    """核對階段：使用者回覆正確 / 取消 / 更正。"""
    state = session_store.get(user_id)
    if not state or state.get("type") != "review":
        # session 遺失或過期
        return [text_msg(
            "⚠️ 這張照片的核對階段已結束（可能超過 15 分鐘或系統重啟）。\n"
            "請重新上傳一張食物照片，我們再重新核對一次 📷")]

    # 1) 確認正確 → 進入計算
    if text in CONFIRM_WORDS:
        items = state["items"]
        session_store.clear(user_id)
        return finalize(user_id, items)

    # 2) 取消
    if text in CANCEL_WORDS:
        session_store.clear(user_id)
        return [text_msg("好的，已取消這次紀錄。需要時再傳一張食物照片即可 📷",
                         quick_options=MAIN_QUICK)]

    # 3) 其他 → 當作更正指示，交給 Gemini 套用
    try:
        updated = gemini_service.apply_corrections(state["items"], text)
    except Exception as e:
        print("套用更正失敗：", e)
        return [text_msg("我沒聽懂這個更正 😅 可以換個說法嗎？\n"
                         "例如「第5項是煎豆腐」或「第1項數量是3」")]
    if not updated:
        return [text_msg("更新後清單變空了，我先保留原本的。\n"
                         "如果是想刪除品項，請確認至少保留一項；或回「取消」重來。",
                         quick_options=REVIEW_QUICK)]

    state["items"] = updated
    session_store.set(user_id, state)  # 更新並刷新逾時計時
    return [text_msg("已更新，請再核對一次：\n\n" + format_item_list(updated),
                     quick_options=REVIEW_QUICK)]


def finalize(user_id, items):
    """核對完成後：估算營養、顯示結果，並詢問是否計入紀錄（先不寫入）。"""
    try:
        nutrition = gemini_service.estimate_nutrition(items)
    except Exception as e:
        print("估算失敗：", e)
        return [text_msg("估算營養時發生問題，請稍後再試或重新上傳照片 🙏")]
    result_items = nutrition.get("items", [])
    total_cal = nutrition.get("total_calories", 0)
    total_pro = nutrition.get("total_protein", 0)
    lines = ["🍽️ 這份餐點的營養估算：\n"]
    for it in result_items:
        lines.append(f"• {it['name']}（{it.get('portion','')}）\n"
                     f"   {it.get('calories',0)} kcal / 蛋白質 {it.get('protein',0)} g")
    lines += ["\n──────────",
              f"總熱量：約 {total_cal} kcal",
              f"總蛋白質：約 {total_pro} g",
              "\n是否要將這餐計入今天的飲食紀錄？"]

    # 把結果暫存進 session，等使用者按「是/否」再決定要不要寫入
    session_store.set(user_id, {
        "type": "pending_save",
        "result_items": result_items,
        "total_cal": total_cal,
        "total_pro": total_pro,
    })
    return [text_msg("\n".join(lines), quick_options=SAVE_QUICK)]


def handle_pending_save(user_id, text):
    """使用者對『是否記錄』的回覆。"""
    state = session_store.get(user_id)
    if not state or state.get("type") != "pending_save":
        # 逾時或狀態遺失
        return [text_msg(
            "⚠️ 這筆營養結果已逾時（可能超過 15 分鐘或系統重啟），沒有記錄下來。\n"
            "如果要記錄，請重新上傳一次照片 📷")]

    if text in SAVE_YES_WORDS:
        nickname = get_nickname(user_id)
        saved = notion_service.save_record(
            user_id, state["result_items"],
            state["total_cal"], state["total_pro"], nickname)
        session_store.clear(user_id)
        msg = ("✅ 已計入今天的飲食紀錄！" if saved
               else "⚠️ 記錄儲存失敗，請稍後再試 🙏")
        return [text_msg(msg, quick_options=MAIN_QUICK)]

    if text in SAVE_NO_WORDS:
        session_store.clear(user_id)
        return [text_msg("好的，這餐不計入紀錄 👌（純查詢）", quick_options=MAIN_QUICK)]

    # 其他輸入 → 再問一次
    return [text_msg("請問是否要將這餐計入今天的飲食紀錄？請點下方按鈕 🙏",
                     quick_options=SAVE_QUICK)]


# ---------------------------------------------------------------------------
# 營養統計
# ---------------------------------------------------------------------------
def handle_stats(user_id, kind):
    if kind == "today":
        s = notion_service.get_today_total(user_id)
        txt = (f"📊 今日累計（{s['meals']} 餐）\n"
               f"總熱量：{s['calories']} kcal\n總蛋白質：{s['protein']} g")
    elif kind == "week":
        s = notion_service.get_week_average(user_id)
        txt = (f"📊 本週每日平均（統計 {s['days']} 天）\n"
               f"平均熱量：{s['calories']} kcal/天\n平均蛋白質：{s['protein']} g/天")
    else:
        s = notion_service.get_month_average(user_id)
        txt = (f"📊 本月每日平均（統計 {s['days']} 天）\n"
               f"平均熱量：{s['calories']} kcal/天\n平均蛋白質：{s['protein']} g/天")
    return [text_msg(txt, quick_options=MAIN_QUICK)]


# ---------------------------------------------------------------------------
# 上傳體重
# ---------------------------------------------------------------------------
def prompt_weight(user_id):
    session_store.set(user_id, {"type": "weight"})
    return [text_msg("請輸入今日體重（公斤，小數點後一位）\n例如：68.5\n\n（不想記錄可按下方「取消」）",
                     quick_options=CANCEL_QUICK)]


def handle_weight_input(user_id, text):
    raw = text.strip().replace("公斤", "").replace("kg", "").replace("KG", "").strip()
    try:
        weight = round(float(raw), 1)
    except ValueError:
        return [text_msg("格式不正確 😅 請只輸入數字，例如：68.5\n（或按下方「取消」離開）",
                         quick_options=CANCEL_QUICK)]
    if not (20 <= weight <= 400):
        return [text_msg("這個體重數值看起來不太對，請確認後再輸入一次（例如 68.5）\n（或按下方「取消」離開）",
                         quick_options=CANCEL_QUICK)]
    session_store.clear(user_id)
    nickname = get_nickname(user_id)
    ok = notion_service.save_weight(user_id, weight, nickname)
    msg = (f"✅ 已記錄今日體重：{weight} kg" if ok else "⚠️ 體重儲存失敗，請稍後再試")
    return [text_msg(msg, quick_options=MAIN_QUICK)]


# ---------------------------------------------------------------------------
# 計算基礎代謝率
# ---------------------------------------------------------------------------
def handle_calc_bmr(user_id):
    r = bmr_service.compute_week_bmr(user_id)
    status = r["status"]
    if status == "ok":
        notion_service.upsert_bmr(user_id, r["value"], get_nickname(user_id))
        txt = (f"🔥 過去一週基礎代謝率估算\n"
               f"估算值：約 {r['value']} kcal/天\n"
               f"（推估區間 {r['lower']} ~ {r['upper']} kcal）\n\n"
               f"✅ 已更新至你的代謝率紀錄")
    elif status == "lower_only":
        txt = (f"目前資料只有『體重下降』的日子，\n"
               f"只能推估基礎代謝率 大於 {r['lower']} kcal。\n"
               f"需要體重有升有降，才能夾出完整範圍 🙏")
    elif status == "upper_only":
        txt = (f"目前資料只有『體重上升』的日子，\n"
               f"只能推估基礎代謝率 小於 {r['upper']} kcal。\n"
               f"需要體重有升有降，才能夾出完整範圍 🙏")
    elif status == "contradiction":
        txt = (f"⚠️ 資料互相矛盾（推估下界 {r['lower']} > 上界 {r['upper']}）。\n"
               f"這通常是體重受水分波動影響造成的，\n"
               f"建議持續每天記錄體重與三餐，累積更多天後再試 🙏")
    else:
        txt = (f"資料不足，目前只有 {r['days']} 天有效紀錄，\n"
               f"至少需要連續上傳 {r['need']} 天體重 & 飲食紀錄"
               f"（上傳的紀錄越完整之後，重新估算的結果會越準確哦~）。\n"
               f"請持續記錄後再試 🙏")
    return [text_msg(txt, quick_options=MAIN_QUICK)]


# ---------------------------------------------------------------------------
# 熱量盈餘
# ---------------------------------------------------------------------------
def handle_surplus(user_id):
    r = bmr_service.compute_today_surplus(user_id)
    if r["status"] == "missing":
        txt = ("無法計算今日熱量盈餘，缺少以下資料：\n"
               + "\n".join(f"• {m}" for m in r["missing"])
               + "\n\n補齊後再試一次即可 🙏")
    else:
        surplus = r["surplus"]
        sign = "盈餘（攝取 > 消耗）" if surplus > 0 else "赤字（攝取 < 消耗）"
        txt = (f"⚖️ 今日熱量盈餘\n"
               f"攝取：{r['intake']} kcal\n"
               f"代謝率（消耗）：{r['bmr']} kcal\n"
               f"──────────\n"
               f"淨值：{surplus:+d} kcal（{sign}）")
    return [text_msg(txt, quick_options=MAIN_QUICK)]


# ---------------------------------------------------------------------------
# Webhook
# ---------------------------------------------------------------------------
@app.route("/", methods=["GET"])
def health():
    return "OK", 200


@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return "OK"


@handler.add(MessageEvent, message=ImageMessageContent)
def on_image(event):
    user_id = event.source.user_id
    maybe_purge()
    try:
        image_bytes = download_image(event.message.id)
        messages = start_new_analysis(user_id, image_bytes)
    except Exception as e:
        print("處理照片失敗：", e)
        messages = [text_msg("辨識照片時發生問題，請再試一次 🙏")]
    reply(event.reply_token, messages)


def looks_like_correction(text):
    """粗略判斷是否像更正指示（用於 session 過期時給明確提示）。"""
    keys = ["第", "項", "新增", "刪", "數量", "改成", "應該"]
    return any(k in text for k in keys)


@handler.add(MessageEvent, message=TextMessageContent)
def on_text(event):
    user_id = event.source.user_id
    maybe_purge()
    text = event.message.text.strip()
    state = session_store.get(user_id)

    # 0) 全域取消：任何進行中的狀態，只要說「取消/算了/不用了…」一律退出回主選單
    if state and text in CANCEL_WORDS:
        session_store.clear(user_id)
        reply(event.reply_token, [text_msg("好的，已取消 👌 需要時再從選單開始即可。",
                                           quick_options=MAIN_QUICK)])
        return

    # 1) 等待體重輸入
    if state and state.get("type") == "weight":
        reply(event.reply_token, handle_weight_input(user_id, text))
        return

    # 1.5) 等待「是否記錄」的回覆
    if state and state.get("type") == "pending_save":
        reply(event.reply_token, handle_pending_save(user_id, text))
        return

    # 2) 核對階段
    if state and state.get("type") == "review":
        reply(event.reply_token, handle_review_reply(user_id, text))
        return

    # 3) 指令
    if text in STAT_KEYWORDS:
        reply(event.reply_token, handle_stats(user_id, STAT_KEYWORDS[text]))
        return
    if text in CMD_UPLOAD_WEIGHT:
        reply(event.reply_token, prompt_weight(user_id))
        return
    if text in CMD_CALC_BMR:
        reply(event.reply_token, handle_calc_bmr(user_id))
        return
    if text in CMD_SURPLUS:
        reply(event.reply_token, handle_surplus(user_id))
        return

    # 4) 沒有進行中狀態，但訊息像更正指示 → 可能是核對階段過期了
    if looks_like_correction(text):
        reply(event.reply_token, [text_msg(
            "⚠️ 我這邊沒有正在核對中的照片了（可能已超過 15 分鐘或系統重啟）。\n"
            "請重新上傳一張食物照片，我會重新辨識並列點讓你核對 📷")])
        return

    # 5) 使用說明
    reply(event.reply_token, [text_msg(
        "嗨！我可以幫你：\n"
        "📷 傳食物照片 → 列點核對 → 算熱量與蛋白質並存檔\n"
        "⚖️ 上傳今日體重 → 記錄每日體重\n"
        "🔥 計算並設定基礎代謝率 → 用一週體重變化估算\n"
        "📊 今日統計 / 本週統計 / 本月統計 → 查詢紀錄\n"
        "➕ 今日熱量盈餘 → 今日攝取減消耗",
        quick_options=MAIN_QUICK)])


if __name__ == "__main__":
    import os
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port)
