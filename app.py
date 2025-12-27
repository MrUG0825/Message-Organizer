import os
import json
import re
from urllib.parse import quote, unquote
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo

from flask import Flask, request, abort, send_from_directory
from werkzeug.utils import safe_join

from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    Configuration,
    ApiClient,
    MessagingApi,
    ReplyMessageRequest,
    PushMessageRequest,
    TextMessage,
)
from linebot.v3.webhooks import (
    MessageEvent,
    TextMessageContent,
    FollowEvent,
    JoinEvent,
    PostbackEvent,
)

from dotenv import load_dotenv

# -----------------------------
# Load env
# -----------------------------
load_dotenv()

# -----------------------------
# Config
# -----------------------------
TZ_NAME = os.getenv("TZ_NAME", "Asia/Taipei")
TPE_TZ = ZoneInfo("Asia/Taipei")

LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "")
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET", "")

# 重要：用來產生「檔案下載連結」的公開 Base URL
# 例如用 ngrok：https://xxxx-xxxx.ngrok-free.app
# 需對外可連到你這台 Flask 伺服器
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")

print("ENV OK:", bool(LINE_CHANNEL_ACCESS_TOKEN), bool(LINE_CHANNEL_SECRET))
if not LINE_CHANNEL_ACCESS_TOKEN or not LINE_CHANNEL_SECRET:
    print(
        "[WARN] Missing env vars: LINE_CHANNEL_ACCESS_TOKEN / LINE_CHANNEL_SECRET. "
        "Set them before deploying."
    )

# -----------------------------
# Storage
# -----------------------------
BASE_DIR = Path(os.getenv("BOT_DATA_DIR", "./bot_data"))
LOG_DIR = BASE_DIR / "logs"  # logs/<chat_id>/YYYY-MM-DD.jsonl
CFG_DIR = BASE_DIR / "configs"  # configs/<chat_id>.json
OUT_DIR = BASE_DIR / "exports"  # exports/YYYY-MM/*.txt  (集中同資料夾)

for d in (LOG_DIR, CFG_DIR, OUT_DIR):
    d.mkdir(parents=True, exist_ok=True)


def now_tpe() -> datetime:
    """保證 Asia/Taipei 時區的現在時間（Python 3.9+ 內建 zoneinfo）。"""
    return datetime.now(tz=TPE_TZ)


def today_str(dt: datetime | None = None) -> str:
    dt = dt or now_tpe()
    return dt.strftime("%Y-%m-%d")


def ym_str(dt: datetime | None = None) -> str:
    dt = dt or now_tpe()
    return dt.strftime("%Y-%m")


def safe_name(s: str) -> str:
    """讓檔名安全、避免特殊字元。"""
    return re.sub(r"[^0-9A-Za-z\u4e00-\u9fff._-]+", "_", s).strip("_")


def get_chat_id(event) -> str:
    """把 user / group / room 都統一成一個可用的 chat_id。"""
    src = getattr(event, "source", None)
    if not src:
        return "unknown"
    for attr in ("group_id", "room_id", "user_id"):
        v = getattr(src, attr, None)
        if v:
            return v
    return "unknown"


def cfg_path(chat_id: str) -> Path:
    return CFG_DIR / f"{chat_id}.json"


def load_cfg(chat_id: str) -> dict:
    p = cfg_path(chat_id)
    if not p.exists():
        return {"keywords": [], "daily_enabled": False, "daily_time": "23:59"}
    try:
        cfg = json.loads(p.read_text(encoding="utf-8"))
        if "keywords" not in cfg:
            cfg["keywords"] = []
        if "daily_enabled" not in cfg:
            cfg["daily_enabled"] = False
        if "daily_time" not in cfg:
            cfg["daily_time"] = "23:59"
        return cfg
    except Exception:
        return {"keywords": [], "daily_enabled": False, "daily_time": "23:59"}


def save_cfg(chat_id: str, cfg: dict) -> None:
    cfg_path(chat_id).write_text(
        json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def is_command_text(text: str) -> bool:
    t = (text or "").strip()
    if t in {"功能選單", "menu", "選單"}:
        return True
    prefixes = (
        "設定關鍵字",
        "刪除關鍵字",
        "設定每日時間",
    )
    equals = {
        "查看關鍵字",
        "關鍵字",
        "keywords",
        "立即整理",
        "整理",
        "run",
        "啟用每日整理",
        "停用每日整理",
    }
    return t in equals or t.startswith(prefixes)


def append_log(chat_id: str, message_text: str, event) -> None:
    day = today_str()
    d = LOG_DIR / chat_id
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{day}.jsonl"

    src = getattr(event, "source", None)
    payload = {
        "ts": now_tpe().isoformat(timespec="seconds"),
        "chat_id": chat_id,
        "source_type": getattr(src, "type", None),
        "user_id": getattr(src, "user_id", None),
        "text": message_text,
        "is_command": is_command_text(message_text),
    }
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=False) + "\n")


def build_public_url(rel_path: str) -> str | None:
    """
    rel_path: 例如 "exports/2025-12/2025-12-23_xxx.txt"
    需要 PUBLIC_BASE_URL 才能回傳完整公開 URL
    """
    if not PUBLIC_BASE_URL:
        return None
    rel_path = rel_path.lstrip("/")
    return f"{PUBLIC_BASE_URL}/{rel_path}"


def export_per_keyword(chat_id: str, *, manual: bool) -> tuple[bool, str, list[Path]]:
    """
    功能：
    - 每個 keyword 各自輸出一個 txt
    - 檔名：YYYY-MM-DD_{keyword}_{groupId}.txt
    - txt 內容：只保留乾淨的「符合該 keyword 的訊息」(不含 header / ts / user_id / 指令)
    - 回傳輸出的檔案路徑清單
    """
    cfg = load_cfg(chat_id)
    keywords: list[str] = [
        k for k in cfg.get("keywords", []) if isinstance(k, str) and k.strip()
    ]
    keywords = [k.strip() for k in keywords]
    if not keywords:
        return (
            False,
            "尚未設定關鍵字。\n\n請先輸入：\n請輸入：設定關鍵字 你的關鍵字\n例如：設定關鍵字 日報表",
            [],
        )

    day = today_str()
    log_file = LOG_DIR / chat_id / f"{day}.jsonl"
    if not log_file.exists():
        return (True, f"今天 ({day}) 尚無紀錄訊息可整理。", [])

    # 讀取今日訊息（先濾掉指令）
    texts: list[str] = []
    total = 0
    with log_file.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            total += 1
            try:
                obj = json.loads(line)
            except Exception:
                continue
            if obj.get("is_command"):
                continue
            t = str(obj.get("text", "")).strip()
            if t:
                texts.append(t)

    if not texts:
        return (True, f"今天 ({day}) 尚無可整理內容（可能全是指令或空訊息）。", [])

    ym = ym_str()
    out_dir = OUT_DIR / ym
    out_dir.mkdir(parents=True, exist_ok=True)

    chat_safe = safe_name(chat_id)  # 你要求要含群組id（完整保留）
    written: list[Path] = []
    matched_total = 0

    for kw in keywords:
        kw_safe = safe_name(kw)
        out_file = out_dir / f"{day}_{kw_safe}_{chat_safe}.txt"

        matched_lines = [t for t in texts if kw in t]
        if not matched_lines:
            continue

        out_file.write_text("\n".join(matched_lines).strip() + "\n", encoding="utf-8")
        written.append(out_file)
        matched_total += len(matched_lines)

    mode = "手動" if manual else "自動"
    if not written:
        return (
            True,
            f"今日 ({day}) 共記錄 {total} 則訊息，但沒有符合關鍵字：{', '.join(keywords)}",
            [],
        )

    msg = (
        f"{mode}整理完成 ✅\n"
        f"日期：{day}\n"
        f"輸出檔案：{len(written)} 份\n"
        f"（每個關鍵字各 1 份，已存到：{out_dir}）"
    )
    return (True, msg, written)


# -----------------------------
# LINE API helpers
# -----------------------------
configuration = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)


def reply_text(reply_token: str, text: str):
    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)
        line_bot_api.reply_message_with_http_info(
            ReplyMessageRequest(
                reply_token=reply_token, messages=[TextMessage(text=text)]
            )
        )


def reply_texts(reply_token: str, texts: list[str]):
    # LINE 一次 reply messages 有數量限制；這裡做個安全分段
    chunks = []
    for t in texts:
        if t:
            chunks.append(TextMessage(text=t[:4900]))
    if not chunks:
        return
    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)
        line_bot_api.reply_message_with_http_info(
            ReplyMessageRequest(reply_token=reply_token, messages=chunks[:5])
        )


def push_text(to: str, text: str):
    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)
        line_bot_api.push_message_with_http_info(
            PushMessageRequest(to=to, messages=[TextMessage(text=text)])
        )


def push_texts(to: str, texts: list[str]):
    chunks = []
    for t in texts:
        if t:
            chunks.append(TextMessage(text=t[:4900]))
    if not chunks:
        return
    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)
        line_bot_api.push_message_with_http_info(
            PushMessageRequest(to=to, messages=chunks[:5])
        )


def reply_menu(reply_token: str):
    """回傳 Carousel Template，讓使用者選擇功能。"""
    try:
        from linebot.v3.messaging import (
            TemplateMessage,
            CarouselTemplate,
            CarouselColumn,
            PostbackAction,
        )
    except Exception:
        return reply_text(
            reply_token,
            "功能選單（純文字模式）\n\n"
            "1) 設定關鍵字：設定關鍵字 日報表\n"
            "2) 立即整理：立即整理\n"
            "3) 查看關鍵字：查看關鍵字\n"
            "4) 設定每日時間：設定每日時間 23:55\n"
            "5) 啟用/停用：啟用每日整理 / 停用每日整理\n"
            "6) 刪除關鍵字：點選刪除關鍵字後一鍵刪\n",
        )

    template = CarouselTemplate(
        columns=[
            CarouselColumn(
                title="每日訊息整理",
                text="設定關鍵字、立即整理、查看關鍵字",
                actions=[
                    PostbackAction(label="設定關鍵字", data="action=set_keyword"),
                    PostbackAction(label="立即整理", data="action=run_now"),
                    PostbackAction(label="查看關鍵字", data="action=list_keyword"),
                ],
            ),
            CarouselColumn(
                title="每日定時設定",
                text="設定每日時間、啟用/停用每日整理",
                actions=[
                    PostbackAction(label="設定每日時間", data="action=set_daily_time"),
                    PostbackAction(label="啟用每日整理", data="action=enable_daily"),
                    PostbackAction(label="停用每日整理", data="action=disable_daily"),
                ],
            ),
            CarouselColumn(
                title="關鍵字管理",
                text="一鍵刪除關鍵字（不用手打）",
                actions=[
                    PostbackAction(label="刪除關鍵字", data="action=delete_keyword"),
                    PostbackAction(label="查看關鍵字", data="action=list_keyword"),
                    PostbackAction(label="立即整理", data="action=run_now"),
                ],
            ),
        ]
    )

    msg = TemplateMessage(alt_text="功能選單", template=template)
    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)
        line_bot_api.reply_message_with_http_info(
            ReplyMessageRequest(reply_token=reply_token, messages=[msg])
        )


# -----------------------------
# Flask
# -----------------------------
app = Flask(__name__)


@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)
    app.logger.info("Request body: %s", body)

    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        app.logger.info(
            "Invalid signature. Please check your channel access token/channel secret."
        )
        abort(400)

    return "OK"


# 對外提供 exports 檔案下載（聊天室備份用）
# 下載路徑：/exports/YYYY-MM/<filename>
@app.route("/exports/<ym>/<path:filename>", methods=["GET"])
def download_export(ym: str, filename: str):
    ym = safe_name(ym)
    base = OUT_DIR / ym
    base.mkdir(parents=True, exist_ok=True)
    return send_from_directory(directory=str(base), path=filename, as_attachment=True)


# -----------------------------
# Welcome message
# -----------------------------
@handler.add(FollowEvent)
def handle_follow(event):
    reply_text(
        event.reply_token,
        "嗨～歡迎加入 ✨\n"
        "我是『訊息整理小幫手』，可以把你指定關鍵字的訊息整理成日報（txt）。\n\n"
        "先試試看：\n"
        "1) 輸入『功能選單』開啟功能\n"
        "2) 或直接輸入：設定關鍵字 日報表\n",
    )


@handler.add(JoinEvent)
def handle_join(event):
    reply_text(
        event.reply_token,
        "大家好～我進來了 👋\n"
        "我可以把含特定關鍵字的訊息整理成 txt 日報。\n"
        "輸入『功能選單』開始設定。",
    )


# -----------------------------
# Scheduler (per-chat)
# -----------------------------
SCHED = None


def send_export_links(chat_id: str, files: list[Path], *, is_push: bool):
    """
    把輸出的檔案「在聊天室回一份備份」：
    - 若有 PUBLIC_BASE_URL：回傳每個檔案的下載連結（推薦）
    - 若沒有：回傳本機路徑提示
    """
    if not files:
        return

    ym = ym_str()
    msgs: list[str] = []

    if PUBLIC_BASE_URL:
        msgs.append("📎 已產生文字檔備份（點連結下載）：")
        for p in files[:10]:  # 避免一次太多
            rel = f"exports/{ym}/{p.name}"
            url = build_public_url(rel)
            msgs.append(f"- {p.name}\n{url}")
    else:
        msgs.append(
            "📎 已產生文字檔（本機路徑如下；如要聊天室可下載，請設定 PUBLIC_BASE_URL）："
        )
        for p in files[:10]:
            msgs.append(f"- {p}")

    if is_push:
        push_texts(chat_id, msgs)
    else:
        # reply 會需要 reply_token，外部會呼叫 reply_texts
        # 這裡只回傳 msgs 讓呼叫者 reply
        pass

    return msgs


def run_chat_daily(chat_id: str):
    ok, msg, files = export_per_keyword(chat_id, manual=False)
    print(f"[DAILY] {chat_id}: {msg}")

    # 先推播整理結果
    try:
        push_text(chat_id, msg)
    except Exception as e:
        print(f"[WARN] push msg failed for {chat_id}: {e}")

    # 再推播檔案連結（聊天室備份）
    try:
        msgs = send_export_links(chat_id, files, is_push=True)
        if msgs:
            print(f"[DAILY] {chat_id}: sent {len(files)} file link(s)")
    except Exception as e:
        print(f"[WARN] push file links failed for {chat_id}: {e}")


def remove_chat_job(chat_id: str):
    global SCHED
    if not SCHED:
        return
    try:
        SCHED.remove_job(job_id=f"daily_{chat_id}")
    except Exception:
        pass


def reschedule_chat_job(chat_id: str):
    global SCHED
    if not SCHED:
        return

    cfg = load_cfg(chat_id)
    if not cfg.get("daily_enabled"):
        remove_chat_job(chat_id)
        return

    t = cfg.get("daily_time", "23:59")
    try:
        hh, mm = t.split(":")
        hh, mm = int(hh), int(mm)
        if not (0 <= hh <= 23 and 0 <= mm <= 59):
            raise ValueError
    except Exception:
        hh, mm = 23, 59
        cfg["daily_time"] = "23:59"
        save_cfg(chat_id, cfg)

    from apscheduler.triggers.cron import CronTrigger

    SCHED.add_job(
        func=run_chat_daily,
        trigger=CronTrigger(hour=hh, minute=mm),
        args=[chat_id],
        id=f"daily_{chat_id}",
        replace_existing=True,
        misfire_grace_time=300,
    )
    print(f"[INFO] Job scheduled: daily_{chat_id} at {hh:02d}:{mm:02d}")


def setup_scheduler():
    global SCHED
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
    except Exception:
        print(
            "[WARN] APScheduler not installed. Daily scheduling disabled. "
            "Install: pip install APScheduler"
        )
        return None

    SCHED = BackgroundScheduler(timezone=TZ_NAME)
    SCHED.start()
    print("[INFO] Scheduler started.")

    # 啟動時載入所有已啟用的聊天室排程
    for p in CFG_DIR.glob("*.json"):
        chat_id = p.stem
        reschedule_chat_job(chat_id)

    return SCHED


def maybe_start_scheduler():
    """
    flask run 會 import app.py，不會走 __main__。
    所以排程要在 import 時啟動。
    為避免重複啟動，用 SCHED 判斷是否已啟動。
    """
    global SCHED
    if SCHED is not None:
        return
    if os.getenv("ENABLE_SCHEDULER", "1") != "1":
        print("[INFO] Scheduler disabled by ENABLE_SCHEDULER=0")
        return
    try:
        setup_scheduler()
        if SCHED is not None:
            print("[INFO] Scheduler started (flask run).")
    except Exception as e:
        print(f"[WARN] setup_scheduler failed: {e}")


# -----------------------------
# Postback (Carousel actions)
# -----------------------------
@handler.add(PostbackEvent)
def handle_postback(event):
    data = getattr(getattr(event, "postback", None), "data", "") or ""
    chat_id = get_chat_id(event)

    # 一鍵刪除：點某個 kw 按鈕
    if data.startswith("action=del_kw&kw="):
        kw = unquote(data.split("kw=", 1)[1])
        cfg = load_cfg(chat_id)
        before = cfg.get("keywords", [])
        cfg["keywords"] = [k for k in before if k != kw]
        save_cfg(chat_id, cfg)
        return reply_text(event.reply_token, f"已刪除關鍵字 ✅\n- {kw}")

    if data == "action=set_keyword":
        return reply_text(
            event.reply_token,
            "請輸入：設定關鍵字 你的關鍵字\n例如：設定關鍵字 日報表",
        )

    if data == "action=run_now":
        ok, msg, files = export_per_keyword(chat_id, manual=True)

        # 先回覆整理結果
        reply_text(event.reply_token, msg)

        # 再回覆檔案連結（聊天室備份）
        try:
            msgs = send_export_links(chat_id, files, is_push=False)
            if msgs:
                # reply_token 用掉了，這裡用 push 回到同聊天室
                push_texts(chat_id, msgs)
        except Exception as e:
            print(f"[WARN] send links failed: {e}")
        return

    if data == "action=list_keyword":
        cfg = load_cfg(chat_id)
        kws = cfg.get("keywords", [])
        if not kws:
            return reply_text(
                event.reply_token,
                "目前尚未設定任何關鍵字。\n請輸入：設定關鍵字 你的關鍵字\n例如：設定關鍵字 日報表",
            )
        return reply_text(event.reply_token, "目前關鍵字：\n- " + "\n- ".join(kws))

    if data == "action=delete_keyword":
        cfg = load_cfg(chat_id)
        kws = cfg.get("keywords", [])
        if not kws:
            return reply_text(event.reply_token, "目前沒有關鍵字可刪。")

        # 用 QuickReply 做「每個 kw 一顆按鈕」
        try:
            from linebot.v3.messaging import QuickReply, QuickReplyItem, PostbackAction

            items = []
            for kw in kws:
                items.append(
                    QuickReplyItem(
                        action=PostbackAction(
                            label=f"刪除：{kw}",
                            data=f"action=del_kw&kw={quote(kw)}",
                        )
                    )
                )

            with ApiClient(configuration) as api_client:
                line_bot_api = MessagingApi(api_client)
                line_bot_api.reply_message_with_http_info(
                    ReplyMessageRequest(
                        reply_token=event.reply_token,
                        messages=[
                            TextMessage(
                                text="點選要刪除的關鍵字：",
                                quick_reply=QuickReply(items=items),
                            )
                        ],
                    )
                )
            return
        except Exception:
            return reply_text(
                event.reply_token,
                "請輸入：刪除關鍵字 你的關鍵字\n例如：刪除關鍵字 日報表",
            )

    if data == "action=set_daily_time":
        return reply_text(
            event.reply_token,
            "請輸入：設定每日時間 HH:MM\n例如：設定每日時間 23:55",
        )

    if data == "action=enable_daily":
        cfg = load_cfg(chat_id)
        cfg["daily_enabled"] = True
        save_cfg(chat_id, cfg)
        if SCHED:
            reschedule_chat_job(chat_id)
        return reply_text(
            event.reply_token,
            f"已啟用每日整理 ✅\n時間：{cfg.get('daily_time','23:59')}",
        )

    if data == "action=disable_daily":
        cfg = load_cfg(chat_id)
        cfg["daily_enabled"] = False
        save_cfg(chat_id, cfg)
        if SCHED:
            remove_chat_job(chat_id)
        return reply_text(event.reply_token, "已停用每日整理 ✅")

    return reply_text(
        event.reply_token,
        "已收到操作，但我看不懂這個指令 😅\n輸入『功能選單』再試一次。",
    )


# -----------------------------
# Text messages
# -----------------------------
@handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event):
    text = (event.message.text or "").strip()
    chat_id = get_chat_id(event)

    # 先記錄（避免漏紀錄）
    if text:
        append_log(chat_id, text, event)

    # --- commands ---
    if text in {"功能選單", "menu", "選單"}:
        return reply_menu(event.reply_token)

    if text.startswith("設定關鍵字"):
        kw = text.replace("設定關鍵字", "", 1).strip()
        if not kw:
            return reply_text(
                event.reply_token,
                "格式：設定關鍵字 你的關鍵字\n例如：設定關鍵字 日報表",
            )
        cfg = load_cfg(chat_id)
        kws = set(cfg.get("keywords", []))
        kws.add(kw)
        cfg["keywords"] = sorted(kws)
        save_cfg(chat_id, cfg)
        return reply_text(
            event.reply_token,
            f"已新增關鍵字 ✅\n- {kw}\n\n輸入『立即整理』可馬上測試。",
        )

    if text.startswith("刪除關鍵字"):
        kw = text.replace("刪除關鍵字", "", 1).strip()
        if not kw:
            return reply_text(
                event.reply_token,
                "格式：刪除關鍵字 你的關鍵字\n例如：刪除關鍵字 日報表",
            )
        cfg = load_cfg(chat_id)
        before = cfg.get("keywords", [])
        cfg["keywords"] = [k for k in before if k != kw]
        save_cfg(chat_id, cfg)
        return reply_text(event.reply_token, f"已刪除關鍵字 ✅\n- {kw}")

    if text in {"查看關鍵字", "關鍵字", "keywords"}:
        cfg = load_cfg(chat_id)
        kws = cfg.get("keywords", [])
        if not kws:
            return reply_text(
                event.reply_token,
                "目前尚未設定任何關鍵字。\n請輸入：設定關鍵字 你的關鍵字\n例如：設定關鍵字 日報表",
            )
        return reply_text(event.reply_token, "目前關鍵字：\n- " + "\n- ".join(kws))

    if text in {"立即整理", "整理", "run"}:
        ok, msg, files = export_per_keyword(chat_id, manual=True)
        reply_text(event.reply_token, msg)

        # 檔案連結備份（用 push 回同聊天室）
        try:
            msgs = send_export_links(chat_id, files, is_push=False)
            if msgs:
                push_texts(chat_id, msgs)
        except Exception as e:
            print(f"[WARN] send links failed: {e}")
        return

    if text.startswith("設定每日時間"):
        t = text.replace("設定每日時間", "", 1).strip()
        if not re.match(r"^\d{1,2}:\d{2}$", t):
            return reply_text(
                event.reply_token, "格式：設定每日時間 HH:MM\n例如：設定每日時間 23:55"
            )
        hh_s, mm_s = t.split(":")
        hh, mm = int(hh_s), int(mm_s)
        if not (0 <= hh <= 23 and 0 <= mm <= 59):
            return reply_text(event.reply_token, "時間範圍錯誤，HH 0~23、MM 0~59")

        cfg = load_cfg(chat_id)
        cfg["daily_time"] = f"{hh:02d}:{mm:02d}"
        save_cfg(chat_id, cfg)
        if cfg.get("daily_enabled") and SCHED:
            reschedule_chat_job(chat_id)
        return reply_text(
            event.reply_token,
            f"已設定每日整理時間 ✅\n時間：{cfg['daily_time']}\n（如已啟用，將自動套用）",
        )

    if text == "啟用每日整理":
        cfg = load_cfg(chat_id)
        cfg["daily_enabled"] = True
        save_cfg(chat_id, cfg)
        if SCHED:
            reschedule_chat_job(chat_id)
        return reply_text(
            event.reply_token,
            f"已啟用每日整理 ✅\n時間：{cfg.get('daily_time','23:59')}",
        )

    if text == "停用每日整理":
        cfg = load_cfg(chat_id)
        cfg["daily_enabled"] = False
        save_cfg(chat_id, cfg)
        if SCHED:
            remove_chat_job(chat_id)
        return reply_text(event.reply_token, "已停用每日整理 ✅")

    # 非指令：不回覆（避免群組洗版）
    return


# -----------------------------
# Start scheduler for flask run
# -----------------------------
maybe_start_scheduler()


if __name__ == "__main__":
    # 直接 python app.py 時也能跑
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")))
