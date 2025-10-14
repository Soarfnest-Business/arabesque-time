import os
import multiprocessing

# Basic configuration（Render最適化）
bind = f"0.0.0.0:{os.environ.get('PORT', 10000)}"  # Renderのデフォルトポート10000を使用
# Slackイベントの重複処理を避けるため、まずはワーカーを1に固定
workers = 1
worker_class = "sync"
worker_connections = 1000
max_requests = 1000
max_requests_jitter = 50

# Performance tuning（502エラー対策）
keepalive = 60  # Keep-alive時間を増加
timeout = 120  # タイムアウトを120秒に増加（統計計算対応）
graceful_timeout = 120  # Graceful shutdown時間を増加
preload_app = True

# Memory management
worker_tmp_dir = "/dev/shm"  # メモリファイルシステムを使用
worker_rlimit_nofile = 1024

# Logging（デバッグ用）
loglevel = "info"
accesslog = "-"
errorlog = "-"
access_log_format = '%(h)s %(l)s %(u)s %(t)s "%(r)s" %(s)s %(b)s "%(f)s" "%(a)s" %(D)s'
capture_output = True  # エラーキャプチャを有効化

# Security
limit_request_line = 4094
limit_request_fields = 100
limit_request_field_size = 8190

# Render最適化設定
max_worker_connections = 1000
forwarded_allow_ips = "*"  # Renderプロキシからの接続を許可

# Application
module = "app:app" 
