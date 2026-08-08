"""
LINE 食物營養紀錄 Bot — 主程式

功能：
  1. 傳食物照片 → 辨識 → Quick Reply/打字確認 → 估算熱量與蛋白質 → 存入 Notion
  2. 營養統計：今日總計 / 本週每日平均 / 本月每日平均
  3. 上傳體重：按下「上傳體重」後回覆純數字（例如 68.5）
  4. 計算基礎代謝率：用過去一週體重變化 + 前一日攝取，區間夾擠估算，更新至代謝率表
  5. 熱量盈餘：當日攝取 − 當前代謝率；缺資料時明確回報缺哪一項

例外處理：
  - 確認流程 session 遺失（重啟/逾時）→ 請使用者重新上傳照片
  - 體重輸入非數字 → 提示正確格式
  - BMR / 熱量盈餘缺資料 → 明確回報
"""
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

app = Flask(__name__)
line_config = Configuration(access_token=Config.LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(Config.LINE_CHANNEL_SECRET)

# 指令關鍵字（純文字或 Rich Menu 皆可觸發）
STAT_KEYWORDS = {"今日": "today", "今天": "today",
                 "本週": "week", "這週": "week",
                 "本月": "month", "這個月": "month"}
CMD_UPLOAD_WEIGHT = {"上傳體重", "輸入體重", "體重"}
CMD_CALC_BMR = {"計算基礎代謝率", "基礎代謝率", "計算代謝率", "bmr", "BMR"}
CMD_SURPLUS = {"熱量盈餘", "今日盈餘", "計算盈餘"}

# 主選單常用按鈕
MAIN_QUICK = ["今日", "本週", "本月", "上傳體重", "計算基礎代謝率", "熱量盈餘"]


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


# ---------------------------------------------------------------------------
# 功能 1：食物照片辨識與確認
# ---------------------------------------------------------------------------
def start_new_analysis(user_id, image_bytes):
    result = gemini_service.analyze_image(image_bytes)
    items = result.get("items", [])
    questions = result.get("questions", [])
    if not questions:
        return finalize(user_id, items)
    session_store.set(user_id, {
        "type": "confirm",
        "confirmed_items": items,
        "pending_questions": questions,
        "current_index": 0,
    })
    return ask_next_question(user_id)


def ask_next_question(user_id):
    state = session_store.get(user_id)
    if not state:
        return [text_msg("階段已過期，請重新上傳照片 📷")]
    idx = state["current_index"]
    q = state["pending_questions"][idx]
    prompt = (f"❓ 關於「{q.get('item_ref', '某項食物')}」：\n{q['question']}\n\n"
              f"（可點選下方按鈕，或直接打字回覆）")
    return [text_msg(prompt, quick_options=q.get("options", []))]


def handle_food_answer(user_id, answer_text):
    state = session_store.get(user_id)
    if not state:
        return [text_msg(
            "找不到先前的照片紀錄，可能是等待太久或系統重啟了 😅\n"
            "請重新上傳一張食物照片，我們再開始～ 📷")]
    idx = state["current_index"]
    q = state["pending_questions"][idx]
    state["confirmed_items"].append({
        "name": answer_text.strip(),
        "portion": q.get("portion", ""),
        "certain": True,
    })
    state["current_index"] = idx + 1
    if state["current_index"] < len(state["pending_questions"]):
        session_store.set(user_id, state)
        return ask_next_question(user_id)
    final_items = state["confirmed_items"]
    session_store.clear(user_id)
    return finalize(user_id, final_items)


def finalize(user_id, items):
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
              f"總蛋白質：約 {total_pro} g"]
    saved = notion_service.save_record(user_id, result_items, total_cal, total_pro)
    lines.append("\n✅ 已存入紀錄" if saved else "\n⚠️ 紀錄儲存失敗（已顯示結果）")
    return [text_msg("\n".join(lines), quick_options=MAIN_QUICK)]


# ---------------------------------------------------------------------------
# 功能 2：營養統計
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
# 功能 3：上傳體重
# ---------------------------------------------------------------------------
def prompt_weight(user_id):
    session_store.set(user_id, {"type": "weight"})
    return [text_msg("請輸入今日體重（公斤，小數點後一位）\n例如：68.5")]


def handle_weight_input(user_id, text):
    """使用者處於等待體重狀態時，解析純數字並存檔。"""
    raw = text.strip().replace("公斤", "").replace("kg", "").replace("KG", "").strip()
    try:
        weight = round(float(raw), 1)
    except ValueError:
        # 非數字：保留等待狀態，提示格式
        return [text_msg("格式不正確 😅 請只輸入數字，例如：68.5")]
    if not (20 <= weight <= 400):
        return [text_msg("這個體重數值看起來不太對，請確認後再輸入一次（例如 68.5）")]
    session_store.clear(user_id)
    ok = notion_service.save_weight(user_id, weight)
    msg = (f"✅ 已記錄今日體重：{weight} kg" if ok
           else "⚠️ 體重儲存失敗，請稍後再試")
    return [text_msg(msg, quick_options=MAIN_QUICK)]


# ---------------------------------------------------------------------------
# 功能 4：計算基礎代謝率（過去一週，區間夾擠）
# ---------------------------------------------------------------------------
def handle_calc_bmr(user_id):
    r = bmr_service.compute_week_bmr(user_id)
    status = r["status"]
    if status == "ok":
        notion_service.upsert_bmr(user_id, r["value"])
        txt = (f"🔥 過去一週基礎代謝率估算\n"
               f"估算值：約 {r['value']} kcal/天\n"
               f"（推估區間 {r['lower']} ~ {r['upper']} kcal）\n\n"
               f"✅ 已更新至你的代謝率紀錄")
    elif status == "lower_only":
        txt = (f"目前資料只有『體重下降』的日子，\n"
               f"只能推估基礎代謝率 **大於 {r['lower']} kcal**。\n"
               f"需要體重有升有降，才能夾出完整範圍 🙏")
    elif status == "upper_only":
        txt = (f"目前資料只有『體重上升』的日子，\n"
               f"只能推估基礎代謝率 **小於 {r['upper']} kcal**。\n"
               f"需要體重有升有降，才能夾出完整範圍 🙏")
    elif status == "contradiction":
        txt = (f"⚠️ 資料互相矛盾（推估下界 {r['lower']} > 上界 {r['upper']}）。\n"
               f"這通常是體重受水分波動影響造成的，\n"
               f"建議持續每天記錄體重與三餐，累積更多天後再試 🙏")
    else:  # insufficient
        txt = (f"資料不足，目前只有 {r['days']} 天有效紀錄，\n"
               f"至少需要 {r['need']} 天（每天都要有體重＋前一日的餐點紀錄）。\n"
               f"請持續記錄後再試 🙏")
    return [text_msg(txt, quick_options=MAIN_QUICK)]


# ---------------------------------------------------------------------------
# 功能 5：熱量盈餘
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
    try:
        image_bytes = download_image(event.message.id)
        messages = start_new_analysis(user_id, image_bytes)
    except Exception as e:
        print("處理照片失敗：", e)
        messages = [text_msg("辨識照片時發生問題，請再試一次 🙏")]
    reply(event.reply_token, messages)


@handler.add(MessageEvent, message=TextMessageContent)
def on_text(event):
    user_id = event.source.user_id
    text = event.message.text.strip()
    state = session_store.get(user_id)

    # 1) 若正在等待體重輸入 → 優先解析為體重
    if state and state.get("type") == "weight":
        reply(event.reply_token, handle_weight_input(user_id, text))
        return

    # 2) 指令（統計 / 上傳體重 / 計算BMR / 熱量盈餘）
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

    # 3) 若正在食物確認流程 → 當作答案
    if state and state.get("type") == "confirm":
        reply(event.reply_token, handle_food_answer(user_id, text))
        return

    # 4) 其他 → 使用說明
    reply(event.reply_token, [text_msg(
        "嗨！我可以幫你：\n"
        "📷 傳食物照片 → 算熱量與蛋白質並存檔\n"
        "⚖️ 上傳體重 → 記錄每日體重\n"
        "🔥 計算基礎代謝率 → 用一週體重變化估算\n"
        "📊 今日 / 本週 / 本月 → 查詢統計\n"
        "➕ 熱量盈餘 → 今日攝取減消耗",
        quick_options=MAIN_QUICK)])


if __name__ == "__main__":
    import os
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port)
