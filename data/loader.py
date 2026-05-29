import os
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv
from sqlalchemy import create_engine, text

from config import RECENTLY_CLOSED_DAYS

_env_file = Path(__file__).parent.parent / ".env"
if _env_file.exists():
    load_dotenv(_env_file)


def _build_engine():
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise EnvironmentError(
            "DATABASE_URL is not set.\n"
            "Copy .env.example → .env and fill in the value, or set it in "
            "your PyCharm Run Configuration / Posit Connect environment vars."
        )
    kw = {}
    if url.startswith("postgresql"):
        kw = dict(
            pool_pre_ping=True,
            pool_size=2,
            max_overflow=3,
            connect_args={"connect_timeout": 30},
        )
    return create_engine(url, **kw)


_ENGINE = _build_engine()

_ALLOWED_SCHEMAS = {"", "production", "staging"}
_SCHEMA = os.environ.get("DB_SCHEMA", "")
if _SCHEMA not in _ALLOWED_SCHEMAS:
    raise ValueError(f"Unexpected DB_SCHEMA value: {_SCHEMA!r}")

_IS_PG = _ENGINE.dialect.name == "postgresql"


def _tbl(name: str) -> str:
    """Prefix table name with DB_SCHEMA if set (e.g. 'production.project')."""
    return f"{_SCHEMA}.{name}" if _SCHEMA else name


def load_projects() -> pd.DataFrame:
    """
    Return all active projects plus projects closed within RECENTLY_CLOSED_DAYS.
    asana_percent_complete is stored as a 0–1 ratio in the DB.
    """
    cutoff = date.today() - timedelta(days=RECENTLY_CLOSED_DAYS)
    sql = text(f"""
        SELECT
            tick_project_id,
            project_full_name,
            partner,
            project_budget_hours,
            asana_percent_complete,
            asana_project_start_on,
            asana_project_estimated_date,
            asana_project_adjusted_date,
            asana_portfolio_name,
            asana_project_gross,
            asana_project_completed,
            asana_project_completed_date,
            tick_archived,
            date_closed
        FROM {_tbl('project')}
        WHERE NOT tick_archived
           OR (tick_archived AND date_closed >= :cutoff)
    """)
    with _ENGINE.connect() as conn:
        df = pd.read_sql(sql, conn, params={"cutoff": str(cutoff)})
    df["project_budget_hours"] = pd.to_numeric(df["project_budget_hours"], errors="coerce")
    df["asana_percent_complete"] = pd.to_numeric(df["asana_percent_complete"], errors="coerce").fillna(0)
    df["asana_project_gross"] = pd.to_numeric(df["asana_project_gross"], errors="coerce")
    df["asana_project_completed_date"] = pd.to_datetime(df["asana_project_completed_date"], errors="coerce")
    return df


def load_hours() -> pd.DataFrame:
    """Return total hours, last entry date, and direct COGS per project_id.

    On PostgreSQL: COGS = SUM(hours × validated bamboo_hr_payrate), rate clamped
    to [10, 200] with a $40 default (matching budget_dash methodology).
    On SQLite (local dev): COGS = hours × $40 flat rate.
    """
    if _IS_PG:
        _rate = """
            CASE
                WHEN NULLIF(REGEXP_REPLACE(
                             COALESCE(u.bamboo_hr_payrate, ''), '[^0-9.]', '', 'g'), '') IS NULL
                    THEN 40.0
                WHEN CAST(NULLIF(REGEXP_REPLACE(
                             COALESCE(u.bamboo_hr_payrate, ''), '[^0-9.]', '', 'g'), '') AS NUMERIC)
                         BETWEEN 10 AND 200
                    THEN CAST(NULLIF(REGEXP_REPLACE(
                             COALESCE(u.bamboo_hr_payrate, ''), '[^0-9.]', '', 'g'), '') AS NUMERIC)
                ELSE 40.0
            END
        """
        sql = text(f"""
            SELECT
                te.project_id,
                SUM(te.entry_hours)                                AS total_hours,
                MAX(te.entry_date)                                 AS last_entry_date,
                ROUND(SUM(te.entry_hours * {_rate})::numeric, 2)  AS direct_cogs
            FROM {_tbl('tick_entries')} te
            LEFT JOIN {_tbl('user_table')} u ON te.tick_user_id = u.tick_user_id
            GROUP BY te.project_id
        """)
    else:
        sql = text(f"""
            SELECT
                project_id,
                SUM(entry_hours)         AS total_hours,
                MAX(entry_date)          AS last_entry_date,
                SUM(entry_hours * 40.0)  AS direct_cogs
            FROM {_tbl('tick_entries')}
            GROUP BY project_id
        """)
    with _ENGINE.connect() as conn:
        df = pd.read_sql(sql, conn)
    df["total_hours"] = pd.to_numeric(df["total_hours"], errors="coerce").fillna(0)
    df["direct_cogs"] = pd.to_numeric(df["direct_cogs"], errors="coerce").fillna(0)
    df["last_entry_date"] = pd.to_datetime(df["last_entry_date"], errors="coerce")
    return df


def get_data() -> pd.DataFrame:
    """Join projects and hours. Projects with no entries get total_hours=0."""
    projects = load_projects()
    hours = load_hours()
    df = projects.merge(
        hours,
        left_on="tick_project_id",
        right_on="project_id",
        how="left",
    )
    df["total_hours"] = df["total_hours"].fillna(0.0)
    df["direct_cogs"] = df["direct_cogs"].fillna(0.0)
    df = df.drop(columns=["project_id"])
    return df
