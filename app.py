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
    required_code = secret("signup_code")
    if required_code and not hmac.compare_digest(invite_code.strip().encode(), str(required_code).encode()):
        return False, "รหัสเชิญไม่ถูกต้อง"
    if not USERNAME_RE.match(uname):
        return False, "ชื่อผู้ใช้ต้องเป็น a-z, 0-9, _ . - ความยาว 3–32 ตัวอักษร"
    err = validate_new_password(password)
    if err:
        return False, err

    with get_write_lock():
        df = read_users_fresh()
        if find_user(df, uname) is not None:
            return False, "ชื่อผู้ใช้งานนี้มีคนใช้แล้ว กรุณาเลือกชื่ออื่น"
        _append_row(_users_ws(), [uname, hash_pw(password), "User"])
    clear_user_cache()
    return True, f"ลงทะเบียนสำเร็จ! คุณ {uname} ได้รับสิทธิ์ 'User'"


def _write_password(username, pw_plain):
    with get_write_lock():
        df = read_users_fresh()
        row = find_user(df, username)
        if row is None:
            return False
        ws = _users_ws()
        headers = [h.strip() for h in _row_values(ws, 1)]
        _update_cell(ws, int(row["_row"]), headers.index("Password") + 1, hash_pw(pw_plain))
    clear_user_cache()
    return True


def set_password_as_admin(target, new_pw):
    if not require_role("Admin"):
        return False, "ไม่มีสิทธิ์ดำเนินการ"
    err = validate_new_password(new_pw)
    if err:
        return False, err
    if not _write_password(target, new_pw):
        return False, "ไม่พบผู้ใช้นี้"
    return True, f"รีเซ็ตรหัสผ่านของ {target} แล้ว"


def change_own_password(old_pw, new_pw):
    row = find_user(read_users_fresh(), st.session_state.username)
    if row is None or not verify_pw(old_pw, row["Password"])[0]:
        return False, "รหัสผ่านเดิมไม่ถูกต้อง"
    err = validate_new_password(new_pw)
    if err:
        return False, err
    _write_password(st.session_state.username, new_pw)
    return True, "เปลี่ยนรหัสผ่านเรียบร้อย"


def update_user_role(target, new_role):
    if not require_role("Admin"):
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
        ws = _users_ws()
        headers = [h.strip() for h in _row_values(ws, 1)]
        _update_cell(ws, int(row["_row"]), headers.index("Role") + 1, new_role)
    clear_user_cache()
    if target.strip().lower() == st.session_state.username.lower():
        st.session_state.role = new_role
    return True, "บันทึกสิทธิ์เรียบร้อย!"


def migrate_plaintext_passwords():
    if not require_role("Admin"):
        return 0
    with get_write_lock():
        df = read_users_fresh()
        targets = df[(df["Password"] != "") & (~df["Password"].map(is_hashed))]
        if targets.empty:
            return 0
        ws = _users_ws()
        headers = [h.strip() for h in _row_values(ws, 1)]
        pw_col = headers.index("Password") + 1
        data = [
            {"range": rowcol_to_a1(int(r["_row"]), pw_col), "values": [[hash_pw(r["Password"])]]}
            for _, r in targets.iterrows()
        ]
        _batch_update(ws, data)
    clear_user_cache()
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
SESSION_DEFAULTS = {"logged_in": False, "username": "", "role": "", "login_at": 0.0}


def init_session():
    for k, v in SESSION_DEFAULTS.items():
        st.session_state.setdefault(k, v)


def logout():
    for k, v in SESSION_DEFAULTS.items():
        st.session_state[k] = v


def render_login():
    apply_theme(DEFAULT_THEME)
    show_flash()
    _, col2, _ = st.columns([1, 2, 1])
    with col2:
        st.title("🔐 เข้าสู่ระบบ / ลงทะเบียน")
        require_code = bool(secret("signup_code"))

        with st.form("login_form", clear_on_submit=False):
            username = st.text_input("ชื่อผู้ใช้งาน (Username)", max_chars=64).strip()
            password = st.text_input("รหัสผ่าน (Password)", type="password", max_chars=MAX_PW_LEN * 2)
            invite = ""
            if require_code:
                invite = st.text_input("รหัสเชิญ (เฉพาะตอนลงทะเบียน)", type="password")

            c_login, c_signup = st.columns(2)
            with c_login:
                submit_login = st.form_submit_button("เข้าสู่ระบบ (Login)", type="primary")
            with c_signup:
                submit_signup = st.form_submit_button("ลงทะเบียน (Sign Up)")

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
        ok, legacy = False, False
    else:
        ok, legacy = verify_pw(password, row["Password"])

    if not ok:
        register_failure(username)
        st.error("❌ ชื่อผู้ใช้หรือรหัสผ่านไม่ถูกต้อง")
        return

    clear_failures(username)
    if legacy:
        try:
            _write_password(row["Username"], password)
        except Exception:
            logger.exception("Could not upgrade legacy password hash")

    st.session_state.logged_in = True
    st.session_state.username = row["Username"]
    st.session_state.role = row["Role"]
    st.session_state.login_at = time.time()
    flash(f"ยินดีต้อนรับคุณ {row['Username']}", "🎉")
    st.rerun()


def check_login():
    init_session()
    if st.session_state.logged_in and time.time() - st.session_state.login_at > SESSION_HOURS * 3600:
        logout()
        flash("หมดเวลาการใช้งาน กรุณาเข้าสู่ระบบอีกครั้ง", "⏱️")
    if not st.session_state.logged_in:
        render_login()
        st.stop()


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


@st.cache_data(ttl=60, show_spinner=False)
def load_data_cached():
    return _load_data_raw()


def clear_all_caches():
    load_data_cached.clear()
    users_df_cached.clear()


def append_record(row):
    """บันทึกแถวใหม่: ตรวจสิทธิ์สด + ตรวจรหัสซ้ำกับข้อมูลสด + เรียงคอลัมน์ตามหัวตารางจริง"""
    if not require_role("Admin", "Editor"):
        return False, "คุณไม่มีสิทธิ์บันทึกข้อมูล"

    with get_write_lock():
        ws = _data_ws()
        headers = [h.strip() for h in _row_values(ws, 1)]
        missing = [c for c in DATA_COLS if c not in headers]
        if missing:
            return False, f"ชีตข้อมูลหลักขาดคอลัมน์: {', '.join(missing)}"

        existing = {norm_id(v) for v in _col_values(ws, headers.index('เลขรหัส') + 1)}
        if norm_id(row['เลขรหัส']) in existing:
            return False, "รหัสนี้เพิ่งถูกใช้งานแล้ว (อาจมีผู้อื่นบันทึกไปก่อน)"

        _append_row(ws, [row.get(h, "") for h in headers])
    load_data_cached.clear()
    return True, "บันทึกข้อมูลสำเร็จ!"


def get_last_id_in_category(df, prefix):
    if df.empty or not prefix:
        return "ยังไม่มีข้อมูล"
    ids = df['เลขรหัส'].astype(str).str.strip()
    in_category = ids[ids.map(extract_prefix) == prefix].tolist()
    if not in_category:
        return "ยังไม่มีข้อมูล"
    return sorted(in_category, key=natural_sort_key)[-1]


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
    df = guard(load_data_cached)

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

    results = df
    if search_id:
        results = results[results['เลขรหัส'].str.contains(search_id, na=False, case=False, regex=False)]
    if search_name:
        results = results[results['ชื่อ'].str.contains(search_name, na=False, case=False, regex=False)]
    if search_surname:
        results = results[results['นามสกุล'].str.contains(search_surname, na=False, case=False, regex=False)]

    if search_id or search_name or search_surname:
        st.success(f"พบ {len(results)} รายการ")
        show_df(results)
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

                # 2. ตรวจสอบรหัสซ้ำ
                if norm_id(new_id_input) in {norm_id(v) for v in df['เลขรหัส'].values}:
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
        df_sorted = df.copy()
        df_sorted['sort_key'] = df_sorted['เลขรหัส'].apply(natural_sort_key)
        df_sorted = df_sorted.sort_values('sort_key').drop(columns=['sort_key'])
        show_df(df_sorted)
    else:
        st.info("ยังไม่มีข้อมูลในตาราง")
