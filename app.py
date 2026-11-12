import functools
import hmac
import logging
import os
import re
import threading
import time

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
MIN_PW_LEN, MAX_PW_LEN = 6, 64
MAX_FAILS = 5            # ใส่รหัสผิดได้กี่ครั้งก่อนถูกล็อก
LOCK_SECONDS = 300       # ล็อกกี่วินาที
SESSION_HOURS = 8        # หมดเวลาใช้งานอัตโนมัติ

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


def guard(fn, *args, **kwargs):
    """เรียกฟังก์ชันที่ต่อ Google Sheets ถ้าพังให้แสดงข้อความที่อ่านรู้เรื่องแล้วหยุดหน้า"""
    try:
        return fn(*args, **kwargs)
    except RuntimeError as e:
        st.error(f"❌ {e}")
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
    ws.append_row(row, value_input_option="RAW")


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


@st.cache_resource(ttl=3000, show_spinner=False)
def _users_ws():
    book = _book()
    try:
        return book.worksheet("Users")
    except gspread.exceptions.WorksheetNotFound:
        initial_pw = str(secret("initial_admin_password") or "")
        if len(initial_pw) < MIN_PW_LEN:
            raise RuntimeError(
                "ยังไม่มีชีต 'Users' — กรุณาตั้งค่า initial_admin_password "
                f"(อย่างน้อย {MIN_PW_LEN} ตัวอักษร) ใน st.secrets เพื่อสร้างบัญชี admin ครั้งแรก"
            )
        ws = book.add_worksheet(title="Users", rows=100, cols=len(USER_COLS))
        _append_row(ws, USER_COLS)
        _append_row(ws, ["admin", hash_pw(initial_pw), "Admin"])
        return ws


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
def hash_pw(pw: str) -> str:
    return bcrypt.hashpw(pw.encode("utf-8")[:72], bcrypt.gensalt()).decode()


def is_hashed(stored: str) -> bool:
    return str(stored).startswith(("$2a$", "$2b$", "$2y$"))


def verify_pw(pw: str, stored: str):
    stored = (stored or "").strip()
    if not stored or not pw:
        return False, False
    if is_hashed(stored):
        try:
            return bcrypt.checkpw(pw.encode("utf-8")[:72], stored.encode()), False
        except ValueError:
            return False, False
    return hmac.compare_digest(pw.encode("utf-8"), stored.encode("utf-8")), True


@st.cache_resource(show_spinner=False)
def _dummy_hash():
    return hash_pw("dummy-password-for-timing")


@st.cache_resource(show_spinner=False)
def _login_state():
    return {"lock": threading.Lock(), "attempts": {}}


def _key(username):
    return username.strip().lower()[:64]


def lock_remaining(username):
    state = _login_state()
    with state["lock"]:
        rec = state["attempts"].get(_key(username))
        return max(0, int(rec["until"] - time.time())) if rec else 0


def register_failure(username):
    state = _login_state()
    now = time.time()
    with state["lock"]:
        attempts = state["attempts"]
        if len(attempts) > 2000:
            for k in [k for k, v in attempts.items() if v["until"] < now and now - v["last"] > LOCK_SECONDS]:
                del attempts[k]
        rec = attempts.setdefault(_key(username), {"count": 0, "until": 0.0, "last": now})
        if now - rec["last"] > LOCK_SECONDS:
            rec["count"] = 0
        rec["last"] = now
        rec["count"] += 1
        if rec["count"] >= MAX_FAILS:
            rec["until"] = now + LOCK_SECONDS
            rec["count"] = 0


def clear_failures(username):
    state = _login_state()
    with state["lock"]:
        state["attempts"].pop(_key(username), None)


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


@st.cache_data(ttl=60, show_spinner=False)
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


def require_role(*roles):
    role = fresh_role(st.session_state.username)
    if role is None or role not in roles:
        if role is None:
            logout()
            flash("บัญชีนี้ไม่มีอยู่ในระบบแล้ว", "⚠️")
        return False
    st.session_state.role = role
    return True


def validate_new_password(pw):
    if len(pw) < MIN_PW_LEN:
        return f"รหัสผ่านต้องมีอย่างน้อย {MIN_PW_LEN} ตัวอักษร"
    if len(pw) > MAX_PW_LEN:
        return f"รหัสผ่านต้องไม่เกิน {MAX_PW_LEN} ตัวอักษร"
    return None


def sign_up(username, password, invite_code=""):
    uname = username.strip().lower()
    required_code = secret
