# Standard library imports
import os
import sys
import base64
import time
import logging
import sqlite3

# Third-party imports
from mistralai import Mistral
import pypandoc
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Configuration
DOC_DIR = "docs_import"
EXPORT_DIR = "docs_exports"
DB_FILE = "processed_files.db"
LOG_FILE = "conversion.log"
MAX_RETRIES = 5
INITIAL_BACKOFF = 1  # in seconds

# Initialize logging
logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format="%(asctime)s %(levelname)s: %(message)s",
)

# Ensure API key is set via environment variable
API_KEY = os.getenv("MISTRAL_API_KEY")
if not API_KEY:
    print("Error: MISTRAL_API_KEY environment variable not set.")
    sys.exit(1)

client = Mistral(api_key=API_KEY)


def init_database():
    """Initialize the SQLite database and create table if it doesn't exist."""
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS processed_files (
            filename TEXT PRIMARY KEY,
            status TEXT NOT NULL,
            attempts INTEGER NOT NULL,
            error TEXT,
            processed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    conn.commit()
    conn.close()


def load_processed():
    """Load processed file records from SQLite database into a dict."""
    init_database()
    processed = {}
    
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute('SELECT filename, status, attempts, error FROM processed_files')
    
    for row in cursor.fetchall():
        filename, status, attempts, error = row
        processed[filename] = {
            'filename': filename,
            'status': status,
            'attempts': str(attempts),
            'error': error or ''
        }
    
    conn.close()
    return processed


def update_db(record):
    """Update or insert a processing record into the SQLite database."""
    init_database()
    
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    
    # Use INSERT OR REPLACE for atomic upsert operation
    cursor.execute('''
        INSERT OR REPLACE INTO processed_files (filename, status, attempts, error)
        VALUES (?, ?, ?, ?)
    ''', (record['filename'], record['status'], int(record['attempts']), record['error']))
    
    conn.commit()
    conn.close()


def ensure_export_directory():
    """Ensure the export directory exists."""
    if not os.path.exists(EXPORT_DIR):
        os.makedirs(EXPORT_DIR)
        print(f"Created export directory: {EXPORT_DIR}")


def get_pdf_files():
    """List all PDF files in the DOC_DIR folder and its subdirectories."""
    if not os.path.isdir(DOC_DIR):
        os.makedirs(DOC_DIR)
        print(
            f"Directory '{DOC_DIR}' was not found, so I created it for you. Please put your PDF files inside and run again."
        )
        sys.exit(1)

    pdf_files = []
    for root, dirs, files in os.walk(DOC_DIR):
        for file in files:
            if file.lower().endswith(".pdf"):
                rel_path = os.path.relpath(os.path.join(root, file), DOC_DIR)
                pdf_files.append(rel_path)

    return pdf_files


def encode_pdf(pdf_path):
    """Encode the PDF file to a base64 string."""
    try:
        with open(pdf_path, "rb") as pdf_file:
            return base64.b64encode(pdf_file.read()).decode("utf-8")
    except Exception as e:
        logging.error(f"Failed to encode {pdf_path}: {e}")
        return None


def convert_pdf_to_markdown(pdf_filename):
    """Perform OCR on the PDF, save as Markdown, and convert to Word safely."""
    full_path = os.path.join(DOC_DIR, pdf_filename)
    b64 = encode_pdf(full_path)
    if not b64:
        raise RuntimeError("PDF encoding failed.")

    # Call Mistral OCR
    response = client.ocr.process(
        model="mistral-ocr-latest",
        document={
            "type": "document_url",
            "document_url": f"data:application/pdf;base64,{b64}",
        },
        include_image_base64=False,
    )

    # Markdown output
    output_name = pdf_filename.rsplit(".", 1)[0] + ".md"
    output_path = os.path.join(EXPORT_DIR, output_name)
    output_dir = os.path.dirname(output_path)
    if output_dir and not os.path.exists(output_dir):
        os.makedirs(output_dir)

    with open(output_path, "w", encoding="utf-8") as md_file:
        for page in response.pages:
            md_file.write(f"## Page {page.index + 1}\n\n")
            md_file.write(page.markdown + "\n\n")

    print(f"Saved markdown file: {output_path}")

    if os.path.exists(output_path):
        try:
            docx_path = os.path.join(
                EXPORT_DIR, pdf_filename.rsplit(".", 1)[0] + ".docx"
            )
            pypandoc.convert_file(output_path, "docx", outputfile=docx_path)
            print(f"Converted to Word: {docx_path}")
        except Exception as e:
            logging.error(f"Word conversion failed for {pdf_filename}: {e}")
            print(f"Word conversion failed for {pdf_filename}: {e}")
    else:
        print(f"Markdown file not found, skipping Word conversion for {pdf_filename}")


def main():
    ensure_export_directory()

    processed = load_processed()
    all_files = get_pdf_files()
    total = len(all_files)
    succeeded = sum(1 for r in processed.values() if r["status"] == "success")
    to_do = [f for f in all_files if processed.get(f, {}).get("status") != "success"]

    print(
        f"Found {total} PDF files in '{DOC_DIR}/'. {succeeded} already converted. {len(to_do)} remaining."
    )
    print(f"Output will be saved to '{EXPORT_DIR}/' directory.")

    converted_count = 0
    for idx, pdf in enumerate(to_do, start=1):
        print(f"[{idx}/{len(to_do)}] Processing: {pdf}")
        attempts = 0
        backoff = INITIAL_BACKOFF
        success = False

        while attempts < MAX_RETRIES and not success:
            attempts += 1
            try:
                convert_pdf_to_markdown(pdf)
                success = True
                converted_count += 1
                update_db(
                    {
                        "filename": pdf,
                        "status": "success",
                        "attempts": attempts,
                        "error": "",
                    }
                )
                print(f"Success: {pdf} (attempt {attempts})")
                print(f"Waiting for the next file...")
                time.sleep(3)
            except Exception as e:
                error_msg = str(e)
                update_db(
                    {
                        "filename": pdf,
                        "status": "error",
                        "attempts": attempts,
                        "error": error_msg,
                    }
                )
                logging.error(f"{pdf} attempt {attempts} failed: {error_msg}")
                print(f"Error converting {pdf} on attempt {attempts}: {error_msg}")
                if attempts < MAX_RETRIES:
                    print(f"Retrying in {backoff} seconds...")
                    time.sleep(backoff)
                    backoff *= 2

        if not success:
            print(f"Failed: {pdf} after {attempts} attempts.")

    print(
        f"\nConversion complete. Total successful conversions: {converted_count} out of {len(to_do)}."
    )
    print(f"All converted files are saved in '{EXPORT_DIR}/' directory.")


if __name__ == "__main__":
    main()
