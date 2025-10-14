import os
import re
from datetime import datetime, timezone, timedelta
from flask import Flask, render_template, redirect, url_for, request, jsonify, session, flash
from slack_bolt import App
from slack_bolt.adapter.flask import SlackRequestHandler
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
from models import db, User, Attendance
from dotenv import load_dotenv
import threading
import requests
import logging
import statistics
from collections import defaultdict
import pytz
from typing import Optional

# Optional agent imports
try:
    from agents.run import run_propose
    from agents.analyzer import Analyzer
    from agents.git_utils import get_repo_root
    from agents.reviewer import review_and_act
except Exception:
    run_propose = None  # type: ignore
    Analyzer = None  # type: ignore
    get_repo_root = None  # type: ignore
    review_and_act = None  # type: ignore

# 日本時間のタイムゾーン定義
JST_TZ = pytz.timezone('Asia/Tokyo')

# ログ設定の改善
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# 環境変数を読み込み
load_dotenv()

# 必須環境変数の検証
required_env_vars = [
    'SLACK_BOT_TOKEN',
    'SLACK_SIGNING_SECRET',
    'SLACK_CLIENT_ID',
    'SLACK_CLIENT_SECRET'
]

missing_vars = []
for var in required_env_vars:
    if not os.environ.get(var):
        missing_vars.append(var)

if missing_vars:
    logger.error(f"Missing required environment variables: {missing_vars}")
    raise ValueError(f"Missing required environment variables: {missing_vars}")

# Flaskアプリケーションの設定
app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'fallback-secret-key-for-development')

# セッション設定（持続性を改善）
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(days=30)  # セッションを30日間保持
# Renderの環境を検出してHTTPS設定を最適化
is_render_env = os.environ.get('RENDER') == 'true' or os.environ.get('RENDER_SERVICE_ID') is not None
app.config['SESSION_COOKIE_SECURE'] = is_render_env  # Render環境でのみHTTPS必須
app.config['SESSION_COOKIE_HTTPONLY'] = True  # JavaScriptからアクセス不可
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'  # CSRF保護

# データベース設定の改善
database_url = os.environ.get('DATABASE_URL')
if database_url:
    # PostgreSQL用の接続設定の最適化
    if database_url.startswith('postgres://'):
        database_url = database_url.replace('postgres://', 'postgresql://', 1)
    
    app.config['SQLALCHEMY_DATABASE_URI'] = database_url
    app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {
        'pool_pre_ping': True,  # 接続前にping
        'pool_recycle': 3600,   # 1時間で接続をリサイクル
        'pool_size': 10,        # 接続プールサイズ
        'max_overflow': 20,     # 最大オーバーフロー
        'pool_timeout': 30,     # 接続タイムアウト（秒）
        'connect_args': {'sslmode': 'require', 'connect_timeout': 30}
    }
else:
    app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///instance/attendance.db'
    app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {
        'pool_pre_ping': True,
        'pool_recycle': 3600,
        'pool_timeout': 30
    }

app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

# データベースの初期化
db.init_app(app)

# カスタムフィルタを追加（UTC時間を日本時間に変換）
@app.template_filter('jst')
def jst_filter(utc_datetime):
    """UTC時間を日本時間に変換するフィルタ"""
    if utc_datetime is None:
        return None
    if utc_datetime.tzinfo is None:
        utc_datetime = utc_datetime.replace(tzinfo=timezone.utc)
    return utc_datetime.astimezone(JST_TZ)

# カスタムフィルタを追加（strftimeフィルター）
@app.template_filter('strftime')
def strftime_filter(datetime_obj, format_str):
    """datetimeオブジェクトをフォーマットするフィルタ"""
    if datetime_obj is None:
        return None
    return datetime_obj.strftime(format_str)

# Slack Bolt の自動OAuth設定を無効にするために環境変数を一時的に削除
slack_client_id = os.environ.get('SLACK_CLIENT_ID')
slack_client_secret = os.environ.get('SLACK_CLIENT_SECRET')
if 'SLACK_CLIENT_ID' in os.environ:
    del os.environ['SLACK_CLIENT_ID']
if 'SLACK_CLIENT_SECRET' in os.environ:
    del os.environ['SLACK_CLIENT_SECRET']

# Slack Boltアプリケーションの設定（シンプルなトークンベース）
slack_app = App(
    token=os.environ.get('SLACK_BOT_TOKEN'),
    signing_secret=os.environ.get('SLACK_SIGNING_SECRET'),
    process_before_response=True
)

# 環境変数を復元
if slack_client_id:
    os.environ['SLACK_CLIENT_ID'] = slack_client_id
if slack_client_secret:
    os.environ['SLACK_CLIENT_SECRET'] = slack_client_secret

# Slack Web クライアント
slack_client = WebClient(token=os.environ.get('SLACK_BOT_TOKEN'))

# SlackRequestHandlerの設定
handler = SlackRequestHandler(slack_app)

# Slack Bot イベントリスナー（最適化）
@slack_app.message(re.compile(r'(出勤|おはよう)', re.IGNORECASE))
def handle_checkin(message, say):
    """出勤打刻を処理"""
    try:
        user_id = message['user']
        logger.info(f"Received checkin message from user: {user_id}")
        
        # ユーザー情報を取得
        user = get_or_create_user(user_id)
        if not user:
            logger.error(f"Failed to get or create user: {user_id}")
            say("申し訳ありませんが、ユーザー情報の取得に失敗しました。")
            return
        
        # 出勤記録を作成
        attendance = Attendance(
            user_id=user.id,
            type='出勤',
            timestamp=datetime.now(timezone.utc)
        )
        
        db.session.add(attendance)
        db.session.commit()
        
        # 返信メッセージを送信（日本時間で表示）
        jst_timestamp = attendance.timestamp.astimezone(JST_TZ)
        say(f"出勤打刻を受け付けました！ {jst_timestamp.strftime('%Y-%m-%d %H:%M:%S')}")
        logger.info(f"Checkin recorded for user: {user_id}")
        
    except Exception as e:
        logger.error(f"Error handling checkin: {e}")
        say("申し訳ありませんが、出勤打刻の処理中にエラーが発生しました。")

@slack_app.message(re.compile(r'(退勤|おつかれ)', re.IGNORECASE))
def handle_checkout(message, say):
    """退勤打刻を処理"""
    try:
        user_id = message['user']
        logger.info(f"Received checkout message from user: {user_id}")
        
        # ユーザー情報を取得
        user = get_or_create_user(user_id)
        if not user:
            logger.error(f"Failed to get or create user: {user_id}")
            say("申し訳ありませんが、ユーザー情報の取得に失敗しました。")
            return
        
        # 退勤記録を作成
        attendance = Attendance(
            user_id=user.id,
            type='退勤',
            timestamp=datetime.now(timezone.utc)
        )
        
        db.session.add(attendance)
        db.session.commit()
        
        # 返信メッセージを送信（日本時間で表示）
        jst_timestamp = attendance.timestamp.astimezone(JST_TZ)
        say(f"退勤打刻を受け付けました！ {jst_timestamp.strftime('%Y-%m-%d %H:%M:%S')}")
        logger.info(f"Checkout recorded for user: {user_id}")
        
    except Exception as e:
        logger.error(f"Error handling checkout: {e}")
        say("申し訳ありませんが、退勤打刻の処理中にエラーが発生しました。")

@slack_app.message(re.compile(r'(ヘルプ|help)', re.IGNORECASE))
def handle_help(message, say):
    """ヘルプメッセージを送信"""
    help_text = """
📋 **出退勤管理ボットの使い方**

🌅 **出勤打刻:**
• `出勤`
• `おはよう`

🌙 **退勤打刻:**
• `退勤`
• `おつかれ`

❓ **このヘルプを表示:**
• `ヘルプ`
• `help`

💻 **Web画面でも確認できます:**
https://arabesque-time.onrender.com/
    """
    say(help_text)

# メッセージでの提案（ワンショット）
@slack_app.message(re.compile(r'^(?:agent\s+propose|提案)\s+(.+)', re.IGNORECASE))
def agent_msg_propose(message, say):
    goal = message.get('text', '')
    m = re.search(r'^(?:agent\s+propose|提案)\s+(.+)', goal, flags=re.IGNORECASE)
    if not m:
        say("使用例: 提案 トップページの可読性改善")
        return
    goal_text = m.group(1).strip()
    channel_id = message.get('channel')

    if run_propose is None:
        say("エージェント機能が無効です。サーバにagentsモジュールを配置してください。")
        return
    say(f"提案を開始します: {goal_text}\n検証後にPRリンクを共有します…")

    def _do_propose():
        try:
            pr_url = run_propose(goal_text, push_and_pr=True)
            if pr_url:
                slack_client.chat_postMessage(channel=channel_id, text=f"✅ 提案が完了: {pr_url}")
            else:
                slack_client.chat_postMessage(channel=channel_id, text="✅ 提案を作成（PRなし）")
        except Exception as e:
            logger.error(f"Agent proposal failed: {e}")
            slack_client.chat_postMessage(channel=channel_id, text=f"❌ 提案に失敗: {e}")

    threading.Thread(target=_do_propose, daemon=True).start()

# レビュー実行
@slack_app.message(re.compile(r'^(?:レビュー|review)\s+(\d+)', re.IGNORECASE))
def agent_msg_review(message, say):
    text = message.get('text', '')
    channel_id = message.get('channel')
    m = re.search(r'^(?:レビュー|review)\s+(\d+)', text, flags=re.IGNORECASE)
    if not m:
        say("使用例: レビュー 42")
        return
    pr_number = int(m.group(1))
    if review_and_act is None:
        say("レビュー機能が利用不可です。サーバにagentsモジュールを配置してください。")
        return
    say(f"PR #{pr_number} の自動レビューを開始します…")

    def _do():
        try:
            res = review_and_act(pr_number, auto_fix=True, auto_merge=True)
            slack_client.chat_postMessage(channel=channel_id, text=f"レビュー完了: {res}")
        except Exception as e:
            logger.error(f"Auto-review failed: {e}")
            slack_client.chat_postMessage(channel=channel_id, text=f"レビューに失敗: {e}")

    threading.Thread(target=_do, daemon=True).start()

# 全自動（提案→PR→レビュー→修正→マージ）
@slack_app.message(re.compile(r'^(?:自動|全自動|auto|オート)(?:\s*(?:提案|レビュー|改善))?$', re.IGNORECASE))
def agent_full_auto(message, say):
    channel_id = message.get('channel')
    thread_ts = message.get('ts')

    if run_propose is None or review_and_act is None:
        say("エージェント機能が無効です。agentsモジュールを確認してください。", thread_ts=thread_ts)
        return

    # 目標を自動生成（Analyzerがあれば利用）
    goal = "UIの小規模改善（_base.html と style.cssの微修正）"
    try:
        if Analyzer and get_repo_root:
            analyzer = Analyzer(get_repo_root())
            result = analyzer.analyze()
            targets = ", ".join(result.suggested_targets) or "UI全体（小）"
            goal = f"{result.suggested_area} の改善。対象: {targets}。制約: 破壊的変更なし・小規模差分・アクセシビリティ/可読性重視。"
            say(f"初期分析完了。提案を作成します…\n- 目標: {goal}", thread_ts=thread_ts)
        else:
            say(f"提案を作成します…\n- 目標: {goal}", thread_ts=thread_ts)
    except Exception as e:
        logger.error(f"Analyzer failed: {e}")
        say(f"簡易目標で進めます。\n- 目標: {goal}", thread_ts=thread_ts)

    def _do_all():
        try:
            # 1) 提案→PR
            pr_url = run_propose(goal, push_and_pr=True)
            if not pr_url:
                slack_client.chat_postMessage(channel=channel_id, text="❌ PR作成に失敗しました。", thread_ts=thread_ts)
                return
            slack_client.chat_postMessage(channel=channel_id, text=f"PR作成: {pr_url}\nレビューを実行します…", thread_ts=thread_ts)

            # 2) PR番号抽出
            m = re.search(r"/pull/(\d+)", pr_url)
            if not m:
                slack_client.chat_postMessage(channel=channel_id, text="PR番号の特定に失敗しました。", thread_ts=thread_ts)
                return
            pr_number = int(m.group(1))

            # 3) レビュー→（必要であれば修正）→マージ
            res = review_and_act(pr_number, auto_fix=True, auto_merge=True)
            slack_client.chat_postMessage(channel=channel_id, text=f"最終結果: {res}", thread_ts=thread_ts)
        except Exception as e:
            logger.error(f"Full-auto failed: {e}")
            slack_client.chat_postMessage(channel=channel_id, text=f"❌ 全自動処理に失敗: {e}", thread_ts=thread_ts)

    threading.Thread(target=_do_all, daemon=True).start()

# 提案/開始用の会話セッション管理
SESSIONS = {}

# 提案（キーワードのみ）: 自動スキャン→（任意質問）→確認→PR（提案モード）
@slack_app.message(re.compile(r'^(?:提案|agent\\s*propose)\\s*$', re.IGNORECASE))
def agent_msg_propose_interactive(message, say, body=None):
    # Deduplicate Slack retries using event_id
    try:
        if body and isinstance(body, dict):
            ev_id = body.get('event_id')
            if ev_id:
                if not hasattr(app, 'processed_events'):
                    app.processed_events = {}
                now = int(datetime.now(timezone.utc).timestamp())
                # prune
                for k in list(app.processed_events.keys()):
                    if now - app.processed_events[k] > 300:
                        app.processed_events.pop(k, None)
                if ev_id in app.processed_events:
                    logger.info("flow.dedup.hit event_id=%s", ev_id)
                    return
                app.processed_events[ev_id] = now
    except Exception:
        pass
    channel_id = message.get('channel')
    user_id = message.get('user')
    thread_ts = message.get('ts')  # start of thread is this ts

    summary = "簡易スキャンのみ。詳細分析は有効時に実行されます。"
    suggested_goal = "UIの小規模改善（_base.html と style.cssの微修正）"
    questions = []
    try:
        if Analyzer and get_repo_root:
            analyzer = Analyzer(get_repo_root())
            result = analyzer.analyze()
            summary = result.summary
            targets = ", ".join(result.suggested_targets) or "UI全体（小）"
            questions = result.questions or []
            suggested_goal = f"{result.suggested_area} の改善。対象: {targets}。制約: 破壊的変更なし・小規模差分。"
    except Exception as e:
        logger.exception("flow.analyzer.failed")
        say(f"❌ 解析に失敗しました: {e}", thread_ts=thread_ts)
        return

    say(f"初期分析:\n{summary}", thread_ts=thread_ts)
    session_key = f"{channel_id}:{thread_ts}"
    logger.info("flow.start event_id=%s channel=%s ts=%s session_key=%s", (body or {}).get('event_id') if isinstance(body, dict) else None, channel_id, thread_ts, session_key)
    SESSIONS[session_key] = {
        "user": user_id,
        "goal": suggested_goal,
        "thread_ts": thread_ts,
        "questions": questions,
        "answers": [],
        "q_index": 0,
        "step": "ask" if questions else "confirm",
        "full_auto": False,
    }
    if questions:
        say(f"Q) {questions[0]}", thread_ts=thread_ts)
    else:
        say(
            f"初期案: {suggested_goal}\n→『はい』で実行、『いいえ』で中止、または『修正: ...』で上書きできます。",
            thread_ts=thread_ts,
        )

# 開始: フルフロー（分析→質問→確認→PR→レビュー→修正→マージ）
@slack_app.message(re.compile(r'^(?:開始)$', re.IGNORECASE))
def agent_msg_start_full(message, say, body=None):
    # Deduplicate
    try:
        if body and isinstance(body, dict):
            ev_id = body.get('event_id')
            if ev_id:
                if not hasattr(app, 'processed_events'):
                    app.processed_events = {}
                now = int(datetime.now(timezone.utc).timestamp())
                for k in list(app.processed_events.keys()):
                    if now - app.processed_events[k] > 300:
                        app.processed_events.pop(k, None)
                if ev_id in app.processed_events:
                    logger.info("flow.dedup.hit event_id=%s", ev_id)
                    return
                app.processed_events[ev_id] = now
    except Exception:
        pass
    channel_id = message.get('channel')
    user_id = message.get('user')
    thread_ts = message.get('ts')  # start of thread is this ts

    summary = "簡易スキャンのみ。詳細分析は有効時に実行されます。"
    suggested_goal = "UIの小規模改善（_base.html と style.cssの微修正）"
    questions = []
    try:
        if Analyzer and get_repo_root:
            analyzer = Analyzer(get_repo_root())
            result = analyzer.analyze()
            summary = result.summary
            targets = ", ".join(result.suggested_targets) or "UI全体（小）"
            questions = result.questions or []
            suggested_goal = f"{result.suggested_area} の改善。対象: {targets}。制約: 破壊的変更なし・小規模差分・A11y/可読性重視。"
    except Exception as e:
        logger.exception("flow.analyzer.failed")
        say(f"❌ 解析に失敗しました: {e}", thread_ts=thread_ts)
        return

    say(f"初期分析:\n{summary}", thread_ts=thread_ts)
    session_key = f"{channel_id}:{thread_ts}"
    logger.info("flow.start event_id=%s channel=%s ts=%s session_key=%s", (body or {}).get('event_id') if isinstance(body, dict) else None, channel_id, thread_ts, session_key)
    SESSIONS[session_key] = {
        "user": user_id,
        "goal": suggested_goal,
        "thread_ts": thread_ts,
        "questions": questions,
        "answers": [],
        "q_index": 0,
        "step": "ask" if questions else "confirm",
        "full_auto": True,
    }
    if questions:
        say(f"Q) {questions[0]}", thread_ts=thread_ts)
    else:
        say(
            f"初期案: {suggested_goal}\n→『はい』で実行、『いいえ』で中止、または『修正: ...』で上書きできます。",
            thread_ts=thread_ts,
        )


@slack_app.message(re.compile(r'^.*$', re.DOTALL))
def agent_msg_session_progress(message, say, body=None):
    # Deduplicate
    try:
        if body and isinstance(body, dict):
            ev_id = body.get('event_id')
            if ev_id:
                if not hasattr(app, 'processed_events'):
                    app.processed_events = {}
                now = int(datetime.now(timezone.utc).timestamp())
                for k in list(app.processed_events.keys()):
                    if now - app.processed_events[k] > 300:
                        app.processed_events.pop(k, None)
                if ev_id in app.processed_events:
                    logger.info("flow.dedup.hit event_id=%s", ev_id)
                    return
                app.processed_events[ev_id] = now
    except Exception:
        pass
    channel_id = message.get('channel') or ""
    user_id = message.get('user') or ""
    text = (message.get('text') or '').strip()
    # Determine thread root ts: replies have thread_ts; new messages have ts
    root_ts = message.get('thread_ts') or message.get('ts')
    session_key = f"{channel_id}:{root_ts}"
    if session_key not in SESSIONS:
        return
    sess = SESSIONS.get(session_key) or {}
    if sess.get('user') != user_id:
        return
    thread_ts = sess.get('thread_ts')

    logger.info(f"session_progress text='{text}' user={user_id} key={session_key} step={sess.get('step')}")

    if text.lower().startswith('修正:') or text.startswith('修正：'):
        new_goal = text.split(':', 1)[1].strip() if ':' in text else text
        sess['goal'] = new_goal
        say(f"修正を反映しました。『はい』で実行します。\n- 目標: {new_goal}", thread_ts=thread_ts)
        return
    # 質問に回答するフェーズ
    if sess.get('step') == 'ask':
        sess['answers'].append(text)
        sess['q_index'] += 1
        questions = sess.get('questions') or []
        if sess['q_index'] < len(questions):
            say(f"Q) {questions[sess['q_index']]}", thread_ts=thread_ts)
            return
        # すべて回答済み → 目標へ反映
        if sess.get('answers'):
            ans_summary = '; '.join(sess['answers'])[:200]
            sess['goal'] = (sess.get('goal') or '') + f" 補足: {ans_summary}"
        sess['step'] = 'confirm'
        say(
            f"最終案: {sess['goal']}\n→『はい』で実行、『いいえ』で中止、または『修正: ...』で上書きできます。",
            thread_ts=thread_ts,
        )
        return
    if text in {'はい', '実行', 'ok', 'OK', 'Go', 'go'}:
        goal_text = sess.get('goal')
        if not run_propose:
            say("エージェント機能が無効です。agentsモジュールを確認してください。", thread_ts=thread_ts)
            SESSIONS.pop(session_key, None)
            return
        say(f"提案を実行します…\n- 目標: {goal_text}", thread_ts=thread_ts)

        def _do():
            try:
                pr_url = run_propose(goal_text, push_and_pr=True)
                if not pr_url:
                    slack_client.chat_postMessage(channel=channel_id, text="✅ 提案を作成（PRなし）", thread_ts=thread_ts)
                    return
                slack_client.chat_postMessage(channel=channel_id, text=f"✅ 提案完了。PRを作成: {pr_url}", thread_ts=thread_ts)

                # フルオートの場合は続けてレビュー→（必要なら修正）→マージ
                if sess.get('full_auto') and review_and_act:
                    m = re.search(r"/pull/(\d+)", pr_url)
                    if m:
                        pr_number = int(m.group(1))
                        slack_client.chat_postMessage(channel=channel_id, text=f"レビューを実行します… (PR #{pr_number})", thread_ts=thread_ts)
                        res = review_and_act(pr_number, auto_fix=True, auto_merge=True)
                        slack_client.chat_postMessage(channel=channel_id, text=f"最終結果: {res}", thread_ts=thread_ts)
            except Exception as e:
                logger.error(f"Interactive proposal failed: {e}")
                slack_client.chat_postMessage(channel=channel_id, text=f"❌ 提案に失敗: {e}", thread_ts=thread_ts)
            finally:
                SESSIONS.pop(session_key, None)
        
        threading.Thread(target=_do, daemon=True).start()
        return
    if text in {'いいえ', 'no', 'キャンセル', '中止'}:
        say("提案をキャンセルしました。", thread_ts=thread_ts)
        SESSIONS.pop(session_key, None)
        return
    # 他メッセージはスルー
    return

# 既定の message イベントをACK（未対応メッセージでの404回避）
@slack_app.event("message")
def generic_message_ack(body, logger):
    logger.debug("Unhandled message acked")

# デバッグ用メッセージハンドラーを削除（本番環境では不要）
# 代わりにapp_mentionsイベントのみ処理
@slack_app.event("app_mention")
def handle_app_mention(event, say):
    """ボットへのメンションを処理"""
    text = event.get('text', '').lower()
    if any(keyword in text for keyword in ['ヘルプ', 'help']):
        handle_help(event, say)
    else:
        say("こんにちは！出退勤管理ボットです。`ヘルプ`と送信すると使い方を確認できます。")

def get_or_create_user(slack_user_id):
    """Slackユーザー情報を取得または作成（エラーハンドリング改善）"""
    try:
        user = User.query.filter_by(slack_user_id=slack_user_id).first()
        
        if not user:
            try:
                # Slack APIからユーザー情報を取得
                response = slack_client.users_info(user=slack_user_id)
                if not response.get('ok'):
                    logger.error(f"Slack API error: {response.get('error')}")
                    return None
                    
                user_info = response['user']
                
                user = User(
                    slack_user_id=slack_user_id,
                    display_name=user_info.get('real_name', user_info.get('name', 'Unknown')),
                    email=user_info.get('profile', {}).get('email', '')
                )
                
                db.session.add(user)
                db.session.commit()
                logger.info(f"Created new user: {slack_user_id}")
                
            except SlackApiError as e:
                logger.error(f"Error fetching user info: {e}")
                # エラーの場合はデフォルトユーザーを作成
                user = User(
                    slack_user_id=slack_user_id,
                    display_name=f'User_{slack_user_id[-4:]}',  # IDの末尾4桁のみ表示
                    email=''
                )
                db.session.add(user)
                db.session.commit()
            except Exception as e:
                logger.error(f"Database error creating user: {e}")
                return None
        
        return user
        
    except Exception as e:
        logger.error(f"Error in get_or_create_user: {e}")
        return None

def calculate_work_hours_from_records(records):
    """
    出退勤記録から労働時間を計算（日跨ぎ対応）
    
    Args:
        records: 出退勤記録のリスト（時系列順にソート済み）
    
    Returns:
        float: 総労働時間（時間単位）
    """
    try:
        if not records:
            return 0
        
        # 記録を時系列順にソート
        sorted_records = sorted(records, key=lambda x: x.timestamp)
        
        total_hours = 0
        current_checkin = None
        
        for record in sorted_records:
            if record.type == '出勤':
                # 既に出勤中の場合は、前の出勤記録を更新
                current_checkin = record
            elif record.type == '退勤' and current_checkin is not None:
                # 出勤中の場合、労働時間を計算
                hours = (record.timestamp - current_checkin.timestamp).total_seconds() / 3600
                total_hours += hours
                current_checkin = None  # 退勤したのでリセット
        
        return round(total_hours, 2)
    
    except Exception as e:
        logger.error(f"Error calculating work hours from records: {e}")
        return 0

def calculate_work_hours_statistics(user_id=None):
    """活動時間の統計を計算（週単位）- 最適化版"""
    try:
        # 対象のユーザーを決定（最適化：必要なデータのみ取得）
        if user_id:
            attendances = Attendance.query.filter_by(user_id=user_id).order_by(Attendance.timestamp).all()
        else:
            # 全体統計の場合、過去3ヶ月に制限（パフォーマンス対策）
            three_months_ago = datetime.now(timezone.utc) - timedelta(days=90)
            attendances = Attendance.query.filter(
                Attendance.timestamp >= three_months_ago
            ).order_by(Attendance.timestamp).all()
        
        if not attendances:
            return {
                'weekly_hours': [],
                'average_hours': 0,
                'median_hours': 0,
                'total_weeks': 0,
                'total_hours': 0
            }
        
        # ユーザーごとの出退勤記録を整理
        user_attendances = defaultdict(list)
        for attendance in attendances:
            user_attendances[attendance.user_id].append(attendance)
        
        # 週ごとの作業時間を計算
        weekly_hours = []
        
        for uid, records in user_attendances.items():
            # 週ごとに記録を分類
            weekly_records = defaultdict(list)
            for record in records:
                week_start = record.timestamp.date() - timedelta(days=record.timestamp.weekday())
                weekly_records[week_start].append(record)
            
            # 各週の作業時間を計算（日跨ぎ対応）
            for week_start, week_records in weekly_records.items():
                week_hours = calculate_work_hours_from_records(week_records)
                if week_hours > 0:
                    weekly_hours.append(week_hours)
        
        # 統計値を計算
        if weekly_hours:
            average_hours = statistics.mean(weekly_hours)
            median_hours = statistics.median(weekly_hours)
            total_hours = sum(weekly_hours)
        else:
            average_hours = 0
            median_hours = 0
            total_hours = 0
        
        return {
            'weekly_hours': weekly_hours,
            'average_hours': round(average_hours, 2),
            'median_hours': round(median_hours, 2),
            'total_weeks': len(weekly_hours),
            'total_hours': round(total_hours, 2)
        }
        
    except Exception as e:
        logger.error(f"Error calculating statistics: {e}")
        return {
            'weekly_hours': [],
            'average_hours': 0,
            'median_hours': 0,
            'total_weeks': 0,
            'total_hours': 0
        }

def get_all_users_work_hours():
    """全ユーザーの総労働時間を取得"""
    try:
        users = User.query.all()
        user_work_data = []
        
        for user in users:
            # 各ユーザーの出退勤記録を取得
            attendances = Attendance.query.filter_by(user_id=user.id).order_by(Attendance.timestamp).all()
            
            if not attendances:
                user_work_data.append({
                    'user': user,
                    'total_hours': 0
                })
                continue
            
            # 出退勤記録から総労働時間を計算（日跨ぎ対応）
            total_hours = calculate_work_hours_from_records(attendances)
            
            user_work_data.append({
                'user': user,
                'total_hours': round(total_hours, 2)
            })
        
        return sorted(user_work_data, key=lambda x: x['total_hours'], reverse=True)
    
    except Exception as e:
        logger.error(f"Error getting all users work hours: {e}")
        return []

def get_period_work_hours(start_date=None, end_date=None):
    """指定期間の全ユーザーの労働時間を取得"""
    try:
        if start_date and end_date:
            # 指定された期間を使用（日本時間）
            start_jst = JST_TZ.localize(datetime.fromisoformat(start_date))
            end_jst = JST_TZ.localize(datetime.fromisoformat(end_date))
            end_jst = end_jst.replace(hour=23, minute=59, second=59)
        else:
            # デフォルト：今月の開始日と終了日を取得（日本時間）
            now_jst = datetime.now(JST_TZ)
            start_jst = JST_TZ.localize(datetime(now_jst.year, now_jst.month, 1))
            
            # 今月末日を計算
            if now_jst.month == 12:
                next_month_jst = JST_TZ.localize(datetime(now_jst.year + 1, 1, 1))
            else:
                next_month_jst = JST_TZ.localize(datetime(now_jst.year, now_jst.month + 1, 1))
            
            end_jst = next_month_jst - timedelta(seconds=1)
        
        # UTC時間に変換
        start_datetime = start_jst.astimezone(timezone.utc)
        end_datetime = end_jst.astimezone(timezone.utc)
        
        users = User.query.all()
        period_work_data = []
        
        for user in users:
            # 指定期間の出退勤記録を取得
            attendances = Attendance.query.filter(
                Attendance.user_id == user.id,
                Attendance.timestamp >= start_datetime,
                Attendance.timestamp <= end_datetime
            ).order_by(Attendance.timestamp).all()
            
            if not attendances:
                period_work_data.append({
                    'user': user,
                    'period_hours': 0
                })
                continue
            
            # 出退勤記録から指定期間の労働時間を計算（日跨ぎ対応）
            period_hours = calculate_work_hours_from_records(attendances)
            
            period_work_data.append({
                'user': user,
                'period_hours': round(period_hours, 2)
            })
        
        return period_work_data
    
    except Exception as e:
        logger.error(f"Error getting period work hours: {e}")
        return []

def get_cumulative_work_hours(end_date=None):
    """指定日までの累積労働時間を取得（配分計算用）"""
    try:
        if end_date:
            # 指定された日まで（日本時間）
            end_jst = JST_TZ.localize(datetime.fromisoformat(end_date))
            end_jst = end_jst.replace(hour=23, minute=59, second=59)
            end_datetime = end_jst.astimezone(timezone.utc)
        else:
            # デフォルト：今日まで
            end_datetime = datetime.now(timezone.utc)
        
        users = User.query.all()
        cumulative_work_data = []
        
        for user in users:
            # 指定日までの全出退勤記録を取得
            attendances = Attendance.query.filter(
                Attendance.user_id == user.id,
                Attendance.timestamp <= end_datetime
            ).order_by(Attendance.timestamp).all()
            
            if not attendances:
                cumulative_work_data.append({
                    'user': user,
                    'cumulative_hours': 0
                })
                continue
            
            # 出退勤記録から累積労働時間を計算（日跨ぎ対応）
            cumulative_hours = calculate_work_hours_from_records(attendances)
            
            cumulative_work_data.append({
                'user': user,
                'cumulative_hours': round(cumulative_hours, 2)
            })
        
        return sorted(cumulative_work_data, key=lambda x: x['cumulative_hours'], reverse=True)
    
    except Exception as e:
        logger.error(f"Error getting cumulative work hours: {e}")
        return []

def calculate_revenue_distribution(revenue, start_date=None, end_date=None):
    """収益に基づいて労働時間比率で配分を計算（累積労働時間ベース、時給は対象期間労働時間ベース）"""
    try:
        # 累積労働時間データを取得（配分計算用）
        cumulative_work_data = get_cumulative_work_hours(end_date)
        # 対象期間の労働時間データを取得（時給計算用）
        period_work_data = get_period_work_hours(start_date, end_date)
        
        # 対象期間の労働時間をユーザーIDでマッピング
        period_hours_map = {data['user'].id: data['period_hours'] for data in period_work_data}
        
        # 累積総労働時間を計算（配分用）
        total_cumulative_hours = sum(data['cumulative_hours'] for data in cumulative_work_data if data['cumulative_hours'] > 0)
        
        if total_cumulative_hours == 0:
            return {
                'total_revenue': revenue,
                'total_cumulative_hours': 0,
                'distributions': [],
                'period_info': {
                    'start_date': start_date,
                    'end_date': end_date
                }
            }
        
        # 各ユーザーへの配分を計算
        distributions = []
        for data in cumulative_work_data:
            if data['cumulative_hours'] > 0:
                # 累積労働時間に基づく配分率
                work_ratio = data['cumulative_hours'] / total_cumulative_hours
                allocated_amount = revenue * work_ratio
                
                # 対象期間の労働時間を取得
                period_hours = period_hours_map.get(data['user'].id, 0)
                
                distributions.append({
                    'user': data['user'],
                    'cumulative_hours': data['cumulative_hours'],  # 累積労働時間（配分用）
                    'period_hours': period_hours,                  # 対象期間労働時間（時給計算用）
                    'work_ratio': round(work_ratio * 100, 2),      # パーセンテージ
                    'allocated_amount': round(allocated_amount, 0)  # 整数に丸める
                })
        
        return {
            'total_revenue': revenue,
            'total_cumulative_hours': round(total_cumulative_hours, 2),  # 累積総労働時間
            'distributions': distributions,
            'period_info': {
                'start_date': start_date,
                'end_date': end_date
            }
        }
    
    except Exception as e:
        logger.error(f"Error calculating revenue distribution: {e}")
        return {
            'total_revenue': revenue,
            'total_cumulative_hours': 0,
            'distributions': [],
            'period_info': {
                'start_date': start_date,
                'end_date': end_date
            }
        }

def get_currently_working_members():
    """
    現在出勤中のメンバーを取得する関数
    """
    try:
        # 今日の日付を取得（日本時間）
        today_jst = datetime.now(JST_TZ).date()
        start_jst = JST_TZ.localize(datetime.combine(today_jst, datetime.min.time()))
        
        # UTC時間に変換
        start_datetime = start_jst.astimezone(timezone.utc)
        
        # 今日の出退勤記録を取得
        attendances = Attendance.query.filter(
            Attendance.timestamp >= start_datetime
        ).order_by(Attendance.timestamp.desc()).all()
        
        # ユーザーごとの最新の出退勤状況を追跡
        user_status = {}
        
        for attendance in attendances:
            user_id = attendance.user_id
            if user_id not in user_status:
                user_status[user_id] = {
                    'last_type': attendance.type,
                    'timestamp': attendance.timestamp,
                    'user': attendance.user
                }
        
        # 現在出勤中のメンバーを抽出
        currently_working = []
        for user_id, status in user_status.items():
            if status['last_type'] == '出勤':
                currently_working.append({
                    'user': status['user'],
                    'checkin_time': status['timestamp']
                })
        
        # 出勤時刻順にソート
        currently_working.sort(key=lambda x: x['checkin_time'])
        
        return currently_working
    
    except Exception as e:
        logger.error(f"Error getting currently working members: {e}")
        return []

# Webアプリケーションのルート
@app.route('/')
def index():
    """ログイン後の出退勤一覧ページ"""
    if 'user_id' not in session:
        return redirect(url_for('login'))
    
    try:
        user = User.query.get(session['user_id'])
        if not user:
            session.clear()
            return redirect(url_for('login'))
        
        # 期間指定を取得
        start_date = request.args.get('start_date')
        end_date = request.args.get('end_date')
        
        if start_date and end_date:
            # 期間指定がある場合（日本時間での指定をUTC時間に変換）
            try:
                # 日本時間で指定された日付をUTC時間に変換
                start_jst = JST_TZ.localize(datetime.fromisoformat(start_date))
                end_jst = JST_TZ.localize(datetime.fromisoformat(end_date))
                end_jst = end_jst.replace(hour=23, minute=59, second=59)
                
                # UTC時間に変換
                start_datetime = start_jst.astimezone(timezone.utc)
                end_datetime = end_jst.astimezone(timezone.utc)
                
                # 指定期間内の出退勤記録を取得
                attendances = Attendance.query.filter(
                    Attendance.user_id == user.id,
                    Attendance.timestamp >= start_datetime,
                    Attendance.timestamp <= end_datetime
                ).order_by(Attendance.timestamp.desc()).all()
                
                formatted_start_date = start_jst.strftime('%Y-%m-%d')
                formatted_end_date = end_jst.strftime('%Y-%m-%d')
                
            except ValueError:
                flash('日付の形式が正しくありません。', 'error')
                return redirect(url_for('index'))
        else:
            # デフォルト：今日の記録を表示（日本時間の今日）
            today_jst = datetime.now(JST_TZ).date()
            start_jst = JST_TZ.localize(datetime.combine(today_jst, datetime.min.time()))
            end_jst = JST_TZ.localize(datetime.combine(today_jst, datetime.max.time()))
            
            # UTC時間に変換
            start_datetime = start_jst.astimezone(timezone.utc)
            end_datetime = end_jst.astimezone(timezone.utc)
            
            # 今日の出退勤記録を取得
            attendances = Attendance.query.filter(
                Attendance.user_id == user.id,
                Attendance.timestamp >= start_datetime,
                Attendance.timestamp <= end_datetime
            ).order_by(Attendance.timestamp.desc()).all()
            
            formatted_start_date = today_jst.strftime('%Y-%m-%d')
            formatted_end_date = today_jst.strftime('%Y-%m-%d')
        
        # 管理者権限チェック用
        admin_user_id = os.environ.get('ADMIN_USER_ID')
        
        # 統計情報を計算（エラーハンドリング強化）
        try:
            personal_statistics = calculate_work_hours_statistics(user.id)
        except Exception as e:
            logger.error(f"Error calculating personal statistics: {e}")
            personal_statistics = {'average_hours': 0, 'median_hours': 0, 'total_hours': 0, 'total_weeks': 0}
        
        try:
            overall_statistics = calculate_work_hours_statistics()  # 全体統計
        except Exception as e:
            logger.error(f"Error calculating overall statistics: {e}")
            overall_statistics = {'average_hours': 0, 'median_hours': 0, 'total_hours': 0, 'total_weeks': 0}

        # 現在出勤中のメンバーを取得
        try:
            currently_working = get_currently_working_members()
        except Exception as e:
            logger.error(f"Error getting currently working members: {e}")
            currently_working = []

        return render_template('index.html', 
                             user=user, 
                             attendances=attendances, 
                             admin_user_id=admin_user_id,
                             personal_statistics=personal_statistics,
                             overall_statistics=overall_statistics,
                             currently_working=currently_working,
                             start_date=formatted_start_date,
                             end_date=formatted_end_date)
    except Exception as e:
        logger.error(f"Error in index route: {e}")
        flash('データの取得中にエラーが発生しました。', 'error')
        return redirect(url_for('login'))

@app.route('/', methods=['POST'])
def handle_slack_events():
    """Slackイベントを処理（ルートパス）"""
    return handler.handle(request)

@app.route('/login')
def login():
    """Slack認証ページ（Modern Sign in with Slack - OpenID Connect）"""
    if 'user_id' in session:
        return redirect(url_for('index'))
    
    # Modern Sign in with Slack (OpenID Connect) のURL
    client_id = os.environ.get('SLACK_CLIENT_ID')
    # OpenID Connect スコープ: openid（必須）, profile（ユーザー名・チーム情報）, email（メールアドレス）
    scope = 'openid profile email'
    redirect_uri = url_for('callback', _external=True)
    
    slack_oauth_url = f"https://slack.com/openid/connect/authorize?client_id={client_id}&scope={scope}&redirect_uri={redirect_uri}&response_type=code"
    
    return render_template('login.html', oauth_url=slack_oauth_url)

@app.route('/callback')
def callback():
    """Slack認証後のコールバック（Modern Sign in with Slack - OpenID Connect）"""
    code = request.args.get('code')
    error = request.args.get('error')
    
    if error:
        logger.error(f"OAuth error: {error}")
        flash(f'認証エラー: {error}', 'error')
        return redirect(url_for('login'))
    
    if not code:
        logger.error("Authorization code not received")
        flash('認証コードが受信されませんでした。', 'error')
        return redirect(url_for('login'))
    
    try:
        # Modern Sign in with Slack (OpenID Connect) のトークン交換エンドポイント
        token_url = "https://slack.com/api/openid.connect.token"
        
        token_data = {
            'client_id': os.environ.get('SLACK_CLIENT_ID'),
            'client_secret': os.environ.get('SLACK_CLIENT_SECRET'),
            'code': code,
            'grant_type': 'authorization_code',
            'redirect_uri': url_for('callback', _external=True)
        }
        
        response = requests.post(token_url, data=token_data)
        token_response = response.json()
        
        if not token_response.get('ok', False):
            logger.error(f"Token exchange failed: {token_response}")
            flash('認証に失敗しました。', 'error')
            return redirect(url_for('login'))
        
        access_token = token_response.get('access_token')
        id_token = token_response.get('id_token')  # JWT形式のIDトークン
        
        if not access_token:
            logger.error("Access token not received")
            flash('アクセストークンが受信されませんでした。', 'error')
            return redirect(url_for('login'))
        
        # OpenID Connect userInfo エンドポイントでユーザー情報を取得
        user_info_url = "https://slack.com/api/openid.connect.userInfo"
        headers = {'Authorization': f'Bearer {access_token}'}
        
        user_response = requests.get(user_info_url, headers=headers)
        user_data = user_response.json()
        
        if not user_data.get('ok', False):
            logger.error(f"User info request failed: {user_data}")
            flash('ユーザー情報の取得に失敗しました。', 'error')
            return redirect(url_for('login'))
        
        # OpenID Connect レスポンスから必要な情報を取得
        slack_user_id = user_data.get('sub')  # OpenID Connect標準のsubject ID
        user_name = user_data.get('name', 'Unknown User')
        user_email = user_data.get('email', '')
        team_id = user_data.get('https://slack.com/team_id')
        
        if not slack_user_id:
            logger.error("Slack user ID not found in response")
            flash('ユーザーIDの取得に失敗しました。', 'error')
            return redirect(url_for('login'))
        
        # ユーザーを取得または作成
        user = User.query.filter_by(slack_user_id=slack_user_id).first()
        
        if not user:
            user = User(
                slack_user_id=slack_user_id,
                display_name=user_name,
                email=user_email
            )
            db.session.add(user)
            db.session.commit()
            logger.info(f"Created new user: {slack_user_id}")
        else:
            # 既存ユーザーの情報を更新
            user.display_name = user_name
            user.email = user_email
            db.session.commit()
            logger.info(f"Updated user info: {slack_user_id}")
        
        # セッションに保存
        session.permanent = True  # セッションを永続化
        session['user_id'] = user.id
        session['slack_user_id'] = slack_user_id
        session['user_name'] = user_name
        session['team_id'] = team_id
        
        flash(f'{user_name} さん、ようこそ！', 'success')
        return redirect(url_for('index'))
        
    except requests.RequestException as e:
        logger.error(f"Request error during OAuth: {e}")
        flash('認証処理中にネットワークエラーが発生しました。', 'error')
        return redirect(url_for('login'))
    except Exception as e:
        logger.error(f"Unexpected error during OAuth: {e}")
        flash('認証処理中に予期しないエラーが発生しました。', 'error')
        return redirect(url_for('login'))

@app.route('/logout')
def logout():
    """ログアウト処理"""
    session.clear()
    flash('ログアウトしました。', 'info')
    return redirect(url_for('login'))

@app.route('/attendance/add', methods=['POST'])
def add_attendance():
    """出退勤記録の新規追加"""
    if 'user_id' not in session:
        return jsonify({'error': 'ログインが必要です'}), 401
    
    try:
        data = request.get_json()
        
        if not data.get('type') or not data.get('timestamp'):
            return jsonify({'error': '種別と日時は必須です'}), 400
        
        if data['type'] not in ['出勤', '退勤']:
            return jsonify({'error': '種別は「出勤」または「退勤」である必要があります'}), 400
        
        try:
            # 日本時間で入力された時間をUTC時間に変換
            jst_timestamp = datetime.fromisoformat(data['timestamp'])
            jst_timezone_aware = JST_TZ.localize(jst_timestamp)
            timestamp = jst_timezone_aware.astimezone(timezone.utc)
        except ValueError:
            return jsonify({'error': '日時の形式が正しくありません'}), 400
        
        # 新規出退勤記録を作成
        attendance = Attendance(
            user_id=session['user_id'],
            type=data['type'],
            timestamp=timestamp
        )
        
        db.session.add(attendance)
        db.session.commit()
        
        return jsonify({'message': '記録を追加しました', 'attendance': attendance.to_dict()})
    except Exception as e:
        logger.error(f"Error adding attendance: {e}")
        return jsonify({'error': '追加中にエラーが発生しました'}), 500

@app.route('/attendance/update/<int:id>', methods=['POST'])
def update_attendance(id):
    """出退勤記録の更新"""
    if 'user_id' not in session:
        return jsonify({'error': 'ログインが必要です'}), 401
    
    try:
        attendance = Attendance.query.get(id)
        if not attendance:
            return jsonify({'error': '記録が見つかりません'}), 404
        
        # 所有者チェック
        if attendance.user_id != session['user_id']:
            return jsonify({'error': '権限がありません'}), 403
        
        data = request.get_json()
        
        if 'type' in data:
            attendance.type = data['type']
        
        if 'timestamp' in data:
            try:
                # 日本時間で入力された時間をUTC時間に変換
                jst_timestamp = datetime.fromisoformat(data['timestamp'])
                jst_timezone_aware = JST_TZ.localize(jst_timestamp)
                attendance.timestamp = jst_timezone_aware.astimezone(timezone.utc)
            except ValueError:
                return jsonify({'error': '日時の形式が正しくありません'}), 400
        
        attendance.updated_at = datetime.now(timezone.utc)
        db.session.commit()
        
        return jsonify({'message': '更新しました', 'attendance': attendance.to_dict()})
    except Exception as e:
        logger.error(f"Error updating attendance: {e}")
        return jsonify({'error': '更新中にエラーが発生しました'}), 500

@app.route('/attendance/delete/<int:id>', methods=['DELETE'])
def delete_attendance(id):
    """出退勤記録の削除"""
    if 'user_id' not in session:
        return jsonify({'error': 'ログインが必要です'}), 401
    
    try:
        attendance = Attendance.query.get(id)
        if not attendance:
            return jsonify({'error': '記録が見つかりません'}), 404
        
        # 所有者チェック
        if attendance.user_id != session['user_id']:
            return jsonify({'error': '権限がありません'}), 403
        
        db.session.delete(attendance)
        db.session.commit()
        
        return jsonify({'message': '削除しました'})
    except Exception as e:
        logger.error(f"Error deleting attendance: {e}")
        return jsonify({'error': '削除中にエラーが発生しました'}), 500

@app.route('/admin')
def admin():
    """管理者用の全ユーザー出退勤一覧ページ"""
    if 'user_id' not in session:
        return redirect(url_for('login'))
    
    try:
        # 管理者チェック
        user = User.query.get(session['user_id'])
        admin_user_id = os.environ.get('ADMIN_USER_ID')
        
        if not user or user.slack_user_id != admin_user_id:
            flash('管理者権限が必要です。', 'error')
            return redirect(url_for('index'))
        
        # 今日の全ユーザーの出退勤記録を取得（日本時間）
        today_jst = datetime.now(JST_TZ).date()
        start_jst = JST_TZ.localize(datetime.combine(today_jst, datetime.min.time()))
        end_jst = JST_TZ.localize(datetime.combine(today_jst, datetime.max.time()))
        
        # UTC時間に変換
        start_datetime = start_jst.astimezone(timezone.utc)
        end_datetime = end_jst.astimezone(timezone.utc)
        
        attendances = db.session.query(Attendance, User).join(User).filter(
            Attendance.timestamp >= start_datetime,
            Attendance.timestamp <= end_datetime
        ).order_by(Attendance.timestamp.desc()).all()
        
        # 全ユーザーの情報を取得（ユーザー一覧表示用）
        users = User.query.all()
        
        # 各ユーザーの最新の出退勤記録を取得
        users_with_last_attendance = []
        for u in users:
            last_attendance = Attendance.query.filter_by(user_id=u.id).order_by(Attendance.timestamp.desc()).first()
            users_with_last_attendance.append({
                'user': u,
                'last_attendance': last_attendance
            })
        
        # 全体の統計情報を計算（エラーハンドリング強化）
        try:
            statistics_data = calculate_work_hours_statistics()
        except Exception as e:
            logger.error(f"Error calculating admin statistics: {e}")
            statistics_data = {'average_hours': 0, 'median_hours': 0, 'total_hours': 0, 'total_weeks': 0}
        
        return render_template('admin.html', 
                             attendances=attendances,
                             users_with_last_attendance=users_with_last_attendance,
                             statistics=statistics_data,
                             admin_user_id=admin_user_id)
    except Exception as e:
        logger.error(f"Error in admin route: {e}")
        flash('データの取得中にエラーが発生しました。', 'error')
        return redirect(url_for('index'))

@app.route('/admin/user/<int:user_id>')
def admin_user_detail(user_id):
    """管理者用の個別ユーザー詳細ページ"""
    if 'user_id' not in session:
        return redirect(url_for('login'))
    
    try:
        # 管理者チェック
        admin_user = User.query.get(session['user_id'])
        admin_user_id = os.environ.get('ADMIN_USER_ID')
        
        if not admin_user or admin_user.slack_user_id != admin_user_id:
            flash('管理者権限が必要です。', 'error')
            return redirect(url_for('index'))
        
        # 対象ユーザーを取得
        target_user = User.query.get(user_id)
        if not target_user:
            flash('ユーザーが見つかりません。', 'error')
            return redirect(url_for('admin'))
        
        # 期間指定を取得（デフォルトは過去30日）
        start_date = request.args.get('start_date')
        end_date = request.args.get('end_date')
        
        if start_date and end_date:
            try:
                # 日本時間で指定された日付をUTC時間に変換
                start_jst = JST_TZ.localize(datetime.fromisoformat(start_date))
                end_jst = JST_TZ.localize(datetime.fromisoformat(end_date))
                end_jst = end_jst.replace(hour=23, minute=59, second=59)
                
                # UTC時間に変換
                start_datetime = start_jst.astimezone(timezone.utc)
                end_datetime = end_jst.astimezone(timezone.utc)
            except ValueError:
                flash('日付の形式が正しくありません。', 'error')
                return redirect(url_for('admin_user_detail', user_id=user_id))
        else:
            # デフォルト：過去30日間（日本時間基準）
            end_jst = datetime.now(JST_TZ)
            start_jst = end_jst - timedelta(days=30)
            
            # UTC時間に変換
            end_datetime = end_jst.astimezone(timezone.utc)
            start_datetime = start_jst.astimezone(timezone.utc)
        
        # 指定期間内のユーザーの出退勤記録を取得
        attendances = Attendance.query.filter(
            Attendance.user_id == user_id,
            Attendance.timestamp >= start_datetime,
            Attendance.timestamp <= end_datetime
        ).order_by(Attendance.timestamp.desc()).all()
        
        # 個別ユーザーの統計情報を計算
        try:
            user_statistics = calculate_work_hours_statistics(user_id)
        except Exception as e:
            logger.error(f"Error calculating user statistics: {e}")
            user_statistics = {'average_hours': 0, 'median_hours': 0, 'total_hours': 0, 'total_weeks': 0}
        
        # 期間指定のフォーマット（日本時間で表示）
        if start_date and end_date:
            formatted_start_date = start_jst.strftime('%Y-%m-%d')
            formatted_end_date = end_jst.strftime('%Y-%m-%d')
        else:
            formatted_start_date = start_jst.strftime('%Y-%m-%d')
            formatted_end_date = end_jst.strftime('%Y-%m-%d')
        
        return render_template('admin_user_detail.html', 
                             target_user=target_user,
                             attendances=attendances,
                             user_statistics=user_statistics,
                             start_date=formatted_start_date,
                             end_date=formatted_end_date,
                             admin_user_id=admin_user_id)
    except Exception as e:
        logger.error(f"Error in admin_user_detail route: {e}")
        flash('データの取得中にエラーが発生しました。', 'error')
        return redirect(url_for('admin'))

@app.route('/admin/accounting', methods=['GET', 'POST'])
def admin_accounting():
    """管理者用決算ページ"""
    if 'user_id' not in session:
        return redirect(url_for('login'))
    
    try:
        # 管理者チェック
        user = User.query.get(session['user_id'])
        admin_user_id = os.environ.get('ADMIN_USER_ID')
        
        if not user or user.slack_user_id != admin_user_id:
            flash('管理者権限が必要です。', 'error')
            return redirect(url_for('index'))
        
        # 期間パラメータを取得
        start_date = request.form.get('start_date') or request.args.get('start_date')
        end_date = request.form.get('end_date') or request.args.get('end_date')
        
        # デフォルト期間設定（今月）
        if not start_date or not end_date:
            now_jst = datetime.now(JST_TZ)
            start_date = f"{now_jst.year}-{now_jst.month:02d}-01"
            
            # 今月末日を計算
            if now_jst.month == 12:
                next_month = datetime(now_jst.year + 1, 1, 1)
            else:
                next_month = datetime(now_jst.year, now_jst.month + 1, 1)
            last_day = (next_month - timedelta(days=1)).day
            end_date = f"{now_jst.year}-{now_jst.month:02d}-{last_day:02d}"
        
        # POSTリクエストの場合（収益計算実行）
        calculated_data = None
        if request.method == 'POST':
            try:
                revenue = float(request.form.get('revenue', 0))
                if revenue <= 0:
                    flash('正の収益額を入力してください。', 'error')
                else:
                    calculated_data = calculate_revenue_distribution(revenue, start_date, end_date)
                    flash('収益配分を計算しました。', 'success')
            except ValueError:
                flash('正しい数値を入力してください。', 'error')
        
        # 累積労働時間データを取得（表示用）
        user_work_data = get_cumulative_work_hours(end_date)
        
        return render_template('admin_accounting.html',
                             user_work_data=user_work_data,
                             calculated_data=calculated_data,
                             admin_user_id=admin_user_id,
                             start_date=start_date,
                             end_date=end_date)
    except Exception as e:
        logger.error(f"Error in admin_accounting route: {e}")
        flash('データの取得中にエラーが発生しました。', 'error')
        return redirect(url_for('admin'))

# Slack イベントエンドポイント
@app.route('/slack/events', methods=['POST'])
def slack_events():
    """Slack イベントを処理"""
    return handler.handle(request)

# ヘルスチェックエンドポイント（デプロイ最適化）
@app.route('/health')
def health_check():
    """ヘルスチェックエンドポイント"""
    try:
        # データベース接続確認
        db.session.execute('SELECT 1')
        return jsonify({'status': 'healthy', 'database': 'connected'}), 200
    except Exception as e:
        logger.error(f"Health check failed: {e}")
        return jsonify({'status': 'unhealthy', 'error': str(e)}), 503

# Favicon エンドポイント（404エラー対策）
@app.route('/favicon.ico')
def favicon():
    """Faviconエンドポイント（404エラー対策）"""
    return '', 204

# 簡易診断用（必要に応じて無効化推奨）
@app.route('/diag')
def diag():
    try:
        key_present = bool(os.getenv('OPENAI_API_KEY') or os.getenv('OPEN_AI') or os.getenv('OPENAI_KEY'))
        model = os.getenv('OPENAI_MODEL')
        gh_repo = os.getenv('GITHUB_REPOSITORY')
        gh_tok_present = bool(os.getenv('GITHUB_TOKEN') or os.getenv('GITHUB_PAT'))
        return jsonify({
            'openai_key_present': key_present,
            'openai_model': model,
            'github_repo': gh_repo,
            'github_token_present': gh_tok_present,
        }), 200
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# データベース初期化コマンド
@app.cli.command()
def init_db():
    """データベースを初期化"""
    try:
        db.create_all()
        logger.info('データベースが初期化されました。')
    except Exception as e:
        logger.error(f"Database initialization failed: {e}")
        raise

# アプリケーション初期化関数
def create_app():
    """アプリケーションファクトリー関数"""
    try:
        with app.app_context():
            # データベーステーブルの作成（存在しない場合のみ）
            db.create_all()
            logger.info("Database tables created/verified successfully")
    except Exception as e:
        logger.error(f"Failed to initialize database: {e}")
        # データベース接続エラーでもアプリケーションは起動を続行
        pass
    
    return app

# Gunicorn用の初期化（本番環境）
if __name__ != '__main__':
    # Gunicornから起動される場合（本番環境）
    gunicorn_logger = logging.getLogger('gunicorn.error')
    app.logger.handlers = gunicorn_logger.handlers
    app.logger.setLevel(gunicorn_logger.level)
    create_app()

if __name__ == '__main__':
    # 開発環境での直接実行
    create_app()
    port = int(os.environ.get('PORT', 5000))  # PORT環境変数を使用
    app.run(debug=True, host='0.0.0.0', port=port) 
