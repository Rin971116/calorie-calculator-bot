"""
Gunicorn 設定，針對 Render 免費方案（512MB RAM）調校。

重點：
- workers=1：單一 worker，避免多份程式各自吃記憶體而爆掉。
- threads=2：用少量執行緒處理並發，比多 worker 省記憶體。
- max_requests：每處理一定數量請求就自動重啟 worker，回收可能累積的記憶體
  （gRPC/影像處理容易殘留記憶體，定期回收可避免長時間後 OOM）。
- timeout=120：影像辨識 + Gemini 呼叫較久，加大逾時避免被誤判卡死而中斷。

啟動方式改為：gunicorn -c gunicorn.conf.py app:app
"""
import os

bind = f"0.0.0.0:{os.environ.get('PORT', '10000')}"
workers = 1
threads = 2
worker_class = "gthread"
max_requests = 50
max_requests_jitter = 10
timeout = 120
graceful_timeout = 30
