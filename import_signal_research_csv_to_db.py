import os
import pandas as pd

from database import init_db, insert_signal_research, count_rows, preview_signal_research


CSV_FILE = "signal_research.csv"


def clean_value(value):
    if pd.isna(value):
        return None
    return value


def main():
    init_db()

    if not os.path.exists(CSV_FILE):
        print(f"{CSV_FILE} not found.")
        return

    df = pd.read_csv(CSV_FILE, encoding="utf-8-sig", dtype={"code": str})

    inserted = 0
    skipped = 0

    for _, row in df.iterrows():
        signal = {
            "entry_time": clean_value(row.get("entry_time")),
            "exit_time": clean_value(row.get("exit_time")),
            "code": str(clean_value(row.get("code"))).zfill(6),
            "name": clean_value(row.get("name")),
            "entry_price": clean_value(row.get("entry_price")),
            "last_price": clean_value(row.get("last_price")),
            "setup_quality": clean_value(row.get("setup_quality")),
            "change_rate": clean_value(row.get("change_rate")),
            "strength": clean_value(row.get("strength")),
            "theme_score": clean_value(row.get("theme_score")),
            "matched_themes": clean_value(row.get("matched_themes")),
            "rule_version": clean_value(row.get("rule_version")),
            "ret_5m": clean_value(row.get("ret_5m")),
            "ret_10m": clean_value(row.get("ret_10m")),
            "ret_30m": clean_value(row.get("ret_30m")),
            "max_return_30m": clean_value(row.get("max_return_30m")),
            "min_return_30m": clean_value(row.get("min_return_30m")),
            "first_touch": clean_value(row.get("first_touch")),
            "first_touch_time": clean_value(row.get("first_touch_time")),
            "first_touch_return": clean_value(row.get("first_touch_return")),
        }

        for key in ("trading_value", "change_rate_bucket", "strength_bucket", "trading_value_bucket", "momentum_efficiency"):
            signal[key] = clean_value(row.get(key))
        for minute in (5, 10, 30):
            for field in ("target_timestamp", "observed_timestamp", "price", "status"):
                key = f"{field}_{minute}m"
                signal[key] = clean_value(row.get(key))
        if insert_signal_research(signal):
            inserted += 1
        else:
            skipped += 1

    print("CSV import complete.")
    print(f"Inserted rows: {inserted}")
    print(f"Skipped duplicates: {skipped}")
    print(f"Total signal_research rows: {count_rows('signal_research')}")

    print("\nLatest rows:")
    rows = preview_signal_research(limit=10)

    for row in rows:
        print(row)


if __name__ == "__main__":
    main()