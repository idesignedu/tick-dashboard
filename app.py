"""
Partner TICK Health Dashboard
Tracks TICK hours vs budget for active projects per partner.

Local dev:
    shiny run app.py

Deploy to Posit Connect:
    rsconnect deploy shiny . --server idesign --title "Partner TICK Health"

Environment variables:
    DATABASE_URL   SQLAlchemy connection string
    DB_SCHEMA      Schema prefix: "" for SQLite, "production" for PostgreSQL
"""

from datetime import date, datetime

import pandas as pd
import plotly.graph_objects as go
from shiny import App, Inputs, Outputs, Session, reactive, render, ui

from config import AMBER, GOLD, GREEN, LGREY, NAVY, RED, TEAL
from data.loader import get_data
from data.transforms import (
    compute_action_items,
    full_pipeline,
    get_partner_list,
    prepare_display,
)

# ── CSS ───────────────────────────────────────────────────────────────────────
_CSS = f"""
body {{ font-family: 'Segoe UI', Arial, sans-serif; background: {LGREY}; margin: 0; }}
.page-header {{
    background: {NAVY};
    color: #fff;
    padding: 18px 24px 12px;
    margin-bottom: 0;
}}
.page-header h1 {{ margin: 0; font-size: 1.5rem; font-weight: 700; color: #fff; }}
.page-subtitle {{ margin: 4px 0 0; font-size: 0.85rem; color: rgba(255,255,255,0.75); }}
.info-banner {{
    background: #fff;
    border-left: 4px solid {TEAL};
    padding: 8px 16px;
    margin: 8px 16px 0;
    font-size: 0.82rem;
    color: #555;
    border-radius: 0 4px 4px 0;
}}
.main-content {{ padding: 0 16px 24px; }}
.kpi-row {{ display: flex; gap: 12px; margin: 16px 0; flex-wrap: wrap; }}
.kpi-card {{
    flex: 1; min-width: 140px;
    background: #fff;
    border-radius: 8px;
    padding: 16px 20px;
    box-shadow: 0 2px 6px rgba(0,0,0,.07);
    border-top: 4px solid {NAVY};
    text-align: center;
}}
.kpi-label {{ font-size: 0.72rem; color: #6c757d; text-transform: uppercase;
              letter-spacing: .05em; margin-bottom: 6px; }}
.kpi-value {{ font-size: 1.75rem; font-weight: 700; color: {NAVY}; line-height: 1.1; }}
.kpi-sub   {{ font-size: 0.72rem; color: #6c757d; margin-top: 4px; }}
.kpi-red   {{ border-top-color: {RED}; }}
.kpi-red .kpi-value {{ color: {RED}; }}
.kpi-amber {{ border-top-color: {AMBER}; }}
.kpi-amber .kpi-value {{ color: #856404; }}
.kpi-green {{ border-top-color: {GREEN}; }}
.kpi-green .kpi-value {{ color: {GREEN}; }}
.kpi-teal  {{ border-top-color: {TEAL}; }}
.kpi-gold  {{ border-top-color: {GOLD}; }}
.section-header {{
    font-size: 1rem; font-weight: 600; color: {NAVY};
    border-left: 4px solid {GOLD}; padding-left: 10px;
    margin: 20px 0 10px;
}}
.attention-row {{ display: flex; gap: 12px; flex-wrap: wrap; margin-bottom: 16px; }}
.attention-card {{
    background: #fff;
    border: 1px solid #e0e0e0;
    border-left: 4px solid {AMBER};
    border-radius: 6px;
    padding: 12px 16px;
    min-width: 200px;
    max-width: 280px;
    font-size: 0.82rem;
}}
.attention-card .ac-title {{ font-weight: 600; color: {NAVY}; margin-bottom: 4px; }}
.attention-card .ac-detail {{ color: #555; line-height: 1.6; }}
.attention-card.over-budget {{ border-left-color: {RED}; }}
.action-list {{ list-style: none; padding: 0; margin: 0 0 16px; }}
.action-list li {{
    background: #fff;
    border-radius: 6px;
    padding: 10px 14px;
    margin-bottom: 8px;
    font-size: 0.85rem;
    display: flex;
    align-items: flex-start;
    gap: 10px;
    box-shadow: 0 1px 3px rgba(0,0,0,.06);
}}
.badge {{
    display: inline-block;
    padding: 3px 8px;
    border-radius: 12px;
    font-size: 0.72rem;
    font-weight: 700;
    white-space: nowrap;
    flex-shrink: 0;
}}
.badge-urgent  {{ background: {RED};   color: #fff; }}
.badge-monitor {{ background: {AMBER}; color: #333; }}
.filter-row {{ display: flex; align-items: center; gap: 16px; margin: 16px 0 8px; }}
.charts-row {{ display: flex; gap: 16px; margin-bottom: 16px; }}
.chart-card {{
    flex: 1;
    background: #fff;
    border-radius: 8px;
    padding: 16px;
    box-shadow: 0 2px 6px rgba(0,0,0,.07);
}}
.chart-title {{
    font-size: 0.82rem; font-weight: 600; color: {NAVY};
    margin-bottom: 8px;
}}
.no-flag-msg {{ color: #6c757d; font-size: 0.85rem; font-style: italic; }}
"""


# ── Helpers ───────────────────────────────────────────────────────────────────
def _kpi(label: str, value: str, sub: str = "", css: str = "") -> ui.Tag:
    return ui.div(
        ui.div(label, class_="kpi-label"),
        ui.div(value, class_="kpi-value"),
        ui.div(sub, class_="kpi-sub") if sub else ui.tags.span(),
        class_=f"kpi-card {css}",
    )


def _plotly_html(fig) -> ui.HTML:
    return ui.HTML(
        fig.to_html(full_html=False, include_plotlyjs="cdn", config={"responsive": True})
    )


def _short_name(full_name: str) -> str:
    parts = [p.strip() for p in str(full_name).split("::")]
    if len(parts) >= 3:
        return f"{parts[1]} — {parts[2]}"
    return full_name


def _rules_text() -> str:
    from config import GLOBAL_NAME_EXCLUSIONS, PARTNER_OVERRIDES
    excl = ", ".join(GLOBAL_NAME_EXCLUSIONS)
    overrides = "; ".join(
        f"{p}: {list(v.get('include_only', v.get('exclude_names', [])))}"
        for p, v in PARTNER_OVERRIDES.items()
    )
    return f"Display rules: '{excl}' project names excluded · {overrides}"


# ── UI ────────────────────────────────────────────────────────────────────────
_today_str = date.today().strftime("%B %d, %Y")
_pulled_at = datetime.now().strftime("%B %d, %Y %I:%M %p")

app_ui = ui.page_fluid(
    ui.tags.style(_CSS),

    # Header
    ui.div(
        ui.h1("Partner TICK Health"),
        ui.p(
            f"Week of {_today_str} · Active projects + projects closed within 30 days",
            class_="page-subtitle",
        ),
        class_="page-header",
    ),

    ui.div(class_="main-content", *[
        # Data source banners
        ui.div(
            f"💡 Data source: PostgreSQL (pulled live {_pulled_at}) · Hours from tick_entries · Budget from project table",
            class_="info-banner",
        ),
        ui.div(_rules_text(), class_="info-banner", style="margin-top: 4px;"),

        # KPI row
        ui.div(ui.output_ui("kpi_row"), class_="kpi-row"),

        # Needs Your Attention
        ui.div("🚨 Needs Your Attention", class_="section-header"),
        ui.output_ui("attention_cards"),

        # Charts
        ui.div(
            ui.div(
                ui.div("Budget Used by Partner (Active Projects %)", class_="chart-title"),
                ui.output_ui("chart_partner"),
                class_="chart-card",
            ),
            ui.div(
                ui.div("Near/Over-Budget Courses — Hrs Logged vs. Budget", class_="chart-title"),
                ui.output_ui("chart_flagged"),
                class_="chart-card",
            ),
            class_="charts-row",
        ),

        # Action Items
        ui.div("✅ Action Items This Week", class_="section-header"),
        ui.output_ui("action_items"),

        # Partner filter
        ui.div(
            ui.input_select(
                "partner_filter",
                "Show:",
                choices=["All Partners"],
                width="220px",
            ),
            ui.input_checkbox("flagged_only", "Flagged Only", False),
            class_="filter-row",
        ),

        # Detail table
        ui.output_data_frame("detail_table"),
    ]),
)


# ── Server ────────────────────────────────────────────────────────────────────
def server(input: Inputs, output: Outputs, session: Session):

    @reactive.calc
    def raw_data() -> pd.DataFrame:
        return get_data()

    @reactive.calc
    def processed_data() -> pd.DataFrame:
        return full_pipeline(raw_data())

    @reactive.effect
    def _update_partners():
        partners = get_partner_list(processed_data())
        choices = {"All Partners": "All Partners"}
        for p in partners:
            choices[p] = p
        ui.update_select("partner_filter", choices=choices)

    @reactive.calc
    def filtered_data() -> pd.DataFrame:
        df = processed_data()
        sel = input.partner_filter()
        if sel and sel != "All Partners":
            df = df[df["partner"].astype(str).str.upper() == sel.upper()].copy()
        if input.flagged_only():
            df = df[df["is_flagged"]].copy()
        return df

    # ── KPI row ───────────────────────────────────────────────────────────────
    @render.ui
    def kpi_row():
        df = filtered_data()
        if df.empty or "is_flagged" not in df.columns:
            return ui.div(
                _kpi("Over Budget", "—", "", ""),
                _kpi("Behind Schedule", "—", "", ""),
                _kpi("On Track", "—", "", ""),
                _kpi("Total Hours Used", "—", "", ""),
                _kpi("Overall Budget %", "—", "", ""),
                class_="kpi-row",
            )
        over    = int(df["over_budget"].sum())
        behind  = int(df["behind_schedule"].sum())
        on_track = int((~df["is_flagged"]).sum())
        total_hrs = round(df["total_hours"].sum(), 0)
        total_budget = df["project_budget_hours"].sum()
        budget_pct = round(total_hrs / total_budget * 100, 1) if total_budget > 0 else 0.0

        return ui.tags.div(
            _kpi("Over Budget", str(over), "active projects", "kpi-red" if over > 0 else ""),
            _kpi("Behind Schedule", str(behind), "active projects", "kpi-amber" if behind > 0 else ""),
            _kpi("On Track", str(on_track), "projects", "kpi-green"),
            _kpi("Total Hours Used", f"{total_hrs:,.0f}", f"of {total_budget:,.0f} budgeted", "kpi-teal"),
            _kpi("Overall Budget %", f"{budget_pct:.1f}%", "active projects only", "kpi-gold"),
            class_="kpi-row",
        )

    # ── Needs Your Attention ──────────────────────────────────────────────────
    @render.ui
    def attention_cards():
        df = filtered_data()
        flagged = df[df["near_budget"] | df["over_budget"]].sort_values(
            "hours_pct", ascending=False
        ).head(8)

        if flagged.empty:
            return ui.p("No projects currently flagged.", class_="no-flag-msg")

        cards = []
        for _, row in flagged.iterrows():
            hrs  = round(float(row["total_hours"]), 1)
            bud  = int(row["project_budget_hours"]) if pd.notna(row["project_budget_hours"]) else "?"
            pct  = round(float(row["hours_pct"]), 0)
            left = round(float(row["hrs_left"]), 1) if pd.notna(row["hrs_left"]) else "?"
            name = _short_name(row["project_full_name"])
            partner = str(row.get("partner") or "")
            css = "attention-card over-budget" if row["over_budget"] else "attention-card"
            cards.append(
                ui.div(
                    ui.div(f"⚠️ {partner} · {name}", class_="ac-title"),
                    ui.div(
                        f"{hrs} / {bud} hrs ({pct:.0f}% used)",
                        ui.tags.br(),
                        f"📍 {left} hrs remaining",
                        class_="ac-detail",
                    ),
                    class_=css,
                )
            )
        return ui.div(*cards, class_="attention-row")

    # ── Chart: Budget Used by Partner ─────────────────────────────────────────
    @render.ui
    def chart_partner():
        df = processed_data()
        active = df[~df["is_recently_closed"]].copy()
        if active.empty:
            return ui.p("No data.", class_="no-flag-msg")

        summary = (
            active.groupby("partner", observed=True)
            .agg(total_hours=("total_hours", "sum"), budget=("project_budget_hours", "sum"))
            .reset_index()
        )
        summary = summary[summary["budget"] > 0].copy()
        summary["partner"] = summary["partner"].astype(str).str.replace("<", "&lt;", regex=False).str.replace(">", "&gt;", regex=False)
        summary["pct"] = (summary["total_hours"] / summary["budget"] * 100).round(1)
        summary = summary.sort_values("pct", ascending=True)

        colors = [
            "#DC3545" if p > 100 else "#FFC107" if p >= 80 else NAVY
            for p in summary["pct"]
        ]
        fig = go.Figure(
            go.Bar(
                x=summary["pct"],
                y=summary["partner"],
                orientation="h",
                marker_color=colors,
                text=summary["pct"].apply(lambda v: f"{v:.0f}%"),
                textposition="outside",
            )
        )
        fig.update_layout(
            margin=dict(l=10, r=40, t=10, b=10),
            xaxis=dict(title="% Budget Used", range=[0, max(summary["pct"].max() * 1.15, 110)]),
            yaxis=dict(title=""),
            height=280,
            paper_bgcolor="white",
            plot_bgcolor="white",
        )
        return _plotly_html(fig)

    # ── Chart: Near/Over-Budget Courses ───────────────────────────────────────
    @render.ui
    def chart_flagged():
        df = filtered_data()
        flagged = df[df["near_budget"] | df["over_budget"]].copy()
        if flagged.empty:
            return ui.p("No near/over-budget projects.", class_="no-flag-msg")

        flagged = flagged.sort_values("hours_pct", ascending=False).head(10)
        flagged["label"] = flagged.apply(
            lambda r: f"{r.get('partner','')} · {_short_name(r['project_full_name'])}",
            axis=1,
        )
        flagged["label"] = flagged["label"].str.replace("<", "&lt;", regex=False).str.replace(">", "&gt;", regex=False)

        fig = go.Figure()
        fig.add_trace(go.Bar(
            name="Logged Hours",
            x=flagged["label"],
            y=flagged["total_hours"].round(1),
            marker_color=AMBER,
        ))
        fig.add_trace(go.Bar(
            name="Budget",
            x=flagged["label"],
            y=flagged["project_budget_hours"],
            marker_color="#BFBFBF",
        ))
        fig.update_layout(
            barmode="group",
            margin=dict(l=10, r=10, t=10, b=80),
            xaxis=dict(tickangle=-30, title=""),
            yaxis=dict(title="Hours"),
            height=300,
            legend=dict(orientation="h", y=-0.35),
            paper_bgcolor="white",
            plot_bgcolor="white",
        )
        return _plotly_html(fig)

    # ── Action Items ──────────────────────────────────────────────────────────
    @render.ui
    def action_items():
        items = compute_action_items(filtered_data())
        if not items:
            return ui.p("No flagged projects this week.", class_="no-flag-msg")

        li_tags = []
        for item in items:
            badge_css = "badge badge-urgent" if item["priority"] == "URGENT" else "badge badge-monitor"
            li_tags.append(
                ui.tags.li(
                    ui.span(item["priority"], class_=badge_css),
                    ui.span(item["message"]),
                )
            )
        return ui.tags.ul(*li_tags, class_="action-list")

    # ── Detail Table ──────────────────────────────────────────────────────────
    @render.data_frame
    def detail_table():
        df = prepare_display(filtered_data())
        return render.DataGrid(df, selection_mode="none", width="100%")


app = App(app_ui, server)
