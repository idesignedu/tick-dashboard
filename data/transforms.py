from datetime import date, timedelta

import pandas as pd

from config import (
    GLOBAL_NAME_EXCLUSIONS,
    NEAR_BUDGET_THRESHOLD,
    PARTNER_OVERRIDES,
    RECENTLY_CLOSED_DAYS,
)


def apply_display_rules(df: pd.DataFrame) -> pd.DataFrame:
    """Remove globally excluded project names (case-insensitive substring match)."""
    if df.empty:
        return df
    if not GLOBAL_NAME_EXCLUSIONS:
        return df
    pattern = "|".join(GLOBAL_NAME_EXCLUSIONS)
    mask = df["project_full_name"].str.contains(pattern, case=False, na=False)
    return df[~mask].copy()


def apply_partner_rules(df: pd.DataFrame) -> pd.DataFrame:
    """Apply per-partner include_only / exclude_names overrides from config."""
    if df.empty or not PARTNER_OVERRIDES:
        return df

    mask = pd.Series(True, index=df.index)
    for idx, row in df.iterrows():
        partner_key = str(row.get("partner", "") or "").upper().strip()
        overrides = PARTNER_OVERRIDES.get(partner_key, {})
        name = str(row["project_full_name"])

        if "include_only" in overrides:
            if not any(t.lower() in name.lower() for t in overrides["include_only"]):
                mask.at[idx] = False
                continue

        if "exclude_names" in overrides:
            if any(t.lower() in name.lower() for t in overrides["exclude_names"]):
                mask.at[idx] = False

    return df[mask].reset_index(drop=True).copy()


def compute_metrics(df: pd.DataFrame) -> pd.DataFrame:
    """Add hours_pct, hrs_left, pct_complete, time_pct, trend, is_recently_closed."""
    df = df.copy()
    today = date.today()

    # Hours percent of budget consumed
    df["hours_pct"] = df.apply(
        lambda r: (r["total_hours"] / r["project_budget_hours"] * 100)
        if pd.notna(r["project_budget_hours"]) and r["project_budget_hours"] > 0
        else 0.0,
        axis=1,
    )

    # Hours remaining (can be negative if over budget)
    df["hrs_left"] = df.apply(
        lambda r: round(r["project_budget_hours"] - r["total_hours"], 1)
        if pd.notna(r["project_budget_hours"])
        else None,
        axis=1,
    )

    # Asana percent complete: DB stores 0–1 ratio → convert to 0–100
    df["pct_complete"] = df["asana_percent_complete"].fillna(0) * 100

    # Percent of scheduled time elapsed (uses adjusted date if set, else estimated)
    def _time_pct(row) -> float | None:
        start = row["asana_project_start_on"]
        end   = row["asana_project_adjusted_date"] or row["asana_project_estimated_date"]
        if not start or not end or pd.isna(start) or pd.isna(end):
            return None
        try:
            start_d = date.fromisoformat(str(start)[:10])
            end_d   = date.fromisoformat(str(end)[:10])
            total   = (end_d - start_d).days
            if total <= 0:
                return None
            elapsed = (today - start_d).days
            return elapsed / total * 100
        except (ValueError, TypeError):
            return None

    df["time_pct"] = df.apply(_time_pct, axis=1)

    # Trend arrow: compare Asana % complete vs time elapsed
    def _trend(row) -> str:
        tp = row["time_pct"]
        if tp is None:
            return "→"
        diff = row["pct_complete"] - tp
        if diff >= 10:
            return "↑"
        if diff <= -10:
            return "↓"
        return "→"

    df["trend"] = df.apply(_trend, axis=1)

    # Flag recently-closed projects (shown with a badge in the detail table)
    cutoff = today - timedelta(days=RECENTLY_CLOSED_DAYS)

    def _is_recently_closed(row) -> bool:
        if row["tick_archived"] != 1:
            return False
        dc = row.get("date_closed")
        if dc is None or (isinstance(dc, float) and pd.isna(dc)):
            return False
        try:
            return str(dc)[:10] >= str(cutoff)
        except (TypeError, ValueError):
            return False

    df["is_recently_closed"] = df.apply(_is_recently_closed, axis=1)

    return df


def compute_flags(df: pd.DataFrame) -> pd.DataFrame:
    """Add over_budget, near_budget, behind_schedule, is_flagged, status columns."""
    df = df.copy()
    df["over_budget"]      = df["hours_pct"] > 100
    df["near_budget"]      = (df["hours_pct"] >= NEAR_BUDGET_THRESHOLD) & (df["hours_pct"] <= 100)
    df["behind_schedule"]  = df["trend"] == "↓"
    df["is_flagged"]       = df["over_budget"] | df["near_budget"] | df["behind_schedule"]

    def _status(row) -> str:
        if row["over_budget"]:
            return "❗"
        if row["near_budget"] or row["behind_schedule"]:
            return "⚠️"
        return "✅"

    df["status"] = df.apply(_status, axis=1)
    return df


def compute_action_items(df: pd.DataFrame) -> list[dict]:
    """
    Return a list of action item dicts for all flagged projects.
    Each dict: {"priority": "URGENT"|"MONITOR", "message": str, "project": str}
    Sorted URGENT first.
    """
    items = []
    for _, row in df[df["is_flagged"]].iterrows():
        name    = str(row["project_full_name"])
        hrs     = round(float(row["total_hours"]), 1)
        budget  = int(row["project_budget_hours"]) if pd.notna(row["project_budget_hours"]) else "?"
        hrs_left = round(float(row["hrs_left"]), 1) if pd.notna(row["hrs_left"]) else "?"
        pct     = round(float(row["hours_pct"]), 0)

        is_urgent = row["over_budget"] or (row["near_budget"] and row["behind_schedule"])

        if is_urgent:
            priority = "URGENT"
            if row["over_budget"]:
                msg = (
                    f"{name} is over budget ({hrs}/{budget} hrs, {pct:.0f}% used). "
                    "Review scope or log a change order."
                )
            else:
                msg = (
                    f"{name} is near budget ({hrs}/{budget} hrs, {pct:.0f}% used) "
                    "and behind schedule. Escalate immediately."
                )
        else:
            priority = "MONITOR"
            if row["near_budget"]:
                msg = (
                    f"{name} has {hrs_left} hrs remaining ({pct:.0f}% used). "
                    "Monitor closely as work continues."
                )
            else:
                msg = (
                    f"{name} is behind schedule. "
                    "Review Asana timeline and confirm project dates are current."
                )

        items.append({"priority": priority, "message": msg, "project": name})

    items.sort(key=lambda x: 0 if x["priority"] == "URGENT" else 1)
    return items


def get_partner_list(df: pd.DataFrame) -> list[str]:
    """Return sorted list of unique non-null partner values."""
    return sorted(df["partner"].dropna().astype(str).unique().tolist())


def prepare_display(df: pd.DataFrame) -> pd.DataFrame:
    """Return a display-ready DataFrame for the Shiny DataGrid."""
    d = df.copy()

    def _short_name(full_name: str) -> str:
        parts = [p.strip() for p in str(full_name).split("::")]
        if len(parts) >= 3:
            return f"{parts[1]} — {parts[2]}"
        return full_name

    d["Project"] = d.apply(
        lambda r: (
            f"{_short_name(r['project_full_name'])} [Closed]"
            if r["is_recently_closed"]
            else _short_name(r["project_full_name"])
        ),
        axis=1,
    )
    d["Hrs Used"]  = d["total_hours"].round(1)
    d["Budget"]    = d["project_budget_hours"].fillna(0).round(0).astype(int)
    d["% Used"]    = d["hours_pct"].round(1)
    d["Hrs Left"]  = d["hrs_left"].apply(lambda v: round(v, 1) if pd.notna(v) else "—")
    d["Trend"]     = d["trend"]
    d["Status"]    = d["status"]
    return d[["Project", "Hrs Used", "Budget", "% Used", "Hrs Left", "Trend", "Status"]]


def full_pipeline(raw_df: pd.DataFrame) -> pd.DataFrame:
    """Apply all display rules, partner overrides, metrics, and flags in order."""
    df = apply_display_rules(raw_df)
    df = apply_partner_rules(df)
    df = compute_metrics(df)
    df = compute_flags(df)
    return df
