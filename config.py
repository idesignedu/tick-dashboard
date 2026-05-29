NEAR_BUDGET_THRESHOLD = 80      # % hours used to flag as near-budget
RECENTLY_CLOSED_DAYS  = 30      # days after close to keep project visible

GLOBAL_NAME_EXCLUSIONS = [
    "Leads", "Integrated Services", "Video", "Admin"
]

# Partner-specific display overrides.
# Keys must match the uppercase `partner` field in the `project` table exactly.
# include_only: only show projects whose project_full_name contains one of these strings
# exclude_names: hide projects whose project_full_name contains one of these strings
PARTNER_OVERRIDES = {
    "ASCEND": {
        "include_only": ["Creative Services", "Hourly Support"]
    },
    "TEACH ACCESS": {
        "exclude_names": ["Curriculum Repository"]
    },
}

# iDesign brand colours — shared between loader and UI
NAVY  = "#1B2A4A"
GOLD  = "#C8973A"
TEAL  = "#2A7B8C"
GREEN = "#28A745"
RED   = "#DC3545"
AMBER = "#FFC107"
LGREY = "#F8F9FA"
