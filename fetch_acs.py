"""
fetch_acs.py
------------
Pull ACS 5-year demographic / socioeconomic estimates from the Census Data API
at the ZCTA level (ZIP Code Tabulation Area) -- the geography that matches the
UCMR "ZIP Codes Served" field -- and write a tidy Parquet ready to join.

Why ZCTA: UCMR locates each system by ZIP(s) served (a ZIP-area centroid or a
service-area centroid), so the honest join unit is the ZIP/ZCTA, not the tract.
ACS publishes ZCTA estimates only in the 5-year (acs5) product.

Setup (per the Census Data API User Guide):
  1. Register a free key: https://www.census.gov/data/developers.html
  2. export CENSUS_API_KEY=your_key_here
  3. pip install requests pandas pyarrow
  4. python fetch_acs.py --year 2023 --out-dir data/processed

Note on geography: ACS ZCTAs are not identical to USPS ZIP codes. Most UCMR ZIPs
match a ZCTA directly; for the minority that don't, use a ZIP->ZCTA crosswalk
(HUD-USPS or Census ZCTA relationship file) when you do the join.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from urllib.parse import urlencode, quote

import pandas as pd
import requests

# ACS detailed-table variables -> friendly names. <50 vars (API limit). Edit freely.
ACS_VARS = {
    "B01003_001E": "total_pop",
    "B19013_001E": "median_hh_income",
    "B17001_001E": "poverty_universe",
    "B17001_002E": "poverty_below",
    "B03002_001E": "race_universe",
    "B03002_003E": "white_nh",
    "B03002_004E": "black_nh",
    "B03002_005E": "amerindian_alaska_nh",
    "B03002_006E": "asian_nh",
    "B03002_012E": "hispanic",
    "B01002_001E": "median_age",
    "B25003_001E": "tenure_universe",
    "B25003_003E": "renter_occupied",
}
# ACS annotation/jam values that mean "no estimate" (negatives). Treat as NaN.
ACS_JAM_FLOOR = -666_666_665   # anything <= this is a jam value


def fetch(year: int, key: str) -> pd.DataFrame:
    base = f"https://api.census.gov/data/{year}/acs/acs5"
    get_cols = ["NAME", *ACS_VARS.keys(), "GEO_ID"]
    params = {
        "get": ",".join(get_cols),
        "for": "zip code tabulation area:*",
        "key": key,
    }
    # Encode spaces as %20 (the API expects %20, not '+') and keep : and *.
    url = f"{base}?{urlencode(params, quote_via=quote, safe=':*')}"

    r = requests.get(url, timeout=120)
    if r.status_code != 200:
        sys.exit(f"Census API error {r.status_code}: {r.text[:300]}")
    rows = r.json()
    df = pd.DataFrame(rows[1:], columns=rows[0])
    return df


def tidy(df: pd.DataFrame) -> pd.DataFrame:
    df = df.rename(columns={**ACS_VARS, "zip code tabulation area": "ZCTA"})
    df["ZCTA"] = df["ZCTA"].astype("string").str.zfill(5)   # keep leading zeros

    num = list(ACS_VARS.values())
    for c in num:
        df[c] = pd.to_numeric(df[c], errors="coerce")
        df.loc[df[c] <= ACS_JAM_FLOOR, c] = pd.NA              # drop jam values

    # Derived EJ measures (guard against divide-by-zero).
    def pct(n, d):
        return (df[n] / df[d] * 100).where(df[d] > 0)

    df["pct_below_poverty"] = pct("poverty_below", "poverty_universe")
    df["pct_white_nh"]      = pct("white_nh", "race_universe")
    df["pct_black_nh"]      = pct("black_nh", "race_universe")
    df["pct_asian_nh"]      = pct("asian_nh", "race_universe")
    df["pct_amerind_nh"]    = pct("amerindian_alaska_nh", "race_universe")
    df["pct_hispanic"]      = pct("hispanic", "race_universe")
    df["pct_nonwhite"]      = 100 - df["pct_white_nh"]
    df["pct_renter"]        = pct("renter_occupied", "tenure_universe")

    keep = ["ZCTA", "GEO_ID", "NAME", *num,
            "pct_below_poverty", "pct_white_nh", "pct_black_nh", "pct_asian_nh",
            "pct_amerind_nh", "pct_hispanic", "pct_nonwhite", "pct_renter"]
    return df[keep]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--year", type=int, default=2023,
                    help="ACS5 vintage; confirm latest at https://api.census.gov/data.html")
    ap.add_argument("--out-dir", type=Path, default=Path("data/processed"))
    args = ap.parse_args()

    key = os.environ.get("CENSUS_API_KEY")
    if not key:
        sys.exit("Set CENSUS_API_KEY (register free at "
                 "https://www.census.gov/data/developers.html).")

    args.out_dir.mkdir(parents=True, exist_ok=True)
    out = args.out_dir / f"acs5_{args.year}_zcta.parquet"

    print(f"Fetching ACS5 {args.year} ZCTA estimates ({len(ACS_VARS)} variables) ...")
    df = tidy(fetch(args.year, key))
    print(f"  {len(df):,} ZCTAs")
    df.to_parquet(out, engine="pyarrow", compression="zstd", index=False)
    print(f"Wrote {out}  ({out.stat().st_size/1e6:.1f} MB)")
    print("Join key: ACS 'ZCTA' <-> UCMR 'ZIP Codes Served' "
          "(use a ZIP->ZCTA crosswalk for non-matching ZIPs).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())