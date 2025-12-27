# app.py
# LINE Bot：訊息關鍵字日報整理（群組/好友皆可）
# 功能：
# - Follow/Join 歡迎訊息
# - Carousel 功能選單（立即整理 / 關鍵字管理 / 每日定時設定）
# - 設定/查看/刪除關鍵字（刪除：按鈕點一下就刪，不用手打）
# - 每個關鍵字輸出「獨立 txt」：YYYYMMDD_關鍵字_chatid.txt
# - 本地輸出放在同一資料夾：OUT_DIR/YYYY-MM/
# - 同時回傳群組「可下載連結」（含下載保護 token）
# - 同時回傳群組一份「檔案訊息」做備份（若 SDK 不支援則回連結）
# - 內建台北時區（zoneinfo）
# - 內建排程：每分鐘 tick，到了你設定的 HH:MM 就自動整理並 push

import os
import re
import json
import secrets
from pathlib import Path
from datetime import datetime
from typing import Optional, Tuple, List

from flask import Flask, request, abort, send_file

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
# Env
# -----------------------------
load_dotenv()

TZ_NAME = os.getenv("TZ_NAME", "Asia/Taipei")

LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "")
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET", "")

# 公開網址（Render）：用來組「下載連結」
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")

# 下載保護 token（你已決定要開啟）
FILE_TOKEN = os.getenv("FILE_TOKEN", "")

# 你本機/伺服器資料資料夾
BASE_DIR = Path(os.getenv("BOT_DATA_DIR", "./bot_data")).resolve()
LOG_DIR = (BASE_DIR / "logs").resolve()  # logs/<chat_id>/YYYY-MM-DD.jsonl
CFG_DIR = (BASE_DIR / "configs").resolve()  # configs/<chat_id>.json
OUT_DIR = (BASE_DIR / "exports").resolve()  # exports/YYYY-MM/*.txt

for d in (LOG_DIR, CFG_DIR, OUT_DIR):
    d.mkdir(parents=True, exist_ok=True)

print("ENV OK:", bool(LINE_CHANNEL_ACCESS_TOKEN), bool(LINE_CHANNEL_SECRET))
if not LINE_CHANNEL_ACCESS_TOKEN or not LINE_CHANNEL_SECRET:
    print("[WARN] Missing env vars: LINE_CHANNEL_ACCESS_TOKEN / LINE_CHANNEL_SECRET")

if not PUBLIC_BASE_URL:
    print("[WARN] PUBLIC_BASE_URL is empty. Download links will be unavailable.")
if not FILE_TOKEN:
    print(
        "[WARN] FILE_TOKEN is empty. Download protection is NOT enabled (you said you want it on)."
    )


# -----------------------------
# Timezone (Taipei)
# -----------------------------
def now_tpe() -> datetime:
    """
    保證台北時間：
    - Python 3.9+ 內建 zoneinfo
    - 若系統缺 tzdata，仍會依系統時區；建議在 Render 不會有問題
    """
    try:
        from zoneinfo import ZoneInfo

        return datetime.now(ZoneInfo(TZ_NAME))
    except Exception:
        return datetime.now()


def today_ymd(dt: Optional[datetime] = None) -> str:
    dt = dt or now_tpe()
    return dt.strftime("%Y-%m-%d")


def month_ym(dt: Optional[datetime] = None) -> str:
    dt = dt or now_tpe()
    return dt.strftime("%Y-%m")


def today_compact(dt: Optional[datetime] = None) -> str:
    dt = dt or now_tpe()
    return dt.strftime("%Y%m%d")


def hhmm(dt: Optional[datetime] = None) -> str:
    dt = dt or now_tpe()
    return dt.strftime("%H:%M")


# -----------------------------
# Helpers: chat_id / config / logging
# -----------------------------
def get_chat_id(event) -> str:
    """把 user / group / room 統一成一個 chat_id 供檔名、push 使用。"""
    src = getattr(event, "source", None)
    if not src:
        return "unknown"
    for attr in ("group_id", "room_id", "user_id"):
        v = getattr(src, attr, None)
        if v:
            return v
    return "unknown"


def cfg_path(chat_id: str) -> Path:
    return (CFG_DIR / f"{chat_id}.json").resolve()


def load_cfg(chat_id: str) -> dict:
    p = cfg_path(chat_id)
    if not p.exists():
        return {
            "keywords": [],
            "daily_time": None,  # "HH:MM"
            "last_daily_run": None,  # "YYYY-MM-DD"
        }
    try:
        obj = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(obj, dict):
            raise ValueError("cfg not dict")
    except Exception:
        obj = {}
    obj.setdefault("keywords", [])
    obj.setdefault("daily_time", None)
    obj.setdefault("last_daily_run", None)
    return obj


def save_cfg(chat_id: str, cfg: dict) -> None:
    cfg_path(chat_id).write_text(
        json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def append_log(chat_id: str, message_text: str, event) -> None:
    day = today_ymd()
    d = (LOG_DIR / chat_id).resolve()
    d.mkdir(parents=True, exist_ok=True)
    p = (d / f"{day}.jsonl").resolve()

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


def normalize_keyword(k: str) -> str:
    return k.strip()


def safe_filename_keyword(k: str) -> str:
    """
    檔名安全化（保留中英數與常見字，其他換成底線）
    """
    k = k.strip()
    k = re.sub(r"[\\/:*?\"<>|]+", "_", k)
    k = re.sub(r"\s+", "_", k)
    return k[:50] if len(k) > 50 else k


def make_public_url(relpath: str) -> Optional[str]:
    if not PUBLIC_BASE_URL:
        return None
    token_part = f"?token={FILE_TOKEN}" if FILE_TOKEN else ""
    return f"{PUBLIC_BASE_URL}/files/{relpath}{token_part}"


# -----------------------------
# Summarize: per keyword -> one txt
# -----------------------------
def summarize_today_per_keyword(
    chat_id: str, *, manual: bool
) -> Tuple[bool, str, List[dict]]:
    """
    依每個關鍵字輸出獨立檔案。
    回傳：
      ok, summary_text, outputs
      outputs: [{keyword, out_path, relpath, url}]
    """
    cfg = load_cfg(chat_id)
    keywords = [
        normalize_keyword(k)
        for k in cfg.get("keywords", [])
        if isinstance(k, str) and k.strip()
    ]
    keywords = sorted(set(keywords), key=lambda x: x.lower())

    if not keywords:
        return (
            False,
            "尚未設定關鍵字。\n\n請先輸入：\n請輸入：設定關鍵字 你的關鍵字\n例如：設定關鍵字 日報表",
            [],
        )

    day = today_ymd()
    log_file = (LOG_DIR / chat_id / f"{day}.jsonl").resolve()
    if not log_file.exists():
        return (True, f"今天（{day}）尚無紀錄訊息可整理。", [])

    # 讀所有訊息
    texts: List[str] = []
    with log_file.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue
            t = str(obj.get("text", "")).strip()
            if t:
                texts.append(t)

    total = len(texts)
    if total == 0:
        return (True, f"今天（{day}）尚無紀錄訊息可整理。", [])

    ym_folder = month_ym()
    out_month_dir = (OUT_DIR / ym_folder).resolve()
    out_month_dir.mkdir(parents=True, exist_ok=True)

    outputs: List[dict] = []
    matched_any = False

    for kw in keywords:
        matched_lines = [t for t in texts if kw in t]
        if not matched_lines:
            continue

        matched_any = True
        fn_kw = safe_filename_keyword(kw)
        filename = f"{today_compact()}_{fn_kw}_{chat_id}.txt"
        out_path = (out_month_dir / filename).resolve()

        # 你要「乾淨訊息」：只輸出訊息本文，一行一則
        out_path.write_text("\n".join(matched_lines) + "\n", encoding="utf-8")

        relpath = f"{ym_folder}/{filename}"
        url = make_public_url(relpath)

        outputs.append(
            {
                "keyword": kw,
                "out_path": str(out_path),
                "relpath": relpath,
                "url": url,
            }
        )

    mode = "手動" if manual else "自動"

    if not matched_any:
        return (
            True,
            f"{mode}整理完成 ✅\n日期：{day}\n共 {total} 則訊息，但沒有任何關鍵字命中。\n"
            f"目前關鍵字：{', '.join(keywords)}",
            [],
        )

    # 給聊天室看的摘要（含下載連結）
    lines = [
        f"{mode}整理完成 ✅",
        f"日期：{day}",
        f"總訊息：{total} 則",
        "",
        "已輸出檔案（每關鍵字一份）：",
    ]
    for o in outputs:
        if o["url"]:
            lines.append(f"- {o['keyword']}：{o['url']}")
        else:
            lines.append(
                f"- {o['keyword']}：{o['out_path']}（未設定 PUBLIC_BASE_URL，無法產生連結）"
            )

    return (True, "\n".join(lines), outputs)


# -----------------------------
# LINE API
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


def push_text(to_chat_id: str, text: str):
    with ApiClient(configuration) as api_client:
        api = MessagingApi(api_client)
        api.push_message_with_http_info(
            PushMessageRequest(to=to_chat_id, messages=[TextMessage(text=text)])
        )


def reply_menu(reply_token: str):
    """Carousel Template 功能選單（含：立即整理 / 關鍵字管理 / 每日定時設定）"""
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
            "功能選單（文字版）\n\n"
            "• 設定關鍵字：設定關鍵字 日報表\n"
            "• 立即整理：立即整理\n"
            "• 查看關鍵字：查看關鍵字\n"
            "• 刪除關鍵字：刪除關鍵字（會跳出按鈕）\n"
            "• 設定每日時間：設定每日時間 23:55\n"
            "• 關閉每日整理：關閉每日整理\n",
        )

    template = CarouselTemplate(
        columns=[
            CarouselColumn(
                title="每日訊息整理",
                text="立即整理 / 產生下載連結 + 備份檔案",
                actions=[
                    PostbackAction(label="立即整理", data="action=run_now"),
                    PostbackAction(label="查看關鍵字", data="action=list_keyword"),
                    PostbackAction(label="刪除關鍵字", data="action=delete_keyword"),
                ],
            ),
            CarouselColumn(
                title="關鍵字管理",
                text="新增/查看/刪除關鍵字",
                actions=[
                    PostbackAction(label="設定關鍵字", data="action=set_keyword"),
                    PostbackAction(label="查看關鍵字", data="action=list_keyword"),
                    PostbackAction(label="刪除關鍵字", data="action=delete_keyword"),
                ],
            ),
            CarouselColumn(
                title="每日定時設定",
                text="設定每天自動整理時間（HH:MM）",
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


def reply_delete_keyword_buttons(reply_token: str, chat_id: str):
    """
    點「刪除關鍵字」後：
      bot 直接回「目前關鍵字清單」+ 每個關鍵字一個按鈕（postback）
      點一下就刪，不用手打
    """
    cfg = load_cfg(chat_id)
    kws = [k for k in cfg.get("keywords", []) if isinstance(k, str) and k.strip()]
    kws = sorted(set(kws), key=lambda x: x.lower())

    if not kws:
        return reply_text(
            reply_token,
            "目前尚未設定任何關鍵字。\n\n請輸入：設定關鍵字 你的關鍵字\n例如：設定關鍵字 日報表",
        )

    # 優先用 QuickReply（最像「一排按鈕」）
    try:
        from linebot.v3.messaging import QuickReply, QuickReplyItem, PostbackAction

        items = []
        for k in kws[:13]:  # QuickReply 大致上限 13
            items.append(
                QuickReplyItem(
                    action=PostbackAction(
                        label=f"刪除：{k}", data=f"action=del_kw&kw={k}"
                    )
                )
            )

        text = "點選要刪除的關鍵字（點一下就刪）：\n\n" + "\n".join(
            [f"- {k}" for k in kws]
        )
        msg = TextMessage(text=text, quick_reply=QuickReply(items=items))

        with ApiClient(configuration) as api_client:
            api = MessagingApi(api_client)
            api.reply_message_with_http_info(
                ReplyMessageRequest(reply_token=reply_token, messages=[msg])
            )
        return

    except Exception:
        # fallback：純文字提示
        return reply_text(
            reply_token,
            "目前關鍵字：\n- "
            + "\n- ".join(kws)
            + "\n\n（你的 SDK 版本不支援按鈕刪除，請改用：刪除關鍵字 關鍵字）",
        )


def try_send_file_message(
    to_chat_id: str, file_name: str, file_url: str, file_size: int
) -> bool:
    """
    嘗試用 LINE 的 file message 類型做「聊天室備份」。
    若 SDK 版本/通道不支援，回傳 False。
    """
    try:
        from linebot.v3.messaging import FileMessage
    except Exception:
        return False

    try:
        with ApiClient(configuration) as api_client:
            api = MessagingApi(api_client)
            api.push_message_with_http_info(
                PushMessageRequest(
                    to=to_chat_id,
                    messages=[
                        FileMessage(
                            original_content_url=file_url,
                            file_name=file_name,
                            file_size=file_size,
                        )
                    ],
                )
            )
        return True
    except Exception:
        return False


# -----------------------------
# Flask routes
# -----------------------------
app = Flask(__name__)


@app.get("/")
def home():
    return "OK"


@app.post("/callback")
def callback():
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)
    app.logger.info("Request body: %s", body)

    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        app.logger.info("Invalid signature. Check channel secret/access token.")
        abort(400)

    return "OK"


@app.get("/files/<path:relpath>")
def download_file(relpath: str):
    """
    下載連結：
      /files/YYYY-MM/filename.txt?token=FILE_TOKEN
    """
    # token 保護
    if FILE_TOKEN:
        if request.args.get("token", "") != FILE_TOKEN:
            abort(403)

    base = OUT_DIR.resolve()
    target = (OUT_DIR / relpath).resolve()

    # 防止 ../ 逃逸
    if not str(target).startswith(str(base)):
        abort(400)

    if not target.exists() or not target.is_file():
        abort(404)

    return send_file(target, as_attachment=True)


# -----------------------------
# Welcome
# -----------------------------
@handler.add(FollowEvent)
def handle_follow(event):
    reply_text(
        event.reply_token,
        "嗨～歡迎加入 ✨\n"
        "我是『訊息整理小幫手』，可以把你指定關鍵字的訊息整理成 txt（日報）。\n\n"
        "先試試看：\n"
        "1) 輸入『功能選單』\n"
        "2) 或直接輸入：設定關鍵字 日報表\n",
    )


@handler.add(JoinEvent)
def handle_join(event):
    reply_text(
        event.reply_token,
        "大家好～我進來了 👋\n"
        "我可以把含特定關鍵字的訊息整理成 txt（日報）。\n"
        "輸入『功能選單』開始設定。",
    )


# -----------------------------
# Postback actions
# -----------------------------
def parse_postback_data(data: str) -> dict:
    # data 形式：action=xxx&kw=...
    out = {}
    for part in data.split("&"):
        if "=" in part:
            k, v = part.split("=", 1)
            out[k] = v
    return out


@handler.add(PostbackEvent)
def handle_postback(event):
    data = getattr(getattr(event, "postback", None), "data", "") or ""
    chat_id = get_chat_id(event)
    p = parse_postback_data(data)
    action = p.get("action", "")

    if action == "set_keyword":
        return reply_text(
            event.reply_token, "請輸入：設定關鍵字 你的關鍵字\n例如：設定關鍵字 日報表"
        )

    if action == "list_keyword":
        cfg = load_cfg(chat_id)
        kws = [k for k in cfg.get("keywords", []) if isinstance(k, str) and k.strip()]
        kws = sorted(set(kws), key=lambda x: x.lower())
        if not kws:
            return reply_text(
                event.reply_token,
                "目前尚未設定任何關鍵字。\n\n請輸入：設定關鍵字 你的關鍵字\n例如：設定關鍵字 日報表",
            )
        return reply_text(event.reply_token, "目前關鍵字：\n- " + "\n- ".join(kws))

    if action == "delete_keyword":
        return reply_delete_keyword_buttons(event.reply_token, chat_id)

    if action == "del_kw":
        kw = p.get("kw", "").strip()
        if not kw:
            return reply_text(event.reply_token, "刪除失敗：關鍵字不存在")
        cfg = load_cfg(chat_id)
        before = [k for k in cfg.get("keywords", []) if isinstance(k, str)]
        after = [k for k in before if k != kw]
        cfg["keywords"] = after
        save_cfg(chat_id, cfg)
        return reply_text(event.reply_token, f"已刪除關鍵字 ✅\n- {kw}")

    if action == "run_now":
        ok, msg, outputs = summarize_today_per_keyword(chat_id, manual=True)
        reply_text(event.reply_token, msg)

        # 同時「推播檔案」做備份（如果有 url）
        for o in outputs:
            if o.get("url"):
                # 檔名顯示用
                file_name = Path(o["out_path"]).name
                try:
                    size = Path(o["out_path"]).stat().st_size
                except Exception:
                    size = 1
                sent = try_send_file_message(chat_id, file_name, o["url"], size)
                if not sent:
                    # fallback：再補一行連結（避免 SDK/通道不支援檔案訊息）
                    push_text(chat_id, f"備份檔案（{o['keyword']}）：{o['url']}")
        return

    if action == "set_daily_time":
        return reply_text(
            event.reply_token, "請輸入：設定每日時間 HH:MM\n例如：設定每日時間 23:55"
        )

    if action == "disable_daily":
        cfg = load_cfg(chat_id)
        cfg["daily_time"] = None
        save_cfg(chat_id, cfg)
        return reply_text(event.reply_token, "已關閉每日自動整理 ✅")

    if action == "show_daily":
        cfg = load_cfg(chat_id)
        t = cfg.get("daily_time")
        if t:
            return reply_text(event.reply_token, f"目前每日自動整理時間：{t}")
        return reply_text(event.reply_token, "目前未啟用每日自動整理。")

    return reply_text(
        event.reply_token,
        "已收到操作，但我看不懂這個指令 😅\n輸入『功能選單』再試一次。",
    )


# -----------------------------
# Text messages
# -----------------------------
def is_valid_hhmm(s: str) -> bool:
    m = re.fullmatch(r"([01]\d|2[0-3]):([0-5]\d)", s.strip())
    return bool(m)


@handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event):
    text = (event.message.text or "").strip()
    if not text:
        return

    chat_id = get_chat_id(event)

    # 先記錄（避免漏紀錄）
    append_log(chat_id, text, event)

    # 指令：功能選單
    if text in {"功能選單", "menu", "選單"}:
        return reply_menu(event.reply_token)

    # 設定關鍵字
    if text.startswith("設定關鍵字"):
        kw = text.replace("設定關鍵字", "", 1).strip()
        kw = normalize_keyword(kw)
        if not kw:
            return reply_text(
                event.reply_token,
                "請輸入：設定關鍵字 你的關鍵字\n例如：設定關鍵字 日報表",
            )
        cfg = load_cfg(chat_id)
        kws = set([k for k in cfg.get("keywords", []) if isinstance(k, str)])
        kws.add(kw)
        cfg["keywords"] = sorted(kws, key=lambda x: x.lower())
        save_cfg(chat_id, cfg)
        return reply_text(
            event.reply_token,
            f"已新增關鍵字 ✅\n- {kw}\n\n輸入『立即整理』可馬上測試。",
        )

    # 查看關鍵字
    if text in {"查看關鍵字", "關鍵字", "keywords"}:
        cfg = load_cfg(chat_id)
        kws = [k for k in cfg.get("keywords", []) if isinstance(k, str) and k.strip()]
        kws = sorted(set(kws), key=lambda x: x.lower())
        if not kws:
            return reply_text(
                event.reply_token,
                "目前尚未設定任何關鍵字。\n\n請輸入：設定關鍵字 你的關鍵字\n例如：設定關鍵字 日報表",
            )
        return reply_text(event.reply_token, "目前關鍵字：\n- " + "\n- ".join(kws))

    # 刪除關鍵字：改成按鈕模式（不手打）
    if text in {"刪除關鍵字", "刪關鍵字", "delete"}:
        return reply_delete_keyword_buttons(event.reply_token, chat_id)

    # 兼容：手打刪除（若你想保留）
    if text.startswith("刪除關鍵字 "):
        kw = text.replace("刪除關鍵字", "", 1).strip()
        if not kw:
            return reply_text(event.reply_token, "格式：刪除關鍵字 日報表")
        cfg = load_cfg(chat_id)
        before = [k for k in cfg.get("keywords", []) if isinstance(k, str)]
        after = [k for k in before if k != kw]
        cfg["keywords"] = after
        save_cfg(chat_id, cfg)
        return reply_text(event.reply_token, f"已刪除關鍵字 ✅\n- {kw}")

    # 立即整理
    if text in {"立即整理", "整理", "run"}:
        ok, msg, outputs = summarize_today_per_keyword(chat_id, manual=True)
        reply_text(event.reply_token, msg)

        # 同時推播檔案（若有 url）
        for o in outputs:
            if o.get("url"):
                file_name = Path(o["out_path"]).name
                try:
                    size = Path(o["out_path"]).stat().st_size
                except Exception:
                    size = 1
                sent = try_send_file_message(chat_id, file_name, o["url"], size)
                if not sent:
                    push_text(chat_id, f"備份檔案（{o['keyword']}）：{o['url']}")
        return

    # 設定每日時間
    if text.startswith("設定每日時間"):
        t = text.replace("設定每日時間", "", 1).strip()
        if not is_valid_hhmm(t):
            return reply_text(
                event.reply_token,
                "時間格式不正確。\n請輸入：設定每日時間 HH:MM\n例如：設定每日時間 23:55",
            )
        cfg = load_cfg(chat_id)
        cfg["daily_time"] = t
        save_cfg(chat_id, cfg)
        return reply_text(
            event.reply_token,
            f"已設定每日整理時間 ✅\n時間：{t}\n（如已啟用，將自動套用）",
        )

    if text in {"關閉每日整理", "停止每日整理"}:
        cfg = load_cfg(chat_id)
        cfg["daily_time"] = None
        save_cfg(chat_id, cfg)
        return reply_text(event.reply_token, "已關閉每日自動整理 ✅")

    # 非指令：不回覆，避免群組洗版
    return


# -----------------------------
# Scheduler: tick every minute (cloud-friendly)
# -----------------------------
def tick_daily_scheduler():
    """
    每分鐘跑一次：
      - 找出設定了 daily_time 的聊天室
      - 若現在 HH:MM 命中且今天還沒跑過 -> 自動整理 + push
    """
    now = now_tpe()
    now_hhmm = hhmm(now)
    today = today_ymd(now)

    for p in CFG_DIR.glob("*.json"):
        chat_id = p.stem
        cfg = load_cfg(chat_id)
        t = cfg.get("daily_time")
        if not t:
            continue

        if t != now_hhmm:
            continue

        if cfg.get("last_daily_run") == today:
            continue

        ok, msg, outputs = summarize_today_per_keyword(chat_id, manual=False)

        # push 摘要（含下載連結）
        try:
            push_text(chat_id, msg)
        except Exception as e:
            print(f"[WARN] daily push text failed for {chat_id}: {e}")

        # push 檔案備份（若有 url）
        for o in outputs:
            if o.get("url"):
                file_name = Path(o["out_path"]).name
                try:
                    size = Path(o["out_path"]).stat().st_size
                except Exception:
                    size = 1
                sent = try_send_file_message(chat_id, file_name, o["url"], size)
                if not sent:
                    try:
                        push_text(chat_id, f"備份檔案（{o['keyword']}）：{o['url']}")
                    except Exception:
                        pass

        cfg["last_daily_run"] = today
        save_cfg(chat_id, cfg)


def setup_scheduler():
    """
    APScheduler（Render 上可用）
    用 interval 每 60 秒 tick（比 cron 更容易動態變更 daily_time）
    """
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
    except Exception:
        print("[WARN] APScheduler not installed. Install: pip install APScheduler")
        return None

    sched = BackgroundScheduler(timezone=TZ_NAME)
    sched.add_job(tick_daily_scheduler, "interval", seconds=60, id="tick_daily")
    sched.start()
    print("[INFO] Scheduler started: tick every 60s.")
    return sched


# -----------------------------
# Main
# -----------------------------
# Render 建議 Start Command：
#   gunicorn app:app --bind 0.0.0.0:$PORT
#
# 本機跑：
#   python app.py
#
if __name__ == "__main__":
    setup_scheduler()
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)
