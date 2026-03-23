import streamlit as st
import pandas as pd
import gspread
from oauth2client.service_account import ServiceAccountCredentials
import re
import time
import json

# --- ตั้งค่าหน้าเว็บ ---
st.set_page_config(page_title="ระบบทะเบียนคุมเลขรหัสหนังสือ (Online)", layout="wide")

# ==========================================
# ⚙️ ตั้งค่า (Google Sheets)
# ==========================================
SHEET_URL = "https://docs.google.com/spreadsheets/d/1DziBBBmgoXnUiX-MvsuUGTIEHvD5Jqjib1Ap-bmhins/edit?usp=sharing"
KEY_FILE = "credentials.json"

# --- เชื่อมต่อ Google Sheets ---
@st.cache_resource
def init_connection():
    scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    try:
        try:
            if "gcp_service_account" in st.secrets:
                creds_dict = st.secrets["gcp_service_account"]
                creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
            else:
                raise Exception("No secrets found")
        except Exception:
            creds = ServiceAccountCredentials.from_json_keyfile_name(KEY_FILE, scope)
            
        client = gspread.authorize(creds)
        return client
    except Exception as e:
        st.error(f"❌ เชื่อมต่อไม่ได้: {e}")
        st.stop()

# ==========================================
# 🎨 ระบบจัดการธีมสี (Theme Manager) - แก้ไข Font สีดำ
# ==========================================
def apply_theme(theme_name):
    themes = {
        "🌿 เขียว-ฟ้า (Green)": {
            "bg": "#F1F8E9", "sidebar": "#E0F7FA", "sidebar_border": "#80DEEA",
            "h1": "#2E7D32", "h1_shadow": "#A5D6A7", "h2": "#00695C",
            "btn_grad": "linear-gradient(to right, #66BB6A, #26A69A)", "btn_shadow": "rgba(38, 166, 154, 0.3)",
            "input_border": "#81C784", "table_border": "#80CBC4"
        },
        "🌸 ชมพู-ฟ้า (Pink)": {
            "bg": "#FFF0F5", "sidebar": "#E1F5FE", "sidebar_border": "#81D4FA",
            "h1": "#D81B60", "h1_shadow": "#F8BBD0", "h2": "#01579B",
            "btn_grad": "linear-gradient(to right, #EC407A, #D81B60)", "btn_shadow": "rgba(233, 30, 99, 0.3)",
            "input_border": "#F48FB1", "table_border": "#B3E5FC"
        },
        "🍊 ส้ม-ครีม (Orange)": {
            "bg": "#FFF3E0", "sidebar": "#FFF8E1", "sidebar_border": "#FFE082",
            "h1": "#E65100", "h1_shadow": "#FFCC80", "h2": "#BF360C",
            "btn_grad": "linear-gradient(to right, #FF9800, #F57C00)", "btn_shadow": "rgba(255, 152, 0, 0.3)",
            "input_border": "#FFB74D", "table_border": "#FFE0B2"
        },
        "🏢 เทา-น้ำเงิน (Professional)": {
            "bg": "#F5F5F5", "sidebar": "#ECEFF1", "sidebar_border": "#CFD8DC",
            "h1": "#37474F", "h1_shadow": "#B0BEC5", "h2": "#455A64",
            "btn_grad": "linear-gradient(to right, #607D8B, #455A64)", "btn_shadow": "rgba(96, 125, 139, 0.3)",
            "input_border": "#90A4AE", "table_border": "#CFD8DC"
        }
    }
    
    c = themes.get(theme_name, themes["🌿 เขียว-ฟ้า (Green)"])

    st.markdown(f"""
    <style>
    /* บังคับพื้นหลัง */
    .stApp {{ background-color: {c['bg']}; }}
    
    /* Sidebar */
    [data-testid="stSidebar"] {{ background-color: {c['sidebar']}; border-right: 2px solid {c['sidebar_border']}; }}
    
    /* หัวข้อ */
    h1 {{ color: {c['h1']} !important; text-shadow: 1px 1px 2px {c['h1_shadow']}; font-family: 'Sarabun', sans-serif; }}
    h2, h3, h4, h5, h6, label, .stMarkdown p {{ color: {c['h2']} !important; }}
    
    /* ปุ่ม */
    .stButton>button {{
        background: {c['btn_grad']}; color: white; border-radius: 20px;
        border: none; padding: 10px 28px; box-shadow: 0 4px 10px {c['btn_shadow']}; font-weight: bold;
    }}
    .stButton>button:hover {{ transform: scale(1.05); }}
    
    /* ✅ แก้ไข: บังคับให้ช่องกรอกข้อมูลมีตัวหนังสือสีดำเสมอ (แก้ปัญหา Dark Mode) */
    .stTextInput>div>div>input {{ 
        border-radius: 12px; 
        border: 1px solid {c['input_border']}; 
        background-color: #FFFFFF !important; 
        color: #000000 !important; /* บังคับดำ */
        -webkit-text-fill-color: #000000 !important;
        caret-color: #000000 !important; /* Cursor สีดำ */
    }}
    
    /* แก้ไข Text Area */
    .stTextArea>div>div>textarea {{
        border-radius: 12px; 
        border: 1px solid {c['input_border']}; 
        background-color: #FFFFFF !important; 
        color: #000000 !important;
        -webkit-text-fill-color: #000000 !important;
    }}
    
    /* แก้ไข Selectbox */
    div[data-baseweb="select"] > div {{
        border-radius: 12px;
        border: 1px solid {c['input_border']};
        background-color: #FFFFFF !important;
        color: #000000 !important;
    }}
    
    /* ตาราง */
    [data-testid="stDataFrame"] {{ border-radius: 10px; overflow: hidden; border: 1px solid {c['table_border']}; }}
    </style>
    """, unsafe_allow_html=True)

# ==========================================
# 👥 ระบบจัดการผู้ใช้งาน (Users System)
# ==========================================
def get_users_data():
    client = init_connection()
    sh = client.open_by_url(SHEET_URL)
    try:
        worksheet = sh.worksheet("Users")
    except:
        worksheet = sh.add_worksheet(title="Users", rows="100", cols="3")
        worksheet.append_row(["Username", "Password", "Role"])
        worksheet.append_row(["admin", "1234", "Admin"])
        
    all_values = worksheet.get_all_values()
    if not all_values: return pd.DataFrame(columns=["Username", "Password", "Role"]), worksheet
    headers = all_values[0]
    data = all_values[1:]
    df = pd.DataFrame(data, columns=headers)
    df = df.astype(str)
    return df, worksheet

def sign_up(username, password, users_df, user_sheet):
    if len(password) < 4:
        st.error("รหัสผ่านต้องมีอย่างน้อย 4 ตัวอักษร")
        return False
    if username in users_df['Username'].values:
        st.error("ชื่อผู้ใช้งานนี้มีคนใช้แล้ว กรุณาเลือกชื่ออื่น")
        return False
    user_sheet.append_row([str(username), str(password), "User"])
    st.success(f"✅ ลงทะเบียนสำเร็จ! คุณ {username} ได้รับสิทธิ์ 'User' แล้ว")
    st.toast("คุณสามารถเข้าสู่ระบบได้ทันที", icon="🔑")
    return True

def check_login():
    if "logged_in" not in st.session_state:
        st.session_state.logged_in = False
        st.session_state.username = ""
        st.session_state.role = ""

    if not st.session_state.logged_in:
        apply_theme("🌿 เขียว-ฟ้า (Green)") 
        
        col1, col2, col3 = st.columns([1, 2, 1])
        with col2:
            st.title("🔐 เข้าสู่ระบบ / ลงทะเบียน")
            try:
                users_df, user_sheet = get_users_data()
                users_df['Username'] = users_df['Username'].str.strip()
                users_df['Password'] = users_df['Password'].str.strip()
            except Exception as e:
                st.error(f"เกิดข้อผิดพลาดในการเชื่อมต่อข้อมูลผู้ใช้: {e}")
                st.stop()
            
            # ---------------------------------------------------------
            # ✅ แก้ไข: ใช้ st.form เพื่อแก้ปัญหา Chrome Autofill (ผีหลอก)
            # ---------------------------------------------------------
            with st.form("login_form", clear_on_submit=False):
                username = st.text_input("ชื่อผู้ใช้งาน (Username)", key="login_u")
                password = st.text_input("รหัสผ่าน (Password)", type="password", key="login_p")
                
                c_login, c_signup = st.columns(2)
                with c_login:
                    # ปุ่มใน form ต้องใช้ st.form_submit_button
                    submit_login = st.form_submit_button("เข้าสู่ระบบ (Login)", type="primary")
                with c_signup:
                    submit_signup = st.form_submit_button("ลงทะเบียน (Sign Up)")

            # --- จัดการเมื่อกดปุ่ม Login ---
            if submit_login:
                clean_u = username.strip()
                clean_p = password.strip()
                user_row = users_df[users_df['Username'] == clean_u]
                if not user_row.empty and user_row.iloc[0]['Password'] == clean_p:
                    st.session_state.logged_in = True
                    st.session_state.username = clean_u
                    st.session_state.role = user_row.iloc[0]['Role']
                    st.toast(f"ยินดีต้อนรับคุณ {clean_u} ({st.session_state.role})", icon="🎉")
                    time.sleep(0.5)
                    st.rerun()
                else:
                    st.error("❌ ชื่อผู้ใช้หรือรหัสผ่านไม่ถูกต้อง")
                    
            # --- จัดการเมื่อกดปุ่ม Sign Up ---
            elif submit_signup:
                if username and password:
                    sign_up(username.strip(), password.strip(), users_df, user_sheet)
                    st.session_state.logged_in = False
                else:
                    st.error("กรุณากรอก Username และ Password ก่อนลงทะเบียน")
                    
        st.stop()

check_login()

# ==========================================
# 🛠️ ฟังก์ชันจัดการข้อมูล (Data)
# ==========================================
def load_data():
    try:
        client = init_connection()
        sheet = client.open_by_url(SHEET_URL).sheet1
        all_values = sheet.get_all_values()
        if not all_values: return pd.DataFrame(columns=['เลขรหัส', 'สถานะ', 'ชื่อ', 'นามสกุล', 'เงินช่วยพิเศษ', 'บำเหน็จตกทอด', 'หมายเหตุ'])
        headers = all_values[0]
        data = all_values[1:]
        df = pd.DataFrame(data, columns=headers).astype(str)
        for col in df.columns: df[col] = df[col].str.strip()
        df = df[df['เลขรหัส'] != 'เลขรหัส']
        df = df[df['เลขรหัส'] != '']
        valid_cols = [c for c in df.columns if "Unnamed" not in c and c != ""]
        df = df[valid_cols]
        return df
    except Exception as e:
        st.error(f"❌ โหลดข้อมูลไม่ได้: {e}")
        return pd.DataFrame()

def save_to_gsheet(df):
    try:
        client = init_connection()
        sheet = client.open_by_url(SHEET_URL).sheet1
        def natural_sort_key(s):
            return [int(text) if text.isdigit() else text.lower() for text in re.split('([0-9]+)', str(s))]
        df['temp_sort'] = df['เลขรหัส'].apply(natural_sort_key)
        df_sorted = df.sort_values('temp_sort').drop(columns=['temp_sort'])
        sheet.clear()
        sheet.update(range_name='A1', values=[df_sorted.columns.tolist()] + df_sorted.values.tolist())
        return df_sorted
    except Exception as e:
        st.error(f"❌ บันทึกข้อมูลไม่สำเร็จ: {e}")
        return df

def get_last_id_in_category(df, prefix):
    df['เลขรหัส'] = df['เลขรหัส'].astype(str).str.strip()
    mask = df['เลขรหัส'].str.startswith(prefix)
    filtered = df[mask].copy()
    if filtered.empty: return "ยังไม่มีข้อมูล"
    def natural_sort_key(s): return [int(t) if t.isdigit() else t.lower() for t in re.split('([0-9]+)', str(s))]
    sorted_ids = sorted(filtered['เลขรหัส'].tolist(), key=natural_sort_key)
    return sorted_ids[-1] if sorted_ids else "ไม่พบ"

# ==========================================
# 🖥️ หน้าจอหลัก
# ==========================================

with st.sidebar:
    st.write(f"สวัสดี, **{st.session_state.username}**")
    
    st.divider()
    selected_theme = st.selectbox("🎨 เลือกธีมสี", 
                                  ["🌿 เขียว-ฟ้า (Green)", 
                                   "🌸 ชมพู-ฟ้า (Pink)", 
                                   "🍊 ส้ม-ครีม (Orange)", 
                                   "🏢 เทา-น้ำเงิน (Professional)"])
    
    apply_theme(selected_theme)

    if st.session_state.role == "Admin":
        st.divider()
        st.subheader("👑 ผู้ดูแลระบบ")
        with st.expander("จัดการผู้ใช้งาน"):
            users_df, user_sheet = get_users_data()
            st.dataframe(users_df, use_container_width=True, hide_index=True)
            st.write("--- แก้ไขสิทธิ์ ---")
            with st.form("edit_user_form"):
                user_list = users_df['Username'].tolist() if not users_df.empty else []
                user_to_edit = st.selectbox("เลือก Username", user_list)
                new_role = st.selectbox("เปลี่ยนสิทธิ์", ["Admin", "Editor", "User"])
                if st.form_submit_button("บันทึก"):
                    if user_to_edit:
                        idx = users_df[users_df['Username'] == user_to_edit].index[0]
                        user_sheet.update_cell(idx + 2, 3, new_role) 
                        st.toast("✅ เรียบร้อย!", icon="🔧")
                        time.sleep(1)
                        st.rerun()

    st.divider()
    if st.button("🔄 รีโหลดข้อมูล"):
        st.cache_data.clear()
        st.session_state.df = load_data()
        st.rerun()
    if st.button("🚪 ออกจากระบบ"):
        st.session_state.logged_in = False
        st.rerun()

st.title("📂 ระบบทะเบียนคุมเลขรหัสหนังสือ")
st.caption(f"ผู้ใช้งาน: {st.session_state.username} | สิทธิ์: {st.session_state.role}")

if 'df' not in st.session_state:
    with st.spinner("กำลังโหลดข้อมูล..."):
        st.session_state.df = load_data()
df = st.session_state.df

if st.session_state.role == "User":
    tab_names = ["🔍 ค้นหาข้อมูล", "💾 ดูตารางรวม"]
    tab1, tab3 = st.tabs(tab_names)
    tab2 = None
else:
    tab_names = ["🔍 ค้นหาข้อมูล", "📝 บันทึกข้อมูลใหม่", "💾 ดูตารางรวม"]
    tab1, tab2, tab3 = st.tabs(tab_names)

with tab1:
    st.subheader("🔍 ค้นหาบุคลากร")
    c1, c2, c3 = st.columns(3)
    with c1: search_id = st.text_input("เลขรหัส", placeholder="เช่น ก.1").strip()
    with c2: search_name = st.text_input("ชื่อ", placeholder="พิมพ์ชื่อ...").strip()
    with c3: search_surname = st.text_input("นามสกุล", placeholder="พิมพ์นามสกุล...").strip()
    results = df.copy()
    if search_id: results = results[results['เลขรหัส'].str.contains(search_id, na=False)]
    if search_name: results = results[results['ชื่อ'].str.contains(search_name, na=False)]
    if search_surname: results = results[results['นามสกุล'].str.contains(search_surname, na=False)]
    if len(results) < len(df):
        st.success(f"พบ {len(results)} รายการ")
        st.dataframe(results, use_container_width=True, hide_index=True)
    else:
        st.info("พิมพ์ข้อมูลเพื่อค้นหา")

if tab2:
    with tab2:
        st.subheader("📝 เพิ่มข้อมูลใหม่")
        def submit_callback():
            new_id = st.session_state.input_id
            new_name = st.session_state.input_name
            new_surname = st.session_state.input_surname
            new_status = st.session_state.input_status
            new_special = st.session_state.input_special
            new_pension = st.session_state.input_pension
            new_note = st.session_state.input_note
            if not (new_id and new_name and new_surname and new_status):
                st.toast("❌ กรุณากรอกข้อมูลช่องที่มี * ให้ครบ")
                return
            new_row = {'เลขรหัส': new_id, 'สถานะ': new_status, 'ชื่อ': new_name, 'นามสกุล': new_surname, 'เงินช่วยพิเศษ': new_special, 'บำเหน็จตกทอด': new_pension, 'หมายเหตุ': new_note}
            st.session_state.df = pd.concat([st.session_state.df, pd.DataFrame([new_row])], ignore_index=True)
            st.toast("⏳ กำลังบันทึกข้อมูล...")
            save_to_gsheet(st.session_state.df)
            for key in ['input_id', 'input_name', 'input_surname', 'input_status', 'input_note']: st.session_state[key] = ""
            st.toast("✅ บันทึกเรียบร้อย!")

        col_id_input, col_id_hint = st.columns([1, 2])
        with col_id_input: new_id_input = st.text_input("1. เลขรหัส (จำเป็น) *", key="input_id")
        is_duplicate = False
        with col_id_hint:
            if new_id_input:
                if new_id_input in df['เลขรหัส'].values:
                    st.error(f"❌ รหัสซ้ำ!")
                    is_duplicate = True
                else:
                    match = re.match(r"([ก-ฮa-zA-Z]+\.?)", new_id_input)
                    prefix = match.group(1) if match else (new_id_input[0] if len(new_id_input)>0 else "")
                    if prefix:
                        last = get_last_id_in_category(df, prefix)
                        st.info(f"หมวด '{prefix}' ล่าสุดคือ: {last}")
            else: st.write("")

        c1, c2 = st.columns(2)
        with c1: st.text_input("2. ชื่อ (จำเป็น) *", key="input_name")
        with c2: st.text_input("3. นามสกุล (จำเป็น) *", key="input_surname")
        c1, c2, c3 = st.columns(3)
        with c1: st.selectbox("4. สถานะ *", ["", "ขรก.", "ลจ."], key="input_status")
        with c2: st.selectbox("เงินช่วยพิเศษ", ["มี", "ไม่มี", "-"], key="input_special")
        with c3: st.selectbox("บำเหน็จตกทอด", ["มี", "ไม่มี", "-"], key="input_pension")
        st.text_area("หมายเหตุ", key="input_note")
        st.write("---")
        st.button("💾 บันทึกข้อมูล", type="primary", disabled=is_duplicate, on_click=submit_callback)

with tab3:
    st.write(f"ข้อมูลทั้งหมด {len(df)} รายการ")
    # ป้องกัน pyarrow error โดยบังคับให้ข้อมูลว่างเปล่ากลายเป็น "" และเป็น String ก่อนแสดงผล
    df_display = df.copy()
    df_display = df_display.fillna("").astype(str)
    st.dataframe(df_display, use_container_width=True)
