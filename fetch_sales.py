"""
Fetch sales data from the Petpooja API and store it in a local SQLite database.

Usage:
    python fetch_sales.py            # fetch and store data
    python fetch_sales.py --inspect  # fetch and print the raw response structure
"""

import argparse
import logging
import os
import re
import sqlite3
import sys
import time

import requests
from dotenv import load_dotenv

# ----------------------------------------------------------------------------
# Configuration (credentials come from environment variables / .env file)
# ----------------------------------------------------------------------------
load_dotenv()

API_URL = os.getenv("API_URL", "http://api.petpooja.com/V1/orders/get_sales_data/")
DB_NAME = os.getenv("DB_NAME", "sales_data.db")
FROM_DATE = os.getenv("FROM_DATE", "2025-05-03 00:00:00")
TO_DATE = os.getenv("TO_DATE", "2025-05-30 23:59:19")

MAX_RETRIES = 3          # total attempts for the API call
REQUEST_TIMEOUT = 30     # seconds

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
log = logging.getLogger(__name__)

# ----------------------------------------------------------------------------
# Mapping: database column -> possible API field names.
# Names are compared after normalisation (lower-case, letters/digits only),
# so "Receipt number", "receipt_number" and "receiptNumber" all match.
# The first six come directly from the task description.
# ----------------------------------------------------------------------------
FIELD_MAP = {
    "receipt_number":     ["Receipt number", "receipt_no", "transaction_id", "bill_number"],
    "sale_date":          ["Receipt Date", "sale_date", "date"],
    "transaction_time":   ["Transaction time", "time", "receipt_time", "created_time"],
    "sale_amount":        ["Invoice amount", "sale_amount", "gross_amount"],
    "tax_amount":         ["Tax amount", "total_tax", "tax"],
    "discount_amount":    ["Discount amount", "discount", "total_discount"],
    "round_off":          ["Round off", "roundoff", "round_off_amount"],
    "net_sale":           ["Net sale", "final_sale_amount", "net_amount"],
    "payment_mode":       ["Payment mode", "payment_type", "payment_method"],
    "order_type":         ["Order type", "order_mode", "type"],
    "transaction_status": ["Transaction status", "status", "sale_type"],
}
NUMERIC_COLUMNS = {"sale_amount", "tax_amount", "discount_amount", "round_off", "net_sale"}
COLUMNS = list(FIELD_MAP.keys())


# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------
def normalize(key):
    """'Receipt number' / 'receiptNumber' / 'receipt_number' -> 'receiptnumber'."""
    return re.sub(r"[^a-z0-9]", "", str(key).lower())


def safe_float(value):
    """Convert to float; return 0.0 for missing/invalid values ('1,234.50' works too)."""
    if value is None or value == "":
        return 0.0
    try:
        return float(str(value).replace(",", "").strip())
    except (ValueError, TypeError):
        return 0.0


def get_value(lookup, candidates):
    """Return the first non-empty value whose normalised key matches a candidate."""
    for name in candidates:
        value = lookup.get(normalize(name))
        if value not in (None, ""):
            return value
    return None


def redact(error):
    """Return an error description with the credential-bearing URL removed."""
    text = str(error)
    return re.sub(r"https?://\S+", "<api-url>", text)


def validate_config():
    """Make sure all required credentials are present."""
    required = ["APP_KEY", "APP_SECRET", "ACCESS_TOKEN", "REST_ID"]
    missing = [name for name in required if not os.getenv(name)]
    if missing:
        raise ValueError(
            "Missing environment variables: " + ", ".join(missing)
            + ". Copy .env.example to .env and fill in the values."
        )


# ----------------------------------------------------------------------------
# API
# ----------------------------------------------------------------------------
def fetch_sales_data():
    """Call the API. Retries with exponential backoff on network/5xx errors."""
    params = {
        "app_key": os.getenv("APP_KEY"),
        "app_secret": os.getenv("APP_SECRET"),
        "access_token": os.getenv("ACCESS_TOKEN"),
        "restID": os.getenv("REST_ID"),
        "from_date": FROM_DATE,
        "to_date": TO_DATE,
    }

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            log.info("Fetching sales data (attempt %d/%d)...", attempt, MAX_RETRIES)
            response = requests.get(API_URL, params=params, timeout=REQUEST_TIMEOUT)

            # 4xx = our request is wrong (bad credentials etc.); retrying won't help.
            if 400 <= response.status_code < 500:
                raise PermissionError(
                    f"API rejected the request (HTTP {response.status_code}): "
                    f"{response.text[:200]}"
                )

            response.raise_for_status()  # 5xx -> HTTPError -> retried below
            data = response.json()
            log.info("API request successful.")
            return data

        except (requests.exceptions.RequestException, ValueError) as error:
            # RequestException: timeouts, connection errors, 5xx. ValueError: bad JSON.
            # Don't log str(error): requests includes the full URL (with secrets).
            log.error("API call failed: %s", redact(error))
            if attempt == MAX_RETRIES:
                raise
            wait = 2 ** attempt  # 2s, 4s, ...
            log.info("Retrying in %d seconds...", wait)
            time.sleep(wait)


def extract_sales(data):
    """
    Find the list of sale records in the response, wherever it is nested.
    Works for [..], {"data": [..]}, {"data": {"orders": [..]}}, etc.
    """
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]

    if isinstance(data, dict):
        # Prefer well-known keys first, then search every value.
        preferred = [k for k in data if normalize(k) in {"data", "result", "orders", "sales", "salesdata"}]
        others = [k for k in data if k not in preferred]
        for key in preferred + others:
            found = extract_sales(data[key]) if isinstance(data[key], (list, dict)) else []
            if found:
                return found
    return []


# ----------------------------------------------------------------------------
# Transformation
# ----------------------------------------------------------------------------
def process_sale(sale):
    """Convert one API record into a tuple matching COLUMNS."""
    lookup = {normalize(k): v for k, v in sale.items()}
    row = []
    for column in COLUMNS:
        value = get_value(lookup, FIELD_MAP[column])
        if column in NUMERIC_COLUMNS:
            row.append(safe_float(value))
        else:
            row.append(str(value).strip() if value is not None else None)
    return tuple(row)


def warn_on_unmapped_columns(rows):
    """Warn if a column is empty for every row (usually means a wrong field name)."""
    for index, column in enumerate(COLUMNS):
        values = [row[index] for row in rows]
        if column in NUMERIC_COLUMNS:
            empty = all(v == 0.0 for v in values)
        else:
            empty = all(v is None for v in values)
        if empty:
            log.warning("Column '%s' is empty for ALL rows - check FIELD_MAP.", column)


# ----------------------------------------------------------------------------
# Database
# ----------------------------------------------------------------------------
def create_database():
    """Create the sales_data table (exact schema from the task)."""
    connection = sqlite3.connect(DB_NAME)
    try:
        with connection:
            connection.execute("""
                CREATE TABLE IF NOT EXISTS sales_data (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    receipt_number TEXT,
                    sale_date TEXT,
                    transaction_time TEXT,
                    sale_amount REAL,
                    tax_amount REAL,
                    discount_amount REAL,
                    round_off REAL,
                    net_sale REAL,
                    payment_mode TEXT,
                    order_type TEXT,
                    transaction_status TEXT
                )
            """)
        log.info("Database ready: %s", DB_NAME)
    finally:
        connection.close()


def insert_sales(rows):
    """
    Replace table contents with the freshly fetched rows in ONE transaction.
    Re-running the script therefore never creates duplicates, and if the
    insert fails the old data is rolled back untouched.
    """
    placeholders = ", ".join("?" for _ in COLUMNS)
    query = f"INSERT INTO sales_data ({', '.join(COLUMNS)}) VALUES ({placeholders})"

    connection = sqlite3.connect(DB_NAME)
    try:
        with connection:  # commits on success, rolls back on exception
            connection.execute("DELETE FROM sales_data")
            connection.execute("DELETE FROM sqlite_sequence WHERE name = 'sales_data'")
            connection.executemany(query, rows)
        log.info("%d records inserted into sales_data.", len(rows))
    finally:
        connection.close()


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="Fetch sales data into SQLite.")
    parser.add_argument("--inspect", action="store_true",
                        help="print the structure of the first record and exit")
    args = parser.parse_args()

    try:
        validate_config()
        data = fetch_sales_data()
        sales = extract_sales(data)
        log.info("Records received: %d", len(sales))

        if args.inspect:
            top = list(data.keys()) if isinstance(data, dict) else "list"
            print("Top-level keys:", top)
            print("First record:", sales[0] if sales else "none found")
            return

        if not sales:
            top = list(data.keys()) if isinstance(data, dict) else type(data).__name__
            log.warning("No sales records found. Response structure: %s", top)

        create_database()
        rows = [process_sale(s) for s in sales]
        if rows:
            warn_on_unmapped_columns(rows)
        insert_sales(rows)
        log.info("Done.")

    except (sqlite3.Error, requests.exceptions.RequestException,
            ValueError, PermissionError) as error:
        log.error("Process failed: %s", error)
        sys.exit(1)


if __name__ == "__main__":
    main()