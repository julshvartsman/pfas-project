"""
pfas_to_parquet.py -- convert one EPA UCMR PFAS export (Excel or CSV) to Parquet.

The PFAS Analytic Tools "PWS with Single Result(s) >= UCMR MRL" filter operates at the
*system* level, and the full result set exceeds Excel's ~1.05M-row cap. So download the
data in two halves -- one filtered to detections (Result At or Above UCMR MRL = Yes) and
one to non-detections (= No) -- and convert each separately, then concatenate downstream.

Usage:
    pip install pandas pyarrow python-calamine openpyxl
    python pfas_to_parquet.py INPUT [OUTPUT] [--kind {auto,detect,nondetect}] [--nd-value {0,half-mrl}]

Detection status is derived PER ROW from the "Result At or Above UCMR MRL" column whenever
that column is present, so handling is identical and correct for either half. --kind is a
label written to the manifest plus a sanity check: if the file's rows don't match the kind
you claim (e.g. a "detect" file containing "No" rows), it warns you -- which is how you spot
an accidental system-level download that would double-count on concatenation.

Non-detections are substituted with 0 (default) so they enter the mean as real zeros;
is_detection is preserved, so detection *rates* remain fully recoverable. Reads everything
as text first (keeps leading-zero IDs like PWS "AK2110342", ZIP "06338").
"""
import argparse
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
RESULT = "Analytical Result Value (ng/L)"
MRL = "Minimum Reporting Level (ng/L)"
DET_COL = "Result At or Above UCMR MRL"


def read_any(src: Path) -> pd.DataFrame:
    """Read Excel or CSV/TSV as all-text (dtype=str) to preserve leading-zero IDs."""
    ext = src.suffix.lower()
    if ext in (".csv", ".tsv"):
        return pd.read_csv(src, dtype=str, sep=("\t" if ext == ".tsv" else ","))
    try:
        return pd.read_excel(src, dtype=str, engine="calamine")   # fast/low-memory
    except ImportError:
        return pd.read_excel(src, dtype=str, engine="openpyxl")


def main():
    ap = argparse.ArgumentParser(description="Convert one EPA UCMR PFAS export to Parquet.")
    ap.add_argument("input", help="path to the .xlsx/.csv export")
    ap.add_argument("output", nargs="?", help="output .parquet (default: alongside input)")
    ap.add_argument("--kind", choices=["auto", "detect", "nondetect"], default="auto",
                    help="which half this file is; label + sanity check (see module docstring)")
    ap.add_argument("--nd-value", choices=["0", "half-mrl"], default="0",
                    help="value substituted for non-detects (default 0)")
    args = ap.parse_args()

    src = Path(args.input)
    if args.output:
        out = Path(args.output)
    elif args.kind != "auto":
        out = src.with_name(f"{src.stem}_{args.kind}.parquet")
    else:
        out = src.with_suffix(".parquet")

    print(f"Reading {src} (kind={args.kind}) ...")
    df = read_any(src)
    print(f"  {len(df):,} rows x {df.shape[1]} columns")            # sanity-check vs the tool
    if len(df) >= 1_048_575:
        print("  WARNING: at Excel's row cap -- this half may be truncated. Re-download as CSV.")

    df.columns = [str(c).strip() for c in df.columns]
    df = df[df["PWS ID"].notna()]
    df = df[df["PWS ID"].str.strip().str.lower() != "totals"]       # drop export Totals row
    df = df.replace(SENTINELS, pd.NA)
    for c in df.columns:
        if pd.api.types.is_string_dtype(df[c]) or df[c].dtype == object:
            df[c] = df[c].str.strip()
    for c in NUMERIC:
        if c in df: df[c] = pd.to_numeric(df[c], errors="coerce")
    for c in DATES:
        if c in df: df[c] = pd.to_datetime(df[c], errors="coerce")

    # --- detection status: per-row from the EPA flag when present, else fall back to --kind ---
    if DET_COL in df:
        df["is_detection"] = df[DET_COL].astype("string").str.strip().str.casefold().eq("yes")
        det = df["is_detection"].fillna(False)
        if args.kind == "detect":
            n = int((~det).sum())
            if n:
                print(f"  NOTE: {n:,} rows in this 'detect' file are flagged '{DET_COL}' != Yes. "
                      f"The >=MRL filter is system-level, so non-detects can ride along -- "
                      f"they'll be treated as non-detects (set to {args.nd_value}), which is correct, "
                      f"but make sure your 'nondetect' half doesn't also include them (double-count).")
        elif args.kind == "nondetect":
            n = int(det.sum())
            if n:
                print(f"  NOTE: {n:,} rows in this 'nondetect' file are flagged '{DET_COL}' == Yes; "
                      f"they'll be kept as detections. Check your download filter.")
    else:
        df["is_detection"] = {"detect": True, "nondetect": False}.get(args.kind, pd.NA)
        print(f"  NOTE: '{DET_COL}' not found; set is_detection from --kind={args.kind}.")

    # --- substitute non-detects so they enter the mean as real values ---
    is_det = df["is_detection"].fillna(False).astype(bool)
    if RESULT in df:
        if args.nd_value == "half-mrl" and MRL in df:
            df.loc[~is_det, RESULT] = df.loc[~is_det, MRL] / 2.0
        else:
            df.loc[~is_det, RESULT] = 0.0

    df = df.reset_index(drop=True)
    df.to_parquet(out, compression="zstd", index=False)
    detections = int(is_det.sum())
    print(f"Wrote {out}  ({out.stat().st_size/1e6:.1f} MB, detections: {detections:,}, "
          f"non-detects set to {args.nd_value})")

    # --- data-retrieval manifest ---
    dates = df["Collection Date"].dropna() if "Collection Date" in df else pd.Series([], dtype="datetime64[ns]")
    manifest = {
        "generated_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": "EPA PFAS Analytic Tools -- Drinking Water (UCMR) layer",
        "source_url": SOURCE_URL,
        "access_method": "Downloaded from the PFAS Analytic Tools (Drinking Water UCMR tab)",
        "kind": args.kind,
        "nondetect_substitution": args.nd_value,
        "filter_note": ("Downloaded in two halves (detections / non-detections) to stay under "
                        "Excel's ~1.05M-row cap; detection status is per-row from "
                        f"'{DET_COL}'. Concatenate the two parquets downstream."),
        "download_date": "FILL_ME_IN",
        "version_note": "UCMR 5 is revised quarterly through fall 2026; treat as versioned.",
        "source_file": src.name,
        "output_parquet": out.name,
        # computed from the data, so the stated handling is verifiable:
        "rows_total": int(len(df)),
        "detections": detections,
        "non_detections": int(len(df) - detections),
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