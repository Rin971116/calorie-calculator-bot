"""
集中管理所有環境變數與設定。
所有金鑰請寫在 .env（本機）或部署平台的環境變數，不要寫死在程式裡。
"""
import os
from dotenv import load_dotenv

load_dotenv()


class Config:
    # ---- LINE ----
    LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
    LINE_CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET", "")

    # ---- Gemini ----
    GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
    GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash-lite")

    # ---- Notion ----
    NOTION_API_KEY = os.environ.get("NOTION_API_KEY", "")
    # 餐點表：只存餐點紀錄
    NOTION_DATABASE_ID = os.environ.get("NOTION_DATABASE_ID", "")
    # 體重表：只存體重，同一人同一天覆蓋為最新一筆
    NOTION_WEIGHT_DATABASE_ID = os.environ.get("NOTION_WEIGHT_DATABASE_ID", "")
    # 代謝率表：每位使用者一列，代表當前代謝率（TDEE）
    NOTION_BMR_DATABASE_ID = os.environ.get("NOTION_BMR_DATABASE_ID", "")
    # 蛋白質目標表：每位使用者一列，存加權數（目標 = 體重 × 加權數）
    NOTION_PROTEIN_DATABASE_ID = os.environ.get("NOTION_PROTEIN_DATABASE_ID", "")

    # ---- 其他 ----
    # 時區偏移（小時）。台灣為 +8，用於各種「今日/本週/本月」的日期界定。
    TIMEZONE_OFFSET = int(os.environ.get("TIMEZONE_OFFSET", "8"))

    # BMR 一週統計所需的最低有效比較天數
    BMR_MIN_DAYS = int(os.environ.get("BMR_MIN_DAYS", "3"))

    # 資料保留天數：超過這個天數的餐點與體重紀錄會被自動清除
    DATA_RETENTION_DAYS = int(os.environ.get("DATA_RETENTION_DAYS", "60"))

    @classmethod
    def validate(cls):
        """啟動時檢查必要金鑰是否齊全，缺少就明確報錯。"""
        missing = []
        for key in [
            "LINE_CHANNEL_ACCESS_TOKEN",
            "LINE_CHANNEL_SECRET",
            "GEMINI_API_KEY",
            "NOTION_API_KEY",
            "NOTION_DATABASE_ID",
            "NOTION_WEIGHT_DATABASE_ID",
            "NOTION_BMR_DATABASE_ID",
            "NOTION_PROTEIN_DATABASE_ID",
        ]:
            if not getattr(cls, key):
                missing.append(key)
        if missing:
            raise RuntimeError(
                "缺少必要環境變數：" + ", ".join(missing) +
                "。請參考 .env.example 設定。"
            )
