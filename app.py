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
        results_display = results.drop(columns=['temp_sort'], errors='ignore').fillna("").astype(str)
        st.dataframe(results_display, use_container_width=True, hide_index=True)
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
    df_display = df.drop(columns=['temp_sort'], errors='ignore').fillna("").astype(str)
    st.dataframe(df_display, use_container_width=True)
