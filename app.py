import os
import json
import hmac
import base64
import hashlib
from pathlib import Path
from datetime import datetime, timedelta

from flask import Flask, request, abort, send_from_directory
from dotenv import load_dotenv

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

# -----------------------------
# Env
# -----------------------------
load_dotenv()

TZ_NAME = os.getenv("TZ_NAME", "Asia/Taipei")
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").rstrip(
    "/"
)  # e.g. https://message-organizer.onrender.com
CRON_TOKEN = os.getenv("CRON_TOKEN", "")  # protect /cron/tick
DOWNLOAD_SECRET = os.getenv("DOWNLOAD_SECRET", "")  # protect download links

LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "")
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET", "")

print("ENV OK:", bool(LINE_CHANNEL_ACCESS_TOKEN), bool(LINE_CHANNEL_SECRET))

if not LINE_CHANNEL_ACCESS_TOKEN or not LINE_CHANNEL_SECRET:
    print(
        "[WARN] Missing env vars: LINE_CHANNEL_ACCESS_TOKEN / LINE_CHANNEL_SECRET. "
        "Set them before deploying."
    )

if not PUBLIC_BASE_URL:
    print("[WARN] PUBLIC_BASE_URL not set. Download links may not work.")

if not DOWNLOAD_SECRET:
    print("[WARN] DOWNLOAD_SECRET not set. Download protection will fail (set it!).")


# -----------------------------
# Timezone (Asia/Taipei) - stable
# -----------------------------
def now_tpe() -> datetime:
    """Return timezone-aware now in Asia/Taipei using stdlib zoneinfo."""
    try:
        from zoneinfo import ZoneInfo  # py3.9+

        return datetime.now(ZoneInfo(TZ_NAME))
    except Exception:
        # fallback: server local time
        return datetime.now()


def today_str(dt: datetime | None = None) -> str:
    dt = dt or now_tpe()
    return dt.strftime("%Y-%m-%d")


def yyyymmdd(dt: datetime | None = None) -> str:
    dt = dt or now_tpe()
    return dt.strftime("%Y%m%d")


def yyyymm(dt: datetime | None = None) -> str:
    dt = dt or now_tpe()
    return dt.strftime("%Y-%m")


# -----------------------------
# Storage
# -----------------------------
BASE_DIR = Path(os.getenv("BOT_DATA_DIR", "./bot_data"))
LOG_DIR = BASE_DIR / "logs"  # logs/<chat_id>/YYYY-MM-DD.jsonl
CFG_DIR = BASE_DIR / "configs"  # configs/<chat_id>.json
OUT_DIR = BASE_DIR / "exports"  # exports/YYYY-MM/<files>

for d in (LOG_DIR, CFG_DIR, OUT_DIR):
    d.mkdir(parents=True, exist_ok=True)


def get_chat_id(event) -> str:
    """Unify user/group/room into one id."""
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
        return {
            "keywords": [],
            "daily_enabled": False,
            "daily_time": "23:59",  # HH:MM
            "last_run_date": "",  # YYYY-MM-DD
        }
    try:
        cfg = json.loads(p.read_text(encoding="utf-8"))
        # fill defaults
        cfg.setdefault("keywords", [])
        cfg.setdefault("daily_enabled", False)
        cfg.setdefault("daily_time", "23:59")
        cfg.setdefault("last_run_date", "")
        return cfg
    except Exception:
        return {
            "keywords": [],
            "daily_enabled": False,
            "daily_time": "23:59",
            "last_run_date": "",
        }


def save_cfg(chat_id: str, cfg: dict) -> None:
    cfg_path(chat_id).write_text(
        json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8"
    )


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
    }
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=False) + "\n")


# -----------------------------
# Download protection (signed token)
# token format: base64url("exp:<unix>|sig:<hex>")
# sig = HMAC_SHA256(secret, f"{relpath}|{exp}")
# -----------------------------
def _b64url_encode(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode("utf-8").rstrip("=")


def _b64url_decode(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


def make_download_token(rel_path: str, expires_in_sec: int = 3600) -> str:
    exp = int((now_tpe() + timedelta(seconds=expires_in_sec)).timestamp())
    msg = f"{rel_path}|{exp}".encode("utf-8")
    sig = hmac.new(DOWNLOAD_SECRET.encode("utf-8"), msg, hashlib.sha256).hexdigest()
    raw = f"exp:{exp}|sig:{sig}".encode("utf-8")
    return _b64url_encode(raw)


def verify_download_token(rel_path: str, token: str) -> bool:
    if not DOWNLOAD_SECRET:
        return False
    try:
        raw = _b64url_decode(token).decode("utf-8")
        parts = dict(p.split(":", 1) for p in raw.split("|"))
        exp = int(parts["exp"])
        sig = parts["sig"]
        if int(now_tpe().timestamp()) > exp:
            return False
        msg = f"{rel_path}|{exp}".encode("utf-8")
        expected = hmac.new(
            DOWNLOAD_SECRET.encode("utf-8"), msg, hashlib.sha256
        ).hexdigest()
        return hmac.compare_digest(sig, expected)
    except Exception:
        return False


# -----------------------------
# Summarize / Export (one file per keyword)
# -----------------------------
def summarize_today(
    chat_id: str, *, manual: bool = False
) -> tuple[bool, str, list[str]]:
    """
    Returns:
        ok, message_to_user, download_urls(list)
    """
    cfg = load_cfg(chat_id)
    keywords: list[str] = [
        k for k in cfg.get("keywords", []) if isinstance(k, str) and k.strip()
    ]
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

    # prepare per-keyword buckets
    buckets: dict[str, list[str]] = {k: [] for k in keywords}
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

            text = str(obj.get("text", "")).strip()
            ts = str(obj.get("ts", ""))

            # clean output line: only message text (optionally keep HH:MM)
            # here we keep HH:MM for readability but no UID / no header
            hhmm = ""
            try:
                # ts like 2025-12-27T14:05:00+08:00 or without tz
                hhmm = ts.split("T", 1)[1][:5] if "T" in ts else ""
            except Exception:
                hhmm = ""

            clean_line = f"{hhmm} {text}".strip() if hhmm else text

            for k in keywords:
                if k in text:
                    buckets[k].append(clean_line)

    # write files
    out_month_dir = OUT_DIR / yyyymm()
    out_month_dir.mkdir(parents=True, exist_ok=True)

    urls: list[str] = []
    written = 0
    for k, lines in buckets.items():
        if not lines:
            continue
        filename = f"{yyyymmdd()}_{k}_{chat_id}.txt"
        file_path = out_month_dir / filename
        file_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        written += 1

        # build protected link
        rel = f"{yyyymm()}/{filename}"  # relative under exports
        token = make_download_token(rel, expires_in_sec=3600)
        if PUBLIC_BASE_URL:
            urls.append(f"{PUBLIC_BASE_URL}/files/{rel}?token={token}")
        else:
            urls.append(str(file_path))

    if written == 0:
        return (
            True,
            f"今日 ({day}) 共記錄 {total} 則訊息，但沒有符合關鍵字：{', '.join(keywords)}",
            [],
        )

    mode = "手動" if manual else "自動"
    msg = (
        f"{mode}整理完成 ✅\n"
        f"日期：{day}\n"
        f"總訊息：{total} 則\n"
        f"已輸出檔案（每關鍵字一份）：{written} 份\n"
    )
    # ✅ 只回 1 組連結（同一段，列出所有檔案連結即可）
    if urls:
        msg += "\n下載連結（有效 60 分鐘）：\n" + "\n".join([f"- {u}" for u in urls])

    return (True, msg, urls)


# -----------------------------
# LINE API helpers
# -----------------------------
configuration = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)


def reply_text(reply_token: str, text: str):
    with ApiClient(configuration) as api_client:
        api = MessagingApi(api_client)
        api.reply_message_with_http_info(
            ReplyMessageRequest(
                reply_token=reply_token, messages=[TextMessage(text=text)]
            )
        )


def push_text(to: str, text: str):
    with ApiClient(configuration) as api_client:
        api = MessagingApi(api_client)
        api.push_message_with_http_info(
            PushMessageRequest(to=to, messages=[TextMessage(text=text)])
        )


def reply_menu(reply_token: str):
    """Carousel menu (clean, no duplicated buttons)."""
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
            "✅ 立即整理：立即整理\n"
            "✅ 關鍵字：設定關鍵字 / 查看關鍵字 / 刪除關鍵字\n"
            "✅ 每日定時：設定每日時間 / 關閉每日整理 / 查看目前設定\n",
        )

    template = CarouselTemplate(
        columns=[
            CarouselColumn(
                title="每日訊息整理",
                text="立即整理 / 產生下載連結",
                actions=[
                    PostbackAction(label="立即整理", data="action=run_now"),
                ],
            ),
            CarouselColumn(
                title="關鍵字管理",
                text="新增 / 查看 / 刪除",
                actions=[
                    PostbackAction(label="設定關鍵字", data="action=set_keyword"),
                    PostbackAction(label="查看關鍵字", data="action=list_keyword"),
                    PostbackAction(
                        label="刪除關鍵字", data="action=delete_keyword_menu"
                    ),
                ],
            ),
            CarouselColumn(
                title="每日定時設定",
                text="設定每天自動整理時間 (HH:MM)",
                actions=[
                    PostbackAction(label="設定每日時間", data="action=set_daily_time"),
                    PostbackAction(label="關閉每日整理", data="action=disable_daily"),
                    PostbackAction(label="查看目前設定", data="action=show_daily"),
                ],
            ),
        ]
    )

    msg = TemplateMessage(alt_text="功能選單", template=template)
    with ApiClient(configuration) as api_client:
        api = MessagingApi(api_client)
        api.reply_message_with_http_info(
            ReplyMessageRequest(reply_token=reply_token, messages=[msg])
        )


def reply_keyword_delete_buttons(reply_token: str, chat_id: str):
    """Show keyword list; each keyword becomes one postback button; tap to delete."""
    cfg = load_cfg(chat_id)
    kws = [k for k in cfg.get("keywords", []) if isinstance(k, str) and k.strip()]
    if not kws:
        return reply_text(reply_token, "目前沒有任何關鍵字可刪除。")

    # Use QuickReply (clean & scalable)
    try:
        from linebot.v3.messaging import (
            QuickReply,
            QuickReplyItem,
            PostbackAction,
            TextMessage,
        )
    except Exception:
        # fallback text list
        return reply_text(
            reply_token,
            "目前關鍵字：\n- " + "\n- ".join(kws) + "\n\n請手動輸入：刪除關鍵字 XXX",
        )

    items = []
    for k in kws[:13]:  # LINE quick reply limit (safe)
        items.append(
            QuickReplyItem(
                action=PostbackAction(label=k, data=f"action=delete_kw&kw={k}")
            )
        )

    text = "點一下要刪除的關鍵字："
    msg = TextMessage(text=text, quick_reply=QuickReply(items=items))

    with ApiClient(configuration) as api_client:
        api = MessagingApi(api_client)
        api.reply_message_with_http_info(
            ReplyMessageRequest(reply_token=reply_token, messages=[msg])
        )


# -----------------------------
# Daily schedule logic (tick-based)
# -----------------------------
def _parse_hhmm(s: str) -> tuple[int, int] | None:
    try:
        hh, mm = s.strip().split(":")
        hh = int(hh)
        mm = int(mm)
        if 0 <= hh <= 23 and 0 <= mm <= 59:
            return hh, mm
    except Exception:
        pass
    return None


def run_scheduled_tick() -> list[str]:
    """
    Scan all chats; if daily_enabled and time passed and not run today -> run summarize & push message.
    Returns log lines.
    """
    logs = []
    now = now_tpe()
    today = today_str(now)

    for p in CFG_DIR.glob("*.json"):
        chat_id = p.stem
        cfg = load_cfg(chat_id)

        if not cfg.get("daily_enabled", False):
            continue

        hhmm = _parse_hhmm(str(cfg.get("daily_time", "23:59")))
        if not hhmm:
            continue

        hh, mm = hhmm
        due = now.replace(hour=hh, minute=mm, second=0, microsecond=0)

        # if now >= due and not yet run today -> run
        if now >= due and cfg.get("last_run_date", "") != today:
            ok, msg, _ = summarize_today(chat_id, manual=False)
            try:
                push_text(chat_id, msg)  # ✅ only one message (contains links)
                cfg["last_run_date"] = today
                save_cfg(chat_id, cfg)
                logs.append(f"[OK] {chat_id} ran daily at {hh:02d}:{mm:02d}")
            except Exception as e:
                logs.append(f"[WARN] push failed {chat_id}: {e}")

    return logs


# Optional APScheduler (still useful on paid always-on)
def setup_scheduler_optional():
    if os.getenv("ENABLE_APSCHEDULER", "0") != "1":
        print(
            "[INFO] APScheduler disabled. Use /cron/tick with Render Cron Job instead."
        )
        return None
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        from apscheduler.triggers.interval import IntervalTrigger
    except Exception:
        print(
            "[WARN] APScheduler not installed. Set ENABLE_APSCHEDULER=0 or install APScheduler."
        )
        return None

    sched = BackgroundScheduler(timezone=TZ_NAME)
    sched.add_job(
        run_scheduled_tick, IntervalTrigger(minutes=1), id="tick", replace_existing=True
    )
    sched.start()
    print("[INFO] APScheduler enabled: tick every 1 minute.")
    return sched


# -----------------------------
# Flask
# -----------------------------
app = Flask(__name__)


@app.get("/")
def index():
    return "OK"


@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)

    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)

    return "OK"


# Download endpoint (protected)
@app.get("/files/<path:relpath>")
def download_file(relpath: str):
    token = request.args.get("token", "")
    # relpath is like "2025-12/20251227_關鍵字_chatid.txt"
    if not token or not verify_download_token(relpath, token):
        abort(403)

    # serve from OUT_DIR
    # directory: exports/<YYYY-MM>
    parts = relpath.split("/", 1)
    if len(parts) != 2:
        abort(404)
    month_dir, filename = parts[0], parts[1]

    directory = OUT_DIR / month_dir
    if not (directory / filename).exists():
        abort(404)

    return send_from_directory(directory, filename, as_attachment=True)


# Cron tick endpoint (protected)
@app.get("/cron/tick")
def cron_tick():
    if not CRON_TOKEN:
        abort(403)
    token = request.args.get("token", "")
    if token != CRON_TOKEN:
        abort(403)
    logs = run_scheduled_tick()
    return {"ok": True, "logs": logs, "ts": now_tpe().isoformat(timespec="seconds")}


# -----------------------------
# Welcome message
# -----------------------------
@handler.add(FollowEvent)
def handle_follow(event):
    reply_text(
        event.reply_token,
        "嗨～歡迎加入 ✨\n"
        "我是『訊息整理小幫手』，可以把你指定關鍵字的訊息整理成 txt 並提供下載連結。\n\n"
        "先試試看：\n"
        "1) 輸入『功能選單』開啟功能\n"
        "2) 或直接輸入：設定關鍵字 日報表\n",
    )


@handler.add(JoinEvent)
def handle_join(event):
    reply_text(
        event.reply_token,
        "大家好～我進來了 👋\n"
        "我可以把含特定關鍵字的訊息整理成 txt 並提供下載連結。\n"
        "輸入『功能選單』開始設定。",
    )


# -----------------------------
# Postback actions
# -----------------------------
@handler.add(PostbackEvent)
def handle_postback(event):
    data = getattr(getattr(event, "postback", None), "data", "") or ""
    chat_id = get_chat_id(event)

    if data == "action=run_now":
        ok, msg, _ = summarize_today(chat_id, manual=True)
        return reply_text(event.reply_token, msg)

    if data == "action=set_keyword":
        return reply_text(
            event.reply_token,
            "請輸入：設定關鍵字 你的關鍵字\n例如：設定關鍵字 日報表",
        )

    if data == "action=list_keyword":
        cfg = load_cfg(chat_id)
        kws = [k for k in cfg.get("keywords", []) if isinstance(k, str) and k.strip()]
        if not kws:
            return reply_text(
                event.reply_token,
                "目前尚未設定任何關鍵字。\n請輸入：設定關鍵字 日報表",
            )
        return reply_text(event.reply_token, "目前關鍵字：\n- " + "\n- ".join(kws))

    if data == "action=delete_keyword_menu":
        return reply_keyword_delete_buttons(event.reply_token, chat_id)

    if data.startswith("action=delete_kw&kw="):
        kw = data.split("action=delete_kw&kw=", 1)[1]
        cfg = load_cfg(chat_id)
        before = [k for k in cfg.get("keywords", []) if isinstance(k, str)]
        after = [k for k in before if k != kw]
        cfg["keywords"] = after
        save_cfg(chat_id, cfg)
        return reply_text(event.reply_token, f"已刪除關鍵字 ✅\n- {kw}")

    if data == "action=set_daily_time":
        return reply_text(
            event.reply_token,
            "請輸入：設定每日時間 HH:MM\n例如：設定每日時間 23:55",
        )

    if data == "action=disable_daily":
        cfg = load_cfg(chat_id)
        cfg["daily_enabled"] = False
        save_cfg(chat_id, cfg)
        return reply_text(event.reply_token, "已關閉每日自動整理 ✅")

    if data == "action=show_daily":
        cfg = load_cfg(chat_id)
        enabled = "啟用" if cfg.get("daily_enabled") else "未啟用"
        return reply_text(
            event.reply_token,
            f"每日自動整理：{enabled}\n"
            f"時間：{cfg.get('daily_time','23:59')}\n"
            f"上次執行：{cfg.get('last_run_date','') or '尚未'}",
        )

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

    # record first
    if text:
        append_log(chat_id, text, event)

    # menu
    if text in {"功能選單", "menu", "選單"}:
        return reply_menu(event.reply_token)

    # keyword add
    if text.startswith("設定關鍵字"):
        kw = text.replace("設定關鍵字", "", 1).strip()
        if not kw:
            return reply_text(event.reply_token, "格式：設定關鍵字 日報表")
        cfg = load_cfg(chat_id)
        kws = set([k for k in cfg.get("keywords", []) if isinstance(k, str)])
        kws.add(kw)
        cfg["keywords"] = sorted(kws)
        save_cfg(chat_id, cfg)
        return reply_text(
            event.reply_token,
            f"已新增關鍵字 ✅\n- {kw}\n\n輸入『立即整理』可馬上測試。",
        )

    # manual delete fallback (still supported)
    if text.startswith("刪除關鍵字"):
        kw = text.replace("刪除關鍵字", "", 1).strip()
        if not kw:
            return reply_text(event.reply_token, "格式：刪除關鍵字 日報表")
        cfg = load_cfg(chat_id)
        cfg["keywords"] = [k for k in cfg.get("keywords", []) if k != kw]
        save_cfg(chat_id, cfg)
        return reply_text(event.reply_token, f"已刪除關鍵字 ✅\n- {kw}")

    # list keywords
    if text in {"查看關鍵字", "關鍵字", "keywords"}:
        cfg = load_cfg(chat_id)
        kws = [k for k in cfg.get("keywords", []) if isinstance(k, str) and k.strip()]
        if not kws:
            return reply_text(
                event.reply_token, "目前尚未設定任何關鍵字。\n請輸入：設定關鍵字 日報表"
            )
        return reply_text(event.reply_token, "目前關鍵字：\n- " + "\n- ".join(kws))

    # run now
    if text in {"立即整理", "整理", "run"}:
        ok, msg, _ = summarize_today(chat_id, manual=True)
        return reply_text(event.reply_token, msg)

    # set daily time (enable)
    if text.startswith("設定每日時間"):
        t = text.replace("設定每日時間", "", 1).strip()
        hhmm = _parse_hhmm(t)
        if not hhmm:
            return reply_text(
                event.reply_token, "格式：設定每日時間 HH:MM\n例如：設定每日時間 23:55"
            )
        cfg = load_cfg(chat_id)
        cfg["daily_time"] = f"{hhmm[0]:02d}:{hhmm[1]:02d}"
        cfg["daily_enabled"] = True
        save_cfg(chat_id, cfg)
        return reply_text(
            event.reply_token,
            f"已設定每日整理時間 ✅\n時間：{cfg['daily_time']}\n（如已啟用，將自動套用）",
        )

    # show current daily settings
    if text in {"查看目前設定", "每日設定"}:
        cfg = load_cfg(chat_id)
        enabled = "啟用" if cfg.get("daily_enabled") else "未啟用"
        return reply_text(
            event.reply_token,
            f"每日自動整理：{enabled}\n"
            f"時間：{cfg.get('daily_time','23:59')}\n"
            f"上次執行：{cfg.get('last_run_date','') or '尚未'}",
        )

    # disable daily
    if text in {"關閉每日整理", "停止每日整理"}:
        cfg = load_cfg(chat_id)
        cfg["daily_enabled"] = False
        save_cfg(chat_id, cfg)
        return reply_text(event.reply_token, "已關閉每日自動整理 ✅")

    # non-command: no reply (avoid spamming in group)
    return


# -----------------------------
# Main
# -----------------------------
if __name__ == "__main__":
    setup_scheduler_optional()
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)
