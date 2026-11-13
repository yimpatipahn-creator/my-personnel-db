import base64
import functools
import hashlib
import hmac
import logging
import os
import re
import threading
import time
from datetime import datetime, timedelta, timezone

import bcrypt
import gspread
import pandas as pd
import requests
import streamlit as st
from gspread.utils import rowcol_to_a1

# --- ตั้งค่าหน้าเว็บ ---
st.set_page_config(page_title="ระบบทะเบียนคุมเลขรหัสหนังสือ (Online)", layout="wide")

logger = logging.getLogger("registry_app")

# ==========================================
# ⚙️ ค่าคงที่ / การตั้งค่า
# ==========================================
KEY_FILE = "credentials.json"  # ใช้เฉพาะตอนรันในเครื่อง ถ้าไม่มี st.secrets
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

USER_COLS = ["Username", "Password", "Role"]
DATA_COLS = ['เลขรหัส', 'สถานะ', 'ชื่อ', 'นามสกุล', 'เงินช่วยพิเศษ', 'บำเหน็จตกทอด', 'หมายเหตุ']
VALID_ROLES = ("Admin", "Editor", "User")
ROLE_MAP = {r.lower(): r for r in VALID_ROLES}

USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]{3,32}$")
MIN_PW_LEN, MAX_PW_LEN = 8, 64
COMMON_PASSWORDS = {
    "12345678", "123456789", "1234567890", "password", "password1", "qwertyui",
    "qwerty123", "11111111", "00000000", "abc12345", "iloveyou", "admin123",
    "admin1234", "letmein1",
}

# --- ความปลอดภัยเพิ่มเติม ---
MAX_FAILS_USER = 20       # ผิดสะสมต่อ username จากทุกเครื่อง (กันเดาแบบกระจาย)
MAX_FAILS_CLIENT = 30     # ผิดสะสมต่อเครื่อง/IP ข้ามทุก username (กันไล่เดา username)
STRIKE_DECAY = 6 * 3600   # ถูกล็อกซ้ำภายในเวลานี้ → ระยะล็อกเพิ่มเป็นทวีคูณ (สูงสุด 16 เท่า)
IDLE_MINUTES = 60         # ไม่มีการใช้งานนานเท่านี้ → ออกจากระบบอัตโนมัติ
SIGNUP_MAX_PER_HOUR = 10  # จำนวนบัญชีใหม่สูงสุดต่อชั่วโมง (รวมทั้งระบบ)
FORMULA_PREFIXES = ("=", "+", "@")
CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
MAX_FAILS = 5            # ใส่รหัสผิดได้กี่ครั้งก่อนถูกล็อก
LOCK_SECONDS = 300       # ล็อกกี่วินาที
SESSION_HOURS = 8        # หมดเวลาใช้งานอัตโนมัติ

DATA_TTL = 120           # แคชข้อมูลหลัก (วินาที) — หลังบันทึก/กดรีโหลดจะล้างทันที
USERS_TTL = 60           # แคชรายชื่อผู้ใช้ (ใช้ตอน login)
HEADER_TTL = 300         # แคชหัวตาราง (แทบไม่เปลี่ยน)
ROLE_TTL = 20            # แคชผลตรวจสิทธิ์ต่อ session (งาน Admin จะตรวจสดเสมอ)

# คำนำหน้าหมวดรหัส และรูปแบบบังคับกรอก (ต้องมีตัวอักษร + จุด + ตัวเลข เช่น บ.123)
ID_PREFIX_RE = re.compile(r"^((?:[ก-ฮA-Za-z]+\.?)+)")
STRICT_ID_RE = re.compile(r"^(?:[ก-ฮA-Za-z]+\.)+[0-9]+$")


# ==========================================
# 🧰 ฟังก์ชันช่วยทั่วไป
# ==========================================
def natural_sort_key(s):
    return [int(text) if text.isdigit() else text.lower() for text in re.split(r'([0-9]+)', str(s))]


def secret(key, default=None):
    """อ่านค่าจาก st.secrets แบบไม่ error ถ้าไม่มีไฟล์ secrets"""
    try:
        return st.secrets[key] if key in st.secrets else default
    except Exception:
        return default


def normalize_role(value):
    return ROLE_MAP.get(str(value).strip().lower(), "User")


def norm_id(value):
    return str(value).strip().casefold()


def extract_prefix(value):
    m = ID_PREFIX_RE.match(str(value).strip())
    return m.group(1) if m else ""


def flash(message, icon=None):
    """เก็บข้อความไว้แสดงหลัง st.rerun()"""
    st.session_state.setdefault("_flash", []).append((message, icon))


def show_flash():
    for message, icon in st.session_state.pop("_flash", []):
        st.toast(message, icon=icon)


def show_df(d):
    d = d.fillna("").astype(str)
    try:
        st.dataframe(d, width="stretch", hide_index=True)
    except TypeError:
        st.dataframe(d, use_container_width=True, hide_index=True)


def visible(d):
    """ตัดคอลัมน์ช่วยภายใน (ขึ้นต้นด้วย _) ออกก่อนแสดงผล"""
    return d.loc[:, [c for c in d.columns if not str(c).startswith("_")]]


def clean_text(value):
    """ตัดอักขระควบคุมที่มองไม่เห็นออก แล้ว strip"""
    return CTRL_RE.sub("", str(value)).strip()


def looks_like_formula(value):
    """กัน formula injection เมื่อมีคนนำข้อมูลไป export เปิดใน Excel/Sheets"""
    return str(value).lstrip().startswith(FORMULA_PREFIXES)


def client_id():
    """ระบุเครื่องผู้ใช้จาก header ของ proxy (ต้องใช้ Streamlit >= 1.37)
    ถ้าอ่านไม่ได้จะรวมเป็น 'unknown' ซึ่งทำให้การล็อกต่อเครื่องไม่แม่นยำ"""
    try:
        headers = st.context.headers
        parts = [p.strip() for p in headers.get("X-Forwarded-For", "").split(",") if p.strip()]
        if parts:
            hops = max(1, int(secret("trusted_proxy_hops", 1)))
            return (parts[-hops] if len(parts) >= hops else parts[0])[:64]
        return (headers.get("X-Real-Ip", "") or "unknown")[:64]
    except Exception:
        return "unknown"


def fmt_wait(seconds):
    return f"{seconds} วินาที" if seconds < 90 else f"{-(-seconds // 60)} นาที"


def guard(fn, *args, **kwargs):
    """เรียกฟังก์ชันที่ต่อ Google Sheets ถ้าพังให้แสดงข้อความที่อ่านรู้เรื่องแล้วหยุดหน้า"""
    try:
        return fn(*args, **kwargs)
    except RuntimeError as e:
        logger.error("Configuration/runtime error: %s", e)
        if st.session_state.get("logged_in"):
            st.error(f"❌ {e}")
        else:
            # ยังไม่ล็อกอิน: ไม่เปิดเผยรายละเอียดการตั้งค่าระบบ
            st.error("❌ ระบบยังไม่พร้อมใช้งาน กรุณาติดต่อผู้ดูแลระบบ")
        st.stop()
    except Exception:
        logger.exception("Google Sheets operation failed")
        st.error("❌ เชื่อมต่อ Google Sheets ไม่สำเร็จ หรือใช้โควตาเต็มชั่วคราว กรุณารอสักครู่แล้วลองใหม่")
        st.stop()


# ==========================================
# 🔁 Retry สำหรับ Google Sheets API
# ==========================================
def with_retry(attempts=3, base_delay=1.0, write=False):
    retry_codes = {429} if write else {429, 500, 502, 503, 504}

    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            for i in range(attempts):
                try:
                    return fn(*args, **kwargs)
                except gspread.exceptions.APIError as e:
                    code = getattr(getattr(e, "response", None), "status_code", None)
                    if code not in retry_codes or i == attempts - 1:
                        raise
                except requests.exceptions.RequestException:
                    if write or i == attempts - 1:
                        raise
                time.sleep(base_delay * (2 ** i))
        return wrapper
    return decorator


@with_retry()
def _get_values(ws):
    return ws.get_all_values()


@with_retry()
def _row_values(ws, row):
    return ws.row_values(row)


@with_retry()
def _col_values(ws, col):
    return ws.col_values(col)


@with_retry(write=True)
def _append_row(ws, row):
    # table_range="A1" ให้ต่อท้ายตารางที่เริ่มจาก A1 เสมอ (ไม่ให้ Google เดาตารางเอง)
    # INSERT_ROWS แทรกแถวใหม่ ไม่เขียนทับข้อมูลที่อยู่ด้านล่าง
    ws.append_row(
        row,
        value_input_option="RAW",
        insert_data_option="INSERT_ROWS",
        table_range="A1",
    )


@with_retry(write=True)
def _update_cell(ws, row, col, value):
    ws.update_cell(row, col, value)


@with_retry(write=True)
def _batch_update(ws, data):
    ws.batch_update(data, value_input_option="RAW")


# ==========================================
# 🔌 เชื่อมต่อ Google Sheets
# ==========================================
@st.cache_resource(show_spinner=False)
def get_write_lock():
    return threading.Lock()


@st.cache_resource(show_spinner=False)
def get_init_lock():
    """ล็อกแยกสำหรับสร้างชีต Users (ห้ามใช้ล็อกเดียวกับ get_write_lock เพราะจะ deadlock)"""
    return threading.Lock()


@st.cache_resource(ttl=3000, show_spinner=False)
def _book():
    sheet_url = secret("sheet_url") or os.environ.get("SHEET_URL")
    if not sheet_url:
        raise RuntimeError("ยังไม่ได้ตั้งค่า sheet_url ใน st.secrets")

    info = secret("gcp_service_account")
    if info:
        client = gspread.service_account_from_dict(dict(info), scopes=SCOPES)
    elif os.path.exists(KEY_FILE):
        client = gspread.service_account(filename=KEY_FILE, scopes=SCOPES)
    else:
        raise RuntimeError("ไม่พบ gcp_service_account ใน st.secrets และไม่พบไฟล์ credentials.json")
    return client.open_by_url(sheet_url)


@st.cache_resource(ttl=3000, show_spinner=False)
def _data_ws():
    return _book().sheet1


def _wait_for_users_ws(book, tries=5):
    """รอให้อีก session/instance ที่กำลังสร้างชีต Users สร้างเสร็จ (รวมถึงเขียนหัวตาราง)"""
    for _ in range(tries):
        time.sleep(1)
        try:
            ws = book.worksheet("Users")
        except gspread.exceptions.WorksheetNotFound:
            continue
        for _ in range(tries):
            if _row_values(ws, 1):
                return ws
            time.sleep(1)
        return ws
    return None


@st.cache_resource(ttl=3000, show_spinner=False)
def _users_ws():
    book = _book()
    with get_init_lock():
        try:
            return book.worksheet("Users")
        except gspread.exceptions.WorksheetNotFound:
            pass

        initial_pw = str(secret("initial_admin_password") or "")
        if len(initial_pw) < MIN_PW_LEN:
            raise RuntimeError(
                "ยังไม่มีชีต 'Users' — กรุณาตั้งค่า initial_admin_password "
                f"(อย่างน้อย {MIN_PW_LEN} ตัวอักษร) ใน st.secrets เพื่อสร้างบัญชี admin ครั้งแรก"
            )
        try:
            ws = book.add_worksheet(title="Users", rows=100, cols=len(USER_COLS))
        except gspread.exceptions.APIError:
            # อาจมีอีก instance สร้างพร้อมกัน → ลองดึงชีตที่เพิ่งถูกสร้าง
            ws = _wait_for_users_ws(book)
            if ws is None:
                raise
            return ws
        _append_row(ws, USER_COLS)
        _append_row(ws, ["admin", hash_pw(initial_pw), "Admin"])
        return ws


@st.cache_data(ttl=HEADER_TTL, show_spinner=False)
def data_headers():
    return [h.strip() for h in _row_values(_data_ws(), 1)]


@st.cache_data(ttl=HEADER_TTL, show_spinner=False)
def users_headers():
    return [h.strip() for h in _row_values(_users_ws(), 1)]


# --- Audit log: บันทึกเหตุการณ์สำคัญลงชีต AuditLog ---
AUDIT_COLS = ["เวลา", "ผู้ใช้", "การกระทำ", "รายละเอียด"]
TZ_TH = timezone(timedelta(hours=7))


@st.cache_resource(ttl=3000, show_spinner=False)
def _audit_ws():
    book = _book()
    with get_init_lock():
        try:
            return book.worksheet("AuditLog")
        except gspread.exceptions.WorksheetNotFound:
            pass
        try:
            ws = book.add_worksheet(title="AuditLog", rows=1000, cols=len(AUDIT_COLS))
        except gspread.exceptions.APIError:
            time.sleep(1)  # อาจมี instance อื่นสร้างพร้อมกัน
            return book.worksheet("AuditLog")
        _append_row(ws, AUDIT_COLS)
        return ws


def audit(action, detail="", user=None):
    """บันทึกเหตุการณ์ (best-effort: ถ้าเขียนไม่ได้จะไม่ทำให้งานหลักล้ม) — ห้ามใส่รหัสผ่านใน detail"""
    who = user if user is not None else st.session_state.get("username", "")
    logger.info("AUDIT user=%s action=%s detail=%s", who, action, detail)
    try:
        ts = datetime.now(TZ_TH).strftime("%Y-%m-%d %H:%M:%S")
        _append_row(_audit_ws(), [ts, who, action, str(detail)[:300]])
    except Exception:
        logger.exception("audit write failed")


def values_to_df(values, required):
    if not values:
        return pd.DataFrame(columns=list(required))
    headers = [str(h).strip() for h in values[0]]
    n = len(headers)
    rows = [(list(r) + [""] * n)[:n] for r in values[1:]]
    df = pd.DataFrame(rows, columns=headers).astype(str)
    keep = [c != "" and "Unnamed" not in c for c in df.columns]
    return df.loc[:, keep]


# ==========================================
# 🔐 รหัสผ่าน (bcrypt) และกันเดารหัส
# ==========================================
def _prep(pw: str) -> bytes:
    """SHA-256 + base64 → 44 ไบต์เสมอ ไม่เกินลิมิต 72 ไบต์ของ bcrypt (รหัสผ่านไทยไม่ถูกตัดเงียบ ๆ)"""
    return base64.b64encode(hashlib.sha256(pw.encode("utf-8")).digest())


def hash_pw(pw: str) -> str:
    return bcrypt.hashpw(_prep(pw), bcrypt.gensalt()).decode()


def is_hashed(stored: str) -> bool:
    return str(stored).startswith(("$2a$", "$2b$", "$2y$"))


def verify_pw(pw: str, stored: str):
    """คืน (ถูกต้องไหม, ควรอัปเกรด hash ไหม)"""
    stored = (stored or "").strip()
    if not stored or not pw:
        return False, False
    if is_hashed(stored):
        try:
            if bcrypt.checkpw(_prep(pw), stored.encode()):
                return True, False
            # hash แบบเก่า (bcrypt ตรง ๆ ตัดที่ 72 ไบต์) → ผ่านแล้วให้อัปเกรด
            if bcrypt.checkpw(pw.encode("utf-8")[:72], stored.encode()):
                return True, True
        except ValueError:
            pass
        return False, False
    return hmac.compare_digest(pw.encode("utf-8"), stored.encode("utf-8")), True


@st.cache_resource(show_spinner=False)
def _dummy_hash():
    return hash_pw("dummy-password-for-timing")


@st.cache_resource(show_spinner=False)
def _limiter():
    return {"lock": threading.Lock(), "recs": {}}


def _prune(recs, now):
    for k in [k for k, v in recs.items() if v["until"] < now and now - v["last"] > STRIKE_DECAY]:
        del recs[k]
    if len(recs) > 5000:  # กันหน่วยความจำบวม: ทิ้งรายการเก่าสุดครึ่งหนึ่ง
        for k, _ in sorted(recs.items(), key=lambda kv: kv[1]["last"])[: len(recs) // 2]:
            del recs[k]


def limit_remaining(keys):
    """จำนวนวินาทีที่ยังถูกล็อก (0 = ไม่ถูกล็อก) — ดูทุก key แล้วเอาค่าสูงสุด"""
    s = _limiter()
    now = time.time()
    with s["lock"]:
        waits = [
            int(s["recs"][k]["until"] - now) + 1
            for k in keys
            if k in s["recs"] and s["recs"][k]["until"] > now
        ]
    return max([0] + waits)


def limit_hit(specs):
    """บันทึกความผิดพลาด specs = [(key, max_fails, window_sec, lock_sec), ...]
    ผิดครบ max_fails ภายใน window → ล็อก lock_sec (ล็อกซ้ำเร็ว ๆ จะนานขึ้นเป็นทวีคูณ สูงสุด 16 เท่า)"""
    s = _limiter()
    now = time.time()
    with s["lock"]:
        recs = s["recs"]
        if len(recs) > 2000:
            _prune(recs, now)
        for key, max_fails, window, lock_sec in specs:
            rec = recs.setdefault(key, {"count": 0, "first": now, "until": 0.0, "last": now, "strikes": 0})
            if now - rec["last"] > STRIKE_DECAY:
                rec["strikes"] = 0
            if rec["count"] == 0 or now - rec["first"] > window:
                rec["count"], rec["first"] = 0, now
            rec["last"] = now
            rec["count"] += 1
            if rec["count"] >= max_fails:
                rec["until"] = now + lock_sec * (2 ** min(rec["strikes"], 4))
                rec["strikes"] += 1
                rec["count"] = 0
                logger.warning("Rate limit triggered: key=%s lock=%ss", key, int(rec["until"] - now))


def limit_clear(keys):
    s = _limiter()
    with s["lock"]:
        for k in keys:
            s["recs"].pop(k, None)


def _login_keys(username):
    u, c = username.strip().lower()[:64], client_id()
    # (ชื่อนี้จากเครื่องนี้, ชื่อนี้จากทุกเครื่อง, เครื่องนี้กับทุกชื่อ)
    return f"login|{u}|{c}", f"login|{u}", f"login|*|{c}"


def lock_remaining(username):
    return limit_remaining(_login_keys(username))


def register_failure(username):
    k_user_client, k_user, k_client = _login_keys(username)
    limit_hit([
        (k_user_client, MAX_FAILS, LOCK_SECONDS, LOCK_SECONDS),
        (k_user, MAX_FAILS_USER, 3600, LOCK_SECONDS),
        (k_client, MAX_FAILS_CLIENT, 600, LOCK_SECONDS),
    ])


def clear_failures(username):
    limit_clear(_login_keys(username)[:1])


# ==========================================
# 👥 ระบบจัดการผู้ใช้งาน (Users System)
# ==========================================
def read_users_fresh():
    df = values_to_df(_get_values(_users_ws()), USER_COLS)
    missing = [c for c in USER_COLS if c not in df.columns]
    if missing:
        raise RuntimeError(f"ชีต Users ขาดคอลัมน์: {', '.join(missing)}")
    df = df.copy()
    df["_row"] = range(2, len(df) + 2)
    df["Username"] = df["Username"].str.strip()
    df["Password"] = df["Password"].str.strip()
    df["Role"] = df["Role"].map(normalize_role)
    df = df[df["Username"] != ""].reset_index(drop=True)
    return df[USER_COLS + ["_row"]]


@st.cache_data(ttl=USERS_TTL, show_spinner=False)
def users_df_cached():
    return read_users_fresh()


def clear_user_cache():
    users_df_cached.clear()


def find_user(df, username):
    m = df[df["Username"].str.lower() == username.strip().lower()]
    return m.iloc[0] if not m.empty else None


def fresh_role(username):
    row = find_user(read_users_fresh(), username)
    return None if row is None else row["Role"]


def require_role(*roles, fresh=False):
    """ตรวจสิทธิ์ — ถ้าเพิ่งตรวจผ่านภายใน ROLE_TTL วินาทีจะใช้ผลเดิม (ลดการเรียก API)
    งานที่กระทบสิทธิ์/รหัสผ่านผู้ใช้ให้ส่ง fresh=True เพื่ออ่านสดทุกครั้ง"""
    user = st.session_state.username
    cached = st.session_state.get("_role_cache")
    if (
        not fresh
        and cached
        and cached[0] == user
        and time.time() - cached[2] < ROLE_TTL
        and cached[1] in roles
    ):
        return True

    role = fresh_role(user)
    st.session_state["_role_cache"] = (user, role, time.time())
    if role is None or role not in roles:
        if role is None:
            logout()
            flash("บัญชีนี้ไม่มีอยู่ในระบบแล้ว", "⚠️")
        return False
    st.session_state.role = role
    return True


def validate_new_password(pw, username=""):
    if len(pw) < MIN_PW_LEN:
        return f"รหัสผ่านต้องมีอย่างน้อย {MIN_PW_LEN} ตัวอักษร"
    if len(pw) > MAX_PW_LEN:
        return f"รหัสผ่านต้องไม่เกิน {MAX_PW_LEN} ตัวอักษร"
    low = pw.lower()
    if low in COMMON_PASSWORDS or len(set(pw)) == 1:
        return "รหัสผ่านเดาง่ายเกินไป กรุณาเลือกรหัสอื่น"
    if username and low == username.strip().lower():
        return "รหัสผ่านต้องไม่เหมือนชื่อผู้ใช้"
    return None


def signup_enabled():
    """เปิดให้สมัครเองได้ก็ต่อเมื่อมี signup_code หรือตั้ง allow_open_signup = true อย่างตั้งใจ"""
    return bool(secret("signup_code")) or str(secret("allow_open_signup", "")).lower() in ("1", "true", "yes")


def sign_up(username, password, invite_code=""):
    if not signup_enabled():
        return False, "ระบบปิดการลงทะเบียนด้วยตนเอง กรุณาติดต่อผู้ดูแลระบบ"

    uname = username.strip().lower()
    invite_keys = [f"invite|{client_id()}", "invite|*"]
    wait = limit_remaining(invite_keys + ["signup|*"])
    if wait:
        return False, f"มีการลงทะเบียน/ลองรหัสเชิญถี่เกินไป กรุณารอ {fmt_wait(wait)}"

    required_code = secret("signup_code")
    if required_code and not hmac.compare_digest(invite_code.strip().encode(), str(required_code).encode()):
        limit_hit([
            (invite_keys[0], MAX_FAILS, 600, LOCK_SECONDS),
            (invite_keys[1], MAX_FAILS_CLIENT, 600, LOCK_SECONDS),
        ])
        return False, "รหัสเชิญไม่ถูกต้อง"
    if not USERNAME_RE.match(uname):
        return False, "ชื่อผู้ใช้ต้องเป็น a-z, 0-9, _ . - ความยาว 3–32 ตัวอักษร"
    err = validate_new_password(password, uname)
    if err:
        return False, err

    with get_write_lock():
        df = read_users_fresh()
        if find_user(df, uname) is not None:
            return False, "ชื่อผู้ใช้งานนี้มีคนใช้แล้ว กรุณาเลือกชื่ออื่น"
        _append_row(_users_ws(), [uname, hash_pw(password), "User"])
    clear_user_cache()
    limit_hit([("signup|*", SIGNUP_MAX_PER_HOUR, 3600, 3600)])
    audit("signup", uname, user=uname)
    return True, f"ลงทะเบียนสำเร็จ! คุณ {uname} ได้รับสิทธิ์ 'User'"


def _write_password(username, pw_plain):
    with get_write_lock():
        df = read_users_fresh()
        row = find_user(df, username)
        if row is None:
            return False
        _update_cell(
            _users_ws(), int(row["_row"]), users_headers().index("Password") + 1, hash_pw(pw_plain)
        )
    clear_user_cache()
    return True


def set_password_as_admin(target, new_pw):
    if not require_role("Admin", fresh=True):
        return False, "ไม่มีสิทธิ์ดำเนินการ"
    err = validate_new_password(new_pw, target)
    if err:
        return False, err
    if not _write_password(target, new_pw):
        return False, "ไม่พบผู้ใช้นี้"
    revoke_sessions(target)
    audit("reset_password", target)
    return True, f"รีเซ็ตรหัสผ่านของ {target} แล้ว"


def change_own_password(old_pw, new_pw):
    user = st.session_state.username
    key = f"chpw|{user.lower()}"
    wait = limit_remaining([key])
    if wait:
        return False, f"ใส่รหัสเดิมผิดหลายครั้ง กรุณารอ {fmt_wait(wait)}"

    row = find_user(read_users_fresh(), user)
    if row is None or not verify_pw(old_pw, row["Password"])[0]:
        limit_hit([(key, MAX_FAILS, 600, LOCK_SECONDS)])
        return False, "รหัสผ่านเดิมไม่ถูกต้อง"
    err = validate_new_password(new_pw, user)
    if err:
        return False, err
    if not _write_password(user, new_pw):
        return False, "ไม่พบผู้ใช้นี้"
    limit_clear([key])
    revoke_sessions(user)
    audit("change_password", user)
    return True, "เปลี่ยนรหัสผ่านเรียบร้อย (เครื่องอื่นที่ล็อกอินค้างอยู่จะถูกออกจากระบบ)"


def update_user_role(target, new_role):
    if not require_role("Admin", fresh=True):
        return False, "ไม่มีสิทธิ์ดำเนินการ"
    if new_role not in VALID_ROLES:
        return False, "สิทธิ์ไม่ถูกต้อง"
    with get_write_lock():
        df = read_users_fresh()
        row = find_user(df, target)
        if row is None:
            return False, "ไม่พบผู้ใช้นี้"
        admin_count = int((df["Role"] == "Admin").sum())
        if row["Role"] == "Admin" and new_role != "Admin" and admin_count <= 1:
            return False, "ต้องมีผู้ดูแลระบบ (Admin) อย่างน้อย 1 คนเสมอ"
        _update_cell(
            _users_ws(), int(row["_row"]), users_headers().index("Role") + 1, new_role
        )
    clear_user_cache()
    if target.strip().lower() == st.session_state.username.lower():
        st.session_state.role = new_role
        st.session_state.pop("_role_cache", None)
    audit("change_role", f"{target} -> {new_role}")
    return True, "บันทึกสิทธิ์เรียบร้อย!"


def migrate_plaintext_passwords():
    if not require_role("Admin", fresh=True):
        return 0
    with get_write_lock():
        df = read_users_fresh()
        targets = df[(df["Password"] != "") & (~df["Password"].map(is_hashed))]
        if targets.empty:
            return 0
        pw_col = users_headers().index("Password") + 1
        data = [
            {"range": rowcol_to_a1(int(r["_row"]), pw_col), "values": [[hash_pw(r["Password"])]]}
            for _, r in targets.iterrows()
        ]
        _batch_update(_users_ws(), data)
    clear_user_cache()
    audit("migrate_passwords", f"{len(targets)} accounts")
    return len(targets)


# ==========================================
# 🎨 ระบบจัดการธีมสี (Theme Manager)
# ==========================================
DEFAULT_THEME = "🌿 เขียว-ฟ้า (Green)"
THEMES = {
    "🌿 เขียว-ฟ้า (Green)": {
        "bg": "#F4F9F4", "sidebar": "#E8F5E9", "sidebar_border": "#A5D6A7",
        "h1": "#1B5E20", "h1_shadow": "#C8E6C9", "h2": "#2E7D32",
        "tab_active": "#1B5E20",
        "btn_grad": "linear-gradient(to right, #43A047, #1E88E5)", "btn_shadow": "rgba(30, 136, 229, 0.3)",
        "input_border": "#81C784", "table_border": "#A5D6A7"
    },
    "🌸 ชมพู-ฟ้า (Pink)": {
        "bg": "#FDF2F4", "sidebar": "#FCE4EC", "sidebar_border": "#F48FB1",
        "h1": "#AD1457", "h1_shadow": "#F8BBD0", "h2": "#C2185B",
        "tab_active": "#AD1457",
        "btn_grad": "linear-gradient(to right, #D81B60, #8E24AA)", "btn_shadow": "rgba(216, 27, 96, 0.3)",
        "input_border": "#F48FB1", "table_border": "#F8BBD0"
    },
    "🍊 ส้ม-ครีม (Orange)": {
        "bg": "#FFF8F0", "sidebar": "#FFF3E0", "sidebar_border": "#FFCC80",
        "h1": "#BF360C", "h1_shadow": "#FFE0B2", "h2": "#D84315",
        "tab_active": "#BF360C",
        "btn_grad": "linear-gradient(to right, #FB8C00, #F4511E)", "btn_shadow": "rgba(244, 81, 30, 0.3)",
        "input_border": "#FFB74D", "table_border": "#FFE0B2"
    },
    "🏢 เทา-น้ำเงิน (Professional)": {
        "bg": "#F8FAFC", "sidebar": "#EDF2F7", "sidebar_border": "#CBD5E1",
        "h1": "#0F172A", "h1_shadow": "#E2E8F0", "h2": "#1E293B",
        "tab_active": "#0F172A",
        "btn_grad": "linear-gradient(to right, #334155, #475569)", "btn_shadow": "rgba(51, 65, 85, 0.3)",
        "input_border": "#94A3B8", "table_border": "#CBD5E1"
    },
}


def apply_theme(theme_name):
    c = THEMES.get(theme_name, THEMES[DEFAULT_THEME])

    st.markdown(f"""
    <style>
    .stApp {{
        background-color: {c['bg']} !important;
        color: #111827 !important;
    }}
    [data-testid="stSidebar"] {{
        background-color: {c['sidebar']} !important;
        border-right: 2px solid {c['sidebar_border']};
    }}
    [data-testid="stSidebar"] p,
    [data-testid="stSidebar"] span,
    [data-testid="stSidebar"] label,
    [data-testid="stSidebar"] h1,
    [data-testid="stSidebar"] h2,
    [data-testid="stSidebar"] h3 {{
        color: #111827 !important;
    }}
    [data-testid="stExpander"] details summary,
    [data-testid="stExpander"] details summary * {{
        color: #111827 !important;
        font-weight: 600 !important;
    }}
    div[data-baseweb="tab-list"] button {{
        background: transparent !important;
    }}
    div[data-baseweb="tab-list"] button * {{
        color: #374151 !important;
        -webkit-text-fill-color: #374151 !important;
        font-size: 16px !important;
        font-weight: 600 !important;
    }}
    div[data-baseweb="tab-list"] button[aria-selected="true"] * {{
        color: {c['tab_active']} !important;
        -webkit-text-fill-color: {c['tab_active']} !important;
        font-weight: 800 !important;
    }}
    div[data-baseweb="tab-highlight"] {{
        background-color: {c['tab_active']} !important;
        height: 3px !important;
    }}
    div[data-baseweb="select"] > div {{
        background-color: #FFFFFF !important;
        border-radius: 10px !important;
        border: 1.5px solid {c['input_border']} !important;
    }}
    div[data-baseweb="select"] * {{
        color: #111827 !important;
        -webkit-text-fill-color: #111827 !important;
    }}
    ul[data-baseweb="menu"] {{
        background-color: #FFFFFF !important;
    }}
    ul[data-baseweb="menu"] li * {{
        color: #111827 !important;
    }}
    h1 {{
        color: {c['h1']} !important;
        text-shadow: 1px 1px 2px {c['h1_shadow']};
        font-family: 'Sarabun', sans-serif;
    }}
    h2, h3, h4, h5, h6 {{
        color: {c['h2']} !important;
        font-weight: 700 !important;
    }}
    p, span, label, [data-testid="stWidgetLabel"] p {{
        color: #111827 !important;
        -webkit-text-fill-color: #111827 !important;
        font-weight: 500 !important;
    }}
    .stButton>button, [data-testid="stFormSubmitButton"]>button {{
        background: {c['btn_grad']} !important;
        border-radius: 20px !important;
        border: none !important;
        padding: 8px 24px !important;
        box-shadow: 0 4px 10px {c['btn_shadow']} !important;
    }}
    .stButton>button *, [data-testid="stFormSubmitButton"]>button * {{
        color: #FFFFFF !important;
        -webkit-text-fill-color: #FFFFFF !important;
        font-weight: bold !important;
    }}
    .stTextInput input, .stTextArea textarea {{
        background-color: #FFFFFF !important;
        color: #000000 !important;
        -webkit-text-fill-color: #000000 !important;
        border: 1.5px solid {c['input_border']} !important;
        border-radius: 10px !important;
    }}
    [data-testid="stAlert"] * {{
        color: #0F172A !important;
        -webkit-text-fill-color: #0F172A !important;
    }}
    </style>
    """, unsafe_allow_html=True)


# ==========================================
# 🔑 Login / Session
# ==========================================
SESSION_DEFAULTS = {"logged_in": False, "username": "", "role": "", "login_at": 0.0, "last_seen": 0.0}


def init_session():
    for k, v in SESSION_DEFAULTS.items():
        st.session_state.setdefault(k, v)


def logout():
    for k, v in SESSION_DEFAULTS.items():
        st.session_state[k] = v
    st.session_state.pop("_role_cache", None)


def render_login():
    apply_theme(DEFAULT_THEME)
    show_flash()
    _, col2, _ = st.columns([1, 2, 1])
    with col2:
        st.title("🔐 เข้าสู่ระบบ / ลงทะเบียน")
        require_code = bool(secret("signup_code"))
        can_signup = signup_enabled()

        with st.form("login_form", clear_on_submit=False):
            username = st.text_input("ชื่อผู้ใช้งาน (Username)", max_chars=64).strip()
            password = st.text_input("รหัสผ่าน (Password)", type="password", max_chars=MAX_PW_LEN * 2)
            invite = ""
            if require_code:
                invite = st.text_input("รหัสเชิญ (เฉพาะตอนลงทะเบียน)", type="password")

            c_login, c_signup = st.columns(2)
            with c_login:
                submit_login = st.form_submit_button("เข้าสู่ระบบ (Login)", type="primary")
            submit_signup = False
            with c_signup:
                if can_signup:
                    submit_signup = st.form_submit_button("ลงทะเบียน (Sign Up)")

        if not can_signup:
            st.caption("ต้องการบัญชีใหม่? กรุณาติดต่อผู้ดูแลระบบ")

        if submit_login:
            handle_login(username, password)
        elif submit_signup:
            if username and password:
                ok, msg = guard(sign_up, username, password, invite)
                (st.success if ok else st.error)(("✅ " if ok else "❌ ") + msg)
            else:
                st.error("กรุณากรอก Username และ Password ก่อนลงทะเบียน")


def handle_login(username, password):
    remaining = lock_remaining(username)
    if remaining > 0:
        st.error(f"❌ ใส่รหัสผิดหลายครั้งเกินไป กรุณารอ {remaining} วินาทีแล้วลองใหม่")
        return
    if not username or not password:
        st.error("กรุณากรอก Username และ Password")
        return

    users_df = guard(users_df_cached)
    row = find_user(users_df, username)
    if row is None:
        verify_pw(password, _dummy_hash())
        ok, upgrade = False, False
    else:
        ok, upgrade = verify_pw(password, row["Password"])

    if not ok:
        register_failure(username)
        st.error("❌ ชื่อผู้ใช้หรือรหัสผ่านไม่ถูกต้อง")
        return

    clear_failures(username)
    if upgrade:
        # รหัสผ่านเป็นข้อความธรรมดา หรือ hash แบบเก่า → อัปเกรดเป็นแบบใหม่
        try:
            _write_password(row["Username"], password)
        except Exception:
            logger.exception("Could not upgrade password hash")

    st.session_state.logged_in = True
    st.session_state.username = row["Username"]
    st.session_state.role = row["Role"]
    st.session_state.login_at = time.time()
    st.session_state["_role_cache"] = (row["Username"], row["Role"], time.time())
    st.session_state.last_seen = time.time()
    audit("login", user=row["Username"])
    flash(f"ยินดีต้อนรับคุณ {row['Username']}", "🎉")
    st.rerun()


@st.cache_resource(show_spinner=False)
def _revoked():
    return {}


def revoke_sessions(username):
    """ทำให้ session เดิมของผู้ใช้นี้ (ทุกเครื่อง) หมดอายุ — session ปัจจุบันของผู้ที่กดเองจะถูกต่ออายุ
    (เก็บในหน่วยความจำของ process: รีสตาร์ทแอปแล้วรายการนี้จะหายไป)"""
    _revoked()[username.strip().lower()] = time.time()
    if username.strip().lower() == st.session_state.get("username", "").strip().lower():
        st.session_state.login_at = time.time()


def sync_session_user():
    """ตรวจกับรายชื่อผู้ใช้ล่าสุด (แคช 60 วินาที): บัญชีถูกลบ → ออกจากระบบ, สิทธิ์เปลี่ยน → อัปเดตทันที"""
    try:
        row = find_user(users_df_cached(), st.session_state.username)
    except Exception:
        logger.exception("sync_session_user failed")
        return  # Sheets มีปัญหา: คง session เดิมไว้ (การเขียนข้อมูลยังตรวจสิทธิ์เสมอ)
    if row is None:
        logout()
        flash("บัญชีนี้ไม่มีอยู่ในระบบแล้ว", "⚠️")
        st.rerun()
    if row["Role"] != st.session_state.role:
        st.session_state.role = row["Role"]
        st.session_state.pop("_role_cache", None)


def check_login():
    init_session()
    ss = st.session_state
    if ss.logged_in:
        revoked_at = _revoked().get(ss.username.strip().lower(), 0.0)
        if ss.login_at < revoked_at:
            logout()
            flash("รหัสผ่านของบัญชีนี้ถูกเปลี่ยน กรุณาเข้าสู่ระบบใหม่", "🔒")
        elif ss.last_seen and time.time() - ss.last_seen > IDLE_MINUTES * 60:
            logout()
            flash("ไม่มีการใช้งานนาน ระบบออกจากระบบให้อัตโนมัติ", "⏱️")
    if st.session_state.logged_in and time.time() - st.session_state.login_at > SESSION_HOURS * 3600:
        logout()
        flash("หมดเวลาการใช้งาน กรุณาเข้าสู่ระบบอีกครั้ง", "⏱️")
    if not st.session_state.logged_in:
        render_login()
        st.stop()
    st.session_state.last_seen = time.time()
    sync_session_user()


# ==========================================
# 🛠️ ฟังก์ชันจัดการข้อมูล (Data)
# ==========================================
def _load_data_raw():
    df = values_to_df(_get_values(_data_ws()), DATA_COLS)
    missing = [c for c in DATA_COLS if c not in df.columns]
    if missing:
        raise RuntimeError(f"ชีตข้อมูลหลักขาดคอลัมน์: {', '.join(missing)}")
    for col in df.columns:
        df[col] = df[col].str.strip()
    df = df[(df['เลขรหัส'] != 'เลขรหัส') & (df['เลขรหัส'] != '')]
    return df.reset_index(drop=True)


@st.cache_data(ttl=DATA_TTL, show_spinner=False)
def load_data_cached():
    """โหลดข้อมูล + คำนวณคอลัมน์ช่วย (_prefix, _norm) + เรียงตามเลขรหัสแบบ natural ครั้งเดียวต่อรอบแคช"""
    df = _load_data_raw()
    keys = [natural_sort_key(v) for v in df['เลขรหัส']]
    order = sorted(range(len(keys)), key=keys.__getitem__)
    df = df.iloc[order].reset_index(drop=True)
    df["_prefix"] = df['เลขรหัส'].map(extract_prefix)
    df["_norm"] = df['เลขรหัส'].map(norm_id)
    return df


@st.cache_resource(show_spinner=False)
def _last_good():
    return {}


def load_data_safe():
    """โหลดข้อมูล ถ้า Google Sheets ล่ม/โควตาเต็ม ให้ใช้ชุดล่าสุดที่โหลดสำเร็จแทน"""
    try:
        df = load_data_cached()
        _last_good()["df"] = df
        return df
    except Exception:
        logger.exception("load_data failed")
        stale = _last_good().get("df")
        if stale is not None:
            st.warning("⚠️ เชื่อมต่อ Google Sheets ไม่ได้ชั่วคราว กำลังแสดงข้อมูลล่าสุดที่โหลดไว้ (การบันทึกยังตรวจรหัสซ้ำกับข้อมูลสดเสมอ)")
            return stale
        raise


def clear_all_caches():
    load_data_cached.clear()
    users_df_cached.clear()
    data_headers.clear()
    users_headers.clear()


def append_record(row):
    """บันทึกแถวใหม่: ตรวจสิทธิ์ + ตรวจรหัสซ้ำกับข้อมูลสด + เรียงคอลัมน์ตามหัวตาราง"""
    if not require_role("Admin", "Editor"):
        return False, "คุณไม่มีสิทธิ์บันทึกข้อมูล"

    row = {k: clean_text(v) for k, v in row.items()}
    bad = [k for k, v in row.items() if looks_like_formula(v)]
    if bad:
        return False, f"ห้ามขึ้นต้นข้อมูลด้วยเครื่องหมาย = + หรือ @ (ช่อง: {', '.join(bad)})"

    with get_write_lock():
        ws = _data_ws()
        headers = data_headers()
        missing = [c for c in DATA_COLS if c not in headers]
        if missing:
            data_headers.clear()  # หัวตารางอาจเปลี่ยน ให้อ่านใหม่รอบหน้า
            return False, f"ชีตข้อมูลหลักขาดคอลัมน์: {', '.join(missing)}"

        existing = {norm_id(v) for v in _col_values(ws, headers.index('เลขรหัส') + 1)}
        if norm_id(row['เลขรหัส']) in existing:
            return False, "รหัสนี้เพิ่งถูกใช้งานแล้ว (อาจมีผู้อื่นบันทึกไปก่อน)"

        _append_row(ws, [row.get(h, "") for h in headers])
    load_data_cached.clear()
    audit("add_record", row['เลขรหัส'])
    return True, "บันทึกข้อมูลสำเร็จ!"


def get_last_id_in_category(df, prefix):
    """df เรียงตามเลขรหัสไว้แล้ว และมีคอลัมน์ _prefix → แถวสุดท้ายของหมวดคือเลขล่าสุด"""
    if df.empty or not prefix:
        return "ยังไม่มีข้อมูล"
    ids = df.loc[df["_prefix"] == prefix, 'เลขรหัส']
    return ids.iloc[-1] if not ids.empty else "ยังไม่มีข้อมูล"


# ==========================================
# 🖥️ หน้าจอหลัก
# ==========================================
check_login()
show_flash()

with st.sidebar:
    st.write(f"สวัสดี, **{st.session_state.username}**")
    st.divider()
    st.selectbox("🎨 เลือกธีมสี", list(THEMES.keys()), key="theme")
    apply_theme(st.session_state.theme)

    with st.expander("🔑 เปลี่ยนรหัสผ่านของฉัน"):
        with st.form("change_pw_form", clear_on_submit=True):
            old_pw = st.text_input("รหัสผ่านเดิม", type="password")
            new_pw1 = st.text_input("รหัสผ่านใหม่", type="password")
            new_pw2 = st.text_input("ยืนยันรหัสผ่านใหม่", type="password")
            if st.form_submit_button("เปลี่ยนรหัสผ่าน"):
                if new_pw1 != new_pw2:
                    st.error("❌ รหัสผ่านใหม่ไม่ตรงกัน")
                else:
                    ok, msg = guard(change_own_password, old_pw, new_pw1)
                    (st.success if ok else st.error)(("✅ " if ok else "❌ ") + msg)

    if st.session_state.role == "Admin":
        st.divider()
        st.subheader("👑 ผู้ดูแลระบบ")
        with st.expander("จัดการผู้ใช้งาน"):
            users_df = guard(users_df_cached)
            show_df(users_df[['Username', 'Role']])

            legacy_count = int(((users_df["Password"] != "") & (~users_df["Password"].map(is_hashed))).sum())
            if legacy_count:
                st.warning(f"มี {legacy_count} บัญชีที่รหัสผ่านยังเป็นข้อความธรรมดาในชีต")
                if st.button("🔒 แปลงรหัสผ่านเก่าเป็น hash"):
                    n = guard(migrate_plaintext_passwords)
                    flash(f"แปลงรหัสผ่านแล้ว {n} บัญชี", "🔒")
                    st.rerun()

            user_list = users_df['Username'].tolist()

            st.write("--- แก้ไขสิทธิ์ ---")
            with st.form("edit_user_form"):
                user_to_edit = st.selectbox("เลือก Username", user_list)
                new_role = st.selectbox("เปลี่ยนสิทธิ์", list(VALID_ROLES), index=len(VALID_ROLES) - 1)
                if st.form_submit_button("บันทึกสิทธิ์") and user_to_edit:
                    ok, msg = guard(update_user_role, user_to_edit, new_role)
                    if ok:
                        flash("✅ " + msg, "🔧")
                        st.rerun()
                    else:
                        st.error("❌ " + msg)

            st.write("--- รีเซ็ตรหัสผ่านผู้ใช้ ---")
            with st.form("reset_pw_form", clear_on_submit=True):
                user_to_reset = st.selectbox("เลือก Username", user_list, key="reset_user")
                reset_pw = st.text_input("รหัสผ่านใหม่", type="password", key="reset_pw")
                if st.form_submit_button("รีเซ็ตรหัสผ่าน") and user_to_reset:
                    ok, msg = guard(set_password_as_admin, user_to_reset, reset_pw)
                    (st.success if ok else st.error)(("✅ " if ok else "❌ ") + msg)

    st.divider()
    if st.button("🔄 รีโหลดข้อมูล"):
        clear_all_caches()
        st.rerun()
    if st.button("🚪 ออกจากระบบ"):
        logout()
        st.rerun()

st.title("📂 ระบบทะเบียนคุมเลขรหัสหนังสือ")
st.caption(f"ผู้ใช้งาน: {st.session_state.username} | สิทธิ์: {st.session_state.role}")

with st.spinner("กำลังโหลดข้อมูล..."):
    df = guard(load_data_safe)

# กำหนดแท็บตามสิทธิ์การใช้งาน
if st.session_state.role == "User":
    tab1, tab3 = st.tabs(["🔍 ค้นหาข้อมูล", "💾 ดูตารางรวม"])
    tab2 = None
else:
    tab1, tab2, tab3 = st.tabs(["🔍 ค้นหาข้อมูล", "📝 บันทึกข้อมูลใหม่", "💾 ดูตารางรวม"])

# --- TAB 1: ค้นหาข้อมูล ---
with tab1:
    st.subheader("🔍 ค้นหาบุคลากร")
    c1, c2, c3 = st.columns(3)
    with c1:
        search_id = st.text_input("เลขรหัส", placeholder="เช่น ก.1").strip()
    with c2:
        search_name = st.text_input("ชื่อ", placeholder="พิมพ์ชื่อ...").strip()
    with c3:
        search_surname = st.text_input("นามสกุล", placeholder="พิมพ์นามสกุล...").strip()

    if search_id or search_name or search_surname:
        results = df
        if search_id:
            results = results[results['เลขรหัส'].str.contains(search_id, na=False, case=False, regex=False)]
        if search_name:
            results = results[results['ชื่อ'].str.contains(search_name, na=False, case=False, regex=False)]
        if search_surname:
            results = results[results['นามสกุล'].str.contains(search_surname, na=False, case=False, regex=False)]

        st.success(f"พบ {len(results)} รายการ")
        show_df(visible(results))
    else:
        st.info("พิมพ์รหัส, ชื่อ หรือนามสกุล เพื่อค้นหาข้อมูล")

# --- TAB 2: บันทึกข้อมูลใหม่ ---
if tab2:
    with tab2:
        st.subheader("📝 เพิ่มข้อมูลใหม่")

        # เคลียร์ฟอร์มหลังบันทึกสำเร็จ
        if st.session_state.pop("_reset_form", False):
            st.session_state.update({
                "temp_id": "", "f_name": "", "f_surname": "", "f_status": "",
                "f_special": "", "f_pension": "", "f_note": "",
            })

        col_id_input, col_id_hint = st.columns([1, 2])
        with col_id_input:
            new_id_input = st.text_input(
                "1. เลขรหัส (จำเป็น) *",
                key="temp_id",
                max_chars=50,
                placeholder="เช่น บ.123"
            ).strip()

        is_id_invalid = False
        with col_id_hint:
            if new_id_input:
                # 1. ดึงหมวดตัวอักษร (เช่น พิมพ์ "บ" หรือ "บ." หรือ "บ.12") และแสดงเลขล่าสุดทันที
                prefix = extract_prefix(new_id_input)
                if prefix:
                    search_prefix = prefix if prefix.endswith(".") else prefix + "."
                    last = get_last_id_in_category(df, search_prefix)
                    st.info(f"📌 หมวด '{search_prefix}' ล่าสุดคือ: **{last}**")

                # 2. ตรวจสอบรหัสซ้ำ (เทียบกับคอลัมน์ที่ normalize ไว้แล้ว)
                if df["_norm"].eq(norm_id(new_id_input)).any():
                    st.error("❌ รหัสนี้ถูกใช้งานแล้ว!")
                    is_id_invalid = True
                # 3. ตรวจสอบรูปแบบให้ครบ (ต้องมีตัวอักษร + จุด + ตัวเลข เช่น บ.123)
                elif not STRICT_ID_RE.match(new_id_input):
                    st.warning("⚠️ กรุณาระบุเลขรหัสให้ครบทั้งตัวอักษร จุด และตัวเลข (เช่น บ.123) จึงจะบันทึกได้")
                    is_id_invalid = True
                else:
                    st.success("✅ รูปแบบรหัสถูกต้องและสามารถใช้งานได้")

        with st.form("add_record_form", clear_on_submit=False):
            c1, c2 = st.columns(2)
            with c1:
                new_name = st.text_input("2. ชื่อ (จำเป็น) *", key="f_name", max_chars=100).strip()
            with c2:
                new_surname = st.text_input("3. นามสกุล (จำเป็น) *", key="f_surname", max_chars=100).strip()

            c1, c2, c3 = st.columns(3)
            with c1:
                new_status = st.selectbox("4. สถานะ (จำเป็น) *", ["", "ขรก.", "ลจ."], key="f_status")
            with c2:
                new_special = st.selectbox("5. เงินช่วยพิเศษ (จำเป็น) *", ["", "มี", "ไม่มี", "-"], key="f_special")
            with c3:
                new_pension = st.selectbox("6. บำเหน็จตกทอด (จำเป็น) *", ["", "มี", "ไม่มี", "-"], key="f_pension")

            new_note = st.text_area("7. หมายเหตุ (ถ้าไม่มีให้ใส่ -)", key="f_note", max_chars=500).strip()

            submitted = st.form_submit_button("💾 บันทึกข้อมูล", type="primary", disabled=is_id_invalid)

            if submitted:
                required_fields = [
                    ("เลขรหัส", new_id_input),
                    ("ชื่อ", new_name),
                    ("นามสกุล", new_surname),
                    ("สถานะ", new_status),
                    ("เงินช่วยพิเศษ", new_special),
                    ("บำเหน็จตกทอด", new_pension),
                    ("หมายเหตุ", new_note),
                ]
                missing_fields = [label for label, val in required_fields if not val]

                if missing_fields:
                    st.error(f"❌ กรุณากรอกข้อมูลให้ครบทุกช่อง (ยังขาด: {', '.join(missing_fields)})")
                elif any(looks_like_formula(v) for v in (new_name, new_surname, new_note)):
                    st.error("❌ ห้ามขึ้นต้นข้อมูลด้วยเครื่องหมาย = + หรือ @")
                elif not STRICT_ID_RE.match(new_id_input):
                    st.error("❌ เลขรหัสต้องมีทั้งตัวอักษร จุด และตัวเลขครบถ้วน (เช่น บ.123)")
                elif is_id_invalid:
                    st.error("❌ รหัสซ้ำ ไม่สามารถบันทึกได้")
                else:
                    new_row = {
                        'เลขรหัส': new_id_input,
                        'สถานะ': new_status,
                        'ชื่อ': new_name,
                        'นามสกุล': new_surname,
                        'เงินช่วยพิเศษ': new_special,
                        'บำเหน็จตกทอด': new_pension,
                        'หมายเหตุ': new_note,
                    }
                    try:
                        ok, msg = append_record(new_row)
                    except Exception:
                        logger.exception("append_record failed")
                        ok, msg = False, "บันทึกลง Google Sheets ไม่สำเร็จ (กรุณาลองกดบันทึกอีกครั้ง)"

                    if ok:
                        st.session_state["_reset_form"] = True
                        flash("✅ " + msg, "💾")
                        st.rerun()
                    else:
                        st.error("❌ " + msg)

# --- TAB 3: ดูตารางรวม ---
with tab3:
    st.write(f"ข้อมูลทั้งหมด **{len(df)}** รายการ")
    if not df.empty:
        show_df(visible(df))  # เรียงไว้แล้วตอนโหลด (อยู่ในแคช)
    else:
        st.info("ยังไม่มีข้อมูลในตาราง")
