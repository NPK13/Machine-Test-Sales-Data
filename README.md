# Sales Data Fetcher

Fetches sales data from the Petpooja API and stores it in a local SQLite database (`sales_data.db`).

## Features
- Credentials loaded from environment variables (no hardcoded secrets)
- Retry with exponential backoff for network and server errors
- Error handling for API, JSON and database failures
- Re-running does not create duplicate rows

## Requirements
- Python 3.8+

## Setup
```bash
git clone <your-repo-url>
cd Machine-Test-Sales-Data
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
```

## Configuration
Copy `.env.example` to `.env` and fill in your credentials:
```bash
copy .env.example .env
```

## Run
```bash
python fetch_sales.py
```

## Output
Creates `sales_data.db` with a `sales_data` table containing: id, receipt_number, sale_date, transaction_time, sale_amount, tax_amount, discount_amount, round_off, net_sale, payment_mode, order_type, transaction_status.

## Verify
```bash
python -c "import sqlite3; c=sqlite3.connect('sales_data.db'); print(c.execute('SELECT COUNT(*) FROM sales_data').fetchone())"
```