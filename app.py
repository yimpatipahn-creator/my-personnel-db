import streamlit as st
import pandas as pd
import gspread
from oauth2client.service_account import ServiceAccountCredentials
import re
import time
import json

# --- ตั้งค่าหน้าเว็บ ---
st.set_page_config(page_title="ระบบทะเบียนคุมเลขรหัสหนังสือ (Online)", layout="wide")

# 🎨 ธีมเขียว-ฟ้า
def add_custom_design():
    st.markdown("""
    <style>
    .stApp { background-color: #F1F8E9; }
    [data-testid="stSidebar"] { background-color: #E0F7FA; border-right: 2px solid #80DEEA; }
    h1 { color: #2E7D32 !important; text-shadow: 1px 1px 2px #A5D6A7; font-family: 'Sarabun', sans-serif; }
    h2, h3 { color: #00695C !important; }
    .stButton>button {
        background: linear-gradient(to right, #66BB6A, #26A69A); color: white; border-radius: 25px;
        border: none; padding: 10px 28px; box-shadow: 0 4px 10px rgba(38, 166, 154, 0.3); font-weight: bold;
    }
    .stButton>button:hover { transform: scale(1.05); }
    .stTextInput>div>div>input { border-radius: 12px; border: 1px solid #81C784; }
    [data-testid="stDataFrame"] { border-radius: 10px; overflow: hidden; border: 1px solid #80CBC4; }
    .stToast { background-color: #E8F5E9; border: 1px solid #A5D6A7; }
    </style>
    """, unsafe_allow_html=True)

add_custom_design()

# ==========================================
# ⚙️ ตั้งค่า (Google Sheets)
# ==========================================
SHEET_URL = "https://docs.google.com/spreadsheets/d/1DziBBBmgoXnUiX-MvsuUGTIEHvD5Jqjib1Ap-bmhins/edit?usp=sharing"
KEY_FILE = "credentials.json"

# --- ฟังก์ชันช่วยเรียงลำดับ ---
def natural_sort_key(s):
    return [int(text) if text.isdigit() else text.lower()
            for text in re.split('([0-9]+)', str(s))]

def get_last_id_in_category(df, prefix):
    df['เลขรหัส'] = df['เลขรหัส'].astype(str).str.strip()
    mask = df['เลขรหัส'].str.startswith(prefix)
    filtered = df[mask].copy()
    if filtered.empty: return "ยังไม่มีข้อมูล"
    sorted_ids = sorted(filtered['เลขรหัส'].tolist(), key=natural_sort_key)
    return sorted_ids[-1] if sorted_ids else "ไม่พบ"

# --- เชื่อมต่อ Google Sheets (รองรับทั้ง Cloud และ Local) ---
@st.cache_resource
def init_connection():
    scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    
    try:
        # 1. ลองเช็คว่ารันบน Streamlit Cloud หรือไม่ (ใช้ st.secrets)
        if "gcp_service_account" in st.secrets:
            creds_dict = st.secrets["gcp_service_account"]
            creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
        # 2. ถ้าไม่ใช่ Cloud ให้หาไฟล์ credentials.json ในเครื่อง
        else:
            creds = ServiceAccountCredentials.from_json_keyfile_name(KEY_FILE, scope)
            
        client = gspread.authorize(creds)
        return client
    except Exception as e:
        st.error(f"❌ เชื่อมต่อไม่ได้: {e}")
        st.stop()

def load_data():
    try:
        client = init_connection()
        sheet = client.open_by_url(SHEET_URL).sheet1
        data = sheet.get_all_records()
        if not data: return pd.DataFrame(columns=['เลขรหัส', 'สถานะ', 'ชื่อ', 'นามสกุล', 'เงินช่วยพิเศษ', 'บำเหน็จตกทอด', 'หมายเหตุ'])
        df = pd.DataFrame(data).astype(str)
        # Big Cleaning
        for col in df.columns: df[col] = df[col].str.strip()
        df = df[df['เลขรหัส'] != 'เลขรหัส']
        df = df[df['เลขรหัส'] != '']
        df = df.loc[:, ~df.columns.str.contains('^Unnamed')]
        return df
    except Exception as e:
        st.error(f"❌ โหลดข้อมูลไม่ได้: {e}")
        return pd.DataFrame()

def save_to_gsheet(df):
    try:
        client = init_connection()
        sheet = client.open_by_url(SHEET_URL).sheet1
        df['temp_sort'] = df['เลขรหัส'].apply(natural_sort_key)
        df_sorted = df.sort_values('temp_sort').drop(columns=['temp_sort'])
        header = df_sorted.columns.tolist()
        values = df_sorted.values.tolist()
        sheet.clear()
        sheet.update(range_name='A1', values=[header] + values)
        return df_sorted
    except Exception as e:
        st.error(f"❌ บันทึกข้อมูลไม่สำเร็จ: {e}")
        return df

# ==========================================
# 🛠️ ฟังก์ชัน Callback
# ==========================================
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

    new_row = {
        'เลขรหัส': new_id, 'สถานะ': new_status, 
        'ชื่อ': new_name, 'นามสกุล': new_surname, 
        'เงินช่วยพิเศษ': new_special, 'บำเหน็จตกทอด': new_pension, 
        'หมายเหตุ': new_note
    }
    
    df = st.session_state.df
    updated_df = pd.concat([df, pd.DataFrame([new_row])], ignore_index=True)
    
    st.toast("⏳ กำลังบันทึกข้อมูล... กรุณารอสักครู่")
    saved_df = save_to_gsheet(updated_df)
    st.session_state.df = saved_df
    
    keys_to_clear = ['input_id', 'input_name', 'input_surname', 'input_status', 'input_note']
    for key in keys_to_clear:
        if key in st.session_state:
            st.session_state[key] = ""
            
    st.toast(f"✅ บันทึกคุณ {new_name} เรียบร้อยแล้ว!")

# --- หน้าจอหลัก ---
st.title("📂 ระบบทะเบียนคุมเลขรหัสหนังสือ")
st.caption(f"☁️ เชื่อมต่อ Google Sheets | 🌿 Theme: Green & Blue")

if 'df' not in st.session_state:
    with st.spinner("กำลังโหลดข้อมูล..."):
        st.session_state.df = load_data()

df = st.session_state.df

with st.sidebar:
    if st.button("🔄 รีโหลดและล้างข้อมูลขยะ"):
        st.cache_data.clear()
        st.session_state.df = load_data()
        st.success("อัปเดตแล้ว")
        st.rerun()

tab1, tab2, tab3 = st.tabs(["🔍 ค้นหาข้อมูล", "📝 บันทึกข้อมูลใหม่", "💾 ดูตารางรวม"])

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

with tab2:
    st.subheader("📝 เพิ่มข้อมูลใหม่")
    col_id_input, col_id_hint = st.columns([1, 2])
    
    with col_id_input:
        new_id_input = st.text_input("1. ระบุเลขรหัสใหม่ (จำเป็น) *", 
                                     placeholder="เช่น ก. หรือ ข.10", 
                                     key="input_id")
    is_duplicate = False
    with col_id_hint:
        if new_id_input:
            if new_id_input in df['เลขรหัส'].values:
                st.error(f"❌ รหัส '{new_id_input}' มีอยู่แล้ว!")
                is_duplicate = True
            else:
                match = re.match(r"([ก-ฮa-zA-Z]+\.?)", new_id_input)
                prefix = match.group(1) if match else (new_id_input[0] if len(new_id_input) > 0 else "")
                if prefix:
                    last_used = get_last_id_in_category(df, prefix)
                    st.info(f"💡 หมวด **'{prefix}'** ล่าสุดถึงเลขที่: **{last_used}**")
        else: st.write("") 

    col_name, col_surname = st.columns(2)
    with col_name: new_name = st.text_input("2. ชื่อ (จำเป็น) *", key="input_name").strip()
    with col_surname: new_surname = st.text_input("3. นามสกุล (จำเป็น) *", key="input_surname").strip()

    col_status, col_money, col_pension = st.columns(3)
    with col_status:
        new_status = st.selectbox("4. สถานะ (จำเป็น) *", ["", "ขรก.", "ลจ.", "กนก.", "พรก.", "อื่นๆ"], key="input_status")
    with col_money: new_special = st.selectbox("เงินช่วยพิเศษ", ["มี", "ไม่มี", "-"], key="input_special")
    with col_pension: new_pension = st.selectbox("บำเหน็จตกทอด", ["มี", "ไม่มี", "-"], key="input_pension")

    new_note = st.text_area("หมายเหตุ", key="input_note")
    st.write("---")
    
    st.button("💾 บันทึกข้อมูลขึ้น Cloud", 
              type="primary", 
              disabled=is_duplicate, 
              on_click=submit_callback)

with tab3:
    st.write(f"ข้อมูลทั้งหมด {len(df)} รายการ")
    st.dataframe(df, use_container_width=True)
