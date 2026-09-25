#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
🌙 by_moon — بوت تلقرام لإدارة حساب جيزي
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
سكريبت واحد فقط. يشغّل شيئين في نفس العملية:
  1) بوت تلقرام (polling) -- هو الواجهة الوحيدة، بدل صفحة الويب السابقة.
  2) خادم Flask صغير جداً (في خيط خلفي) لا يحتوي إلا نقاط نهاية "الوكيل"
     (relay) التي يحتاجها relay_agent.py العامل على هاتفك -- انظر شرح
     الوكيل أسفل الملف.

الإعداد (متغيرات بيئة):
  TELEGRAM_BOT_TOKEN       (إجباري) توكن البوت من BotFather
  TELEGRAM_ADMIN_CHAT_ID   (إجباري) معرّف حسابك في تلقرام (المستخدم الوحيد المخوّل)
  APP_URL                  الرابط العلني لهذا السكريبت (لو مستضاف خارجياً)،
                           يُستخدم فقط لبناء أمر ربط الهاتف. بدونه يُفترض
                           تشغيل محلي (http://localhost:PORT) ولن يعمل ربط
                           الهاتف إذا كان هاتفك جهازاً مختلفاً عن مكان الاستضافة.
  PORT                     منفذ خادم الوكيل المحلي (افتراضي 5000)
  DJEZZY_CLIENT_ID / DJEZZY_CLIENT_SECRET   بيانات تطبيق جيزي (لها قيم افتراضية)

عند إقلاع البوت، يرسل تلقائياً لحساب الأدمن رسالة فيها أمر ربط الهاتف
(relay pairing) جاهزاً للنسخ، بدون الحاجة لطلبه يدوياً.
"""

import json
import logging
import os
import queue
import re
import secrets
import shutil
import threading
import time
import uuid
from datetime import datetime, timedelta

import requests
from flask import Flask, jsonify, request

logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
log = logging.getLogger("by_moon_bot")

# ============================================================
# ⚙️ الإعدادات
# ============================================================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_FILE = os.path.join(BASE_DIR, "session.json")
BACKUP_FILE = os.path.join(BASE_DIR, "session.backup.json")

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
ADMIN_CHAT_ID = os.environ.get("TELEGRAM_ADMIN_CHAT_ID", "")
if not BOT_TOKEN or not ADMIN_CHAT_ID:
    raise SystemExit("❌ اضبط TELEGRAM_BOT_TOKEN و TELEGRAM_ADMIN_CHAT_ID قبل التشغيل.")
ADMIN_CHAT_ID = int(ADMIN_CHAT_ID)

CLIENT_ID = os.environ.get("DJEZZY_CLIENT_ID", "87pIExRhxBb3_wGsA5eSEfyATloa")
CLIENT_SECRET = os.environ.get("DJEZZY_CLIENT_SECRET", "uf82p68Bgisp8Yg1Uz8Pf6_v1XYa")
BASE_URL = "https://apim.djezzy.dz/mobile-api"

REFRESH_BEFORE = 60
CHECK_INTERVAL = 30
OTP_COOLDOWN = 60
OTP_TTL = 600
OTP_MAX_TRIES = 5

# مفتاح جلسة ثابت -- هذا البوت لمستخدم واحد (الأدمن)، بخلاف نسخة الويب التي
# كانت تدعم عدة متصفحين (cid لكل واحد). لا حاجة لكوكيز هنا.
SID = "admin"

OFFERS = {
    "w2gb":  {"name": "🎁 2GB الأسبوعية",   "code": "GIFTWALKWIN2GO",           "type": "reward",  "price": "مجاني",   "duration": "7 أيام"},
    "shake": {"name": "🤝 Imtiyaz 5GB",     "code": "BTL500MBDAY",              "type": "product", "price": "90 DA",   "duration": "30 يوم"},
    "d70":   {"name": "🔥 4GB بـ 70DA",     "code": "BTLINTSPEEDDAY2Go",        "type": "product", "price": "70 DA",   "duration": "24 ساعة"},
    "w300":  {"name": "🌟 10GB بـ 300DA",   "code": "DOVINTSPEEDWEEK10GoPRE",   "type": "product", "price": "300 DA",  "duration": "7 أيام"},
    "m1000": {"name": "🚀 30GB بـ 1000DA",  "code": "DOVINTSPEEDMONTH15GoPRE",  "type": "product", "price": "1000 DA", "duration": "30 يوم"},
    "m1500": {"name": "💎 60GB بـ 1500DA",  "code": "DOVINTSPEEDMONTH30GoPRE",  "type": "product", "price": "1500 DA", "duration": "30 يوم"},
}
WEEKLY_KEY = "w2gb"
WEEKLY_COOLDOWN = 7 * 24 * 3600


# ============================================================
# 💾 التخزين (جلسة واحدة + سجل الجائزة الأسبوعية)
# ============================================================
class Store:
    def __init__(self):
        self.lock = threading.RLock()
        self.data = self._load()

    @staticmethod
    def _read(path):
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        if not isinstance(raw, dict):
            raise ValueError("bad root")
        sessions = raw.get("sessions") if isinstance(raw.get("sessions"), dict) else {}
        rewards = raw.get("rewards") if isinstance(raw.get("rewards"), dict) else {}
        return {
            "sessions": {k: v for k, v in sessions.items() if isinstance(v, dict)},
            "rewards": {k: v for k, v in rewards.items() if isinstance(v, str)},
        }

    def _load(self):
        for path in (DATA_FILE, BACKUP_FILE):
            if os.path.exists(path):
                try:
                    data = self._read(path)
                    if path == BACKUP_FILE:
                        log.warning("⚠️ تم الاسترجاع من النسخة الاحتياطية")
                    return data
                except Exception as e:
                    log.error("تعذر قراءة %s: %s", path, e)
        return {"sessions": {}, "rewards": {}}

    def _save(self):
        try:
            if os.path.exists(DATA_FILE):
                try:
                    shutil.copy(DATA_FILE, BACKUP_FILE)
                except Exception as e:
                    log.warning("backup copy: %s", e)
            tmp = DATA_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False, indent=2)
            try:
                os.chmod(tmp, 0o600)
            except Exception:
                pass
            os.replace(tmp, DATA_FILE)
        except Exception as e:
            log.error("session save: %s", e)

    def get(self, sid):
        with self.lock:
            return dict(self.data["sessions"].get(sid, {}))

    def update(self, sid, **kw):
        with self.lock:
            u = dict(self.data["sessions"].get(sid, {}))
            u.update(kw)
            self.data["sessions"][sid] = u
            self._save()
            return dict(u)

    def delete(self, sid):
        with self.lock:
            self.data["sessions"].pop(sid, None)
            self._save()

    def reward_get(self, phone):
        with self.lock:
            return self.data["rewards"].get(phone)

    def reward_set(self, phone, iso):
        with self.lock:
            self.data["rewards"][phone] = iso
            self._save()


store = Store()


# ============================================================
# 🛠️ مساعدات عامة
# ============================================================
def fmt_phone(p):
    p = re.sub(r"\D", "", p or "")
    if p.startswith("0"):
        p = "213" + p[1:]
    elif not p.startswith("213"):
        p = "213" + p
    return p


def local_phone(p):
    return "0" + p[3:] if p and p.startswith("213") else (p or "")


def num(v):
    try:
        return float(v)
    except Exception:
        return 0.0


def pick_name(obj, fallback=""):
    if isinstance(obj, dict):
        return obj.get("ar") or obj.get("en") or obj.get("fr") or fallback
    return str(obj) if obj else fallback


def extract_error(r):
    try:
        m = r.json().get("message", {})
        return (m.get("ar") or m.get("en") or str(m)) if isinstance(m, dict) else str(m)
    except Exception:
        return (r.text or "")[:200] or f"خطأ {r.status_code}"


def to_ts(iso):
    try:
        return datetime.fromisoformat(iso).timestamp()
    except Exception:
        return None


def reward_state(phone):
    last_iso = store.reward_get(phone) if phone else None
    last_ts = to_ts(last_iso) if last_iso else None
    next_ts = (last_ts + WEEKLY_COOLDOWN) if last_ts else None
    remaining = (next_ts - time.time()) if next_ts else 0
    return {"available": remaining <= 0, "last_ts": last_ts, "next_ts": next_ts, "cooldown": WEEKLY_COOLDOWN}


def mark_reward(phone):
    store.reward_set(phone, datetime.now().isoformat())
    log.info("🎁 تفعيل الجائزة الأسبوعية")


def _fmt_amount(v, unit):
    if unit == "MB":
        return f"{v:.0f} MB" if v < 1024 else f"{v / 1024:.2f} GB"
    return (f"{v:g} {unit}").strip()


# ============================================================
# 🌐 طبقة HTTP (مع دعم الوكيل/relay -- انظر أسفل)
# ============================================================
def http(method, url, retries=2, use_relay=True, **kw):
    if use_relay and _relay_id is not None:
        r = relay_dispatch(_relay_id, method, url, timeout=kw.get("timeout", 30), **kw)
        if r is not None:
            return r
        log.warning("relay: تعذّر تنفيذ الطلب عبر الوكيل -- رجوع لمحاولة مباشرة من الخادم")

    kw.setdefault("timeout", 30)
    for attempt in range(retries):
        try:
            return requests.request(method, url, **kw)
        except requests.exceptions.Timeout:
            log.error("timeout: %s", url)
            if attempt < retries - 1:
                continue
            return None
        except requests.exceptions.RequestException as e:
            log.error("request error: %s", e)
            return None
    return None


# ============================================================
# 🔄 إدارة التوكن (تسجيل الدخول / التجديد)
# ============================================================
def token_remaining(sid):
    u = store.get(sid)
    exp = u.get("token_expiry")
    if not exp:
        return 0
    ts = to_ts(exp)
    return (ts - time.time()) if ts else 0


def refresh_token(sid, force=False):
    u = store.get(sid)
    rt = u.get("refresh_token")
    if not rt:
        return None
    if not force and u.get("access_token") and token_remaining(sid) > REFRESH_BEFORE:
        return u["access_token"]

    payload = {
        "grant_type": "refresh_token",
        "refresh_token": rt,
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
    }
    for attempt in range(2):
        r = http("POST", f"{BASE_URL}/oauth2/token", data=payload)
        if r is not None and r.status_code == 200:
            try:
                d = r.json()
                exp = (datetime.now() + timedelta(seconds=int(d.get("expires_in", 3600)))).isoformat()
                store.update(
                    sid,
                    access_token=d["access_token"],
                    refresh_token=d.get("refresh_token", rt),
                    token_expiry=exp,
                    last_refresh=datetime.now().isoformat(),
                    refresh_count=u.get("refresh_count", 0) + 1,
                )
                log.info("✅ تم تجديد الجلسة")
                return d["access_token"]
            except Exception as e:
                log.error("refresh parse: %s", e)
                return None
        if r is not None and r.status_code in (400, 401):
            log.warning("⚠️ refresh_token منتهي (%s) — سيُطلب تسجيل دخول جديد", r.status_code)
            store.update(sid, access_token=None, refresh_token=None)
            return None
        log.warning("⚠️ فشل التجديد (محاولة %d)", attempt + 1)
    return None


def access_token(sid):
    u = store.get(sid)
    if not u.get("access_token"):
        return None
    if token_remaining(sid) <= REFRESH_BEFORE:
        fresh = refresh_token(sid)
        if fresh:
            return fresh
        if token_remaining(sid) > 0:
            return store.get(sid).get("access_token")
        return None
    return u["access_token"]


def djezzy(sid, method, path, **kw):
    """طلب موثّق إلى API جيزي، مع إعادة المحاولة بعد تجديد التوكن عند 401."""
    r = None
    for attempt in range(2):
        tok = access_token(sid) if attempt == 0 else refresh_token(sid, force=True)
        if not tok:
            return None
        headers = {
            "Authorization": f"Bearer {tok}",
            "User-Agent": "MobileApp/3.0.0",
            "Content-Type": "application/json",
        }
        r = http(method, BASE_URL + path, headers=headers, **kw)
        if r is not None and r.status_code == 401 and attempt == 0:
            continue
        return r
    return r


def has_session():
    u = store.get(SID)
    return bool(u.get("access_token") and u.get("phone"))


def refresh_due():
    if not has_session():
        return
    try:
        if token_remaining(SID) <= REFRESH_BEFORE:
            refresh_token(SID)
    except Exception:
        log.exception("auto refresh")


def start_background():
    def loop():
        time.sleep(2)
        while True:
            try:
                refresh_due()
            except Exception:
                log.exception("bg loop")
            time.sleep(CHECK_INTERVAL)

    threading.Thread(target=loop, daemon=True, name="auto-refresh").start()


# ============================================================
# 📱 الوكيل (Relay) -- تنفيذ الطلبات فعلياً من شبكة هاتفك
# ============================================================
# نفس فكرة النسخة السابقة بالضبط: هذا السكريبت (أينما استُضيف) يضع كل طلب
# لجيزي في طابور، وسكربت relay_agent.py العامل على هاتفك (عبر Termux) يسحبه
# وينفّذه فعلياً من شريحتك، ثم يرجع النتيجة هنا. لا يوجد إلا وكيل واحد ممكن
# في نفس الوقت (هذا بوت لمستخدم واحد)، لذلك القيمة _relay_id عامة بدل خريطة
# لكل مستخدم كما كانت في نسخة الويب.
RELAY_PAIR_TTL = 300
RELAY_JOB_POLL_WAIT = 25

_relay_lock = threading.Lock()
_relay_id = None                # معرّف الوكيل الحالي المتصل، أو None
_relay_queue = None             # queue.Queue للوكيل الحالي
_relay_pending = {}             # job_id -> {"event": Event, "result": dict|None}
_relay_pair_code = {"code": None, "ts": 0}  # آخر كود اقتران تم توليده


class FakeResponse:
    def __init__(self, status_code, body_text):
        self.status_code = status_code
        self.text = body_text or ""

    def json(self):
        return json.loads(self.text)


def relay_dispatch(relay_id, method, url, timeout=30, **kw):
    try:
        prepped = requests.Request(
            method, url,
            headers=kw.get("headers"),
            params=kw.get("params"),
            data=kw.get("data"),
            json=kw.get("json"),
        ).prepare()
    except Exception as e:
        log.error("relay: فشل تجهيز الطلب: %s", e)
        return None

    body = prepped.body
    if isinstance(body, bytes):
        try:
            body = body.decode("utf-8")
        except Exception:
            body = ""

    job_id = uuid.uuid4().hex
    job = {"id": job_id, "method": prepped.method, "url": prepped.url,
           "headers": dict(prepped.headers), "body": body or ""}

    ev = threading.Event()
    with _relay_lock:
        if relay_id != _relay_id or _relay_queue is None:
            return None
        _relay_pending[job_id] = {"event": ev, "result": None}
        q = _relay_queue

    q.put(job)
    got = ev.wait(timeout)

    with _relay_lock:
        entry = _relay_pending.pop(job_id, None)

    if not got or not entry or entry["result"] is None:
        log.warning("relay: انتهت المهلة بانتظار رد الوكيل للمهمة %s", job_id)
        return None

    res = entry["result"]
    if res.get("error"):
        log.warning("relay: الوكيل أرجع خطأ: %s", res["error"])
        return None
    return FakeResponse(res.get("status_code", 0), res.get("body", ""))


def generate_pair_code():
    code = f"{secrets.randbelow(1_000_000):06d}"
    with _relay_lock:
        _relay_pair_code["code"] = code
        _relay_pair_code["ts"] = time.time()
    return code


def relay_connected():
    with _relay_lock:
        return _relay_id is not None


def relay_unlink():
    global _relay_id, _relay_queue
    with _relay_lock:
        _relay_id = None
        _relay_queue = None


# --- خادم Flask صغير لنقاط نهاية الوكيل فقط (يستدعيها relay_agent.py من الهاتف) ---
relay_app = Flask(__name__)


@relay_app.post("/api/relay/register")
def _relay_register():
    global _relay_id, _relay_queue
    body = request.get_json(silent=True) or {}
    code = str(body.get("code", "")).strip()
    with _relay_lock:
        valid = (
            code and code == _relay_pair_code["code"]
            and time.time() - _relay_pair_code["ts"] <= RELAY_PAIR_TTL
        )
        if not valid:
            return jsonify(ok=False, error="رمز الاقتران غير صالح أو منتهي الصلاحية"), 400
        _relay_pair_code["code"] = None  # استخدام لمرة واحدة
        new_id = secrets.token_urlsafe(16)
        _relay_id = new_id
        _relay_queue = queue.Queue()
    log.info("relay: تم ربط الهاتف بنجاح ✅")
    notify_admin("📱 ✅ تم ربط هاتفك بنجاح -- الطلبات ستُنفَّذ الآن من شبكته مباشرة.")
    return jsonify(ok=True, relay_id=new_id)


@relay_app.get("/api/relay/next-job")
def _relay_next_job():
    relay_id = request.args.get("relay_id", "")
    with _relay_lock:
        if relay_id != _relay_id or _relay_queue is None:
            return jsonify(ok=False, error="relay_id غير معروف -- أعد الاقتران"), 404
        q = _relay_queue
    try:
        job = q.get(timeout=RELAY_JOB_POLL_WAIT)
    except queue.Empty:
        return jsonify(ok=True, job=None)
    return jsonify(ok=True, job=job)


@relay_app.post("/api/relay/submit-result")
def _relay_submit_result():
    body = request.get_json(silent=True) or {}
    job_id = str(body.get("job_id", ""))
    with _relay_lock:
        entry = _relay_pending.get(job_id)
        if not entry:
            return jsonify(ok=False, error="مهمة غير معروفة أو انتهت مهلتها"), 404
        entry["result"] = {
            "status_code": body.get("status_code", 0),
            "body": body.get("body", ""),
            "error": body.get("error"),
        }
        entry["event"].set()
    return jsonify(ok=True)


@relay_app.get("/health")
def _health():
    return jsonify(ok=True)


def start_relay_server():
    port = int(os.environ.get("PORT", "5000"))
    threading.Thread(
        target=lambda: relay_app.run(host="0.0.0.0", port=port, threaded=True, use_reloader=False),
        daemon=True, name="relay-http",
    ).start()
    log.info("📡 خادم الوكيل يستمع على المنفذ %s", port)


# ============================================================
# 🎁 العروض -- اكتشاف تلقائي (حتى المخفية) + قائمة يدوية مُعدَّة سلفاً
# ============================================================
def _looks_like_offer(d):
    if not isinstance(d, dict):
        return False
    name_keys = ("name", "commercialName", "offerName", "title", "label")
    extra_keys = ("price", "duration", "code", "packageCode", "offerCode", "validity", "cost", "amount")
    has_name = any(k in d and d.get(k) not in (None, "") for k in name_keys)
    has_extra = any(k in d and d.get(k) not in (None, "") for k in extra_keys)
    return has_name and has_extra


def _find_offers_recursive(node, found, seen, path=""):
    if isinstance(node, dict):
        if _looks_like_offer(node):
            key = (
                str(node.get("code") or node.get("packageCode") or node.get("offerCode") or ""),
                str(pick_name(node.get("name"), "") or node.get("commercialName") or node.get("offerName") or node.get("title") or ""),
            )
            if key not in seen:
                seen.add(key)
                found.append({"node": node, "path": path})
        for k, v in node.items():
            _find_offers_recursive(v, found, seen, f"{path}.{k}" if path else str(k))
    elif isinstance(node, list):
        for i, item in enumerate(node):
            _find_offers_recursive(item, found, seen, f"{path}[{i}]")


CANDIDATE_OFFER_PATHS = [
    "/api/v1/subscribers/available-offers/{phone}",
    "/api/v1/subscribers/available-products/{phone}",
    "/api/v1/subscribers/eligible-offers/{phone}",
    "/api/v1/subscribers/eligible-products/{phone}",
    "/api/v1/subscribers/offers/{phone}",
    "/api/v1/subscribers/products-catalog/{phone}",
    "/api/v1/subscribers/recommended-offers/{phone}",
    "/api/v1/subscribers/targeted-offers/{phone}",
    "/api/v1/catalog/subscribers/{phone}/offers",
    "/api/v1/catalog/subscribers/{phone}/available-offers",
]


def fetch_discovered_offers(phone):
    """يرجع (offers, error_message). offers = [] لو فشل كل المسارات."""
    tried = []
    payload = None
    for tpl in CANDIDATE_OFFER_PATHS:
        path = tpl.format(phone=phone)
        r = djezzy(SID, "GET", path)
        if r is None:
            tried.append((path, "no-response"))
            continue
        tried.append((path, r.status_code))
        if r.status_code == 200:
            try:
                payload = r.json()
                break
            except Exception:
                continue

    if payload is None:
        return [], (
            "لم يُعثر على مسار صالح لجلب العروض من جيزي (كل المسارات المجرَّبة "
            "رجعت فشل/404). قد تكون جيزي غيّرت الـ API فعلاً."
        )

    known_offers = payload.get("data", {}).get("offers", []) or []
    known_keys = set()
    for o in known_offers:
        if isinstance(o, dict):
            known_keys.add((
                str(o.get("code") or o.get("packageCode") or o.get("offerCode") or ""),
                str(pick_name(o.get("name"), "") or o.get("commercialName") or o.get("offerName") or o.get("title") or ""),
            ))

    all_found = []
    _find_offers_recursive(payload, all_found, set())

    offers = []
    for item in all_found:
        o = item["node"]
        name = pick_name(o.get("name"), None) or o.get("commercialName") or o.get("offerName") or o.get("title") or o.get("label") or "?"
        code = o.get("code") or o.get("packageCode") or o.get("offerCode") or ""
        key = (str(code), str(name))
        offers.append({
            "name": str(name),
            "price": str(o.get("price") or o.get("cost") or o.get("amount") or "?"),
            "duration": str(o.get("duration") or o.get("validity") or "?"),
            "code": str(code),
            "hidden": key not in known_keys,
        })
    return offers, None


# ============================================================
# 🤖 طبقة تلقرام (Bot API عبر polling -- بدون مكتبات خارجية)
# ============================================================
TG_API = f"https://api.telegram.org/bot{BOT_TOKEN}"


def tg(method, **params):
    try:
        r = requests.post(f"{TG_API}/{method}", json=params, timeout=25)
        return r.json()
    except Exception as e:
        log.error("tg %s: %s", method, e)
        return {"ok": False}


def send(chat_id, text, reply_markup=None):
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    return tg("sendMessage", **payload)


def answer_callback(callback_id, text=None):
    kw = {"callback_query_id": callback_id}
    if text:
        kw["text"] = text
    tg("answerCallbackQuery", **kw)


def notify_admin(text):
    send(ADMIN_CHAT_ID, text)


MAIN_KEYBOARD = {
    "keyboard": [
        [{"text": "🔑 تسجيل الدخول"}, {"text": "📊 الرصيد"}],
        [{"text": "🎁 العروض"}, {"text": "📱 ربط الهاتف"}],
        [{"text": "🔄 تجديد الجلسة"}, {"text": "🚪 تسجيل الخروج"}],
    ],
    "resize_keyboard": True,
    "is_persistent": True,
}

# حالة انتظار إدخال بسيطة (بوت لمستخدم واحد فقط، فمتغيّر عام يكفي)
PENDING = {"action": None, "data": {}}


def is_admin(chat_id):
    return int(chat_id) == ADMIN_CHAT_ID


def send_pairing_message(chat_id):
    code = generate_pair_code()
    app_url = os.environ.get("APP_URL", f"http://localhost:{os.environ.get('PORT', '5000')}")
    cmd = f"python relay_agent.py --server {app_url} --code {code}"
    text = (
        "📱 <b>ربط الهاتف</b>\n\n"
        "شغّل هذا الأمر في Termux على هاتفك (صالح 5 دقائق):\n"
        f"<code>{cmd}</code>\n\n"
        "بعد الاقتران، كل طلب لجيزي يُنفَّذ فعلياً من شبكة شريحتك."
    )
    if "localhost" in app_url:
        text += "\n\n⚠️ لم يُضبط APP_URL -- هذا الأمر لن يعمل من هاتف على شبكة مختلفة عن هذا الخادم. اضبط متغيّر البيئة APP_URL على رابط الاستضافة الحقيقي."
    send(chat_id, text, MAIN_KEYBOARD)


def send_status(chat_id):
    u = store.get(SID)
    lines = ["📋 <b>الحالة الحالية:</b>\n"]
    if has_session():
        lines.append(f"👤 الرقم: {local_phone(u.get('phone', ''))}")
        lines.append(f"🕐 آخر دخول: {u.get('last_login', '؟')}")
    else:
        lines.append("🔴 لست مسجّل الدخول.")
    lines.append(f"📡 الوكيل (الهاتف): {'🟢 مربوط' if relay_connected() else '⚪ غير مربوط'}")
    send(chat_id, "\n".join(lines), MAIN_KEYBOARD)


def start_login(chat_id):
    PENDING["action"] = "await_phone"
    PENDING["data"] = {}
    send(chat_id, "📱 أرسل رقم هاتفك (مثال: 07xxxxxxxx):", {"remove_keyboard": True})


def handle_phone_input(chat_id, text):
    phone = fmt_phone(text)
    if len(phone) != 12 or not phone.startswith("213"):
        send(chat_id, "❌ رقم غير صحيح. أرسله بصيغة 07xxxxxxxx:")
        return

    r = http(
        "POST", f"{BASE_URL}/oauth2/registration",
        params={"msisdn": phone, "client_id": CLIENT_ID, "scope": "smsotp"},
        headers={"User-Agent": "MobileApp/3.0.0"},
        json={"consent-agreement": [{"marketing-notifications": False}], "is-consent": True},
    )
    if r is None or r.status_code not in (200, 201):
        msg = "تعذّر الاتصال بخوادم جيزي" if r is None else f"فشل إرسال الرمز ({r.status_code})"
        send(chat_id, f"❌ {msg}", MAIN_KEYBOARD)
        PENDING["action"] = None
        return

    PENDING["action"] = "await_otp"
    PENDING["data"] = {"phone": phone, "ts": time.time(), "tries": 0}
    send(chat_id, "📩 أُرسل رمز التحقق (6 أرقام) برسالة SMS. أرسله هنا:")


def handle_otp_input(chat_id, text):
    p = PENDING["data"]
    if not p or time.time() - p.get("ts", 0) > OTP_TTL:
        PENDING["action"] = None
        send(chat_id, "⏰ انتهت مهلة الرمز. اضغط 🔑 تسجيل الدخول من جديد.", MAIN_KEYBOARD)
        return

    otp = text.strip()
    if not (otp.isdigit() and len(otp) == 6):
        send(chat_id, "❌ الرمز يتكوّن من 6 أرقام بالضبط. أعد الإرسال:")
        return

    p["tries"] += 1
    if p["tries"] > OTP_MAX_TRIES:
        PENDING["action"] = None
        send(chat_id, "❌ محاولات كثيرة. اضغط 🔑 تسجيل الدخول من جديد.", MAIN_KEYBOARD)
        return

    phone = p["phone"]
    r = http("POST", f"{BASE_URL}/oauth2/token", data={
        "otp": otp, "mobileNumber": phone, "grant_type": "mobile",
        "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET, "scope": "djezzyAppV2",
    })
    if r is None:
        send(chat_id, "❌ تعذّر الاتصال بخوادم جيزي.", MAIN_KEYBOARD)
        return
    if r.status_code != 200:
        send(chat_id, f"❌ رمز خاطئ أو منتهي ({r.status_code}). أعد الإرسال أو اطلب رمزاً جديداً:")
        return

    try:
        d = r.json()
        exp = (datetime.now() + timedelta(seconds=int(d.get("expires_in", 3600)))).isoformat()
        store.update(
            SID, phone=phone, access_token=d["access_token"], refresh_token=d.get("refresh_token"),
            token_expiry=exp, last_login=datetime.now().isoformat(),
            login_count=store.get(SID).get("login_count", 0) + 1,
        )
    except Exception as e:
        log.error("otp token parse: %s", e)
        send(chat_id, "❌ ردّ غير مفهوم من الخادم.", MAIN_KEYBOARD)
        return

    PENDING["action"] = None
    send(chat_id, f"✅ تم تسجيل الدخول بنجاح!\n👤 {local_phone(phone)}", MAIN_KEYBOARD)


def send_balance(chat_id):
    if not has_session():
        send(chat_id, "🔴 سجّل الدخول أولاً.", MAIN_KEYBOARD)
        return
    u = store.get(SID)
    r = djezzy(SID, "GET", f"/api/v1/subscribers/connected-products-balances/{u['phone']}")
    if r is None:
        send(chat_id, "❌ تعذّر الاتصال بخوادم جيزي.", MAIN_KEYBOARD)
        return
    if r.status_code != 200:
        send(chat_id, f"❌ فشل جلب الرصيد ({r.status_code}).", MAIN_KEYBOARD)
        return
    try:
        data = r.json()
    except Exception:
        send(chat_id, "❌ ردّ غير مفهوم من الخادم.", MAIN_KEYBOARD)
        return

    d = data.get("data", {}) or data
    lines = [f"💰 <b>الرصيد الرئيسي:</b> {d.get('mainBalance', 0)} DA\n"]
    for p in d.get("products", []) or d.get("connectedProducts", []) or []:
        name = pick_name(p.get("commercialName"), p.get("code", "باقة"))
        lines.append(f"\n📦 <b>{name}</b> (ينتهي: {p.get('expiryAt', '؟')})")
        for b in p.get("balances", []) or []:
            kind = (b.get("usageType") or "").upper()
            unit = b.get("usageUnit", "") or ""
            rem, tot = num(b.get("remaining", 0)), num(b.get("totalAmount", 0))
            if kind == "DATA":
                label, rem_t = "الإنترنت", _fmt_amount(rem, unit)
            elif kind in ("VOICE", "MINUTES"):
                label, rem_t = "المكالمات", _fmt_amount(rem, unit)
            elif kind == "SMS":
                label, rem_t = "الرسائل", f"{rem:g}"
            else:
                label, rem_t = pick_name(b.get("name"), kind or "رصيد"), _fmt_amount(rem, unit)
            tot_t = _fmt_amount(tot, unit) if tot > 0 else None
            lines.append(f"  • {label}: {rem_t}" + (f" / {tot_t}" if tot_t else ""))

    send(chat_id, "\n".join(lines), MAIN_KEYBOARD)


def send_offers(chat_id):
    if not has_session():
        send(chat_id, "🔴 سجّل الدخول أولاً.", MAIN_KEYBOARD)
        return

    buttons = []
    for key, pkg in OFFERS.items():
        if key == WEEKLY_KEY:
            rs = reward_state(store.get(SID).get("phone"))
            if not rs["available"]:
                continue
        buttons.append([{"text": f"{pkg['name']} — {pkg['price']}", "callback_data": f"act:{key}"}])

    text = "🎁 <b>العروض المتاحة:</b>\n\n"
    u = store.get(SID)
    discovered, err = fetch_discovered_offers(u["phone"])
    if err:
        text += f"⚠️ {err}\n\n"
    elif discovered:
        text += f"🔎 <b>تم اكتشاف {len(discovered)} عرضاً من حسابك مباشرة</b> (منها {sum(1 for o in discovered if o['hidden'])} غير ظاهرة في القائمة الرسمية):\n"
        for o in discovered[:20]:
            tag = " 🔎مخفي" if o["hidden"] else ""
            text += f"  • {o['name']} — {o['price']} — {o['duration']}{tag}\n"
    else:
        text += "لا توجد عروض إضافية مكتشفة من حسابك حالياً.\n"

    text += "\n👇 أو فعّل مباشرة من القائمة الجاهزة:"
    send(chat_id, text, {"inline_keyboard": buttons} if buttons else None)


def do_activate(chat_id, key):
    if not has_session():
        send(chat_id, "🔴 سجّل الدخول أولاً.", MAIN_KEYBOARD)
        return
    pkg = OFFERS.get(key)
    if not pkg:
        send(chat_id, "❌ العرض غير موجود.")
        return

    u = store.get(SID)
    if key == WEEKLY_KEY:
        rs = reward_state(u["phone"])
        if not rs["available"]:
            send(chat_id, "⏳ لم يحن وقت تفعيل الجائزة الأسبوعية بعد.")
            return

    path = (f"/api/v1/services/walk/activate-reward/{u['phone']}" if pkg["type"] == "reward"
            else f"/api/v1/subscribers/activate-product/{u['phone']}")
    r = djezzy(SID, "POST", path, json={"packageCode": pkg["code"]})
    if r is None:
        send(chat_id, "❌ تعذّر الاتصال بخوادم جيزي.")
        return
    if r.status_code in (200, 201, 202):
        if key == WEEKLY_KEY:
            mark_reward(u["phone"])
        send(chat_id, f"✅ تم تفعيل: {pkg['name']}")
    else:
        send(chat_id, f"❌ فشل التفعيل: {extract_error(r)}")


def do_logout(chat_id):
    store.delete(SID)
    PENDING["action"] = None
    send(chat_id, "🚪 تم تسجيل الخروج.", MAIN_KEYBOARD)


def do_refresh(chat_id):
    if not has_session():
        send(chat_id, "🔴 سجّل الدخول أولاً.", MAIN_KEYBOARD)
        return
    if refresh_token(SID, force=True):
        send(chat_id, "✅ تم تجديد الجلسة.", MAIN_KEYBOARD)
    else:
        send(chat_id, "❌ فشل التجديد. سجّل الدخول من جديد.", MAIN_KEYBOARD)


def handle_message(msg):
    chat_id = msg["chat"]["id"]
    if not is_admin(chat_id):
        log.warning("رسالة من مستخدم غير مخوّل: %s", chat_id)
        return  # صامت تماماً لغير الأدمن

    text = (msg.get("text") or "").strip()

    if PENDING["action"] == "await_phone":
        handle_phone_input(chat_id, text)
        return
    if PENDING["action"] == "await_otp":
        handle_otp_input(chat_id, text)
        return

    if text in ("/start", "🏠"):
        send(chat_id, "🌙 <b>مرحباً بك في by_moon</b>\nاختر من القائمة:", MAIN_KEYBOARD)
    elif text in ("/login", "🔑 تسجيل الدخول"):
        start_login(chat_id)
    elif text in ("/balance", "📊 الرصيد"):
        send_balance(chat_id)
    elif text in ("/offers", "🎁 العروض"):
        send_offers(chat_id)
    elif text in ("/pair", "📱 ربط الهاتف"):
        send_pairing_message(chat_id)
    elif text in ("/refresh", "🔄 تجديد الجلسة"):
        do_refresh(chat_id)
    elif text in ("/logout", "🚪 تسجيل الخروج"):
        do_logout(chat_id)
    elif text in ("/status",):
        send_status(chat_id)
    else:
        send(chat_id, "لم أفهم -- استخدم الأزرار أدناه:", MAIN_KEYBOARD)


def handle_callback(cq):
    chat_id = cq["message"]["chat"]["id"]
    if not is_admin(chat_id):
        return
    data = cq.get("data", "")
    answer_callback(cq["id"])
    if data.startswith("act:"):
        do_activate(chat_id, data.split(":", 1)[1])


def poll_loop():
    offset = 0
    log.info("🤖 البوت يعمل الآن (polling)...")
    while True:
        try:
            r = requests.get(f"{TG_API}/getUpdates", params={"offset": offset, "timeout": 30}, timeout=35)
            data = r.json()
            if not data.get("ok"):
                time.sleep(3)
                continue
            for update in data.get("result", []):
                offset = update["update_id"] + 1
                try:
                    if "message" in update:
                        handle_message(update["message"])
                    elif "callback_query" in update:
                        handle_callback(update["callback_query"])
                except Exception:
                    log.exception("خطأ أثناء معالجة تحديث")
        except requests.exceptions.RequestException as e:
            log.error("getUpdates: %s", e)
            time.sleep(3)
        except Exception:
            log.exception("poll_loop")
            time.sleep(3)


# ============================================================
# 🚀 التشغيل
# ============================================================
if __name__ == "__main__":
    start_background()
    start_relay_server()

    # إشعار الأدمن فوراً بأمر ربط الهاتف عند كل إقلاع، كما طُلب.
    try:
        send_pairing_message(ADMIN_CHAT_ID)
    except Exception:
        log.exception("فشل إرسال رسالة الاقتران عند الإقلاع")

    poll_loop()
