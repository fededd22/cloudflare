#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
🌙 by_moon — لوحة جيزي على الويب
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
تشغيل محلي :  python app.py            ثم افتح http://localhost:5000
تشغيل إنتاج :  gunicorn -w 1 --threads 8 -b 0.0.0.0:$PORT app:app

⚠️ استخدم worker واحد فقط (-w 1) لأن الجلسات محفوظة في ملف JSON
   والمجدّد التلقائي يعمل كخيط خلفي داخل العملية.

الجلسة مرتبطة بالمتصفّح (كوكي HttpOnly) وليس بالرقم فقط،
فلا يمكن لأحد استئناف جلسة رقمك بمجرد كتابته.
"""

import json
import logging
import os
import re
import secrets
import shutil
import threading
import time
from datetime import datetime, timedelta
from functools import wraps

import requests
from flask import Flask, g, jsonify, request, send_from_directory

logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
log = logging.getLogger("by_moon")

# ============================================================
# ⚙️ الإعدادات
# ============================================================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")
DATA_FILE = os.path.join(BASE_DIR, "session.json")
BACKUP_FILE = os.path.join(BASE_DIR, "session.backup.json")

CLIENT_ID = os.environ.get("DJEZZY_CLIENT_ID", "87pIExRhxBb3_wGsA5eSEfyATloa")
CLIENT_SECRET = os.environ.get("DJEZZY_CLIENT_SECRET", "uf82p68Bgisp8Yg1Uz8Pf6_v1XYa")
BASE_URL = "https://apim.djezzy.dz/mobile-api"

REFRESH_BEFORE = 60          # جدّد قبل انتهاء التوكن بـ 60 ثانية
CHECK_INTERVAL = 30          # افحص كل 30 ثانية
COOKIE_NAME = "moon_cid"
COOKIE_MAX_AGE = 60 * 60 * 24 * 365
COOKIE_SECURE = os.environ.get("COOKIE_SECURE", "0") == "1"   # فعّلها خلف HTTPS
OTP_COOLDOWN = 60            # ثانية بين كل طلبي OTP (للمتصفح وللرقم)
OTP_TTL = 600                # صلاحية الانتظار لإدخال الكود
OTP_MAX_TRIES = 5

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
# 💾 التخزين (جلسات لكل متصفح + سجل الجائزة لكل رقم)
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

    def get(self, cid):
        with self.lock:
            return dict(self.data["sessions"].get(cid, {}))

    def set(self, cid, value):
        with self.lock:
            self.data["sessions"][cid] = dict(value)
            self._save()

    def update(self, cid, **kw):
        with self.lock:
            u = dict(self.data["sessions"].get(cid, {}))
            u.update(kw)
            self.data["sessions"][cid] = u
            self._save()
            return dict(u)

    def delete(self, cid):
        with self.lock:
            self.data["sessions"].pop(cid, None)
            self._save()

    def ids(self):
        with self.lock:
            return list(self.data["sessions"].keys())

    def reward_get(self, phone):
        with self.lock:
            return self.data["rewards"].get(phone)

    def reward_set(self, phone, iso):
        with self.lock:
            self.data["rewards"][phone] = iso
            self._save()


store = Store()


# ============================================================
# 🛠️ مساعدات
# ============================================================
def fmt_phone(p):
    p = re.sub(r"\D", "", p or "")
    if p.startswith("0"):
        p = "213" + p[1:]
    elif not p.startswith("213"):
        p = "213" + p
    return p


def http(method, url, retries=2, **kw):
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


def to_ts(iso):
    try:
        return datetime.fromisoformat(iso).timestamp()
    except Exception:
        return None


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


# ============================================================
# 🎁 الجائزة الأسبوعية (مرتبطة بالرقم)
# ============================================================
def reward_state(phone):
    last_iso = store.reward_get(phone) if phone else None
    last_ts = to_ts(last_iso) if last_iso else None
    next_ts = (last_ts + WEEKLY_COOLDOWN) if last_ts else None
    remaining = (next_ts - time.time()) if next_ts else 0
    return {
        "available": remaining <= 0,
        "last_ts": last_ts,
        "next_ts": next_ts,
        "cooldown": WEEKLY_COOLDOWN,
    }


def mark_reward(phone):
    store.reward_set(phone, datetime.now().isoformat())
    log.info("🎁 تفعيل الجائزة الأسبوعية")


# ============================================================
# 🔄 إدارة التوكن
# ============================================================
_locks = {}
_locks_guard = threading.Lock()


def lock_for(cid):
    with _locks_guard:
        return _locks.setdefault(cid, threading.Lock())


def token_remaining(cid):
    ts = to_ts(store.get(cid).get("token_expiry") or "")
    return (ts - time.time()) if ts else 0


def refresh_token(cid, force=False):
    """يجدد التوكن ويعيد access_token الجديد أو None."""
    with lock_for(cid):
        u = store.get(cid)
        rt = u.get("refresh_token")
        if not rt:
            return None
        if not force and u.get("access_token") and token_remaining(cid) > REFRESH_BEFORE:
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
                        cid,
                        access_token=d["access_token"],
                        refresh_token=d.get("refresh_token", rt),
                        token_expiry=exp,
                        last_refresh=datetime.now().isoformat(),
                        refresh_count=u.get("refresh_count", 0) + 1,
                    )
                    log.info("✅ تم تجديد توكن جلسة")
                    return d["access_token"]
                except Exception as e:
                    log.error("refresh parse: %s", e)
                    return None
            if r is not None and r.status_code in (400, 401):
                log.warning("⚠️ refresh_token منتهي (%s) — سيُطلب OTP", r.status_code)
                store.update(cid, access_token=None, refresh_token=None)
                return None
            log.warning("⚠️ فشل التجديد (محاولة %d)", attempt + 1)
        return None


def access_token(cid):
    u = store.get(cid)
    if not u.get("access_token"):
        return None
    if token_remaining(cid) <= REFRESH_BEFORE:
        fresh = refresh_token(cid)
        if fresh:
            return fresh
        # فشل التجديد مؤقتاً لكن التوكن الحالي ما زال صالحاً
        if token_remaining(cid) > 0:
            return store.get(cid).get("access_token")
        return None
    return u["access_token"]


def djezzy(cid, method, path, **kw):
    """طلب موثّق إلى API جيزي، مع إعادة المحاولة بعد تجديد التوكن عند 401."""
    r = None
    for attempt in range(2):
        tok = access_token(cid) if attempt == 0 else refresh_token(cid, force=True)
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


def refresh_due():
    for cid in store.ids():
        try:
            u = store.get(cid)
            if not u.get("refresh_token"):
                continue
            if token_remaining(cid) <= REFRESH_BEFORE:
                refresh_token(cid)
        except Exception:
            log.exception("auto refresh")


_bg_started = False


def start_background():
    """يجدد كل الجلسات فور التشغيل ثم كل CHECK_INTERVAL ثانية."""
    global _bg_started
    if _bg_started:
        return
    _bg_started = True

    def loop():
        time.sleep(2)
        while True:
            try:
                refresh_due()
            except Exception:
                log.exception("bg loop")
            time.sleep(CHECK_INTERVAL)

    threading.Thread(target=loop, daemon=True, name="auto-refresh").start()
    log.info("⏰ المجدّد التلقائي يعمل (كل %d ثانية)", CHECK_INTERVAL)


# ============================================================
# 🌐 تطبيق Flask
# ============================================================
app = Flask(__name__, static_folder=None)
try:
    app.json.ensure_ascii = False
except Exception:
    pass


def ok(**kw):
    return jsonify(ok=True, **kw)


def fail(msg, code=400, **kw):
    return jsonify(ok=False, error=msg, **kw), code


_ID_RE = re.compile(r"[A-Za-z0-9_\-]{20,64}")


def current_cid():
    cid = request.cookies.get(COOKIE_NAME, "")
    return cid if _ID_RE.fullmatch(cid) else None


def ensure_cid():
    cid = current_cid()
    if not cid:
        cid = secrets.token_urlsafe(32)
        g.new_cid = cid
    return cid


def has_session(u):
    return bool(u.get("access_token") and u.get("phone"))


def login_required(fn):
    @wraps(fn)
    def wrapper(*a, **kw):
        cid = current_cid()
        u = store.get(cid) if cid else {}
        if not has_session(u):
            return fail("سجّل الدخول أولاً", 401, code="login_required")
        g.cid, g.phone = cid, u["phone"]
        return fn(*a, **kw)
    return wrapper


def upstream_failed(cid):
    if not store.get(cid).get("access_token"):
        return fail("انتهت الجلسة، سجّل الدخول من جديد", 401, code="login_required")
    return fail("تعذّر الاتصال بخوادم جيزي، حاول بعد قليل", 502)


@app.before_request
def guard():
    if request.path.startswith("/api/") and request.method == "POST" and not request.is_json:
        return fail("طلب غير صالح", 415)


@app.after_request
def after(resp):
    if request.path.startswith("/api/") or request.path == "/":
        resp.headers["Cache-Control"] = "no-store"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "no-referrer"
    cid = getattr(g, "new_cid", None)
    if cid:
        resp.set_cookie(
            COOKIE_NAME, cid, max_age=COOKIE_MAX_AGE,
            httponly=True, samesite="Lax", secure=COOKIE_SECURE,
        )
    return resp


@app.errorhandler(500)
def internal(e):
    log.exception("500: %s", e)
    return fail("خطأ داخلي في الخادم", 500)


@app.get("/")
def index():
    return send_from_directory(STATIC_DIR, "index.html")


# ---------- الحالة (تُستدعى عند فتح الصفحة: تتصل تلقائياً وتجدد إن لزم) ----------
@app.get("/api/state")
def api_state():
    cid = current_cid()
    u = store.get(cid) if cid else {}
    if has_session(u) and token_remaining(cid) <= REFRESH_BEFORE:
        refresh_token(cid)
        u = store.get(cid)

    logged = has_session(u)
    phone = u.get("phone")
    return ok(
        now=time.time(),
        logged_in=logged,
        phone=phone,
        token_expiry_ts=to_ts(u.get("token_expiry") or "") if logged else None,
        last_login=u.get("last_login"),
        last_refresh=u.get("last_refresh"),
        login_count=u.get("login_count", 0),
        refresh_count=u.get("refresh_count", 0),
        reward=reward_state(phone) if phone else None,
        weekly_key=WEEKLY_KEY,
        offers=[
            {"key": k, "name": v["name"], "price": v["price"], "duration": v["duration"], "type": v["type"]}
            for k, v in OFFERS.items()
        ],
    )


# ---------- تسجيل الدخول ----------
_otp_lock = threading.Lock()
_otp_sent = {}
_pending = {}


@app.post("/api/login/phone")
def login_phone():
    cid = ensure_cid()
    body = request.get_json(silent=True) or {}
    phone = fmt_phone(str(body.get("phone", "")))
    if len(phone) != 12 or not phone.startswith("213"):
        return fail("رقم غير صحيح. اكتبه بصيغة 07xxxxxxxx")

    now = time.time()
    keys = (f"c:{cid}", f"p:{phone}")
    with _otp_lock:
        if len(_otp_sent) > 2000:
            for k in [k for k, t in _otp_sent.items() if now - t > OTP_COOLDOWN]:
                _otp_sent.pop(k, None)
        wait = OTP_COOLDOWN - (now - max(_otp_sent.get(k, 0) for k in keys))
        if wait > 0:
            return fail(f"انتظر {int(wait) + 1} ثانية قبل طلب رمز جديد", 429)
        for k in keys:
            _otp_sent[k] = now

    r = http(
        "POST", f"{BASE_URL}/oauth2/registration",
        params={"msisdn": phone, "client_id": CLIENT_ID, "scope": "smsotp"},
        headers={"User-Agent": "MobileApp/3.0.0"},
        json={"consent-agreement": [{"marketing-notifications": False}], "is-consent": True},
    )
    if r is None or r.status_code not in (200, 201):
        with _otp_lock:
            for k in keys:
                _otp_sent.pop(k, None)
        if r is None:
            return fail("تعذّر الاتصال بخوادم جيزي", 502)
        return fail(f"فشل إرسال الرمز ({r.status_code})", 400)

    _pending[cid] = {"phone": phone, "ts": now, "tries": 0}
    return ok(phone=phone)


@app.post("/api/login/otp")
def login_otp():
    cid = ensure_cid()
    otp = str((request.get_json(silent=True) or {}).get("otp", "")).strip()
    p = _pending.get(cid)
    if not p or time.time() - p["ts"] > OTP_TTL:
        _pending.pop(cid, None)
        return fail("انتهت مهلة الرمز. اطلب رمزاً جديداً", 400, code="restart")
    if not (otp.isdigit() and len(otp) == 6):
        return fail("الرمز يتكوّن من 6 أرقام")
    p["tries"] += 1
    if p["tries"] > OTP_MAX_TRIES:
        _pending.pop(cid, None)
        return fail("محاولات كثيرة. اطلب رمزاً جديداً", 429, code="restart")

    phone = p["phone"]
    r = http("POST", f"{BASE_URL}/oauth2/token", data={
        "otp": otp, "mobileNumber": phone, "grant_type": "mobile",
        "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET, "scope": "djezzyAppV2",
    })
    if r is None:
        return fail("تعذّر الاتصال بخوادم جيزي", 502)
    if r.status_code != 200:
        try:
            err = r.json().get("error_description") or r.text[:200]
        except Exception:
            err = r.text[:200]
        return fail(f"الرمز غير صحيح ({r.status_code}): {err}")

    try:
        d = r.json()
        exp = (datetime.now() + timedelta(seconds=int(d.get("expires_in", 3600)))).isoformat()
        old = store.get(cid)
        store.set(cid, {
            "phone": phone,
            "access_token": d["access_token"],
            "refresh_token": d.get("refresh_token"),
            "token_expiry": exp,
            "last_login": datetime.now().isoformat(),
            "login_count": old.get("login_count", 0) + 1,
            "refresh_count": old.get("refresh_count", 0),
        })
    except Exception as e:
        log.exception("otp save")
        return fail(f"خطأ أثناء حفظ الجلسة: {e}", 500)

    _pending.pop(cid, None)
    return ok(phone=phone)


@app.post("/api/logout")
def api_logout():
    cid = current_cid()
    if cid:
        store.delete(cid)
    return ok()


@app.post("/api/refresh")
@login_required
def api_refresh():
    if not refresh_token(g.cid, force=True):
        return upstream_failed(g.cid)
    return ok()


# ---------- الرصيد ----------
def _fmt_amount(v, unit):
    if unit == "MB":
        return f"{v:.0f} MB" if v < 1024 else f"{v / 1024:.2f} GB"
    return (f"{v:g} {unit}").strip()


@app.get("/api/balance")
@login_required
def api_balance():
    r = djezzy(g.cid, "GET", f"/api/v1/subscribers/connected-products-balances/{g.phone}")
    if r is None:
        return upstream_failed(g.cid)
    if r.status_code != 200:
        return fail(f"فشل جلب الرصيد ({r.status_code})", 502)
    try:
        data = r.json()
    except Exception:
        return fail("ردّ غير مفهوم من الخادم", 502)

    d = data.get("data", {}) or data
    products = []
    for p in d.get("products", []) or d.get("connectedProducts", []) or []:
        name = pick_name(p.get("commercialName"), p.get("code", "باقة"))
        expiry = p.get("expiryAt", "")
        items = []
        for b in p.get("balances", []) or []:
            kind = (b.get("usageType") or "").upper()
            unit = b.get("usageUnit", "") or ""
            rem, tot = num(b.get("remaining", 0)), num(b.get("totalAmount", 0))
            pct = max(0, min(100, rem / tot * 100)) if tot > 0 else None
            if kind == "DATA":
                label, rem_t = "الإنترنت", _fmt_amount(rem, unit)
                tot_t = _fmt_amount(tot, unit) if tot > 0 else None
            elif kind in ("VOICE", "MINUTES"):
                label, rem_t, tot_t = "المكالمات", _fmt_amount(rem, unit), (_fmt_amount(tot, unit) if tot > 0 else None)
            elif kind == "SMS":
                label, rem_t, tot_t = "الرسائل", f"{rem:g}", (f"{tot:g}" if tot > 0 else None)
            else:
                label = pick_name(b.get("name"), kind or "رصيد")
                rem_t, tot_t = _fmt_amount(rem, unit), (_fmt_amount(tot, unit) if tot > 0 else None)
            items.append({
                "label": label, "remaining": rem_t, "total": tot_t, "pct": pct,
                "expiry": b.get("expiryAt", expiry) or "",
            })
        products.append({"name": str(name), "expiry": str(expiry or ""), "balances": items})

    return ok(main_balance=d.get("mainBalance", 0), products=products)


# ---------- العروض ----------
def _looks_like_offer(d):
    """يحدد إذا كان القاموس يشبه بنية 'عرض' — بحقول اسم + (سعر أو مدة أو كود)،
    بغض النظر عن أسماء المفاتيح المستخدمة (تختلف بين نسخ الـ API)."""
    if not isinstance(d, dict):
        return False
    name_keys = ("name", "commercialName", "offerName", "title", "label")
    extra_keys = ("price", "duration", "code", "packageCode", "offerCode",
                  "validity", "cost", "amount")
    has_name = any(k in d and d.get(k) not in (None, "") for k in name_keys)
    has_extra = any(k in d and d.get(k) not in (None, "") for k in extra_keys)
    return has_name and has_extra


def _find_offers_recursive(node, found, seen, path=""):
    """يمشي داخل أي بنية JSON (قواميس/قوائم متداخلة) ويلتقط كل كائن يشبه
    عرضاً، أينما كان مخبأً في الرد (مثلاً data.offers، أو data.eligibleOffers،
    أو data.campaigns[].offers، أو أي مسار آخر لا نعرفه مسبقاً) بدل الاقتصار
    على مسار واحد ثابت كما كان سابقاً -- وهذا بالضبط سبب عدم ظهور بعض
    العروض: كانت موجودة في الرد لكن ضمن مفتاح مختلف لم يكن الكود يقرأه."""
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


@app.get("/api/available-offers")
@login_required
def api_available_offers():
    # المسار الأصلي (available-offers) بدأ يرجع 404 -- يعني جيزي غيّرت أو
    # أزالت هذا المسار من خادمها، وهذا لا علاقة له بكيفية قراءة الكود للرد.
    # بما أنني لا أملك وصولاً لخادم جيزي الحقيقي لأجرّب المسار الصحيح مباشرة،
    # أجرّب هنا كل الاحتمالات المعقولة (بنفس نمط التسمية المستخدم في المسارات
    # التي نعرف أنها تعمل فعلاً: connected-products-balances و
    # activate-product) وأتوقف عند أول مسار يرجع رداً صالحاً.
    candidate_paths = [
        f"/api/v1/subscribers/available-offers/{g.phone}",
        f"/api/v1/subscribers/available-products/{g.phone}",
        f"/api/v1/subscribers/eligible-offers/{g.phone}",
        f"/api/v1/subscribers/eligible-products/{g.phone}",
        f"/api/v1/subscribers/offers/{g.phone}",
        f"/api/v1/subscribers/products-catalog/{g.phone}",
        f"/api/v1/subscribers/recommended-offers/{g.phone}",
        f"/api/v1/subscribers/targeted-offers/{g.phone}",
        f"/api/v1/catalog/subscribers/{g.phone}/offers",
        f"/api/v1/catalog/subscribers/{g.phone}/available-offers",
    ]

    tried = []
    payload = None
    working_path = None
    for path in candidate_paths:
        r = djezzy(g.cid, "GET", path)
        if r is None:
            tried.append((path, "no-response"))
            continue
        tried.append((path, r.status_code))
        if r.status_code == 200:
            try:
                payload = r.json()
                working_path = path
                break
            except Exception:
                continue

    if payload is None:
        log.warning("available-offers: كل المسارات المجرَّبة فشلت: %s", tried)
        return fail(
            "لم يُعثر على أي مسار صالح لجلب العروض (كلها 404/فشل). "
            "على الأغلب جيزي غيّرت مسار الـ API فعلاً. "
            "أفضل طريقة لمعرفة المسار الصحيح الآن: افتح تطبيق جيزي الرسمي على "
            "هاتفك وتصفّح صفحة العروض بينما هاتفك متصل بأداة تصوير حركة الشبكة "
            "(مثل mitmproxy أو HTTP Toolkit) على نفس حسابك، وابعث لي المسار "
            "(URL) الذي يظهر هناك لأحدّث الكود به مباشرة.",
            404,
            tried_paths=[p for p, _ in tried],
        )

    log.info("available-offers: نجح المسار %s", working_path)

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
            # ظاهر في القائمة الرسمية المعتادة أم مكتشف من مكان آخر في نفس الرد
            "hidden": key not in known_keys,
            "path": item["path"],
        })

    return ok(offers=offers, total=len(offers), hidden_count=sum(1 for o in offers if o["hidden"]))



@app.post("/api/activate")
@login_required
def api_activate():
    key = str((request.get_json(silent=True) or {}).get("key", ""))
    pkg = OFFERS.get(key)
    if not pkg:
        return fail("العرض غير موجود", 404)

    if key == WEEKLY_KEY:
        rs = reward_state(g.phone)
        if not rs["available"]:
            return fail("لم يحن وقت تفعيل الجائزة بعد", 409, code="cooldown", reward=rs)

    if pkg["type"] == "reward":
        path = f"/api/v1/services/walk/activate-reward/{g.phone}"
    else:
        path = f"/api/v1/subscribers/activate-product/{g.phone}"

    r = djezzy(g.cid, "POST", path, json={"packageCode": pkg["code"]})
    if r is None:
        return upstream_failed(g.cid)
    if r.status_code in (200, 201, 202):
        extra = {}
        if key == WEEKLY_KEY:
            mark_reward(g.phone)
            extra["reward"] = reward_state(g.phone)
        return ok(name=pkg["name"], **extra)
    return fail(extract_error(r), 400, upstream=r.status_code)


# ============================================================
# 🚀 التشغيل
# ============================================================
start_background()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    print(f"🌙 by_moon يعمل على http://localhost:{port}")
    app.run(host=os.environ.get("HOST", "0.0.0.0"), port=port, threaded=True)
