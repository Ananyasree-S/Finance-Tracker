import sqlite3, csv, io, xlrd, re
from fastapi import FastAPI, HTTPException, UploadFile, File
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from typing import Optional, List

app = FastAPI()
DB = "finance.db"

EXPENSE_SUBCATEGORIES = [
    "Food", "Transport", "Housing", "Health",
    "Shopping", "Entertainment", "Other", "Investments", "Self"
]

def get_conn():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with get_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS transactions (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                type        TEXT NOT NULL,
                subcategory TEXT,
                amount      REAL NOT NULL,
                description TEXT DEFAULT '',
                date        TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS investments (
                id    INTEGER PRIMARY KEY AUTOINCREMENT,
                date  TEXT NOT NULL,
                notes TEXT NOT NULL
            )
        """)
        # Recreate if schema doesn't match (e.g. old version had name/type/amount columns)
        cols = {row[1] for row in conn.execute("PRAGMA table_info(investments)").fetchall()}
        if cols != {'id', 'date', 'notes'}:
            conn.execute("DROP TABLE investments")
            conn.execute("""
                CREATE TABLE investments (
                    id    INTEGER PRIMARY KEY AUTOINCREMENT,
                    date  TEXT NOT NULL,
                    notes TEXT NOT NULL
                )
            """)

init_db()


class TransactionIn(BaseModel):
    type: str
    subcategory: Optional[str] = None
    amount: float
    description: str = ""
    date: str


# ── Subcategories ─────────────────────────────────────────────────────────────

@app.get("/api/subcategories")
def get_subcategories():
    return EXPENSE_SUBCATEGORIES


# ── Transactions ──────────────────────────────────────────────────────────────

@app.get("/api/transactions")
def list_transactions(
    type: Optional[str] = None,
    subcategory: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
):
    with get_conn() as conn:
        query = "SELECT * FROM transactions WHERE 1=1"
        params = []
        if type:
            query += " AND type=?"; params.append(type)
        if subcategory:
            query += " AND subcategory=?"; params.append(subcategory)
        if start_date:
            query += " AND date>=?"; params.append(start_date)
        if end_date:
            query += " AND date<=?"; params.append(end_date)
        query += " ORDER BY date DESC"
        rows = conn.execute(query, params).fetchall()
    return [dict(r) for r in rows]

@app.post("/api/transactions", status_code=201)
def create_transaction(t: TransactionIn):
    if t.type not in ("income", "expense", "transfer"):
        raise HTTPException(400, "type must be 'income', 'expense', or 'transfer'")
    if t.type == "expense" and t.subcategory not in EXPENSE_SUBCATEGORIES:
        raise HTTPException(400, f"subcategory must be one of {EXPENSE_SUBCATEGORIES}")
    if t.amount <= 0:
        raise HTTPException(400, "amount must be positive")
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO transactions (type, subcategory, amount, description, date) VALUES (?,?,?,?,?)",
            (t.type, t.subcategory, t.amount, t.description, t.date),
        )
        return {"id": cur.lastrowid, **t.model_dump()}


@app.put("/api/transactions/{t_id}")
def update_transaction(t_id: int, t: TransactionIn):
    if t.type not in ("income", "expense", "transfer"):
        raise HTTPException(400, "type must be 'income', 'expense', or 'transfer'")
    if t.type == "expense" and t.subcategory not in EXPENSE_SUBCATEGORIES:
        raise HTTPException(400, f"subcategory must be one of {EXPENSE_SUBCATEGORIES}")
    with get_conn() as conn:
        res = conn.execute(
            "UPDATE transactions SET type=?, subcategory=?, amount=?, description=?, date=? WHERE id=?",
            (t.type, t.subcategory, t.amount, t.description, t.date, t_id),
        )
        if res.rowcount == 0:
            raise HTTPException(404, "Not found")
    return {"id": t_id, **t.model_dump()}


@app.delete("/api/transactions/{t_id}")
def delete_transaction(t_id: int):
    with get_conn() as conn:
        conn.execute("DELETE FROM transactions WHERE id=?", (t_id,))
    return {"ok": True}


# ── Summary (dashboard) ───────────────────────────────────────────────────────

@app.get("/api/summary")
def summary():
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM transactions").fetchall()
    txs = [dict(r) for r in rows]
    income  = sum(t["amount"] for t in txs if t["type"] == "income")
    expense = sum(t["amount"] for t in txs if t["type"] == "expense" and t.get("subcategory") != "Self")
    by_sub = {}
    for t in txs:
        if t["type"] == "expense" and t["subcategory"] and t["subcategory"] != "Self":
            by_sub[t["subcategory"]] = by_sub.get(t["subcategory"], 0) + t["amount"]
    return {"income": income, "expense": expense, "balance": income - expense,
            "expense_by_subcategory": by_sub}


class InvestmentIn(BaseModel):
    date: str
    notes: str


# ── Investments ───────────────────────────────────────────────────────────────

@app.get("/api/investments")
def list_investments():
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM investments ORDER BY date DESC").fetchall()
    return [dict(r) for r in rows]


@app.post("/api/investments", status_code=201)
def create_investment(inv: InvestmentIn):
    if not inv.notes.strip():
        raise HTTPException(400, "notes cannot be empty")
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO investments (date, notes) VALUES (?,?)",
            (inv.date, inv.notes),
        )
        return {"id": cur.lastrowid, **inv.model_dump()}


@app.put("/api/investments/{inv_id}")
def update_investment(inv_id: int, inv: InvestmentIn):
    with get_conn() as conn:
        res = conn.execute(
            "UPDATE investments SET date=?, notes=? WHERE id=?",
            (inv.date, inv.notes, inv_id),
        )
        if res.rowcount == 0:
            raise HTTPException(404, "Not found")
    return {"id": inv_id, **inv.model_dump()}


@app.delete("/api/investments/{inv_id}")
def delete_investment(inv_id: int):
    with get_conn() as conn:
        conn.execute("DELETE FROM investments WHERE id=?", (inv_id,))
    return {"ok": True}


# ── HDFC import ───────────────────────────────────────────────────────────────

CC_PAYMENT_KEYWORDS = [
    "credit card", "creditcard", "cc payment", "ccpay", "cc pay",
    "hdfc crcard", "credit card bill", "ccard", "cc emi",
]

def is_cc_payment(narration: str) -> bool:
    n = narration.lower()
    return any(k in n for k in CC_PAYMENT_KEYWORDS)


CATEGORIZE_KEYWORDS = {
    "Food":          ["swiggy", "zomato", "restaurant", "cafe", "mcdonald", "subway",
                      "dominos", "pizza", "burger", "bigbasket", "blinkit", "zepto", "dunzo"],
    "Transport":     ["uber", "ola", "metro", "petrol", "fuel", "irctc", "rapido",
                      "bus", "toll", "parking", "fastag"],
    "Shopping":      ["amazon", "flipkart", "myntra", "meesho", "ajio", "nykaa", "tata cliq"],
    "Health":        ["pharmacy", "apollo", "hospital", "clinic", "medplus", "netmeds", "1mg"],
    "Entertainment": ["netflix", "spotify", "prime video", "bookmyshow", "pvr", "inox",
                      "disney", "youtube premium", "hotstar"],
    "Housing":       ["rent", "electricity", "water board", "maintenance", "bescom",
                      "mahanagar gas", "tata power", "adani electricity"],
    "Investments":   ["mutual fund", "zerodha", "groww", "nps", "sip", "kuvera", "coin"],
    "Self":          ["self transfer", "own account", "trf to self", "transfer to self",
                      "inter account", "sweep transfer", "own bank transfer"],
}

def categorize(narration: str) -> str:
    n = narration.lower()
    for sub, keywords in CATEGORIZE_KEYWORDS.items():
        if any(k in n for k in keywords):
            return sub
    return "Other"


def parse_hdfc_csv(text: str) -> list:
    if '~|~' in text[:500]:
        return parse_hdfc_cc_csv(text)
    return parse_hdfc_bank_csv(text)


def parse_hdfc_cc_csv(text: str) -> list:
    rows = []
    header_found = False
    for line in text.splitlines():
        line = line.strip().rstrip(',').strip()
        if not line:
            continue
        if any(s in line for s in ['Reward Points', 'Cashback Summary', 'Programs~']):
            break
        if 'DATE' in line and 'Description' in line:
            header_found = True
            continue
        if not header_found:
            continue
        parts = [p.strip().strip('~') for p in line.split('~|~')]
        if len(parts) < 5:
            continue
        date_time   = parts[2]
        description = parts[3]
        amount_str  = parts[4]
        cr_dr       = parts[5].strip().upper() if len(parts) > 5 else ''
        try:
            date_part = date_time.split(' ')[0]
            d, m, y = date_part.split('/')
            if len(y) == 2: y = '20' + y
            iso_date = f"{y}-{m.zfill(2)}-{d.zfill(2)}"
        except Exception:
            continue
        try:
            amount = float(amount_str.replace(',', ''))
            if amount <= 0:
                continue
        except ValueError:
            continue
        tx_type = "income" if cr_dr == 'CR' else "expense"
        rows.append({"date": iso_date, "description": description.strip(), "type": tx_type,
                     "amount": amount, "subcategory": categorize(description) if tx_type == "expense" else None})
    return rows


def parse_hdfc_bank_csv(text: str) -> list:
    rows = []
    reader = csv.reader(io.StringIO(text, newline=''))
    header_found = False
    for row in reader:
        if not header_found:
            if len(row) >= 5 and row[0].strip().lower() == "date":
                header_found = True
            continue
        if len(row) < 5:
            continue
        date_str  = row[0].strip()
        narration = row[1].strip()
        debit     = row[3].strip().replace(",", "")
        credit    = row[4].strip().replace(",", "")
        if not date_str or not narration:
            continue
        try:
            parts = date_str.split("/")
            day, month, year = parts[0], parts[1], parts[2]
            if len(year) == 2: year = "20" + year
            iso_date = f"{year}-{month.zfill(2)}-{day.zfill(2)}"
        except Exception:
            continue
        try:
            if debit and float(debit) > 0:
                rows.append({"date": iso_date, "description": narration, "type": "expense",
                             "amount": float(debit), "subcategory": categorize(narration),
                             "cc_payment": is_cc_payment(narration)})
            elif credit and float(credit) > 0:
                rows.append({"date": iso_date, "description": narration, "type": "income",
                             "amount": float(credit), "subcategory": None, "cc_payment": False})
        except ValueError:
            continue
    return rows


def parse_hdfc_xls(content: bytes) -> list:
    book = xlrd.open_workbook(file_contents=content)
    sheet = book.sheet_by_index(0)
    sample = ' '.join(str(v) for v in sheet.row_values(0) + sheet.row_values(1))
    if '~|~' in sample or ('~' in sample and '|' in sample):
        return parse_hdfc_cc_xls(sheet)
    return parse_hdfc_bank_xls(sheet)


def parse_hdfc_bank_xls(sheet) -> list:
    rows = []
    header_row_idx = None
    col_date = col_narration = col_debit = col_credit = None
    for i in range(min(10, sheet.nrows)):
        row = sheet.row_values(i)
        low = [str(c).strip().lower() for c in row]
        if 'date' in low:
            header_row_idx = i
            col_date      = low.index('date')
            col_narration = next((j for j, c in enumerate(low) if 'narration' in c), 1)
            col_debit     = next((j for j, c in enumerate(low) if 'withdrawal' in c or 'debit' in c), 4)
            col_credit    = next((j for j, c in enumerate(low) if 'deposit' in c or 'credit' in c), 5)
            break
    if header_row_idx is None:
        return []
    for i in range(header_row_idx + 1, sheet.nrows):
        row = sheet.row_values(i)
        if len(row) <= max(col_date, col_narration, col_debit, col_credit):
            continue
        raw_date  = str(row[col_date]).strip()
        narration = str(row[col_narration]).strip()
        if not raw_date or raw_date.startswith('*') or not narration or narration.startswith('*'):
            continue
        try:
            parts = raw_date.split('/')
            day, month, year = parts[0].zfill(2), parts[1].zfill(2), parts[2]
            if len(year) == 2: year = '20' + year
            iso_date = f"{year}-{month}-{day}"
        except Exception:
            continue
        debit_val  = row[col_debit]
        credit_val = row[col_credit]
        try:
            if isinstance(debit_val, float) and debit_val > 0:
                rows.append({"date": iso_date, "description": narration, "type": "expense",
                             "amount": debit_val, "subcategory": categorize(narration),
                             "cc_payment": is_cc_payment(narration)})
            elif isinstance(credit_val, float) and credit_val > 0:
                rows.append({"date": iso_date, "description": narration, "type": "income",
                             "amount": credit_val, "subcategory": None, "cc_payment": False})
            elif str(debit_val).strip() not in ('', '0', '0.0'):
                amount = float(str(debit_val).replace(',', ''))
                if amount > 0:
                    rows.append({"date": iso_date, "description": narration, "type": "expense",
                                 "amount": amount, "subcategory": categorize(narration),
                                 "cc_payment": is_cc_payment(narration)})
            elif str(credit_val).strip() not in ('', '0', '0.0'):
                amount = float(str(credit_val).replace(',', ''))
                if amount > 0:
                    rows.append({"date": iso_date, "description": narration, "type": "income",
                                 "amount": amount, "subcategory": None, "cc_payment": False})
        except (ValueError, TypeError):
            continue
    return rows


def parse_hdfc_cc_xls(sheet) -> list:
    rows = []
    header_row_idx = None
    for i in range(sheet.nrows):
        row_text = ' '.join(str(v) for v in sheet.row_values(i))
        if 'DATE' in row_text and 'Description' in row_text:
            header_row_idx = i
            break
    if header_row_idx is None:
        return []
    for i in range(header_row_idx + 1, sheet.nrows):
        row_vals = sheet.row_values(i)
        row_text = '~|~'.join(str(v).strip() for v in row_vals)
        parts = [p for p in (p.strip().strip('~') for p in row_text.split('~|~')) if p]
        if len(parts) < 4:
            continue
        date_str = desc = amount_str = cr_dr = None
        for p in parts:
            if len(p) >= 10 and p[2] == '/' and p[5] == '/':
                date_str = p[:10]
            elif p in ('Cr', 'Dr', 'CR', 'DR'):
                cr_dr = p.upper()
        for p in parts:
            if p == date_str or p in ('Domestic', 'International', 'Cr', 'Dr', 'CR', 'DR'):
                continue
            try:
                float(p.replace(',', ''))
                amount_str = p
            except ValueError:
                if not date_str or p != date_str[:10]:
                    desc = p
        if not date_str or not amount_str:
            continue
        try:
            d, m, y = date_str.split('/')
            if len(y) == 2: y = '20' + y
            iso_date = f"{y}-{m.zfill(2)}-{d.zfill(2)}"
            amount = float(amount_str.replace(',', ''))
            if amount <= 0:
                continue
        except Exception:
            continue
        tx_type = "income" if cr_dr == 'CR' else "expense"
        rows.append({"date": iso_date, "description": desc or "", "type": tx_type,
                     "amount": amount, "subcategory": categorize(desc or "") if tx_type == "expense" else None})
    return rows


def parse_hdfc_html_xls(text: str) -> list:
    rows = []
    trs = re.findall(r'<tr[^>]*>(.*?)</tr>', text, re.IGNORECASE | re.DOTALL)
    header_idx = None
    cols = {}
    is_cc = False
    for i, tr in enumerate(trs):
        cells = [re.sub(r'<[^>]+>', '', td).strip()
                 for td in re.findall(r'<t[dh][^>]*>(.*?)</t[dh]>', tr, re.IGNORECASE | re.DOTALL)]
        cells_low = [c.lower() for c in cells]
        if header_idx is None:
            if any('date' in c for c in cells_low):
                header_idx = i
                date_col  = next((j for j, c in enumerate(cells_low) if 'date' in c), 0)
                desc_col  = next((j for j, c in enumerate(cells_low) if 'narration' in c or 'description' in c), 1)
                debit_col = next((j for j, c in enumerate(cells_low) if 'withdrawal' in c or ('debit' in c and 'cr' not in c)), None)
                credit_col= next((j for j, c in enumerate(cells_low) if 'deposit' in c or ('credit' in c and 'cr/dr' not in c)), None)
                amount_col= next((j for j, c in enumerate(cells_low) if 'amount' in c), None)
                crdr_col  = next((j for j, c in enumerate(cells_low) if c in ('cr/dr', 'cr', 'dr') or 'cr/dr' in c), None)
                if debit_col is not None or credit_col is not None:
                    is_cc = False
                    cols = {'date': date_col, 'desc': desc_col,
                            'debit': debit_col if debit_col is not None else 4,
                            'credit': credit_col if credit_col is not None else 5}
                elif amount_col is not None:
                    is_cc = True
                    cols = {'date': date_col, 'desc': desc_col, 'amount': amount_col, 'crdr': crdr_col}
                else:
                    cols = {'date': date_col, 'desc': desc_col, 'debit': 4, 'credit': 5}
            continue
        if not cols or len(cells) <= max(v for v in cols.values() if v is not None):
            continue
        date_str  = cells[cols['date']].strip()
        narration = cells[cols['desc']].strip()
        if not date_str or date_str.startswith('*') or not narration:
            continue
        try:
            parts = date_str.split('/')
            day, month, year = parts[0].zfill(2), parts[1].zfill(2), parts[2]
            if len(year) == 2: year = '20' + year
            iso_date = f"{year}-{month}-{day}"
        except Exception:
            continue
        if is_cc:
            amount_str = cells[cols['amount']].replace(',', '').strip() if cols.get('amount') is not None else ''
            crdr_str   = cells[cols['crdr']].strip().upper() if cols.get('crdr') is not None and len(cells) > cols['crdr'] else 'DR'
            try:
                amount = float(amount_str)
                if amount <= 0:
                    continue
                tx_type = "income" if crdr_str == 'CR' else "expense"
                rows.append({"date": iso_date, "description": narration, "type": tx_type,
                             "amount": amount, "subcategory": categorize(narration) if tx_type == "expense" else None,
                             "cc_payment": False})
            except ValueError:
                continue
        else:
            debit  = cells[cols['debit']].replace(',', '').strip()
            credit = cells[cols['credit']].replace(',', '').strip()
            try:
                if debit and float(debit) > 0:
                    rows.append({"date": iso_date, "description": narration, "type": "expense",
                                 "amount": float(debit), "subcategory": categorize(narration),
                                 "cc_payment": is_cc_payment(narration)})
                elif credit and float(credit) > 0:
                    rows.append({"date": iso_date, "description": narration, "type": "income",
                                 "amount": float(credit), "subcategory": None, "cc_payment": False})
            except ValueError:
                continue
    return rows
            continue
    return rows


@app.post("/api/import/preview")
async def import_preview(file: UploadFile = File(...)):
    content = await file.read()
    # Strip UTF-8 / UTF-16 BOM before format detection
    body = content.lstrip(b'\xef\xbb\xbf\xff\xfe\xfe\xff')
    is_xls      = body[:8] == b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1'
    start5      = body[:5].decode('utf-8', errors='ignore').lower()
    is_html_xls = start5.startswith(('<html', '<?xml', '<tabl'))
    filename    = (file.filename or "").lower()

    if is_html_xls:
        rows = parse_hdfc_html_xls(content.decode("utf-8", errors="ignore"))
    elif is_xls or filename.endswith((".xls", ".xlsx")):
        rows = parse_hdfc_xls(content)
    else:
        rows = parse_hdfc_csv(content.decode("utf-8", errors="ignore"))

    # Fallback: if HTML was detected but parsed empty, try as CSV (some CC exports)
    if not rows and is_html_xls:
        rows = parse_hdfc_csv(content.decode("utf-8", errors="ignore"))

    if not rows:
        raise HTTPException(400, "No transactions found. Check this is an HDFC statement.")
    return rows


@app.post("/api/import/commit")
def import_commit(transactions: List[TransactionIn]):
    with get_conn() as conn:
        conn.executemany(
            "INSERT INTO transactions (type, subcategory, amount, description, date) VALUES (?,?,?,?,?)",
            [(t.type, t.subcategory, t.amount, t.description, t.date) for t in transactions],
        )
    return {"imported": len(transactions)}


# ── Frontend ──────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
def serve_frontend():
    with open("templates/index.html", encoding="utf-8") as f:
        return f.read()
