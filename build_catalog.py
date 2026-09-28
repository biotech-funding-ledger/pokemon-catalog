"""
build_catalog.py  -  Rebuild the "Data" tab of the Pokemon sheet entirely from PriceCharting.

Runs on GitHub Actions every morning (see .github/workflows/update_catalog.yml),
or from VS Code with the play button.

What it does
  1. Gets the PriceCharting Pokemon CSV:
       - on GitHub it downloads a fresh copy using the PC_DOWNLOAD_URL secret
       - locally it uses pc_download_url.txt if present, else the pricecharting.csv
         already in the folder (PriceCharting regenerates the file once a day)
  2. Keeps every PriceCharting product (see the two flags below)
  3. Orders sets by release date, and cards by card number within each set
  4. Wipes the Data tab and writes columns A-L
  5. Archives a dated, gzipped snapshot in snapshots/ (GitHub commits it back to the repo,
     so a price history accumulates one file per day)

Credentials
  GitHub secrets: PC_DOWNLOAD_URL  and  GCP_SERVICE_ACCOUNT_JSON (contents of service_account.json)
  Locally:        pc_download_url.txt (optional)  and  service_account.json

New layout
  A Card Name | B Set Name | C Card ID | D Card # | E Release Date | F PC Sales Volume
  G PC Ungraded $ | H PC Grade 9 $ | I PC PSA 10 $ | J TCGplayer ID | K eBay ePID | L Updated
"""
import json
import os
import re
from datetime import date

import gspread
import pandas as pd
import requests

SHEET_ID = "12pB-ZT-DZipOx3UllH7mY9b1JI6pKYJmnJ8yTrARy4E"
TAB_NAME = "Data"
PC_CSV = "pricecharting.csv"
URL_FILE = "pc_download_url.txt"     # optional - your personal CSV link, one line
SNAPSHOT_DIR = "snapshots"
CHUNK = 5000

ENGLISH_ONLY = True      # False -> every language PriceCharting tracks (Japanese, Korean, ...)
SINGLES_ONLY = True      # False -> also keep booster boxes, packs, tins, decks (no "#" in name)

NON_ENGLISH = re.compile(
    r"\b(?:Japanese|Korean|Chinese|Thai|Indonesian|German|French|Italian|Spanish|"
    r"Portuguese|Dutch|Russian|Polish)\b", re.I)

HEADERS = ["Card Name", "Set Name", "Card ID", "Card #", "Release Date",
           "PC Sales Volume", "PC Ungraded $", "PC Grade 9 $", "PC PSA 10 $",
           "TCGplayer ID", "eBay ePID", "Updated"]


# ---------- helpers ----------
def money(v):
    v = str(v).replace("$", "").replace(",", "").strip()
    try:
        return round(float(v), 2)
    except ValueError:
        return ""


def integer(v):
    v = str(v).replace(",", "").strip()
    try:
        return int(float(v))
    except ValueError:
        return ""


def card_number(name):
    m = re.search(r"#(\S+)\s*$", name)
    return m.group(1) if m else ""


def number_parts(num):
    """'4' -> ('', 4, '')   'SWSH001' -> ('SWSH', 1, '')   'TG01a' -> ('TG', 1, 'a')"""
    m = re.match(r"^(\D*?)(\d+)(.*)$", num)
    if m:
        return m.group(1).upper(), int(m.group(2)), m.group(3)
    return num.upper(), 10**9, ""


# ---------- 1. Get the PriceCharting CSV ----------
url = os.environ.get("PC_DOWNLOAD_URL", "").strip()
if not url and os.path.exists(URL_FILE):
    url = open(URL_FILE, encoding="utf-8").read().strip()
if url:
    print("Downloading a fresh Pokemon CSV from PriceCharting ...")
    r = requests.get(url, timeout=300)
    r.raise_for_status()
    if "product-name" not in r.text[:1000]:
        raise SystemExit("That link did not return a CSV (got a web page instead). "
                         "Re-copy the Pokemon Cards download link from "
                         "Subscriptions -> API/Download. Note: one CSV download per 10 minutes.")
    with open(PC_CSV, "w", encoding="utf-8", newline="") as f:
        f.write(r.text)
    print(f"Saved to {PC_CSV}")

if not os.path.exists(PC_CSV):
    raise SystemExit(f"{PC_CSV} not found. On GitHub, add the PC_DOWNLOAD_URL secret. "
                     f"Locally, download the Pokemon Cards CSV from PriceCharting "
                     f"(Subscriptions -> API/Download) into this folder, "
                     f"or put your download link in {URL_FILE}.")

pc = pd.read_csv(PC_CSV, dtype=str).fillna("")
need = {"id", "console-name", "product-name"}
if not need <= set(pc.columns):
    print("Columns found:", list(pc.columns))
    raise SystemExit("Unexpected CSV format - expected id, console-name, product-name.")
print(f"PriceCharting rows loaded: {len(pc):,}")


def col(name):
    return pc[name] if name in pc.columns else pd.Series([""] * len(pc), index=pc.index)


# ---------- 2. Filter ----------
if ENGLISH_ONLY:
    before = len(pc)
    pc = pc[~pc["console-name"].str.contains(NON_ENGLISH)].copy()
    print(f"English-only filter: {before - len(pc):,} non-English rows removed")

pc["card_no"] = pc["product-name"].map(card_number)
if SINGLES_ONLY:
    dropped = pc[pc["card_no"] == ""]
    if len(dropped):
        os.makedirs(SNAPSHOT_DIR, exist_ok=True)
        dropped[["id", "console-name", "product-name"]].to_csv(
            os.path.join(SNAPSHOT_DIR, "dropped_no_card_number.csv"), index=False)
    pc = pc[pc["card_no"] != ""].copy()
    print(f"Singles-only filter: {len(dropped):,} products without a card # removed "
          f"(list saved to {SNAPSHOT_DIR}/dropped_no_card_number.csv)")

# ---------- 3. Order: sets by release date, cards by number ----------
pc["release"] = col("release-date").str.strip()
set_date = (pc[pc["release"] != ""].groupby("console-name")["release"].min())
pc["set_date"] = pc["console-name"].map(set_date).fillna("9999-12-31")

parts = pc["card_no"].map(number_parts)
pc["n_prefix"] = [p[0] for p in parts]
pc["n_num"] = [p[1] for p in parts]
pc["n_suffix"] = [p[2] for p in parts]
pc = pc.sort_values(["set_date", "console-name", "n_prefix", "n_num", "n_suffix", "product-name"],
                    kind="stable")

# ---------- 4. Build the rows ----------
today = date.today().isoformat()
out = pd.DataFrame({
    "Card Name":       pc["product-name"].str.strip(),
    "Set Name":        pc["console-name"].str.strip(),
    "Card ID":         pc["id"].map(integer),
    "Card #":          pc["card_no"],
    "Release Date":    pc["release"],
    "PC Sales Volume": col("sales-volume").map(integer),
    "PC Ungraded $":   col("loose-price").map(money),
    "PC Grade 9 $":    col("graded-price").map(money),
    "PC PSA 10 $":     col("manual-only-price").map(money),
    "TCGplayer ID":    col("tcg-id").map(integer),
    "eBay ePID":       col("epid").map(integer),
    "Updated":         today,
})
values = [HEADERS] + out.values.tolist()
n_cards = len(out)
n_sets = out["Set Name"].nunique()
print(f"Ready to write {n_cards:,} cards across {n_sets} sets.")

# ---------- 5. Snapshot ----------
os.makedirs(SNAPSHOT_DIR, exist_ok=True)
snap = os.path.join(SNAPSHOT_DIR, f"pc_catalog_{today}.csv.gz")
out.to_csv(snap, index=False, compression="gzip")   # pandas reads .csv.gz directly
print(f"Snapshot archived: {snap}")

# ---------- 6. Write the sheet ----------
sa_json = os.environ.get("GCP_SERVICE_ACCOUNT_JSON", "").strip()
if sa_json:
    gc = gspread.service_account_from_dict(json.loads(sa_json))
else:
    gc = gspread.service_account(filename="service_account.json")
ws = gc.open_by_key(SHEET_ID).worksheet(TAB_NAME)
print("Connected to sheet - wiping the Data tab ...")
ws.clear()
ws.resize(rows=len(values) + 10, cols=len(HEADERS))

for start in range(0, len(values), CHUNK):
    chunk = values[start:start + CHUNK]
    ws.update(chunk, f"A{start + 1}", value_input_option="RAW")
    print(f"  wrote rows {start + 1:,}-{start + len(chunk):,}")

ws.freeze(rows=1)
print(f"Done - {n_cards:,} PriceCharting cards in {n_sets} sets, "
      f"release order, columns A-L.")
