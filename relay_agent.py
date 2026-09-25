#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
🌙 by_moon — وكيل الهاتف (Relay Agent)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
يُشغَّل هذا الملف على هاتف المستخدم (عبر Termux)، بينما app.py نفسه مستضاف
خارجياً في مكان آخر تماماً. مهمته الوحيدة: سحب "مهام" (طلبات HTTP جاهزة)
من الخادم المستضاف، تنفيذها فعلياً من هنا (فتطلع من شريحة هذا الهاتف)، ثم
إرجاع النتيجة للخادم.

الاستخدام:
    pip install requests
    python relay_agent.py --server https://your-hosted-app.example.com --code 123456

الكود (--code) يظهر في واجهة الويب عند الضغط على "🔗 ربط الهاتف"، وصالح
لمدة 5 دقائق فقط من لحظة توليده.
"""

import argparse
import sys
import time

import requests


def register(server, code):
    r = requests.post(f"{server}/api/relay/register", json={"code": code}, timeout=15)
    data = r.json()
    if not data.get("ok"):
        raise SystemExit(f"❌ فشل الاقتران: {data.get('error', 'خطأ غير معروف')}")
    return data["relay_id"]


def run_job(job):
    """ينفّذ الطلب المُرسَل من الخادم بالضبط كما هو -- نفس الميثود، الرابط،
    الترويسات، والمحتوى -- من شبكة هذا الهاتف مباشرة."""
    try:
        resp = requests.request(
            job["method"],
            job["url"],
            headers=job.get("headers") or {},
            data=(job.get("body") or "").encode("utf-8") if job.get("body") else None,
            timeout=25,
        )
        return {"job_id": job["id"], "status_code": resp.status_code, "body": resp.text}
    except Exception as e:
        return {"job_id": job["id"], "status_code": 0, "body": "", "error": str(e)}


def main():
    ap = argparse.ArgumentParser(description="وكيل هاتف by_moon")
    ap.add_argument("--server", required=True, help="رابط الخادم المستضاف، مثال: https://your-app.example.com")
    ap.add_argument("--code", required=True, help="كود الاقتران المعروض في واجهة الويب")
    args = ap.parse_args()

    server = args.server.rstrip("/")

    print("🌙 جاري الاقتران بالخادم...")
    try:
        relay_id = register(server, args.code)
    except requests.exceptions.RequestException as e:
        sys.exit(f"❌ تعذّر الوصول للخادم: {e}")

    print("✅ تم الاقتران بنجاح! الوكيل يعمل الآن -- اتركه مفتوحاً في Termux.")
    print("   (اضغط Ctrl+C لإيقافه)")

    backoff = 1
    while True:
        try:
            r = requests.get(
                f"{server}/api/relay/next-job",
                params={"relay_id": relay_id},
                timeout=35,  # أطول قليلاً من مهلة الـ long-poll في الخادم (25 ثانية)
            )
            backoff = 1  # نجح الاتصال، صفّر التراجع الأسي

            if r.status_code == 404:
                sys.exit("❌ انتهت صلاحية الاقتران (ربما أُعيد تشغيل الخادم) -- شغّل الوكيل من جديد بكود جديد.")

            data = r.json()
            job = data.get("job")
            if not job:
                continue  # لا توجد مهمة حالياً، أعد المحاولة (long-poll التالي)

            print(f"📡 تنفيذ طلب: {job['method']} {job['url'][:80]}...")
            result = run_job(job)
            requests.post(f"{server}/api/relay/submit-result", json=result, timeout=15)

        except requests.exceptions.RequestException as e:
            print(f"⚠️ خطأ في الاتصال بالخادم: {e} -- إعادة المحاولة بعد {backoff}s")
            time.sleep(backoff)
            backoff = min(backoff * 2, 30)
        except KeyboardInterrupt:
            print("\n👋 تم إيقاف الوكيل.")
            break


if __name__ == "__main__":
    main()
