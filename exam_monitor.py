"""
my.sa.gov.ge — მართვის თეორიული გამოცდის თავისუფალი ადგილების მონიტორი.

ნაკადი (აღებულია საიტის ფრონტენდიდან):
  1. POST  identity /api/Accounts/GetEmailStatus   {Username, Password}
  2. POST  identity /api/Accounts/Authentication   {Username, Password, authCodeType: 4 (email) | 1 (SMS)}
        -> {session}  და მეილზე მოდის კოდი
  3. POST  identity /connect/token  (password grant, headers: code, session) -> access_token
  4. GET   api-my  /DrivingLicenseTheoryExams/Categories
           api-my  /DrivingLicenseTheoryExams/Centers?CategoryCode=..
           api-my  /DrivingLicenseTheoryExams/Dates?CategoryCode=..&CenterId=..
           api-my  /DrivingLicenseTheoryExams/DateFrames?CategoryCode=..&CenterId=..&ExamDate=yyyy-MM-dd

გაშვება:
  python exam_monitor.py --discover   # ერთხელ შემოწმება და ნედლი პასუხების ჩვენება
  python exam_monitor.py              # უწყვეტი მონიტორინგი
  python exam_monitor.py --chat-id    # Telegram chat id-ის გასაგებად
"""
from __future__ import annotations

import argparse
import email
import imaplib
import json
import os
import re
import sys
import time
from datetime import datetime
from email.header import decode_header
from pathlib import Path

try:  # Windows/კორპორატიული პროქსის სერტიფიკატებისთვის — სისტემის cert store-ს იყენებს
    import truststore

    truststore.inject_into_ssl()
except ImportError:
    pass

import requests
from dotenv import load_dotenv

for _s in (sys.stdout, sys.stderr):  # ქართული ტექსტი Windows კონსოლში
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:
        pass

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

IDENTITY = "https://api-identity.sa.gov.ge/"
API = "https://api-my.sa.gov.ge/api/v1/"
CLIENT_ID = "MySagencyWeb"
CLIENT_SECRET = "VNtru6we8zWM5tqK"  # საჯარო, საიტის JS-შია ჩაშენებული

SA_USERNAME = os.getenv("SA_USERNAME", "")
SA_PASSWORD = os.getenv("SA_PASSWORD", "")
GMAIL_ADDRESS = os.getenv("GMAIL_ADDRESS", SA_USERNAME)
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD", "").replace(" ", "")
TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TG_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
CATEGORY = os.getenv("EXAM_CATEGORY", "B")  # კატეგორიის კოდი ან სახელის ნაწილი
CENTER = os.getenv("EXAM_CENTER", "რუსთავ")  # ცენტრის სახელის ნაწილი
INTERVAL = int(os.getenv("CHECK_INTERVAL_SECONDS", "300"))

STATE_FILE = BASE_DIR / "state.json"
TOKEN_FILE = BASE_DIR / ".token.json"

session = requests.Session()
session.headers.update({"Accept": "application/json", "Origin": "https://my.sa.gov.ge",
                        "Referer": "https://my.sa.gov.ge/", "User-Agent": "Mozilla/5.0"})


def log(msg: str) -> None:
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


# ---------------------------------------------------------------- Telegram
_tg_offset = 0


def tg_send(text: str) -> None:
    if not (TG_TOKEN and TG_CHAT_ID):
        log(f"(Telegram არ არის კონფიგურირებული) {text}")
        return
    try:
        requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                      data={"chat_id": TG_CHAT_ID, "text": text}, timeout=20)
    except requests.RequestException as e:
        log(f"Telegram შეცდომა: {e}")


def tg_updates(timeout: int = 0) -> list:
    global _tg_offset
    r = requests.get(f"https://api.telegram.org/bot{TG_TOKEN}/getUpdates",
                     params={"offset": _tg_offset, "timeout": timeout}, timeout=timeout + 15)
    res = r.json().get("result", [])
    if res:
        _tg_offset = res[-1]["update_id"] + 1
    return res


def tg_wait_for_code(max_wait: int = 175) -> str | None:
    """მეილის app password თუ არ არის — კოდს Telegram-ში ვთხოვთ მომხმარებელს."""
    tg_updates()  # ძველი შეტყობინებების გასუფთავება
    tg_send("🔐 my.sa.gov.ge-მ მეილზე კოდი გამოგზავნა. გთხოვ აქ გამომიგზავნე (3 წუთი გაქვს).")
    deadline = time.time() + max_wait
    while time.time() < deadline:
        for u in tg_updates(timeout=20):
            msg = u.get("message") or {}
            if str(msg.get("chat", {}).get("id")) == str(TG_CHAT_ID):
                m = re.search(r"\b(\d{4,8})\b", msg.get("text", ""))
                if m:
                    return m.group(1)
    return None


# ---------------------------------------------------------------- Gmail
def _decode(s) -> str:
    out = ""
    for part, enc in decode_header(s or ""):
        out += part.decode(enc or "utf-8", "ignore") if isinstance(part, bytes) else part
    return out


def _body(msg) -> str:
    texts = []
    for part in msg.walk() if msg.is_multipart() else [msg]:
        if part.get_content_type() in ("text/plain", "text/html"):
            payload = part.get_payload(decode=True) or b""
            texts.append(payload.decode(part.get_content_charset() or "utf-8", "ignore"))
    return re.sub(r"<[^>]+>", " ", "\n".join(texts))


def gmail_wait_for_code(since: float, max_wait: int = 150) -> str | None:
    deadline = time.time() + max_wait
    while time.time() < deadline:
        time.sleep(8)
        try:
            imap = imaplib.IMAP4_SSL("imap.gmail.com")
            imap.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
            imap.select("INBOX")
            _, data = imap.search(None, "SINCE", datetime.now().strftime("%d-%b-%Y"))
            for num in reversed(data[0].split()[-15:]):
                _, raw = imap.fetch(num, "(RFC822)")
                msg = email.message_from_bytes(raw[0][1])
                sender = _decode(msg.get("From")).lower()
                try:
                    ts = email.utils.parsedate_to_datetime(msg.get("Date")).timestamp()
                except Exception:
                    ts = time.time()
                if ts < since - 60 or "sa.gov.ge" not in sender and "sagency" not in sender:
                    continue
                m = re.search(r"\b(\d{4,8})\b", _decode(msg.get("Subject")) + " " + _body(msg))
                if m:
                    imap.logout()
                    return m.group(1)
            imap.logout()
        except imaplib.IMAP4.error as e:
            log(f"Gmail IMAP შეცდომა: {e}")
            return None
    return None


# ---------------------------------------------------------------- Apps Script
CODE_URL = os.getenv("CODE_URL", "").strip()
CODE_URL_KEY = os.getenv("CODE_URL_KEY", "").strip()


def script_wait_for_code(since: float, max_wait: int = 150) -> str | None:
    """კოდს იღებს Gmail-ში ჩასმული Apps Script-იდან (gmail_code.gs)."""
    deadline = time.time() + max_wait
    while time.time() < deadline:
        time.sleep(8)
        try:
            r = requests.get(CODE_URL, params={"key": CODE_URL_KEY, "since": int(since)}, timeout=30)
            data = r.json()
        except (requests.RequestException, ValueError) as e:
            log(f"Apps Script შეცდომა: {e}")
            continue
        if data.get("error"):
            log(f"Apps Script: {data['error']}")
            return None
        if data.get("code"):
            return str(data["code"])
    return None


# ---------------------------------------------------------------- Auth
def login() -> str:
    log("ავტორიზაცია...")
    creds = {"Username": SA_USERNAME, "Password": SA_PASSWORD}
    r = session.post(IDENTITY + "api/Accounts/GetEmailStatus", json=creds, timeout=30)
    r.raise_for_status()
    verified = r.json().get("emailIsVerified")
    code_type = 4 if verified else 1  # 4 = მეილი, 1 = SMS (თუ მეილი ვერიფიცირებული არაა)

    sent_at = time.time()
    r = session.post(IDENTITY + "api/Accounts/Authentication",
                     json={**creds, "authCodeType": code_type}, timeout=30)
    if r.status_code not in (200, 209):
        raise RuntimeError(f"Authentication ვერ მოხერხდა: {r.status_code} {r.text[:300]}")
    try:
        sess = r.json().get("session")
    except ValueError:
        sess = None
    if not sess:
        raise RuntimeError(f"session ვერ მივიღე: {r.text[:300]}")
    log("კოდი გაიგზავნა " + ("მეილზე" if code_type == 4 else "SMS-ით") + ", ველოდები...")

    code = None
    if code_type == 4 and CODE_URL and CODE_URL_KEY:
        code = script_wait_for_code(sent_at)
    if not code and code_type == 4 and GMAIL_APP_PASSWORD:
        code = gmail_wait_for_code(sent_at)
    if not code and TG_TOKEN and TG_CHAT_ID:
        code = tg_wait_for_code()
    if not code and sys.stdin.isatty():
        code = input("შეიყვანე კოდი: ").strip()
    if not code:
        raise RuntimeError("ვერიფიკაციის კოდი ვერ მივიღე")

    data = {"grant_type": "password", "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET,
            "username": SA_USERNAME, "password": SA_PASSWORD}
    r = session.post(IDENTITY + "connect/token", data=data, timeout=30,
                     headers={"code": code, "session": sess})
    if r.status_code != 200:
        raise RuntimeError(f"ტოკენი ვერ მივიღე: {r.status_code} {r.text[:300]}")
    tok = r.json()
    tok["obtained_at"] = time.time()
    save_token(tok)
    log(f"ავტორიზაცია წარმატებულია (ტოკენი ძალაშია ~{tok.get('expires_in')} წმ)")
    return tok["access_token"]


def _fernet():
    """ტოკენის ფაილი დაშიფრულია (GitHub Actions-ის cache-ში ღიად რომ არ იდოს)."""
    import base64
    import hashlib

    from cryptography.fernet import Fernet

    key = hashlib.sha256(f"{SA_PASSWORD}|{SA_USERNAME}|sa-monitor".encode()).digest()
    return Fernet(base64.urlsafe_b64encode(key))


def save_token(tok: dict) -> None:
    TOKEN_FILE.write_bytes(_fernet().encrypt(json.dumps(tok).encode()))


def get_token(force: bool = False) -> str:
    if not force and TOKEN_FILE.exists():
        try:
            tok = json.loads(_fernet().decrypt(TOKEN_FILE.read_bytes()))
            if time.time() < tok["obtained_at"] + int(tok.get("expires_in", 0)) - 120:
                return tok["access_token"]
        except Exception:  # ძველი/დაზიანებული ფაილი — თავიდან შესვლა
            pass
    return login()


def api_get(path: str, params: dict | None = None):
    for attempt in range(2):
        r = session.get(API + path, params=params, timeout=30,
                        headers={"Authorization": "Bearer " + get_token(force=attempt > 0)})
        if r.status_code == 401 and attempt == 0:
            continue
        r.raise_for_status()
        return r.json()


# ---------------------------------------------------------------- Exam logic
def _find(items: list, needle: str, prefer_keys=("code", "name")) -> dict | None:
    needle = needle.lower()
    for it in items:  # ზუსტი დამთხვევა code-ზე
        if str(it.get("code", "")).lower() == needle:
            return it
    for it in items:  # ნებისმიერ ტექსტურ ველში
        if any(needle in str(v).lower() for v in it.values() if isinstance(v, str)):
            return it
    return None


def _id(item: dict, *keys) -> str:
    for k in keys:
        for kk, v in item.items():
            if kk.lower() == k.lower() and v not in (None, ""):
                return str(v)
    raise KeyError(f"ID ველი ვერ ვიპოვე: {item}")


def _date_str(d) -> str:
    if isinstance(d, dict):
        for k in ("bookingDate", "examDate", "date", "bookingDateStatus"):
            if d.get(k):
                return str(d[k])
        return json.dumps(d, ensure_ascii=False)
    return str(d)


def _to_iso(s: str) -> str:
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        return m.group(0)
    m = re.match(r"(\d{1,2})[-./](\d{1,2})[-./](\d{4})", s)
    return f"{m.group(3)}-{int(m.group(2)):02d}-{int(m.group(1)):02d}" if m else s


def resolve_ids(verbose=False):
    cats = api_get("DrivingLicenseTheoryExams/Categories")
    if verbose:
        print("Categories:", json.dumps(cats, ensure_ascii=False, indent=1)[:3000])
    cat = _find(cats, CATEGORY) or (cats[0] if len(cats) == 1 else None)
    if not cat:
        raise RuntimeError(f"კატეგორია '{CATEGORY}' ვერ ვიპოვე")
    cat_code = _id(cat, "code", "categoryCode", "id")

    centers = api_get("DrivingLicenseTheoryExams/Centers", {"CategoryCode": cat_code})
    if verbose:
        print("Centers:", json.dumps(centers, ensure_ascii=False, indent=1)[:3000])
    ctr = _find(centers, CENTER)
    if not ctr:
        raise RuntimeError(f"ცენტრი '{CENTER}' ვერ ვიპოვე")
    ctr_id = _id(ctr, "serviceCenterId", "centerId", "id")
    ctr_name = next((v for v in ctr.values() if isinstance(v, str) and CENTER.lower() in v.lower()), CENTER)
    return cat_code, ctr_id, ctr_name


def check(cat_code, ctr_id, verbose=False) -> dict[str, list]:
    dates = api_get("DrivingLicenseTheoryExams/Dates", {"CategoryCode": cat_code, "CenterId": ctr_id}) or []
    if verbose:
        print("Dates:", json.dumps(dates, ensure_ascii=False, indent=1)[:3000])
    result = {}
    for d in dates:
        ds = _date_str(d)
        try:
            frames = api_get("DrivingLicenseTheoryExams/DateFrames",
                             {"CategoryCode": cat_code, "CenterId": ctr_id, "ExamDate": _to_iso(ds)}) or []
        except requests.RequestException:
            frames = []
        if verbose:
            print(f"DateFrames {ds}:", json.dumps(frames, ensure_ascii=False)[:1000])
        times = []
        for f in frames:
            if isinstance(f, dict):
                t = next((str(f[k]) for k in ("timeFrameName", "name", "time", "startTime") if f.get(k)), None)
                times.append(t or json.dumps(f, ensure_ascii=False))
            else:
                times.append(str(f))
        result[ds] = times
    return result


def format_slots(slots: dict[str, list], center: str) -> str:
    lines = [f"🚗 თეორიული გამოცდა — {center}: გამოჩნდა თავისუფალი ადგილი!"]
    for d, times in slots.items():
        lines.append(f"📅 {d}" + (": " + ", ".join(times) if times else ""))
    lines.append("👉 https://my.sa.gov.ge/home/Examregistration")
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--discover", action="store_true", help="ერთხელ შემოწმება და ნედლი JSON-ის ჩვენება")
    ap.add_argument("--chat-id", action="store_true", help="Telegram chat id-ის ჩვენება")
    ap.add_argument("--test-telegram", action="store_true")
    ap.add_argument("--once", action="store_true", help="ერთი შემოწმება და გასვლა (GitHub Actions-ისთვის)")
    args = ap.parse_args()

    if args.chat_id:
        if not TG_TOKEN:
            sys.exit(f"TELEGRAM_BOT_TOKEN ცარიელია ფაილში {BASE_DIR / '.env'} — ჩასვი ტოკენი და შეინახე (Ctrl+S).")
        for u in tg_updates():
            c = (u.get("message") or {}).get("chat", {})
            print(c.get("id"), c.get("first_name"), c.get("username"))
        print("თუ ცარიელია — ბოტს Telegram-ში ჯერ რამე მიწერე და თავიდან გაუშვი.")
        return
    if args.test_telegram:
        tg_send("✅ ტესტი: ბოტი მუშაობს")
        return
    if not (SA_USERNAME and SA_PASSWORD):
        sys.exit("შეავსე SA_USERNAME და SA_PASSWORD .env ფაილში")

    cat_code, ctr_id, ctr_name = resolve_ids(verbose=args.discover)
    log(f"კატეგორია={cat_code}, ცენტრი={ctr_name} (id={ctr_id})")
    if args.discover:
        print(json.dumps(check(cat_code, ctr_id, verbose=True), ensure_ascii=False, indent=1))
        return

    if not args.once:
        tg_send(f"▶️ მონიტორინგი დაიწყო: {ctr_name}, ყოველ {INTERVAL // 60} წუთში")
    while True:
        ok = run_check(cat_code, ctr_id, ctr_name)
        if args.once:
            sys.exit(0 if ok else 1)  # GitHub Actions-ში შეცდომა წითელ ✗-ად გამოჩნდება
        time.sleep(INTERVAL)


def load_state() -> dict:
    try:
        st = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"slots": {}, "errors": 0}
    return st if "slots" in st else {"slots": st, "errors": 0}


def run_check(cat_code, ctr_id, ctr_name) -> bool:
    """ერთი შემოწმება; მდგომარეობა state.json-ში ინახება, რომ გაშვებებს შორის შენარჩუნდეს."""
    st = load_state()
    prev = st["slots"]
    try:
        slots = check(cat_code, ctr_id)
        new = {d: t for d, t in slots.items() if d not in prev or set(t) - set(prev.get(d, []))}
        if new:
            log(f"ახალი ადგილები: {new}")
            tg_send(format_slots(new, ctr_name))
        else:
            log(f"ახალი ადგილი არ არის (სულ თარიღები: {len(slots)})")
        st = {"slots": slots, "errors": 0}
    except Exception as e:  # noqa: BLE001 — მონიტორი არ უნდა გაჩერდეს
        st["errors"] = st.get("errors", 0) + 1
        log(f"შეცდომა: {e}")
        if st["errors"] == 3:
            tg_send(f"⚠️ მონიტორი ზედიზედ 3-ჯერ ვერ შემოწმდა: {e}")
    STATE_FILE.write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")
    return st["errors"] == 0


if __name__ == "__main__":
    main()
