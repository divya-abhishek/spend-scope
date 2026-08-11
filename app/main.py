from __future__ import annotations

import hashlib
import io
import json
import re
import sqlite3
from collections import Counter, defaultdict
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

import numpy as np
import pandas as pd
from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
DB_PATH = DATA_DIR / "spend_analyzer.db"
STATIC_DIR = Path(__file__).resolve().parent / "static"
SAMPLE_FILE = BASE_DIR / "sample_data" / "kharch_july_2026.csv"
DATA_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="SpendScope", version="2.0.0")

STOP_WORDS = {
    "the", "and", "for", "from", "to", "of", "a", "an", "in", "on", "at", "with", "by",
    "paid", "payment", "send", "sent", "order", "home", "office", "near", "via", "using",
    "item", "items", "buy", "bought", "expense", "cost", "bill", "back", "part", "transaction",
}

ROLE_ALIASES: dict[str, set[str]] = {
    "date": {
        "date", "time", "datetime", "timestamp", "transaction date", "transaction time",
        "posted date", "posting date", "value date", "booking date", "created at", "occurred at", "when",
    },
    "amount": {
        "amount", "transaction amount", "value", "total", "amt", "money", "transaction value",
    },
    "debit": {
        "debit", "debit amount", "withdrawal", "withdrawal amount", "outflow", "money out",
        "spent", "expense amount", "charge", "charges",
    },
    "credit": {
        "credit", "credit amount", "deposit", "deposit amount", "inflow", "money in",
        "received", "income amount",
    },
    "type": {
        "type", "transaction type", "transaction_type", "kind", "direction", "flow", "entry type",
        "nature", "dr cr", "dr/cr", "debit credit",
    },
    "category": {
        "category", "categories", "expense category", "transaction category", "group", "tag", "tags",
    },
    "account": {
        "account", "account name", "wallet", "payment account", "payment_account", "card",
        "source account", "bank account", "payment method", "method",
    },
    "description": {
        "notes", "note", "description", "memo", "narration", "narrative", "details", "detail",
        "merchant", "payee", "transaction details", "particulars", "remarks", "reference",
    },
    "currency": {"currency", "curr", "ccy", "currency code", "currency_code"},
}

CURRENCY_SYMBOLS = {
    "₹": "INR", "$": "USD", "€": "EUR", "£": "GBP", "¥": "JPY", "₩": "KRW",
    "₽": "RUB", "฿": "THB", "₫": "VND", "₱": "PHP", "₪": "ILS", "₦": "NGN",
}


@contextmanager
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _ensure_column(conn: sqlite3.Connection, table: str, name: str, definition: str) -> None:
    if name not in _columns(conn, table):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")


def init_db() -> None:
    with db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS imports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                filename TEXT NOT NULL,
                sha256 TEXT NOT NULL UNIQUE,
                imported_at TEXT NOT NULL,
                row_count INTEGER NOT NULL,
                month_start TEXT,
                month_end TEXT
            );
            CREATE TABLE IF NOT EXISTS transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                import_id INTEGER NOT NULL,
                occurred_at TEXT NOT NULL,
                tx_type TEXT NOT NULL,
                amount REAL NOT NULL,
                category TEXT NOT NULL,
                account TEXT NOT NULL,
                notes TEXT NOT NULL,
                source_file TEXT NOT NULL,
                FOREIGN KEY(import_id) REFERENCES imports(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_transactions_occurred_at ON transactions(occurred_at);
            CREATE INDEX IF NOT EXISTS idx_transactions_category ON transactions(category);
            CREATE INDEX IF NOT EXISTS idx_transactions_account ON transactions(account);
            """
        )
        _ensure_column(conn, "transactions", "currency", "TEXT NOT NULL DEFAULT 'INR'")
        _ensure_column(conn, "imports", "mapping_json", "TEXT")
        _ensure_column(conn, "imports", "warnings_json", "TEXT")
        _ensure_column(conn, "imports", "currency", "TEXT")
        # v1 stored INR transactions without an import-level currency field.
        conn.execute("UPDATE imports SET currency = 'INR' WHERE currency IS NULL OR TRIM(currency) = ''")


def clean_header(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value).strip())


def canonical(value: Any) -> str:
    s = clean_header(value).lower().replace("_", " ").replace("-", " ")
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def decode_csv(raw: bytes) -> tuple[pd.DataFrame, str, str]:
    last_error: Exception | None = None
    for encoding in ("utf-8-sig", "utf-8", "cp1252", "latin1"):
        try:
            text = raw.decode(encoding)
        except Exception as exc:
            last_error = exc
            continue
        for sep in (None, ",", ";", "\t", "|"):
            try:
                kwargs: dict[str, Any] = {"dtype": str, "keep_default_na": False}
                if sep is None:
                    kwargs.update({"sep": None, "engine": "python"})
                else:
                    kwargs.update({"sep": sep})
                frame = pd.read_csv(io.StringIO(text), **kwargs)
                if len(frame.columns) < 2 and sep is not None:
                    continue
                frame.columns = [clean_header(c) for c in frame.columns]
                frame = frame.loc[:, [c for c in frame.columns if c and not canonical(c).startswith("unnamed")]]
                if len(frame.columns) >= 2:
                    delimiter = "auto" if sep is None else ("tab" if sep == "\t" else sep)
                    return frame, encoding, delimiter
            except Exception as exc:
                last_error = exc
    raise ValueError(f"Could not read this CSV ({last_error}).")


def numeric_series(series: pd.Series) -> pd.Series:
    cleaned = (
        series.astype(str)
        .str.strip()
        .str.replace(r"\(([^)]+)\)", r"-\1", regex=True)
        .str.replace(r"[^0-9,\.\-+]", "", regex=True)
    )

    def parse_one(value: str) -> float | None:
        s = str(value).strip()
        if not s or s in {"-", "+", ".", ","}:
            return None
        if s.endswith("-") and s[:-1].replace(".", "").replace(",", "").isdigit():
            s = "-" + s[:-1]
        # Infer decimal separator from the right-most punctuation. This supports both
        # 1,234.56 and 1.234,56 style exports, including larger grouped values.
        if "," in s and "." in s:
            if s.rfind(",") > s.rfind("."):
                s = s.replace(".", "").replace(",", ".")
            else:
                s = s.replace(",", "")
        elif "," in s:
            parts = s.split(",")
            if len(parts[-1]) in (1, 2):
                s = "".join(parts[:-1]).replace(",", "") + "." + parts[-1]
            else:
                s = s.replace(",", "")
        try:
            return float(s)
        except ValueError:
            return None

    return cleaned.map(parse_one).astype(float)


def date_ratio(series: pd.Series) -> float:
    sample = series.astype(str).str.strip()
    sample = sample[sample != ""].head(80)
    if sample.empty:
        return 0.0
    # Pandas can interpret plain transaction amounts such as "250" as dates. Reject numeric-only
    # columns unless they look like compact YYYYMMDD dates.
    numeric_like = sample.str.fullmatch(r"[+-]?\d+(?:[.,]\d+)?").fillna(False)
    compact_dates = sample.str.fullmatch(r"(?:19|20)\d{6}").fillna(False)
    if float(numeric_like.mean()) > 0.75 and float(compact_dates.mean()) < 0.75:
        return 0.0
    parsed = pd.to_datetime(sample, errors="coerce", format="mixed", dayfirst=False)
    return float(parsed.notna().mean())


def numeric_ratio(series: pd.Series) -> float:
    sample = series.astype(str).str.strip()
    sample = sample[sample != ""].head(100)
    if sample.empty:
        return 0.0
    return float(numeric_series(sample).notna().mean())


def header_score(column: str, role: str) -> float:
    c = canonical(column)
    aliases = {canonical(x) for x in ROLE_ALIASES[role]}
    if c in aliases:
        return 1.0
    tokens = set(c.split())
    score = 0.0
    for alias in aliases:
        at = set(alias.split())
        if alias and alias in c:
            score = max(score, 0.86)
        overlap = len(tokens & at) / max(len(at), 1)
        if overlap >= 0.75:
            score = max(score, 0.74)
    return score


def role_candidates(frame: pd.DataFrame, role: str) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for col in frame.columns:
        h = header_score(col, role)
        data = 0.0
        if role == "date":
            data = date_ratio(frame[col])
        elif role in {"amount", "debit", "credit"}:
            data = numeric_ratio(frame[col])
            # Date strings often become numeric after punctuation is stripped (e.g. 2026-08-01).
            # Never let a clearly date-labelled column win a money role on data shape alone.
            if header_score(col, "date") >= 0.70:
                data = 0.0
        score = h
        if role == "date":
            score = max(score, 0.60 * data + 0.25 * h)
        elif role in {"amount", "debit", "credit"}:
            score = max(score, 0.52 * data + 0.40 * h)
        candidates.append({"column": col, "score": round(min(score, 1.0), 3), "dataScore": round(data, 3)})
    return sorted(candidates, key=lambda x: (x["score"], x["dataScore"]), reverse=True)


def infer_mapping(frame: pd.DataFrame) -> dict[str, Any]:
    mapping: dict[str, str] = {}
    scores: dict[str, float] = {}
    candidates: dict[str, list[dict[str, Any]]] = {}

    for role in ROLE_ALIASES:
        rows = role_candidates(frame, role)
        candidates[role] = rows[:6]
        if not rows:
            continue
        top = rows[0]
        threshold = 0.52 if role in {"date", "amount", "debit", "credit"} else 0.70
        if top["score"] >= threshold:
            mapping[role] = top["column"]
            scores[role] = top["score"]

    # If explicit Debit/Credit headers exist, do not mistake one of those numeric columns for a generic Amount column.
    strong_debit_credit = scores.get("debit", 0) >= 0.72 or scores.get("credit", 0) >= 0.72
    amount_col = mapping.get("amount")
    if strong_debit_credit and amount_col and header_score(amount_col, "amount") < 0.65:
        mapping.pop("amount", None)
        scores.pop("amount", None)
        amount_col = None

    # Avoid assigning one numeric column simultaneously as amount/debit/credit unless the header is explicit.
    if amount_col:
        for role in ("debit", "credit"):
            if mapping.get(role) == amount_col and header_score(amount_col, role) < 0.9:
                mapping.pop(role, None)
                scores.pop(role, None)

    debit_col, credit_col = mapping.get("debit"), mapping.get("credit")
    if debit_col and credit_col and debit_col == credit_col:
        # Prefer a generic amount interpretation if the same column was selected twice.
        mapping["amount"] = debit_col
        scores["amount"] = max(scores.get("debit", 0), scores.get("credit", 0))
        mapping.pop("debit", None)
        mapping.pop("credit", None)
        strong_debit_credit = False

    required_ok = bool(mapping.get("date")) and bool(mapping.get("amount") or mapping.get("debit") or mapping.get("credit"))
    ambiguous = []
    for role in ("date", "amount"):
        if role == "amount" and strong_debit_credit and not mapping.get("amount"):
            continue
        rows = candidates.get(role, [])
        if len(rows) >= 2 and rows[0]["score"] >= 0.55 and rows[1]["score"] >= rows[0]["score"] - 0.05:
            ambiguous.append(role)

    confidence_values = [scores.get("date", 0)]
    money_conf = max(scores.get("amount", 0), scores.get("debit", 0), scores.get("credit", 0))
    confidence_values.append(money_conf)
    confidence = round(float(np.mean(confidence_values)) if confidence_values else 0.0, 2)
    return {
        "mapping": mapping,
        "scores": scores,
        "confidence": confidence,
        "requiredOk": required_ok,
        "ambiguous": ambiguous,
        "candidates": candidates,
    }


def normalize_type(value: Any) -> str:
    s = str(value or "").strip().lower()
    compact = re.sub(r"[^a-z0-9+*\-]", " ", s)
    if any(x in s for x in ("transfer", "internal", "account transfer")) or "(*)" in s:
        return "transfer"
    if any(x in compact.split() for x in ("expense", "debit", "dr", "purchase", "withdrawal", "spent", "charge", "payment")) or "(-)" in s:
        return "expense"
    if any(x in compact.split() for x in ("income", "credit", "cr", "deposit", "received", "refund")) or "(+)" in s:
        return "income"
    if s in {"out", "outflow", "money out"}:
        return "expense"
    if s in {"in", "inflow", "money in"}:
        return "income"
    return "other"


def parse_date_series(series: pd.Series, currency_hint: str = "INR") -> pd.Series:
    raw = series.astype(str).str.strip()
    nonempty = raw[raw != ""]
    # Excel serial dates occasionally appear in financial exports.
    numeric = pd.to_numeric(nonempty, errors="coerce")
    if len(nonempty) and float(numeric.between(20000, 80000).mean()) > 0.8:
        all_numeric = pd.to_numeric(raw, errors="coerce")
        return pd.to_datetime(all_numeric, unit="D", origin="1899-12-30", errors="coerce")

    dayfirst: bool | None = None
    pairs = []
    for value in nonempty.head(80):
        m = re.match(r"^(\d{1,2})[./-](\d{1,2})[./-](\d{2,4})(?:\D|$)", value)
        if m:
            pairs.append((int(m.group(1)), int(m.group(2))))
    if pairs:
        if any(a > 12 and b <= 12 for a, b in pairs):
            dayfirst = True
        elif any(b > 12 and a <= 12 for a, b in pairs):
            dayfirst = False
        else:
            # Ambiguous numeric dates: use a sensible locale hint rather than silently
            # assuming US ordering for every bank export.
            dayfirst = currency_hint.upper() not in {"USD", "CAD", "PHP"}

    return pd.to_datetime(raw, errors="coerce", format="mixed", dayfirst=bool(dayfirst))


def detect_currency(frame: pd.DataFrame, mapping: dict[str, str], fallback: str = "INR") -> str:
    currency_col = mapping.get("currency")
    if currency_col and currency_col in frame.columns:
        vals = [str(v).strip().upper() for v in frame[currency_col].tolist() if str(v).strip()]
        codes = [v for v in vals if re.fullmatch(r"[A-Z]{3}", v)]
        if codes:
            return Counter(codes).most_common(1)[0][0]
    money_cols = [mapping.get(x) for x in ("amount", "debit", "credit") if mapping.get(x)]
    sample = " ".join(" ".join(frame[c].astype(str).head(50).tolist()) for c in money_cols if c in frame.columns)
    for symbol, code in CURRENCY_SYMBOLS.items():
        if symbol in sample:
            return code
    upper = sample.upper()
    for code in ("INR", "USD", "EUR", "GBP", "JPY", "CAD", "AUD", "SGD", "AED", "SAR", "CHF"):
        if re.search(rf"\b{code}\b", upper):
            return code
    return (fallback or "INR").upper()[:3]


def safe_text_series(frame: pd.DataFrame, column: str | None, fallback: str = "") -> pd.Series:
    if column and column in frame.columns:
        return frame[column].fillna("").astype(str).str.strip()
    return pd.Series([fallback] * len(frame), index=frame.index, dtype=str)


def build_normalized_frame(
    frame: pd.DataFrame,
    mapping: dict[str, str],
    filename: str,
    default_currency: str = "INR",
) -> tuple[pd.DataFrame, list[str], str]:
    date_col = mapping.get("date")
    amount_col = mapping.get("amount")
    debit_col = mapping.get("debit")
    credit_col = mapping.get("credit")
    if not date_col or date_col not in frame.columns:
        raise ValueError("A date/time column is required.")
    if not any(c and c in frame.columns for c in (amount_col, debit_col, credit_col)):
        raise ValueError("An amount column, or debit/credit columns, is required.")

    out = pd.DataFrame(index=frame.index)
    currency = detect_currency(frame, mapping, default_currency)
    out["TIME"] = parse_date_series(frame[date_col], currency)
    warnings: list[str] = []

    explicit_type = safe_text_series(frame, mapping.get("type"))
    normalized_explicit = explicit_type.map(normalize_type)

    if amount_col and amount_col in frame.columns:
        signed = numeric_series(frame[amount_col])
        out["AMOUNT"] = signed.abs()
        type_values: list[str] = []
        has_explicit = bool(mapping.get("type")) and (normalized_explicit != "other").mean() >= 0.25
        for idx, value in signed.items():
            explicit = normalized_explicit.loc[idx] if has_explicit else "other"
            if explicit != "other":
                tx_type = explicit
            elif pd.notna(value) and value < 0:
                tx_type = "expense"
            elif pd.notna(value) and value > 0 and signed.dropna().lt(0).any():
                tx_type = "income"
            else:
                tx_type = "expense"
            type_values.append(tx_type)
        out["TYPE"] = type_values
        if not has_explicit and not signed.dropna().lt(0).any():
            warnings.append("No debit/credit direction was detected, so positive amounts were treated as expenses.")
    else:
        debit = numeric_series(frame[debit_col]) if debit_col and debit_col in frame.columns else pd.Series(np.nan, index=frame.index)
        credit = numeric_series(frame[credit_col]) if credit_col and credit_col in frame.columns else pd.Series(np.nan, index=frame.index)
        amounts, types = [], []
        for idx in frame.index:
            d = debit.loc[idx]
            c = credit.loc[idx]
            explicit = normalized_explicit.loc[idx] if mapping.get("type") else "other"
            if pd.notna(d) and abs(float(d)) > 0:
                amounts.append(abs(float(d)))
                types.append(explicit if explicit != "other" else "expense")
            elif pd.notna(c) and abs(float(c)) > 0:
                amounts.append(abs(float(c)))
                types.append(explicit if explicit != "other" else "income")
            else:
                amounts.append(np.nan)
                types.append(explicit if explicit != "other" else "other")
        out["AMOUNT"] = amounts
        out["TYPE"] = types

    out["CATEGORY"] = safe_text_series(frame, mapping.get("category"), "Uncategorized")
    out["ACCOUNT"] = safe_text_series(frame, mapping.get("account"), "Unspecified")
    out["NOTES"] = safe_text_series(frame, mapping.get("description"), "")
    out["CATEGORY"] = out["CATEGORY"].replace({"": "Uncategorized", "-": "Uncategorized", "nan": "Uncategorized"})
    out["ACCOUNT"] = out["ACCOUNT"].replace({"": "Unspecified", "-": "Unspecified", "nan": "Unspecified"})

    out["CURRENCY"] = currency
    out = out.dropna(subset=["TIME", "AMOUNT"])
    out = out[(out["AMOUNT"] >= 0) & np.isfinite(out["AMOUNT"])]
    out = out[out["TYPE"].isin(["expense", "income", "transfer", "other"])]
    if out.empty:
        raise ValueError(f"{filename} contains no valid transaction rows after normalization.")
    return out.sort_values("TIME"), warnings, currency


def preview_csv(raw: bytes, filename: str) -> dict[str, Any]:
    frame, encoding, delimiter = decode_csv(raw)
    inferred = infer_mapping(frame)
    sample = frame.head(5).replace({np.nan: ""}).to_dict(orient="records")
    return {
        "filename": filename,
        "columns": frame.columns.tolist(),
        "sample": sample,
        "encoding": encoding,
        "delimiter": delimiter,
        **inferred,
    }


def import_bytes(
    raw: bytes,
    filename: str,
    mapping_override: dict[str, str] | None = None,
    default_currency: str = "INR",
    require_confident: bool = True,
) -> dict[str, Any]:
    digest = hashlib.sha256(raw).hexdigest()
    with db() as conn:
        existing = conn.execute("SELECT * FROM imports WHERE sha256 = ?", (digest,)).fetchone()
        if existing:
            return {
                "status": "duplicate", "import_id": existing["id"], "filename": filename,
                "rows": existing["row_count"], "currency": existing["currency"] or default_currency,
            }

    frame, encoding, delimiter = decode_csv(raw)
    inferred = infer_mapping(frame)
    mapping = dict(inferred["mapping"])
    if mapping_override is not None:
        # A manual mapping is authoritative: choosing "Not present" must be able to clear
        # an incorrect auto-detected optional field instead of silently restoring it.
        mapping = {k: v for k, v in mapping_override.items() if v and v in frame.columns}

    required_ok = bool(mapping.get("date")) and bool(mapping.get("amount") or mapping.get("debit") or mapping.get("credit"))
    confidence_ok = inferred["confidence"] >= 0.56 and not inferred["ambiguous"]
    if require_confident and not mapping_override and (not required_ok or not confidence_ok):
        return {
            "status": "needs_mapping",
            "filename": filename,
            "preview": {
                "columns": frame.columns.tolist(),
                "sample": frame.head(5).replace({np.nan: ""}).to_dict(orient="records"),
                "mapping": mapping,
                "scores": inferred["scores"],
                "confidence": inferred["confidence"],
                "ambiguous": inferred["ambiguous"],
                "encoding": encoding,
                "delimiter": delimiter,
            },
        }
    if not required_ok:
        raise ValueError("Could not identify the date and monetary columns. Please map them before import.")

    normalized, warnings, currency = build_normalized_frame(frame, mapping, filename, default_currency)
    month_start = normalized["TIME"].min().date().isoformat()
    month_end = normalized["TIME"].max().date().isoformat()
    with db() as conn:
        cur = conn.execute(
            """INSERT INTO imports(filename, sha256, imported_at, row_count, month_start, month_end,
               mapping_json, warnings_json, currency) VALUES(?,?,?,?,?,?,?,?,?)""",
            (
                filename, digest, datetime.now().isoformat(timespec="seconds"), len(normalized), month_start, month_end,
                json.dumps(mapping, ensure_ascii=False), json.dumps(warnings, ensure_ascii=False), currency,
            ),
        )
        import_id = cur.lastrowid
        rows = [
            (
                import_id, row.TIME.isoformat(), row.TYPE, float(row.AMOUNT), row.CATEGORY, row.ACCOUNT,
                row.NOTES, filename, row.CURRENCY,
            )
            for row in normalized.itertuples(index=False)
        ]
        conn.executemany(
            """INSERT INTO transactions(import_id, occurred_at, tx_type, amount, category, account, notes, source_file, currency)
               VALUES(?,?,?,?,?,?,?,?,?)""",
            rows,
        )
    return {
        "status": "imported", "import_id": import_id, "filename": filename, "rows": len(normalized),
        "currency": currency, "mapping": mapping, "warnings": warnings,
    }


def seed_sample() -> None:
    with db() as conn:
        count = conn.execute("SELECT COUNT(*) c FROM transactions").fetchone()["c"]
    if count == 0 and SAMPLE_FILE.exists():
        import_bytes(SAMPLE_FILE.read_bytes(), SAMPLE_FILE.name, require_confident=False)


def load_frame() -> pd.DataFrame:
    with db() as conn:
        rows = conn.execute(
            """SELECT id, import_id, occurred_at, tx_type, amount, category, account, notes, source_file,
               COALESCE(currency, 'INR') AS currency FROM transactions ORDER BY occurred_at"""
        ).fetchall()
    cols = ["id", "import_id", "occurred_at", "tx_type", "amount", "category", "account", "notes", "source_file", "currency"]
    if not rows:
        return pd.DataFrame(columns=cols)
    frame = pd.DataFrame([dict(r) for r in rows])
    frame["occurred_at"] = pd.to_datetime(frame["occurred_at"])
    return frame


def safe_pct(a: float, b: float) -> float | None:
    if not b:
        return None
    return round((a - b) / b * 100, 1)


def top_keywords(notes: Iterable[str], limit: int = 12) -> list[dict[str, Any]]:
    tokens: Counter[str] = Counter()
    for note in notes:
        for token in re.findall(r"[a-zA-Z][a-zA-Z0-9]{2,}", str(note).lower()):
            if token not in STOP_WORDS and not token.isdigit():
                tokens[token] += 1
    return [{"keyword": k, "count": v} for k, v in tokens.most_common(limit)]


def merchant_patterns(expenses: pd.DataFrame, limit: int = 10) -> list[dict[str, Any]]:
    buckets: dict[str, dict[str, Any]] = {}
    for row in expenses.itertuples(index=False):
        note = str(row.notes).lower()
        tokens = [
            t for t in re.findall(r"[a-zA-Z][a-zA-Z0-9]{2,}", note)
            if t not in STOP_WORDS and not t.isdigit()
        ]
        fallback = "" if str(row.category) in {"Uncategorized", ""} else str(row.category).lower()
        key = " ".join(tokens[:2]) if tokens else fallback
        if not key:
            continue
        bucket = buckets.setdefault(key, {"label": key.title(), "count": 0, "spend": 0.0, "amounts": []})
        bucket["count"] += 1
        bucket["spend"] += float(row.amount)
        bucket["amounts"].append(float(row.amount))
    result = []
    for item in buckets.values():
        if item["count"] >= 2:
            result.append({
                "label": item["label"], "count": item["count"], "spend": round(item["spend"], 2),
                "avg": round(float(np.mean(item["amounts"])), 2),
            })
    result.sort(key=lambda x: (x["count"], x["spend"]), reverse=True)
    return result[:limit]


def analyze(frame: pd.DataFrame) -> dict[str, Any]:
    if frame.empty:
        return {"empty": True}

    frame = frame.copy()
    frame["month"] = frame["occurred_at"].dt.strftime("%Y-%m")
    frame["date"] = frame["occurred_at"].dt.date
    frame["weekday"] = pd.Categorical(
        frame["occurred_at"].dt.day_name().str[:3],
        categories=["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"], ordered=True,
    )
    frame["hour"] = frame["occurred_at"].dt.hour

    currencies = sorted(frame["currency"].dropna().astype(str).unique().tolist())
    currency = currencies[0] if len(currencies) == 1 else "MIXED"

    expenses = frame[frame["tx_type"] == "expense"].copy()
    income = frame[frame["tx_type"] == "income"].copy()
    transfers = frame[frame["tx_type"] == "transfer"].copy()
    other = frame[frame["tx_type"] == "other"].copy()

    total_spend = float(expenses["amount"].sum())
    total_income = float(income["amount"].sum())
    total_transfers = float(transfers["amount"].sum())
    total_other = float(other["amount"].sum())
    net_cash_flow = total_income - total_spend

    min_date = frame["occurred_at"].min().normalize()
    max_date = frame["occurred_at"].max().normalize()
    calendar_days = max((max_date - min_date).days + 1, 1)
    avg_day = total_spend / calendar_days
    median_tx = float(expenses["amount"].median()) if len(expenses) else 0.0
    avg_tx = float(expenses["amount"].mean()) if len(expenses) else 0.0

    date_index = pd.date_range(min_date, max_date, freq="D")
    daily = expenses.groupby(expenses["occurred_at"].dt.normalize())["amount"].sum().reindex(date_index, fill_value=0.0)
    rolling = daily.rolling(7, min_periods=1).mean()
    if len(daily) >= 2:
        x = np.arange(len(daily), dtype=float)
        slope = float(np.polyfit(x, daily.values.astype(float), 1)[0])
    else:
        slope = 0.0
    daily_rows = [
        {
            "date": idx.strftime("%Y-%m-%d"), "label": idx.strftime("%d %b"),
            "spend": round(float(daily.loc[idx]), 2), "rolling7": round(float(rolling.loc[idx]), 2),
        }
        for idx in date_index
    ]

    if len(expenses):
        category_stats = expenses.groupby("category")["amount"].agg(["sum", "count"]).sort_values("sum", ascending=False)
    else:
        category_stats = pd.DataFrame(columns=["sum", "count"])
    category_rows = [
        {
            "category": str(idx), "spend": round(float(row["sum"]), 2), "count": int(row["count"]),
            "share": round(float(row["sum"] / total_spend * 100), 1) if total_spend else 0,
        }
        for idx, row in category_stats.iterrows()
    ]

    account_stats = expenses.groupby("account")["amount"].sum().sort_values(ascending=False) if len(expenses) else pd.Series(dtype=float)
    account_rows = [{"account": str(k), "spend": round(float(v), 2)} for k, v in account_stats.items()]

    weekday = (
        expenses.groupby("weekday", observed=False)["amount"].agg(["sum", "count"])
        .reindex(["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]).fillna(0)
    )
    weekday_rows = [
        {"day": idx, "spend": round(float(row["sum"]), 2), "count": int(row["count"])}
        for idx, row in weekday.iterrows()
    ]

    def time_bucket(hour: int) -> str:
        if 5 <= hour < 12:
            return "Morning"
        if 12 <= hour < 17:
            return "Afternoon"
        if 17 <= hour < 22:
            return "Evening"
        return "Night"

    if len(expenses):
        expenses["time_bucket"] = expenses["hour"].map(time_bucket)
        tod = expenses.groupby("time_bucket", observed=True)["amount"].agg(["sum", "count"])
    else:
        tod = pd.DataFrame(columns=["sum", "count"])
    time_rows = [
        {
            "bucket": b,
            "spend": round(float(tod.loc[b, "sum"]), 2) if b in tod.index else 0,
            "count": int(tod.loc[b, "count"]) if b in tod.index else 0,
        }
        for b in ["Morning", "Afternoon", "Evening", "Night"]
    ]

    monthly = expenses.groupby("month", observed=True)["amount"].sum().sort_index()
    monthly_rows = []
    prev: float | None = None
    for month, val in monthly.items():
        value = float(val)
        monthly_rows.append({
            "month": month, "label": pd.Timestamp(month + "-01").strftime("%b %Y"),
            "spend": round(value, 2), "mom": safe_pct(value, prev) if prev is not None else None,
        })
        prev = value

    if len(expenses):
        pcm = expenses.pivot_table(
            index="month", columns="category", values="amount", aggfunc="sum", fill_value=0, observed=True,
        ).sort_index()
    else:
        pcm = pd.DataFrame()
    category_month_rows = []
    for month, row in pcm.iterrows():
        for cat, value in row.items():
            if value:
                category_month_rows.append({"month": str(month), "category": str(cat), "spend": round(float(value), 2)})

    anomalies = []
    if len(expenses) >= 4:
        q1, q3 = expenses["amount"].quantile([0.25, 0.75])
        threshold = float(q3 + 1.5 * (q3 - q1))
        outliers = expenses[expenses["amount"] > threshold].sort_values("amount", ascending=False).head(12)
    else:
        outliers = expenses.nlargest(12, "amount")
    for row in outliers.itertuples(index=False):
        anomalies.append({
            "date": row.occurred_at.strftime("%d %b %Y"), "amount": round(float(row.amount), 2),
            "category": row.category, "notes": row.notes, "account": row.account,
            "multipleOfMedian": round(float(row.amount / median_tx), 1) if median_tx else None,
        })

    top_txs = [
        {
            "date": row.occurred_at.strftime("%d %b"), "amount": round(float(row.amount), 2),
            "category": row.category, "notes": row.notes, "account": row.account,
        }
        for row in expenses.nlargest(12, "amount").itertuples(index=False)
    ]

    top5 = float(expenses.nlargest(5, "amount")["amount"].sum()) if len(expenses) else 0
    concentration = top5 / total_spend * 100 if total_spend else 0
    active_days = int(expenses["date"].nunique()) if len(expenses) else 0
    no_spend_days = max(calendar_days - active_days, 0)
    sorted_daily = sorted(daily_rows, key=lambda r: r["spend"], reverse=True)
    peak_day = sorted_daily[0] if sorted_daily else {"label": "—", "spend": 0}
    category_leader = category_rows[0] if category_rows else {"category": "—", "spend": 0, "share": 0}
    weekday_leader = max(weekday_rows, key=lambda r: r["spend"], default={"day": "—", "spend": 0})

    insights: list[dict[str, str]] = []
    if category_rows:
        insights.append({
            "title": "Largest category",
            "text": f"{category_leader['category']} represents {category_leader['share']}% of spending.",
        })
    if peak_day["spend"]:
        insights.append({"title": "Peak spend day", "text": f"{peak_day['label']} was the highest-spend day in this period."})
    if concentration:
        insights.append({"title": "Spend concentration", "text": f"The five largest expenses make up {concentration:.1f}% of total spend."})
    trend_text = "rising" if slope > 25 else "falling" if slope < -25 else "mostly flat"
    insights.append({"title": "Daily trend", "text": f"Day-by-day spending is {trend_text} over the selected range."})
    if no_spend_days > 0:
        insights.append({"title": "No-spend days", "text": f"{no_spend_days} of {calendar_days} calendar days had no recorded expenses."})
    if weekday_leader["spend"]:
        insights.append({"title": "Weekday pattern", "text": f"{weekday_leader['day']} has the highest aggregate spending."})
    if total_other:
        insights.append({"title": "Unclassified direction", "text": f"{len(other)} rows could not be confidently classified as expense, income, or transfer."})

    return {
        "empty": False,
        "currency": currency,
        "currencies": currencies,
        "period": {"start": min_date.strftime("%Y-%m-%d"), "end": max_date.strftime("%Y-%m-%d"), "days": calendar_days},
        "kpis": {
            "spend": round(total_spend, 2), "income": round(total_income, 2), "transfers": round(total_transfers, 2),
            "other": round(total_other, 2), "netCashFlow": round(net_cash_flow, 2), "avgDay": round(avg_day, 2),
            "avgTransaction": round(avg_tx, 2), "medianTransaction": round(median_tx, 2),
            "expenseCount": int(len(expenses)), "incomeCount": int(len(income)), "transferCount": int(len(transfers)),
            "otherCount": int(len(other)), "activeDays": active_days, "noSpendDays": no_spend_days,
            "top5Concentration": round(concentration, 1), "dailySlope": round(slope, 2),
        },
        "daily": daily_rows,
        "categories": category_rows,
        "accounts": account_rows,
        "weekdays": weekday_rows,
        "timeOfDay": time_rows,
        "monthly": monthly_rows,
        "categoryByMonth": category_month_rows,
        "anomalies": anomalies,
        "topTransactions": top_txs,
        "recurringPatterns": merchant_patterns(expenses),
        "keywords": top_keywords(expenses["notes"].tolist()),
        "insights": insights,
    }


def apply_filters(
    frame: pd.DataFrame,
    months: list[str] | None,
    categories: list[str] | None,
    accounts: list[str] | None,
    currencies: list[str] | None,
    types: list[str] | None,
) -> pd.DataFrame:
    if frame.empty:
        return frame
    out = frame.copy()
    if months:
        out = out[out["occurred_at"].dt.strftime("%Y-%m").isin(months)]
    if categories:
        out = out[out["category"].isin(categories)]
    if accounts:
        out = out[out["account"].isin(accounts)]
    if currencies:
        out = out[out["currency"].isin(currencies)]
    if types:
        out = out[out["tx_type"].isin(types)]
    return out


@app.on_event("startup")
def startup() -> None:
    init_db()
    seed_sample()


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok", "version": "2.0"}


@app.post("/api/import-file")
async def import_file(
    file: UploadFile = File(...),
    mapping_json: str | None = Form(default=None),
    default_currency: str = Form(default="INR"),
) -> dict[str, Any]:
    name = file.filename or "upload.csv"
    if not name.lower().endswith((".csv", ".txt")):
        raise HTTPException(status_code=400, detail="Upload a CSV or delimited text file.")
    raw = await file.read()
    mapping: dict[str, str] | None = None
    if mapping_json:
        try:
            value = json.loads(mapping_json)
            if not isinstance(value, dict):
                raise ValueError
            mapping = {str(k): str(v) for k, v in value.items() if v}
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid mapping JSON.")
    try:
        return import_bytes(raw, name, mapping_override=mapping, default_currency=default_currency, require_confident=True)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/api/preview-file")
async def preview_file(file: UploadFile = File(...)) -> dict[str, Any]:
    name = file.filename or "upload.csv"
    try:
        return preview_csv(await file.read(), name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/api/import")
async def import_csv(files: list[UploadFile] = File(...)) -> dict[str, Any]:
    """Backward-compatible bulk auto-import endpoint."""
    results, errors = [], []
    for uploaded in files:
        name = uploaded.filename or "upload.csv"
        try:
            result = import_bytes(await uploaded.read(), name, require_confident=True)
            if result.get("status") == "needs_mapping":
                errors.append({"filename": name, "error": "Column mapping required", "preview": result.get("preview")})
            else:
                results.append(result)
        except Exception as exc:
            errors.append({"filename": name, "error": str(exc)})
    return {"results": results, "errors": errors}


@app.get("/api/meta")
def meta() -> dict[str, Any]:
    frame = load_frame()
    with db() as conn:
        imports = [
            dict(r) for r in conn.execute(
                """SELECT id, filename, imported_at, row_count, month_start, month_end, currency,
                   mapping_json, warnings_json FROM imports ORDER BY imported_at DESC"""
            ).fetchall()
        ]
    for item in imports:
        try:
            item["mapping"] = json.loads(item.pop("mapping_json") or "{}")
        except Exception:
            item["mapping"] = {}
            item.pop("mapping_json", None)
        try:
            item["warnings"] = json.loads(item.pop("warnings_json") or "[]")
        except Exception:
            item["warnings"] = []
            item.pop("warnings_json", None)

    if frame.empty:
        return {"months": [], "categories": [], "accounts": [], "currencies": [], "types": [], "imports": imports}

    months = sorted(frame["occurred_at"].dt.strftime("%Y-%m").unique().tolist(), reverse=True)
    # Transfer exports often encode a route (A->B) in the account field. Keep those out of normal spend filters.
    normal_accounts = frame[frame["tx_type"] != "transfer"]["account"].dropna().astype(str)
    return {
        "months": [{"value": m, "label": pd.Timestamp(m + "-01").strftime("%B %Y")} for m in months],
        "categories": sorted(frame["category"].dropna().astype(str).unique().tolist()),
        "accounts": sorted(normal_accounts.unique().tolist()),
        "currencies": sorted(frame["currency"].dropna().astype(str).unique().tolist()),
        "types": sorted(frame["tx_type"].dropna().astype(str).unique().tolist()),
        "imports": imports,
    }


@app.get("/api/dashboard")
def dashboard(
    months: list[str] | None = Query(default=None),
    categories: list[str] | None = Query(default=None),
    accounts: list[str] | None = Query(default=None),
    currencies: list[str] | None = Query(default=None),
    types: list[str] | None = Query(default=None),
) -> dict[str, Any]:
    return analyze(apply_filters(load_frame(), months, categories, accounts, currencies, types))


@app.get("/api/transactions")
def transactions(
    months: list[str] | None = Query(default=None),
    categories: list[str] | None = Query(default=None),
    accounts: list[str] | None = Query(default=None),
    currencies: list[str] | None = Query(default=None),
    types: list[str] | None = Query(default=None),
    q: str | None = None,
    limit: int = Query(default=300, ge=1, le=3000),
) -> dict[str, Any]:
    frame = apply_filters(load_frame(), months, categories, accounts, currencies, types)
    if q and not frame.empty:
        needle = q.lower().strip()
        mask = (
            frame["notes"].astype(str).str.lower().str.contains(needle, regex=False)
            | frame["category"].astype(str).str.lower().str.contains(needle, regex=False)
            | frame["account"].astype(str).str.lower().str.contains(needle, regex=False)
            | frame["source_file"].astype(str).str.lower().str.contains(needle, regex=False)
        )
        frame = frame[mask]
    total = len(frame)
    frame = frame.sort_values("occurred_at", ascending=False).head(limit)
    rows = [
        {
            "id": int(r.id), "date": r.occurred_at.strftime("%d %b %Y"), "time": r.occurred_at.strftime("%I:%M %p"),
            "type": r.tx_type, "amount": round(float(r.amount), 2), "category": r.category,
            "account": r.account, "notes": r.notes, "source": r.source_file, "currency": r.currency,
        }
        for r in frame.itertuples(index=False)
    ]
    return {"count": total, "shown": len(rows), "rows": rows}


@app.delete("/api/imports/{import_id}")
def delete_import(import_id: int) -> dict[str, Any]:
    with db() as conn:
        row = conn.execute("SELECT filename FROM imports WHERE id = ?", (import_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Import not found")
        conn.execute("DELETE FROM transactions WHERE import_id = ?", (import_id,))
        conn.execute("DELETE FROM imports WHERE id = ?", (import_id,))
    return {"status": "deleted", "import_id": import_id}


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")
