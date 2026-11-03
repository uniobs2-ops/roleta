"""
Backend da Roleta — API para o Mini App + Bot do Telegram (webhook).

Regras:
- Usuário comum: gira gastando tokens comprados (saldo no banco).
- VIP: ganha 1 token diário (por dia do calendário, fuso de Brasília) que NÃO acumula.
  Se não usar no dia, perde. O VIP também pode ter tokens comprados (usados depois do diário).
- O sorteio é feito aqui no servidor; o prêmio é enviado pelo bot no chat do usuário.
"""
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import threading
import time
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qsl
from zoneinfo import ZoneInfo

import httpx
from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

# ─── CONFIGURAÇÃO (variáveis de ambiente) ────────────────────
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
WEBAPP_URL = os.environ.get("WEBAPP_URL", "")          # URL do site (GitHub Pages)
PUBLIC_URL = os.environ.get("PUBLIC_URL", "").rstrip("/")  # URL pública deste backend
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "troque-esta-chave")
ADMIN_IDS = {int(x) for x in os.environ.get("ADMIN_IDS", "").replace(" ", "").split(",") if x.isdigit()}
ALLOWED_ORIGINS = [o.strip() for o in os.environ.get("ALLOWED_ORIGINS", "*").split(",") if o.strip()]
DB_PATH = os.environ.get("DB_PATH", "roleta.db")
PRIZES_FILE = Path(os.environ.get("PRIZES_FILE", Path(__file__).with_name("prizes.txt")))
TZ = ZoneInfo(os.environ.get("TIMEZONE", "America/Sao_Paulo"))
DEV_MODE = os.environ.get("DEV_MODE") == "1"           # só para testes locais (sem Telegram)
VIP_DEFAULT_DAYS = 30

AUTO_COLORS = ["#ff2d78", "#ff6b9d", "#e91e63", "#ad1457", "#c2185b", "#ff4081", "#f50057", "#ffd700"]
VALID_TYPES = {"tokens", "texto", "link", "arquivo", "nada"}


# ─── BANCO DE DADOS (SQLite) ─────────────────────────────────
db_lock = threading.RLock()


@contextmanager
def tx():
    """Transação serializada por lock (evita girar duas vezes ao mesmo tempo)."""
    with db_lock:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


def init_db():
    with tx() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY,
                first_name TEXT DEFAULT '',
                username TEXT DEFAULT '',
                tokens INTEGER NOT NULL DEFAULT 0,
                vip_until TEXT,
                last_daily TEXT,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS spins (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                prize_id INTEGER NOT NULL,
                prize_name TEXT NOT NULL,
                prize_type TEXT NOT NULL,
                source TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_spins_user ON spins(user_id, id DESC);
            """
        )


# ─── TEMPO ───────────────────────────────────────────────────
def now_utc():
    return datetime.now(timezone.utc)


def today_str():
    return datetime.now(TZ).date().isoformat()


def next_midnight():
    n = datetime.now(TZ)
    return (n + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)


def parse_dt(value):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


# ─── USUÁRIOS ────────────────────────────────────────────────
def ensure_user(conn, uid, first_name="", username=""):
    conn.execute(
        "INSERT OR IGNORE INTO users (id, first_name, username, tokens, created_at) VALUES (?, ?, ?, 0, ?)",
        (uid, first_name or "", username or "", now_utc().isoformat()),
    )
    if first_name:
        conn.execute("UPDATE users SET first_name=?, username=? WHERE id=?", (first_name, username or "", uid))
    return conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()


def is_vip(row):
    until = parse_dt(row["vip_until"])
    return bool(until and until > now_utc())


def daily_available(row):
    return is_vip(row) and row["last_daily"] != today_str()


def user_payload(row):
    vip = is_vip(row)
    daily = daily_available(row)
    return {
        "id": row["id"],
        "first_name": row["first_name"],
        "tokens": row["tokens"],
        "is_vip": vip,
        "vip_until": row["vip_until"] if vip else None,
        "daily_available": daily,
        "daily_resets_at": next_midnight().isoformat(),
        "spins": row["tokens"] + (1 if daily else 0),
    }


# ─── AUTENTICAÇÃO (initData do Telegram) ─────────────────────
def authenticate(init_data):
    if not init_data:
        if DEV_MODE:
            return {"id": 1, "first_name": "Dev", "username": "dev"}
        raise HTTPException(401, "Abra a roleta pelo bot do Telegram.")

    pairs = dict(parse_qsl(init_data, keep_blank_values=True))
    received = pairs.pop("hash", None)
    if not received or not BOT_TOKEN:
        raise HTTPException(401, "Autenticação inválida.")

    check_string = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    calculated = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calculated, received):
        raise HTTPException(401, "Autenticação inválida.")

    try:
        auth_date = int(pairs.get("auth_date", "0"))
        user = json.loads(pairs["user"])
        int(user["id"])
    except (ValueError, KeyError, json.JSONDecodeError):
        raise HTTPException(401, "Autenticação inválida.")
    if time.time() - auth_date > 86400:
        raise HTTPException(401, "Sessão expirada. Feche e abra a roleta novamente.")
    return user


# ─── PRÊMIOS (prizes.txt) ────────────────────────────────────
_prizes_cache = {"mtime": None, "data": []}


def load_prizes():
    mtime = PRIZES_FILE.stat().st_mtime
    if _prizes_cache["mtime"] == mtime:
        return _prizes_cache["data"]

    prizes = []
    for raw in PRIZES_FILE.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split("|", 5)]
        if not parts[0]:
            continue

        def part(i, default=""):
            return parts[i] if len(parts) > i and parts[i] else default

        try:
            prob = float(part(1, "10").replace(",", "."))
        except ValueError:
            prob = 10.0
        if prob <= 0:
            prob = 10.0

        color = part(2)
        if not re.fullmatch(r"#([0-9a-fA-F]{3}|[0-9a-fA-F]{6})", color):
            color = AUTO_COLORS[len(prizes) % len(AUTO_COLORS)]

        icon = part(3, "fa-gift")
        if not icon.startswith("fa-"):
            icon = "fa-" + icon

        ptype = part(4, "texto").lower()
        if ptype not in VALID_TYPES:
            ptype = "texto"

        prizes.append({
            "id": len(prizes) + 1,
            "name": parts[0],
            "probability": prob,
            "color": color,
            "icon": icon,
            "type": ptype,
            "content": part(5).replace("\\n", "\n"),
        })

    if len(prizes) < 2:
        raise RuntimeError("prizes.txt precisa ter pelo menos 2 opções.")
    _prizes_cache.update(mtime=mtime, data=prizes)
    return prizes


def public_prize(p):
    return {k: p[k] for k in ("id", "name", "color", "icon", "probability", "type")}


def pick_prize(prizes):
    total = sum(p["probability"] for p in prizes)
    r = secrets.SystemRandom().uniform(0, total)
    for p in prizes:
        r -= p["probability"]
        if r <= 0:
            return p
    return prizes[-1]


def tokens_amount(prize):
    try:
        return max(0, int(prize["content"]))
    except ValueError:
        return 1


# ─── TELEGRAM ────────────────────────────────────────────────
def tg_call(method, payload):
    if not BOT_TOKEN:
        print(f"[telegram] BOT_TOKEN ausente — ignorando {method}")
        return {"ok": False}
    try:
        r = httpx.post(f"https://api.telegram.org/bot{BOT_TOKEN}/{method}", json=payload, timeout=15)
        data = r.json()
        if not data.get("ok"):
            print(f"[telegram] {method} falhou: {data}")
        return data
    except Exception as exc:  # noqa: BLE001
        print(f"[telegram] erro em {method}: {exc}")
        return {"ok": False}


def open_button():
    if not WEBAPP_URL:
        return None
    return {"inline_keyboard": [[{"text": "🎰 Abrir roleta", "web_app": {"url": WEBAPP_URL}}]]}


def send_message(chat_id, text, with_button=False):
    payload = {"chat_id": chat_id, "text": text, "disable_web_page_preview": False}
    if with_button and open_button():
        payload["reply_markup"] = open_button()
    return tg_call("sendMessage", payload)


def deliver_prize(user_id, prize, tokens_after):
    """Envia o prêmio no chat do usuário com o bot."""
    ptype, name, content = prize["type"], prize["name"], prize["content"]

    if ptype == "nada":
        text = f"😅 Não foi dessa vez!\n\nResultado: {name}\n\nTente novamente, boa sorte!"
    elif ptype == "tokens":
        n = tokens_amount(prize)
        text = f"🎉 Você ganhou: {name}!\n\n🪙 +{n} token(s) adicionados. Saldo atual: {tokens_after}."
    elif ptype == "link":
        text = f"🎉 Você ganhou: {name}!\n\n🔗 {content}"
    elif ptype == "arquivo":
        res = tg_call("sendDocument", {
            "chat_id": user_id,
            "document": content,
            "caption": f"🎉 Você ganhou: {name}!",
            **({"reply_markup": open_button()} if open_button() else {}),
        })
        if res.get("ok"):
            return
        text = f"🎉 Você ganhou: {name}!\n\n(Não consegui enviar o arquivo agora. Fale com o administrador.)"
    else:  # texto
        text = f"🎉 Você ganhou: {name}!" + (f"\n\n{content}" if content else "")

    send_message(user_id, text, with_button=True)


# ─── APP ─────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(_app):
    init_db()
    load_prizes()
    if BOT_TOKEN and PUBLIC_URL:
        await run_in_threadpool(tg_call, "setWebhook", {
            "url": f"{PUBLIC_URL}/webhook",
            "secret_token": WEBHOOK_SECRET,
            "allowed_updates": ["message"],
        })
    if BOT_TOKEN and WEBAPP_URL:
        await run_in_threadpool(tg_call, "setChatMenuButton", {
            "menu_button": {"type": "web_app", "text": "🎰 Girar", "web_app": {"url": WEBAPP_URL}}
        })
    yield


app = FastAPI(title="Roleta API", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)


@app.exception_handler(HTTPException)
async def http_error(_request, exc):
    return JSONResponse({"error": exc.detail}, status_code=exc.status_code)


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/api/prizes")
def api_prizes():
    return {"prizes": [public_prize(p) for p in load_prizes()]}


@app.get("/api/user/me")
def api_me(x_telegram_init_data: str | None = Header(None)):
    tg_user = authenticate(x_telegram_init_data)
    with tx() as conn:
        row = ensure_user(conn, int(tg_user["id"]), tg_user.get("first_name", ""), tg_user.get("username", ""))
        return user_payload(row)


@app.get("/api/spins/history")
def api_history(x_telegram_init_data: str | None = Header(None)):
    tg_user = authenticate(x_telegram_init_data)
    with tx() as conn:
        rows = conn.execute(
            "SELECT prize_name, prize_type, created_at FROM spins WHERE user_id=? ORDER BY id DESC LIMIT 10",
            (int(tg_user["id"]),),
        ).fetchall()
    return {"spins": [dict(r) for r in rows]}


@app.post("/api/spins")
def api_spin(background: BackgroundTasks, x_telegram_init_data: str | None = Header(None)):
    tg_user = authenticate(x_telegram_init_data)
    uid = int(tg_user["id"])
    prizes = load_prizes()

    with tx() as conn:
        user = ensure_user(conn, uid, tg_user.get("first_name", ""), tg_user.get("username", ""))

        # 1º o token diário do VIP (se ainda não usou hoje), 2º tokens comprados
        if daily_available(user):
            source = "daily"
            conn.execute("UPDATE users SET last_daily=? WHERE id=?", (today_str(), uid))
        elif user["tokens"] > 0:
            source = "token"
            conn.execute("UPDATE users SET tokens=tokens-1 WHERE id=?", (uid,))
        else:
            raise HTTPException(403, "Você não tem rodadas disponíveis. Compre tokens ou volte amanhã (VIP).")

        prize = pick_prize(prizes)
        if prize["type"] == "tokens":
            conn.execute("UPDATE users SET tokens=tokens+? WHERE id=?", (tokens_amount(prize), uid))

        conn.execute(
            "INSERT INTO spins (user_id, prize_id, prize_name, prize_type, source, created_at) VALUES (?,?,?,?,?,?)",
            (uid, prize["id"], prize["name"], prize["type"], source, now_utc().isoformat()),
        )
        user = conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
        payload = user_payload(user)

    background.add_task(deliver_prize, uid, prize, payload["tokens"])
    return {
        "prize_id": prize["id"],
        "prize_name": prize["name"],
        "prize_type": prize["type"],
        "spins_left": payload["spins"],
        "tokens_left": payload["tokens"],
        "daily_available": payload["daily_available"],
    }


# ─── BOT: webhook + comandos ─────────────────────────────────
@app.post("/webhook")
async def webhook(request: Request, x_telegram_bot_api_secret_token: str | None = Header(None)):
    if not hmac.compare_digest(x_telegram_bot_api_secret_token or "", WEBHOOK_SECRET):
        raise HTTPException(403, "forbidden")
    update = await request.json()
    await run_in_threadpool(handle_update, update)
    return {"ok": True}


def fmt_date(value):
    dt = parse_dt(value)
    return dt.astimezone(TZ).strftime("%d/%m/%Y") if dt else "—"


def describe_user(row):
    p = user_payload(row)
    vip = f"⭐ VIP até {fmt_date(p['vip_until'])}" if p["is_vip"] else "Usuário comum"
    daily = ""
    if p["is_vip"]:
        daily = "\n🎟 Token diário: " + ("disponível" if p["daily_available"] else "já usado hoje")
    return f"{vip}\n🪙 Tokens: {p['tokens']}{daily}"


def handle_update(update):
    msg = update.get("message")
    if not msg or msg.get("chat", {}).get("type") != "private":
        return
    sender = msg.get("from") or {}
    uid = sender.get("id")
    text = (msg.get("text") or "").strip()
    if not uid or not text.startswith("/"):
        return

    parts = text.split()
    cmd = parts[0].split("@")[0].lower()
    args = parts[1:]
    admin = uid in ADMIN_IDS

    with tx() as conn:
        me = ensure_user(conn, uid, sender.get("first_name", ""), sender.get("username", ""))

    if cmd == "/start":
        send_message(uid, f"👑 Bem-vindo(a), {sender.get('first_name', '')}!\n\n🆔 Seu ID: {uid}\n{describe_user(me)}\n\n"
                          "Toque no botão abaixo para girar a roleta. O prêmio chega aqui no chat.", with_button=True)
    elif cmd == "/saldo":
        send_message(uid, f"🆔 Seu ID: {uid}\n{describe_user(me)}", with_button=True)
    elif cmd in ("/ajuda", "/help"):
        lines = ["/start — abrir a roleta", "/saldo — ver seus tokens e status"]
        if admin:
            lines += [
                "",
                "Admin:",
                "/addtokens <id> <qtd> — adiciona (ou remove com negativo) tokens",
                f"/vip <id> [dias] — ativa/estende VIP (padrão {VIP_DEFAULT_DAYS} dias)",
                "/unvip <id> — remove VIP",
                "/user <id> — consulta um usuário",
            ]
        send_message(uid, "\n".join(lines))
    elif admin and cmd in ("/addtokens", "/vip", "/unvip", "/user"):
        admin_command(uid, cmd, args)
    elif cmd in ("/addtokens", "/vip", "/unvip", "/user"):
        send_message(uid, "⛔ Comando só para administradores.")


def admin_command(admin_id, cmd, args):
    try:
        target = int(args[0])
    except (IndexError, ValueError):
        send_message(admin_id, "Use: " + {
            "/addtokens": "/addtokens <id> <qtd>",
            "/vip": "/vip <id> [dias]",
            "/unvip": "/unvip <id>",
            "/user": "/user <id>",
        }[cmd])
        return

    notify = None
    with tx() as conn:
        ensure_user(conn, target)
        if cmd == "/addtokens":
            try:
                qty = int(args[1])
            except (IndexError, ValueError):
                send_message(admin_id, "Use: /addtokens <id> <qtd>")
                return
            conn.execute("UPDATE users SET tokens=MAX(0, tokens+?) WHERE id=?", (qty, target))
            if qty > 0:
                notify = f"🪙 Você recebeu {qty} token(s)! Toque em /saldo ou abra a roleta."
        elif cmd == "/vip":
            try:
                days = int(args[1]) if len(args) > 1 else VIP_DEFAULT_DAYS
            except ValueError:
                days = VIP_DEFAULT_DAYS
            row = conn.execute("SELECT vip_until FROM users WHERE id=?", (target,)).fetchone()
            base = max(now_utc(), parse_dt(row["vip_until"]) or now_utc())
            conn.execute("UPDATE users SET vip_until=? WHERE id=?", ((base + timedelta(days=days)).isoformat(), target))
            notify = f"⭐ Seu VIP foi ativado por {days} dia(s)! Você ganha 1 token por dia (não acumula)."
        elif cmd == "/unvip":
            conn.execute("UPDATE users SET vip_until=NULL WHERE id=?", (target,))
        row = conn.execute("SELECT * FROM users WHERE id=?", (target,)).fetchone()

    send_message(admin_id, f"✅ Usuário {target} ({row['first_name'] or 'sem nome'})\n{describe_user(row)}")
    if notify:
        send_message(target, notify, with_button=True)
