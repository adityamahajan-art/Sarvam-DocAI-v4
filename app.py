import os
import time
import io
import zipfile
import requests
from flask import Flask, request, jsonify, render_template
from pypdf import PdfReader, PdfWriter

app = Flask(__name__, template_folder='templates')

SARVAM_API_KEY = os.environ.get('SARVAM_API_KEY', 'sk_fdg59vzl_ps5KceuTpgH1aYuikIPo53yD')
SARVAM_CHAT_ENDPOINT = 'https://api.sarvam.ai/v1/chat/completions'
SARVAM_DOC_ENDPOINT = 'https://api.sarvam.ai/doc-digitization/job/v1'

SYSTEM_PROMPT = """You are a strict Document Intelligence Engine designed to parse text data and isolate highly specific parameters from financial/identity documents.
Assume the current evaluation year is 2026. Identify the precise document category and isolate the properties mapped out below.

### MODULE 1: IDENTITY & ADDRESS PROOFS
- PAN Card: "pan_number" (10-char alpha-numeric regex matching ^[A-Z]{5}[0-9]{4}[A-Z]{1}$), "full_name", "father_name", "date_of_birth", "taxpayer_status" (extracted from the 4th character).
- Aadhaar Card: "aadhaar_number" (12 digits), "full_name", "date_of_birth", "gender", "address_raw", "pin_code" (last 6 digits of address).
- Driving License & Passport: "id_number", "full_name", "date_of_birth", "expiry_date" (Flag if expired relative to 2026).
- Utility Bills (Electricity, Water, Gas): "consumer_number", "billing_name", "billing_address", "bill_date" (Flag if older than 3 months relative to 2026).

### MODULE 2: INCOME & BUSINESS PROOFS (SALARIED & SELF-EMPLOYED)
- Salary Slip: "employee_name", "employer_name", "salary_month_year", "gross_salary", "net_salary", "provident_fund_deduction".
- Udyam Certificate / Business Registration: "enterprise_name", "udyam_number", "enterprise_type", "owner_name", "registration_date".
- GST Registration Certificate: "gstin", "legal_name", "trade_name", "validity_period", "principal_place_of_business".
- Partnership Deed / MOA / AOA: "entity_name", "cin_or_registration_number", "directors_or_partners_names".
- Shop & Establishment Certificate: "establishment_name", "registration_number", "employer_name", "valid_upto_date".
- Financial Statements: "entity_name", "financial_year", "total_turnover", "net_profit".
- GST Returns (GSTR 1 & 3B): "gstin", "tax_period", "total_taxable_value", "total_tax_paid".
- Bank Statement: "account_holder_name", "account_number", "ifsc_code", "opening_balance", "closing_balance", "salary_or_business_credits".
- Form 16 & ITR: "assessment_year", "pan_of_assessee", "tan_of_employer" (if Form 16), "gross_total_income", "net_taxable_income".

### MODULE 3: PROPERTY DOCUMENTS
- Registered Sale/Conveyance Deed: "deed_type", "document_number", "execution_date", "vendor_names", "vendee_names", "property_schedule_details".
- Society NOC & Allotment Letter: "issuing_authority", "allottee_name", "unit_details", "noc_status".

CRITICAL JSON RULES:
1. You MUST output STRICTLY valid JSON.
2. ALL property keys MUST be enclosed in double quotes ("). Do not use single quotes.
3. Output exactly this format and nothing else:
{
  "document_type": "The specific localized class identity parsed (e.g., PAN Card)",
  "summary": "Provide a descriptive 2-line processing note. Include validation alerts.",
  "fields": [
    {
      "label": "exact_parameter_key_name",
      "value": "extracted raw value or null",
      "type": "one of: name|date|amount|contact|id|location|category|other"
    }
  ]
}

Ensure the types align correctly (name, date, amount, id, location, other). Return ONLY the raw JSON object."""

def process_doc_digitization(file_bytes, original_filename):
    """Handles the Sarvam Document Digitization pipeline for Images & Scanned PDFs."""
    headers = {"api-subscription-key": SARVAM_API_KEY, "Content-Type": "application/json"}
    
    if original_filename.lower().endswith(('.png', '.jpg', '.jpeg')):
        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, 'a', zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(original_filename, file_bytes)
        file_bytes = zip_buffer.getvalue()
        target_filename = "document.zip"
    else:
        target_filename = original_filename

    print("[DIGITIZE] 📄 Initializing Sarvam Vision job...")
    init_res = requests.post(SARVAM_DOC_ENDPOINT, headers=headers, json={"job_parameters": {"output_format": "md"}})
    if not init_res.ok: raise Exception(f"Init Error: {init_res.text}")
    job_id = init_res.json()["job_id"]

    up_res = requests.post(f"{SARVAM_DOC_ENDPOINT}/upload-files", headers=headers, json={"job_id": job_id, "files": [target_filename]})
    if not up_res.ok: raise Exception(f"Upload Link Error: {up_res.text}")
    upload_url = up_res.json()["upload_urls"][target_filename]["file_url"]

    print("[DIGITIZE] ☁️ Uploading file to Azure...")
    upload_headers = {"Content-Type": "application/octet-stream", "x-ms-blob-type": "BlockBlob"}
    upload_req = requests.put(upload_url, data=file_bytes, headers=upload_headers)
    if not upload_req.ok: raise Exception(f"Azure Upload Error: {upload_req.text}")

    print("[DIGITIZE] ⚙️ Starting extraction engine...")
    start_res = requests.post(f"{SARVAM_DOC_ENDPOINT}/{job_id}/start", headers=headers)
    if not start_res.ok: raise Exception(f"Sarvam Start Error: {start_res.text}")

    timeout_counter = 0
    while True:
        time.sleep(3)
        timeout_counter += 3
        if timeout_counter > 45: 
            raise Exception("Digitization polling exceeded 45 seconds.")

        stat_res = requests.get(f"{SARVAM_DOC_ENDPOINT}/{job_id}/status", headers=headers)
        if not stat_res.ok: raise Exception(f"Status Request Error: {stat_res.text}")
        
        stat_data = stat_res.json()
        state = stat_data["job_state"]

        if state == "Completed":
            break
        elif state in ["Failed", "PartiallyCompleted"]:
            raise Exception(f"Job failed: {stat_data.get('error_message', 'Unknown error')}")

    down_res = requests.post(f"{SARVAM_DOC_ENDPOINT}/{job_id}/download-files", headers=headers)
    if not down_res.ok: raise Exception(f"Download Link Error: {down_res.text}")
    download_url = list(down_res.json()["download_urls"].values())[0]["file_url"]

    print("[DIGITIZE] 📥 Downloading and extracting markdown text...")
    zip_resp = requests.get(download_url)
    if not zip_resp.ok: raise Exception(f"ZIP Download Error: {zip_resp.text}")

    with zipfile.ZipFile(io.BytesIO(zip_resp.content)) as zf:
        for name in zf.namelist():
            if name.endswith('.md'):
                return zf.read(name).decode('utf-8')
                
    raise Exception("No markdown file found in output.")

def process_pdf_smart_router(file_bytes, original_filename):
    """Detects Native vs Scanned PDFs. Extracts text directly or routes to Sarvam."""
    pdf_stream = io.BytesIO(file_bytes)
    reader = PdfReader(pdf_stream)
    num_pages = len(reader.pages)
    
    # Try to extract text from the first 5 pages to test if it's native
    test_text = ""
    for i in range(min(num_pages, 5)):
        test_text += reader.pages[i].extract_text() + "\n"
        
    # If we extracted a decent chunk of text, it's a Native PDF!
    if len(test_text.strip()) > 100:
        print("[ROUTER] ⚡ Native PDF detected! Bypassing Sarvam OCR.")
        full_text = ""
        for page in reader.pages:
            full_text += page.extract_text() + "\n"
        return full_text
        
    # Otherwise, it's a scanned PDF
    print("[ROUTER] 📸 Scanned PDF detected. Preparing for Sarvam OCR...")
    
    # Safeguard: If the scanned PDF is over 10 pages, slice it so Sarvam doesn't crash
    if num_pages > 10:
        print("[ROUTER] ✂️ PDF exceeds 10 pages. Slicing to the first 10 pages...")
        writer = PdfWriter()
        for i in range(10):
            writer.add_page(reader.pages[i])
        sliced_stream = io.BytesIO()
        writer.write(sliced_stream)
        file_bytes = sliced_stream.getvalue()
        target_filename = "sliced_" + original_filename
    else:
        target_filename = original_filename
        
    return process_doc_digitization(file_bytes, target_filename)

@app.route('/')
def home():
    return render_template('index.html')

@app.route('/api/extract', methods=['POST'])
def extract_data():
    if 'document' not in request.files:
        return jsonify({"error": "No file uploaded in the request"}), 400

    file = request.files['document']
    filename = file.filename
    ext = filename.split('.')[-1].lower()
    
    try:
        if ext in ['txt', 'html', 'json']:
            document_text = file.read().decode('utf-8', errors='ignore')
        elif ext == 'pdf':
            # Run PDF through the Smart Router
            file_bytes = file.read()
            document_text = process_pdf_smart_router(file_bytes, filename)
        elif ext in ['png', 'jpg', 'jpeg']:
            file_bytes = file.read()
            document_text = process_doc_digitization(file_bytes, filename)
        else:
            return jsonify({"error": "Unsupported file format"}), 400
    except Exception as e:
        return jsonify({"error": f"Digitization error: {str(e)}"}), 500

    # Ensure payload remains lightweight
    truncated_text = document_text[:5000]
    user_prompt = f"Extract context and execute parameter tracking for the attached document payload:\n\n{truncated_text}"

    sarvam_payload = {
        "model": "sarvam-m", 
        "max_tokens": 2000,
        "temperature": 0.1,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT + "\n\nCRITICAL: Keep your <think> reasoning extremely brief (under 3 sentences). You MUST finish writing the JSON dictionary."},
            {"role": "user", "content": user_prompt}
        ]
    }

    headers = {
        "Content-Type": "application/json",
        "api-subscription-key": SARVAM_API_KEY
    }

    try:
        print("[BACKEND] 🚀 Routing to Sarvam Chat LLM...")
        session = requests.Session()
        session.trust_env = False
        
        response = session.post(SARVAM_CHAT_ENDPOINT, json=sarvam_payload, headers=headers, timeout=60)
        response.raise_for_status()
        
        return jsonify(response.json())

    except requests.exceptions.Timeout:
        return jsonify({"error": "Render server connection timed out during extraction."}), 504
    except requests.exceptions.RequestException as e:
        error_response = response.text if 'response' in locals() else str(e)
        return jsonify({"error": f"API transmission anomaly: {error_response}"}), 500

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)
