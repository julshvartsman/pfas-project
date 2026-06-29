"""
pfas_to_parquet.py -- convert one EPA UCMR PFAS Excel export to Parquet.

Usage:
    pip install pandas pyarrow python-calamine openpyxl
    python pfas_to_parquet.py path/to/pfas_complete.xlsx [output.parquet]

Reads everything as text (keeps leading-zero IDs like PWS "AK2110342", ZIP "06338"),
converts EPA's "-" missing-value sentinel to null, then casts numerics/dates and adds
an is_detection flag. Writes zstd Parquet. Run once; work off the Parquet afterward.
"""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
import pandas as pd

SOURCE_URL = "https://awsedap.epa.gov/public/extensions/PFAS_Tools/PFAS_Tools.html"

SENTINELS = ["-", "", "N/A", "NA"]
NUMERIC = ["Minimum Reporting Level (ng/L)", "Analytical Result Value (ng/L)",
           "Latitude", "Longitude", "Population Served", "Population Served Year",
           "Results", "UCMR_RAA_Active"]
DATES = ["Collection Date"]


def main():
    if len(sys.argv) < 2:
        sys.exit("usage: python pfas_to_parquet.py input.xlsx [output.parquet]")
    src = Path(sys.argv[1])
    out = Path(sys.argv[2]) if len(sys.argv) > 2 else src.with_suffix(".parquet")

    print(f"Reading {src} ...")
    try:
        df = pd.read_excel(src, dtype=str, engine="calamine")   # fast/low-memory
    except ImportError:
        df = pd.read_excel(src, dtype=str, engine="openpyxl")
    print(f"  {len(df):,} rows x {df.shape[1]} columns")        # sanity-check vs Excel
    if len(df) >= 1_048_575:
        print("  WARNING: at Excel's row cap -- the export may be truncated.")

    df.columns = [str(c).strip() for c in df.columns]
    df = df[df["PWS ID"].notna()]
    df = df[df["PWS ID"].str.strip().str.lower() != "totals"]   # drop export Totals row
    df = df.replace(SENTINELS, pd.NA)
    for c in df.columns:
        if pd.api.types.is_string_dtype(df[c]) or df[c].dtype == object:
            df[c] = df[c].str.strip()
    for c in NUMERIC:
        if c in df: df[c] = pd.to_numeric(df[c], errors="coerce")
    for c in DATES:
        if c in df: df[c] = pd.to_datetime(df[c], errors="coerce")
    if "Result At or Above UCMR MRL" in df:
        df["is_detection"] = (df["Result At or Above UCMR MRL"]
                              .astype("string").str.strip().str.casefold().eq("yes"))

    df = df.reset_index(drop=True)
    df.to_parquet(out, compression="zstd", index=False)
    detections = int(df["is_detection"].fillna(False).sum()) if "is_detection" in df else None
    print(f"Wrote {out}  ({out.stat().st_size/1e6:.1f} MB, detections: {detections:,})")

    # --- simple data-retrieval manifest ---
    dates = df["Collection Date"].dropna() if "Collection Date" in df else pd.Series([], dtype="datetime64[ns]")
    manifest = {
        "generated_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": "EPA PFAS Analytic Tools -- Drinking Water (UCMR) layer",
        "source_url": SOURCE_URL,
        "access_method": "Downloaded directly from the PFAS Analytic Tools (Drinking Water UCMR tab)",
        "filter_applied": "Results at or above the Minimum Reporting Level (MRL)",
        "download_date": "FILL_ME_IN",
        "version_note": "UCMR 5 is revised quarterly through fall 2026; treat as versioned.",
        "source_file": src.name,
        "output_parquet": out.name,
        # computed from the data, so the stated filter is verifiable:
        "rows_total": int(len(df)),
        "detections": detections,
        "non_detections": int(len(df) - detections) if detections is not None else None,
        "cycles": df["UCMR Cycle"].value_counts(dropna=False).to_dict() if "UCMR Cycle" in df else {},
        "epa_regions": sorted(df["EPA Region"].dropna().unique().tolist()) if "EPA Region" in df else [],
        "contaminants": int(df["Contaminant"].nunique()) if "Contaminant" in df else None,
        "collection_date_range": [str(dates.min()), str(dates.max())] if len(dates) else None,
    }
    mpath = out.with_name(out.stem + "_manifest.json")
    mpath.write_text(json.dumps(manifest, indent=2, default=str))
    print(f"Wrote {mpath}")


if __name__ == "__main__":
    main()