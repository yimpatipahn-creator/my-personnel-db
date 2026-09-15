import streamlit as st
import pandas as pd
import gspread
from oauth2client.service_account import ServiceAccountCredentials
import re
import time

# --- ตั้งค่าหน้าเว็บ ---
st.set_page_config(page_title="ระบบทะเบียนคุมเลขรหัสหนังสือ (Online)", layout="wide")

# ==========================================
# ⚙️ ตั้งค่า (Google Sheets)
# ==========================================
SHEET_URL = "https://docs.google.com/spreadsheets/d/1DziBBBmgoXnUiX-MvsuUGTIEHvD5Jqjib1Ap-bmhins/edit?usp=sharing"
KEY_FILE = "credentials.json"

# --- ฟังก์ชันเรียงลำดับแบบธรรมชาติ (Natural Sort Key) ---
def natural_sort_key(s):
    return [int(text) if text.isdigit() else text.lower() for text in re.split(r'([0-9]+)', str(s))]

# --- เชื่อมต่อ Google Sheets ---
@st.cache_resource
def init_connection():
    scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    try:
        if "gcp_service_account" in st.secrets:
            creds_dict = st.secrets["gcp_service_account"]
            creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
        else:
            creds = ServiceAccountCredentials.from_json_keyfile_name(KEY_FILE, scope)
            
        client = gspread.authorize(creds)
        return client
    except Exception as e:
        st.error(f"❌ เชื่อมต่อ Google Sheets ไม่สำเร็จ: {e}")
        st.stop()

# ==========================================
# 🎨 ระบบจัดการธีมสี (Theme Manager) - แก้ไขตัวหนังสือกลืน 100%
# ==========================================
def apply_theme(theme_name):
    themes = {
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
        }
    }
    
    c = themes.get(theme_name, themes["🌿 เขียว-ฟ้า (Green)"])

    st.markdown(f"""
    <style>
    /* 1. บังคับพื้นหลังและตัวหนังสือหลักของหน้าจอ */
    .stApp {{ 
        background-color: {c['bg']} !important; 
        color: #111827 !important; 
    }}
    
    /* 2. Sidebar และส่วนประกอบด้านซ้ายทั้งหมด */
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
    
    /* แก้ไขข้อความใน Expander "จัดการผู้ใช้งาน" ที่มองไม่เห็น */
    [data-testid="stExpander"] details summary,
    [data-testid="stExpander"] details summary * {{
        color: #111827 !important;
        font-weight: 600 !important;
    }}
    
    /* 3. แก้ไขแถบเมนูแท็บ (Tabs) ที่ตัวหนังสือสีขาว/กลืน */
    div[data-baseweb="tab-list"] button {{
        background: transparent !important;
    }}
    /* แท็บทั่วไปที่ไม่ได้คลิกเลือก (ให้เป็นสีเทาเข้ม ชัดเจนแน่นอน) */
    div[data-baseweb="tab-list"] button * {{
        color: #374151 !important;
        -webkit-text-fill-color: #374151 !important;
        font-size: 16px !important;
        font-weight: 600 !important;
    }}
    /* แท็บที่กำลังเลือกอยู่ (Active) */
    div[data-baseweb="tab-list"] button[aria-selected="true"] * {{
        color: {c['tab_active']} !important;
        -webkit-text-fill-color: {c['tab_active']} !important;
        font-weight: 800 !important;
    }}
    /* เส้นขีดใต้แท็บ */
    div[data-baseweb="tab-highlight"] {{
        background-color: {c['tab_active']} !important;
        height: 3px !important;
    }}

    /* 4. แก้ไขกล่อง Dropdown Selectbox ที่พื้นหลังดำจนมองไม่เห็น */
    div[data-baseweb="select"] > div {{
        background-color: #FFFFFF !important;
        border-radius: 10px !important;
        border: 1.5px solid {c['input_border']} !important;
    }}
    div[data-baseweb="select"] * {{
        color: #111827 !important;
        -webkit-text-fill-color: #111827 !important;
    }}
    /* เมนูตัวเลือกด้านใน dropdown เมื่อคลิกเปิด */
    ul[data-baseweb="menu"] {{
        background-color: #FFFFFF !important;
    }}
    ul[data-baseweb="menu"] li * {{
        color: #111827 !important;
    }}

    /* 5. หัวข้อขนาดต่าง ๆ */
    h1 {{ 
        color: {c['h1']} !important; 
        text-shadow: 1px 1px 2px {c['h1_shadow']}; 
        font-family: 'Sarabun', sans-serif; 
    }}
    h2, h3, h4, h5, h6 {{ 
        color: {c['h2']} !important; 
        font-weight: 700 !important;
    }}

    /* 6. ตัวหนังสือทั่วไป ป้ายข้อความ และคำอธิบาย */
    p, span, label, [data-testid="stWidgetLabel"] p {{ 
        color: #111827 !important; 
        -webkit-text-fill-color: #111827 !important;
        font-weight: 500 !important;
    }}
    
    /* 7. ปุ่มกด */
    .stButton>button {{
        background: {c['btn_grad']} !important; 
        border-radius: 20px !important;
        border: none !important; 
        padding: 8px 24px !important; 
        box-shadow: 0 4px 10px {c['btn_shadow']} !important; 
    }}
    .stButton>button * {{
        color: #FFFFFF !important;
        -webkit-text-fill-color: #FFFFFF !important;
        font-weight: bold !important;
    }}
    
    /* 8. ช่อง Input และ Text Area */
    .stTextInput input, .stTextArea textarea {{ 
        background-color: #FFFFFF !important; 
        color: #000000 !important; 
        -webkit-text-fill-color: #000000 !important;
        border: 1.5px solid {c['input_border']} !important;
        border-radius: 10px !important;
    }}
    
    /* 9. กล่องข้อความแจ้งเตือนสีฟ้า (st.info) */
    [data-testid="stAlert"] * {{
        color: #0F172A !important;
        -webkit-text-fill-color: #0F172A !important;
    }}
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
    except Exception:
        worksheet = sh.add_worksheet(title="Users", rows="100", cols="3")
        worksheet.append_row(["Username", "Password", "Role"])
        worksheet.append_row(["admin", "1234", "Admin"])
        
    all_values = worksheet.get_all_values()
    if not all_values: 
        return pd.DataFrame(columns=["Username", "Password", "Role"]), worksheet
    headers = all_values[0]
    data = all_values[1:]
    df = pd.DataFrame(data, columns=headers).astype(str)
    return df, worksheet

def sign_up(username, password, users_df, user_sheet):
    if len(password) < 4:
        st.error("❌ รหัสผ่านต้องมีอย่างน้อย 4 ตัวอักษร")
        return False
    if username in users_df['Username'].values:
        st.error("❌ ชื่อผู้ใช้งานนี้มีคนใช้แล้ว กรุณาเลือกชื่ออื่น")
        return False
    user_sheet.append_row([str(username), str(password), "User"])
    st.success(f"✅ ลงทะเบียนสำเร็จ! คุณ {username} ได้รับสิทธิ์ 'User'")
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
            
            with st.form("login_form", clear_on_submit=False):
                username = st.text_input("ชื่อผู้ใช้งาน (Username)").strip()
                password = st.text_input("รหัสผ่าน (Password)", type="password").strip()
                
                c_login, c_signup = st.columns(2)
                with c_login:
                    submit_login = st.form_submit_button("เข้าสู่ระบบ (Login)", type="primary")
                with c_signup:
                    submit_signup = st.form_submit_button("ลงทะเบียน (Sign Up)")

            if submit_login:
                user_row = users_df[users_df['Username'] == username]
                if not user_row.empty and user_row.iloc[0]['Password'] == password:
                    st.session_state.logged_in = True
                    st.session_state.username = username
                    st.session_state.role = user_row.iloc[0]['Role']
                    st.toast(f"ยินดีต้อนรับคุณ {username}", icon="🎉")
                    time.sleep(0.3)
                    st.rerun()
                else:
                    st.error("❌ ชื่อผู้ใช้หรือรหัสผ่านไม่ถูกต้อง")
                    
            elif submit_signup:
                if username and password:
                    if sign_up(username, password, users_df, user_sheet):
                        st.toast("คุณสามารถเข้าสู่ระบบได้ทันที", icon="🔑")
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
        default_cols = ['เลขรหัส', 'สถานะ', 'ชื่อ', 'นามสกุล', 'เงินช่วยพิเศษ', 'บำเหน็จตกทอด', 'หมายเหตุ']
        if not all_values: 
            return pd.DataFrame(columns=default_cols)
        
        headers = all_values[0]
        data = all_values[1:]
        df = pd.DataFrame(data, columns=headers).astype(str)
        for col in df.columns: 
            df[col] = df[col].str.strip()
        df = df[df['เลขรหัส'] != 'เลขรหัส']
        df = df[df['เลขรหัส'] != '']
        valid_cols = [c for c in df.columns if "Unnamed" not in c and c != ""]
        return df[valid_cols]
    except Exception as e:
        st.error(f"❌ โหลดข้อมูลไม่ได้: {e}")
        return pd.DataFrame()

def append_to_gsheet(row_data_dict):
    try:
        client = init_connection()
        sheet = client.open_by_url(SHEET_URL).sheet1
        sheet.append_row(list(row_data_dict.values()))
        return True
    except Exception as e:
        st.error(f"❌ เกิดข้อผิดพลาดขณะบันทึกลง Google Sheets: {e}")
        return False

def get_last_id_in_category(df, prefix):
    if df.empty: 
        return "ยังไม่มีข้อมูล"
    df['เลขรหัส'] = df['เลขรหัส'].astype(str).str.strip()
    filtered = df[df['เลขรหัส'].str.startswith(prefix)].copy()
    if filtered.empty: 
        return "ยังไม่มีข้อมูล"
    sorted_ids = sorted(filtered['เลขรหัส'].tolist(), key=natural_sort_key)
    return sorted_ids[-1] if sorted_ids else "ไม่พบ"

# ==========================================
# 🖥️ หน้าจอหลัก
# ==========================================

with st.sidebar:
    st.write(f"สวัสดี, **{st.session_state.username}**")
    st.divider()
    selected_theme = st.selectbox("🎨 เลือกธีมสี", [
        "🌿 เขียว-ฟ้า (Green)", 
        "🌸 ชมพู-ฟ้า (Pink)", 
        "🍊 ส้ม-ครีม (Orange)", 
        "🏢 เทา-น้ำเงิน (Professional)"
    ])
    apply_theme(selected_theme)

    if st.session_state.role == "Admin":
        st.divider()
        st.subheader("👑 ผู้ดูแลระบบ")
        with st.expander("จัดการผู้ใช้งาน"):
            users_df, user_sheet = get_users_data()
            st.dataframe(users_df[['Username', 'Role']], use_container_width=True, hide_index=True)
            st.write("--- แก้ไขสิทธิ์ ---")
            with st.form("edit_user_form"):
                user_list = users_df['Username'].tolist() if not users_df.empty else []
                user_to_edit = st.selectbox("เลือก Username", user_list)
                new_role = st.selectbox("เปลี่ยนสิทธิ์", ["Admin", "Editor", "User"])
                if st.form_submit_button("บันทึกสิทธิ์"):
                    if user_to_edit:
                        idx = users_df[users_df['Username'] == user_to_edit].index[0]
                        user_sheet.update_cell(idx + 2, 3, new_role) 
                        st.toast("✅ บันทึกสิทธิ์เรียบร้อย!", icon="🔧")
                        time.sleep(0.5)
                        st.rerun()

    st.divider()
    if st.button("🔄 รีโหลดข้อมูล"):
        st.session_state.df = load_data()
        st.rerun()
    if st.button("🚪 ออกจากระบบ"):
        st.session_state.logged_in = False
        st.session_state.username = ""
        st.session_state.role = ""
        st.rerun()

st.title("📂 ระบบทะเบียนคุมเลขรหัสหนังสือ")
st.caption(f"ผู้ใช้งาน: {st.session_state.username} | สิทธิ์: {st.session_state.role}")

if 'df' not in st.session_state:
    with st.spinner("กำลังโหลดข้อมูล..."):
        st.session_state.df = load_data()

df = st.session_state.df

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
    with c1: search_id = st.text_input("เลขรหัส", placeholder="เช่น ก.1").strip()
    with c2: search_name = st.text_input("ชื่อ", placeholder="พิมพ์ชื่อ...").strip()
    with c3: search_surname = st.text_input("นามสกุล", placeholder="พิมพ์นามสกุล...").strip()
    
    results = df.copy()
    if search_id: results = results[results['เลขรหัส'].str.contains(search_id, na=False, case=False)]
    if search_name: results = results[results['ชื่อ'].str.contains(search_name, na=False, case=False)]
    if search_surname: results = results[results['นามสกุล'].str.contains(search_surname, na=False, case=False)]
    
    if search_id or search_name or search_surname:
        st.success(f"พบ {len(results)} รายการ")
        st.dataframe(results.fillna("").astype(str), use_container_width=True, hide_index=True)
    else:
        st.info("พิมพ์รหัส, ชื่อ หรือนามสกุล เพื่อค้นหาข้อมูล")

# --- TAB 2: บันทึกข้อมูลใหม่ ---
if tab2:
    with tab2:
        st.subheader("📝 เพิ่มข้อมูลใหม่")
        
        col_id_input, col_id_hint = st.columns([1, 2])
        with col_id_input: 
            new_id_input = st.text_input("1. เลขรหัส (จำเป็น) *", key="temp_id").strip()
            
        is_duplicate = False
        with col_id_hint:
            if new_id_input:
                if not df.empty and new_id_input in df['เลขรหัส'].values:
                    st.error("❌ รหัสนี้ถูกใช้งานแล้ว!")
                    is_duplicate = True
                else:
                    match = re.match(r"([ก-ฮa-zA-Z]+\.?)", new_id_input)
                    prefix = match.group(1) if match else (new_id_input[0] if len(new_id_input) > 0 else "")
                    if prefix:
                        last = get_last_id_in_category(df, prefix)
                        st.info(f"หมวด '{prefix}' ล่าสุดคือ: **{last}**")
        
        with st.form("add_record_form", clear_on_submit=True):
            c1, c2 = st.columns(2)
            with c1: new_name = st.text_input("2. ชื่อ (จำเป็น) *").strip()
            with c2: new_surname = st.text_input("3. นามสกุล (จำเป็น) *").strip()
            
            c1, c2, c3 = st.columns(3)
            with c1: new_status = st.selectbox("4. สถานะ *", ["", "ขรก.", "ลจ."])
            with c2: new_special = st.selectbox("เงินช่วยพิเศษ", ["-", "มี", "ไม่มี"])
            with c3: new_pension = st.selectbox("บำเหน็จตกทอด", ["-", "มี", "ไม่มี"])
            
            new_note = st.text_area("หมายเหตุ").strip()
            
            submitted = st.form_submit_button("💾 บันทึกข้อมูล", type="primary", disabled=is_duplicate)
            
            if submitted:
                if not (new_id_input and new_name and new_surname and new_status):
                    st.error("❌ กรุณากรอกข้อมูลในช่องที่มีเครื่องหมาย * ให้ครบถ้วน")
                elif is_duplicate:
                    st.error("❌ รหัสซ้ำ ไม่สามารถบันทึกได้")
                else:
                    new_row = {
                        'เลขรหัส': new_id_input, 
                        'สถานะ': new_status, 
                        'ชื่อ': new_name, 
                        'นามสกุล': new_surname, 
                        'เงินช่วยพิเศษ': new_special, 
                        'บำเหน็จตกทอด': new_pension, 
                        'หมายเหตุ': new_note
                    }
                    if append_to_gsheet(new_row):
                        st.session_state.df = pd.concat([st.session_state.df, pd.DataFrame([new_row])], ignore_index=True)
                        st.success("✅ บันทึกข้อมูลสำเร็จ!")
                        time.sleep(0.4)
                        st.rerun()

# --- TAB 3: ดูตารางรวม ---
with tab3:
    st.write(f"ข้อมูลทั้งหมด **{len(df)}** รายการ")
    if not df.empty:
        df_sorted = df.copy()
        df_sorted['sort_key'] = df_sorted['เลขรหัส'].apply(natural_sort_key)
        df_sorted = df_sorted.sort_values('sort_key').drop(columns=['sort_key'])
        st.dataframe(df_sorted.fillna("").astype(str), use_container_width=True, hide_index=True)
    else:
        st.info("ยังไม่มีข้อมูลในตาราง")
