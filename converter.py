import streamlit as st
import pdfplumber
import pandas as pd
import re
import io

# --- CONFIGURATION ---
THICKNESS_MM = 25.4  

def round_to_nearest_5(value):
    return int(round(value / 5.0) * 5)

def get_closest_width(num_x, header_map):
    if not header_map: return None
    closest_width = min(header_map.keys(), key=lambda w: abs(header_map[w] - num_x))
    if abs(header_map[closest_width] - num_x) > 40: return None
    return closest_width

# --- COSTING ENGINE (SOUTH AFRICAN MATRIX) ---
def get_ash_rate(grade, thickness_mm, length_m):
    length_cat = 1 if length_m >= 3.0 else 0
    matrix = {
        "Super Prime White": {27: [1320, 1430], 33: [1320, 1430], 40: [1380, 1490], 52: [1430, 1540], 65: [1820, 1870], 80: [1980, 2040]},
        "Prime White 1 Face": {40: [950, 1000], 52: [950, 1000]},
        "Prime CND": {33: [870, 920], 40: [880, 940], 52: [940, 980], 65: [1050, 1090], 80: [1100, 1150]},
        "FAS": {27: [880, 940], 33: [940, 990], 40: [960, 1130]}
    }
    if grade in matrix:
        t_keys = list(matrix[grade].keys())
        closest_t = min(t_keys, key=lambda x: abs(x - thickness_mm))
        return float(matrix[grade][closest_t][length_cat])
    return None

# --- AMERICAN PARSER ---
def parse_american(pdf_file):
    extracted_rows = []
    total_words = 0
    current_bundle = None 
    
    with pdfplumber.open(pdf_file) as pdf:
        for page in pdf.pages:
            words = page.extract_words(keep_blank_chars=False)
            total_words += len(words)
            
            header_data = {} 
            for w in words:
                clean_text = re.sub(r"[^\d]", "", w['text'])
                if clean_text.isdigit():
                    val = int(clean_text)
                    if 3 <= val <= 16:
                        if val not in header_data or w['top'] < header_data[val]['top']:
                            header_data[val] = {'x': (w['x0'] + w['x1']) / 2, 'top': w['top']}
            
            header_map = {k: v['x'] for k, v in header_data.items()}
            
            lines = {}
            for w in words:
                y_loc = round(w['top'] / 5) * 5
                if y_loc not in lines: lines[y_loc] = []
                lines[y_loc].append(w)
            
            sorted_y = sorted(lines.keys())
            
            for y in sorted_y:
                line_words = sorted(lines[y], key=lambda x: x['x0'])
                line_text = " ".join([w['text'] for w in line_words])
                
                if "Width" in line_text or "Specification" in line_text or "Total" in line_text: 
                    continue
                
                bundle_match = re.search(r"^([A-Za-z]?\d{4,}(?:\s+[A-Za-z]+)?)\b", line_text.strip())
                if bundle_match: 
                    current_bundle = bundle_match.group(1).strip()
                if not current_bundle: 
                    continue

                bundle_digits = re.sub(r"[^\d]", "", current_bundle)

                numeric_words = []
                for w in line_words:
                    clean_text = re.sub(r"[^\d.]", "", w['text'])
                    if clean_text.replace('.', '').isdigit():
                        if clean_text == bundle_digits:
                            continue
                        numeric_words.append(w)

                if len(numeric_words) < 3: 
                    continue 

                try:
                    vol_word = numeric_words[-1]
                    pcs_word = numeric_words[-2]
                    len_word = numeric_words[0]
                    
                    total_m3 = float(vol_word['text'])
                    total_pcs = int(float(pcs_word['text']))
                    length = int(re.sub(r"[^\d]", "", len_word['text']))
                except: 
                    continue

                row_data = {
                    "Category": "American Hardwood",
                    "Bundle": current_bundle, 
                    "Length_Ft": length, 
                    "Pieces_Per_Length": total_pcs, 
                    "M3_Per_Length": total_m3
                }
                for i in range(3, 17): 
                    row_data[i] = 0

                width_words = [
                    w for w in numeric_words 
                    if w['x0'] > len_word['x1'] and w['x0'] < pcs_word['x0']
                ]
                for w in width_words:
                    try:
                        val = int(float(w['text']))
                        matched_width = get_closest_width((w['x0'] + w['x1']) / 2, header_map)
                        if matched_width: 
                            row_data[matched_width] = val
                    except: 
                        pass
                extracted_rows.append(row_data)
                
    if total_words < 20:
        raise ValueError("SCANNED_PDF")
        
    return pd.DataFrame(extracted_rows)

# --- EUROPEAN PARSERS (UPGRADED TO CAPTURE MIXED LOADS) ---
def parse_european(pdf_file):
    extracted_rows = []
    total_words = 0
    current_category = "EUR-HARDWOOD"
    
    with pdfplumber.open(pdf_file) as pdf:
        for page in pdf.pages:
            words = page.extract_words(keep_blank_chars=False)
            total_words += len(words)
            if not words: continue
            
            lines = {}
            for w in words:
                y_loc = round(w['top'] / 5) * 5
                if y_loc not in lines: lines[y_loc] = []
                lines[y_loc].append(w)
                
            sorted_y = sorted(lines.keys())
            
            for y in sorted_y:
                line_words = sorted(lines[y], key=lambda x: x['x0'])
                line_text = " ".join([w['text'] for w in line_words]).strip()
                
                # Check for category change (e.g. "S/E EUR. OAK 27 MM")
                if "MM" in line_text and ("OAK" in line_text or "ASH" in line_text or "BEECH" in line_text):
                    cat_match = re.search(r"(?:S/E\s+)?EUR\.?\s+(?:OAK|ASH|BEECH)\s+\d+\s*MM", line_text)
                    if cat_match:
                        current_category = cat_match.group(0)

                bundle_pattern = r"^(?:\d{2,4}-)?\d{4,}[A-Za-z]?$"
                
                for i, w in enumerate(line_words):
                    if re.match(bundle_pattern, w['text'].strip()):
                        b_text = w['text'].strip()
                        bx = w['x0']
                        by = w['top']
                        
                        right_words = [rw for rw in words if rw['x0'] > bx + 5 and abs(rw['top'] - by) < 15]
                        right_words = sorted(right_words, key=lambda rw: rw['x0'])
                        
                        numerics = []
                        for rw in right_words:
                            clean_num = rw['text'].replace(',', '.').strip(" .")
                            if clean_num.replace('.', '', 1).isdigit():
                                numerics.append(clean_num)
                                
                        if len(numerics) >= 2:
                            try:
                                pieces = int(float(numerics[0]))
                                m3 = float(numerics[1])
                                if pieces > 0 and m3 > 0 and pieces > m3:
                                    if not any(r['Bundle'] == b_text for r in extracted_rows):
                                        extracted_rows.append({
                                            "Category": current_category,
                                            "Bundle": b_text, 
                                            "Pieces_Per_Length": pieces, 
                                            "M3_Per_Length": m3
                                        })
                            except: pass
    if total_words < 20: raise ValueError("SCANNED_PDF")
    return pd.DataFrame(extracted_rows)

def parse_european_txt(txt_file):
    extracted_rows = []
    content = txt_file.getvalue().decode("utf-8")
    lines = content.split('\n')
    
    current_category = "EUR-HARDWOOD"
    bundle_pattern = r"^(?:\d{2,4}-)?\d{4,}[A-Za-z]?$"
    
    for line in lines:
        line_clean = line.strip()
        # Look for category change
        if "MM" in line_clean and ("OAK" in line_clean or "ASH" in line_clean or "BEECH" in line_clean):
            current_category = line_clean
            
        words = line_clean.split()
        for i, w in enumerate(words):
            if re.match(bundle_pattern, w.strip()):
                b_text = w.strip()
                numerics = []
                for j in range(1, 10):
                    if i + j < len(words):
                        clean_num = words[i+j].replace(',', '.').strip(" .")
                        if clean_num.replace('.', '', 1).isdigit():
                            numerics.append(clean_num)
                if len(numerics) >= 2:
                    try:
                        pieces = int(float(numerics[0]))
                        m3 = float(numerics[1])
                        if pieces > 0 and m3 > 0 and pieces > m3:
                            if not any(r['Bundle'] == b_text for r in extracted_rows):
                                extracted_rows.append({
                                    "Category": current_category,
                                    "Bundle": b_text, 
                                    "Pieces_Per_Length": pieces, 
                                    "M3_Per_Length": m3
                                })
                    except: pass
    return pd.DataFrame(extracted_rows)

# --- METRIC PIECE GENERATOR ---
def generate_metric_list(df, is_american, enable_costing, zar_rate, species, grade, base_rate, apply_oak):
    if not is_american:
        return pd.DataFrame({"Notice": ["Individual piece widths are not provided by European suppliers. Please use the Sage Import tab to receive the full Bundle Volume into stock. Warehouse staff will measure actual piece widths upon picking."]})
        
    piece_rows = []
    for index, row in df.iterrows():
        bundle = row['Bundle']
        length_m = round(row['Length_Ft'] * 0.3048, 3)
        length_code = int(length_m * 10)
        
        for width_inch in range(3, 17):
            if width_inch in df.columns:
                qty = row[width_inch]
                if pd.notna(qty) and qty > 0:
                    exact_width_mm = width_inch * 25.4
                    rounded_width_mm = round_to_nearest_5(exact_width_mm)
                    unit_volume_m3 = (THICKNESS_MM / 1000) * (rounded_width_mm / 1000) * length_m
                    total_volume_m3 = unit_volume_m3 * qty
                    
                    stock_code = f"RSHOC{int(THICKNESS_MM)}{rounded_width_mm}{length_code}"
                    desc = f"American White Oak Comsel {rounded_width_mm}mm x {length_m:.3f}m x {int(THICKNESS_MM)}mm x {round(unit_volume_m3, 5)}m3"
                    
                    row_data = {
                        "Code": stock_code,
                        "Bundle": bundle, 
                        "Description": desc, 
                        "Width_mm": rounded_width_mm, 
                        "Length_m": length_m, 
                        "Height_mm": int(THICKNESS_MM),
                        "Unit_Volume_m3": round(unit_volume_m3, 5), 
                        "Quantity": int(qty),
                        "Total_Volume_m3": round(total_volume_m3, 4)
                    }
                    
                    if enable_costing:
                        rate_eur = base_rate
                        
                        if species == "Euro White Ash":
                            ash_rate = get_ash_rate(grade, THICKNESS_MM, length_m)
                            if ash_rate: rate_eur = ash_rate
                            
                        if apply_oak and rounded_width_mm >= 240:
                            if length_m >= 4.2: rate_eur *= 1.60
                            elif length_m >= 3.6: rate_eur *= 1.40
                            else: rate_eur *= 1.30
                                
                        piece_cost_eur = rate_eur * total_volume_m3
                        row_data["Rate_Foreign_per_M3"] = round(rate_eur, 2)
                        row_data["Total_Cost_Foreign"] = round(piece_cost_eur, 2)
                        row_data["Total_Cost_ZAR"] = round(piece_cost_eur * zar_rate, 2)
                        
                    piece_rows.append(row_data)
    return pd.DataFrame(piece_rows)

# --- STREAMLIT UI ---
st.set_page_config(page_title="Timber Packing List Converter", layout="wide")

# --- SIDEBAR COSTING MODULE ---
st.sidebar.header("💰 Financial Costing (Sage Valuation)")
enable_costing = st.sidebar.checkbox("Enable CBM Costing & ZAR Conversion")

zar_rate = 1.0
species = "Other"
grade = "Standard"
base_rate = 0.0
apply_oak = False

if enable_costing:
    zar_rate = st.sidebar.number_input("Exchange Rate (Set to 1.0 if Local ZAR)", value=1.00, step=0.01)
    species = st.sidebar.selectbox("Timber Species", ["Euro White Ash", "Euro White Oak", "Euro Beech", "Other / Custom"])
    
    if species == "Euro White Ash":
        grade = st.sidebar.selectbox("Ash Grade", ["Super Prime White", "Prime White 1 Face", "Prime CND", "FAS"])
        st.sidebar.success("✨ **Ash Matrix Pre-Loaded:**\nThe Base Rate will automatically calculate for every board based on its exact length and grade.")
    elif species == "Euro White Oak":
        base_rate = st.sidebar.number_input("Base Rate per m³ (Foreign/ZAR)", value=940.0, step=10.0)
        apply_oak = st.sidebar.checkbox("Apply Oak Surcharges (XW, XWL, XXL)", value=True)
        st.sidebar.success("✨ **Oak Rules Pre-Loaded:**\n+30%, +40%, and +60% surcharges will automatically be applied to the base rate for any boards wider than 240mm.")
    else:
        base_rate = st.sidebar.number_input("Base Rate per m³ (Foreign/ZAR)", value=21500.0, step=100.0)
        st.sidebar.info("Tip: For local Tradelink shipments, enter your ZAR base rate here and leave Exchange Rate at 1.0")

st.title("🌲 Timber Packing List Converter (v22)")

mode = st.radio("Select Packing List Origin:", ("American (Imperial Detail)", "European (Metric Summary)"))
is_american = (mode == "American (Imperial Detail)")

uploaded_file = st.file_uploader(f"Upload {mode.split()[0]} Document (.pdf or .txt)", type=["pdf", "txt"])

if uploaded_file:
    file_ext = uploaded_file.name.split('.')[-1].lower()
    
    with st.spinner("Processing & Calculating Costs..."):
        try:
            if is_american:
                if file_ext == 'txt':
                    st.error("⚠️ **American Format Requires PDF**\nPlease upload the PDF version to preserve column spacing.")
                    st.stop()
                else:
                    df = parse_american(uploaded_file)
            else:
                if file_ext == 'txt':
                    df = parse_european_txt(uploaded_file)
                else:
                    df = parse_european(uploaded_file)
            
            if df.empty:
                st.error("No data found. Ensure you selected the correct Origin above.")
            else:
                st.success(f"Success! Processed {len(df)} bundles.")

                # 1. NETT TALLY (SAGE GRV)
                sage_df = df.groupby(["Category", "Bundle"]).agg({"Pieces_Per_Length": "sum", "M3_Per_Length": "sum"}).reset_index()
                sage_df.rename(columns={"Pieces_Per_Length": "Nett_Total_Pieces", "M3_Per_Length": "Nett_Tally_M3"}, inplace=True)
                sage_df["StockCode"] = "OAK-25MM-COMSEL" if is_american else "EUR-HARDWOOD"
                sage_df["Nett_Tally_M3"] = sage_df["Nett_Tally_M3"].round(4)
                
                # 2. PICKING SLIP (With Costing per piece)
                metric_df = generate_metric_list(df, is_american, enable_costing, zar_rate, species, grade, base_rate, apply_oak)

                # --- SAGE FINANCIAL CALCULATIONS ---
                if enable_costing:
                    if is_american:
                        bundle_costs = metric_df.groupby('Bundle')[['Total_Cost_Foreign', 'Total_Cost_ZAR']].sum().reset_index()
                        sage_df = sage_df.merge(bundle_costs, on='Bundle', how='left')
                        sage_df['Average_Unit_Cost_Foreign'] = round(sage_df['Total_Cost_Foreign'] / sage_df['Nett_Tally_M3'], 2)
                        
                        cols = ["StockCode", "Category", "Bundle", "Nett_Total_Pieces", "Nett_Tally_M3", "Average_Unit_Cost_Foreign", "Total_Cost_Foreign", "Total_Cost_ZAR"]
                        sage_df = sage_df[cols]
                    else:
                        sage_df['Base_Rate_Foreign'] = base_rate
                        sage_df['Total_Cost_Foreign'] = round(sage_df['Nett_Tally_M3'] * base_rate, 2)
                        sage_df['Total_Cost_ZAR'] = round(sage_df['Total_Cost_Foreign'] * zar_rate, 2)

                # 3. LENGTH SPREAD
                if is_american:
                    width_cols = sorted([c for c in df.columns if isinstance(c, int)])
                    cols = ["Category", "Bundle", "Length_Ft", "Pieces_Per_Length", "M3_Per_Length"] + width_cols
                    tally_df = df[cols]
                else:
                    tally_df = sage_df.copy() 

                # --- DISPLAY ---
                tab1, tab2, tab3 = st.tabs(["1. Nett Tally (Sage GRV)", "2. Length Spread & Tally", "3. Picking Slip (Detailed)"])
                
                with tab1:
                    st.info("This is the full bundle volume (and calculated ZAR cost) to be received directly into Sage.")
                    st.dataframe(sage_df, use_container_width=True)
                    st.download_button("⬇️ Download Sage Import (.csv)", sage_df.to_csv(index=False).encode('utf-8'), "sage_import.csv", "text/csv")
                
                with tab2:
                    st.dataframe(tally_df, use_container_width=True)
                    buffer_tally = io.BytesIO()
                    with pd.ExcelWriter(buffer_tally, engine='openpyxl') as writer:
                        tally_df.to_excel(writer, index=False)
                    st.download_button("⬇️ Download Length Spread (.xlsx)", buffer_tally, "length_spread.xlsx")
                    
                with tab3:
                    if not is_american:
                        st.info("Notice: European suppliers do not provide piece-by-piece width data.")
                    st.dataframe(metric_df, use_container_width=True)
                    if is_american:
                        buffer_picking = io.BytesIO()
                        with pd.ExcelWriter(buffer_picking, engine='openpyxl') as writer:
                            metric_df.to_excel(writer, index=False)
                        st.download_button("⬇️ Download Picking Slip (.xlsx)", buffer_picking, "warehouse_picking_slip.xlsx")

        except ValueError as e:
            if str(e) == "SCANNED_PDF":
                st.error("📸 **Scanned Image Detected!**\nPlease run this file through an online Image-to-Text converter, save it as a .txt file, and upload it here!")
            else:
                st.error(f"Error processing file: {e}")
        except Exception as e:
            st.error(f"Error processing file: {e}")
