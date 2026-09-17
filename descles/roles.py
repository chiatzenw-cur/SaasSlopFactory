"""Corporate roles. A role is a spec object, NOT a persona prompt.

objectives / metrics / permitted_tools / budget / delegation_scope /
approval_thresholds / reporting_interval — and the authority table below is
enforced by policy.authorize(), not by asking the model nicely.
"""

# authority levels
ALLOW = "ALLOW"
DENY = "DENY"
APPROVAL = "APPROVAL"

# tool -> authority.  {"limit_cents": N} means ALLOW up to N, above that APPROVAL,
# above 2N hard DENY (per-action ceiling). "daily_cents" caps per rolling day.
ROLE_SPECS = {
    "CEO": {
        "title": "Chief Executive Officer",
        "objectives": ["maximize sustainable enterprise value within the charter"],
        "metrics": ["MRR", "cash", "growth_rate", "retention"],
        "permits": {
            "spawn_project": ALLOW,
            "kill_project": ALLOW,
            "advance_project": ALLOW,
            "allocate_budget": {"limit_cents": 10000},
            "delegate": ALLOW,
            "set_price": APPROVAL,
            "publish_legal_terms": APPROVAL,
            "hire_human": APPROVAL,
            "borrow_money": DENY,
            "bank_transfer": DENY,
            "mass_unsolicited_email": DENY,
        },
        "delegation_scope": ["CTO", "CFO", "CMO", "COO"],
        "approval_thresholds": {"board_above_cents": 20000},
        "reporting_interval": "daily",
    },
    "CTO": {
        "title": "Chief Technology Officer",
        "objectives": ["ship the smallest thing that tests the hypothesis"],
        "metrics": ["build_cost_cents", "time_to_launch_days", "defect_rate"],
        "permits": {
            "write_code": ALLOW,
            "spawn_worker": ALLOW,
            "run_tests": ALLOW,
            "deploy_staging": ALLOW,
            "deploy_production": APPROVAL,
            "spend_cloud": {"limit_cents": 5000, "hard_cents": 20000},
            "publish_legal_terms": DENY,
            "hire_human": DENY,
            "mass_unsolicited_email": DENY,
        },
        "delegation_scope": ["FrontendEngineer", "QA", "DevOps"],
        "approval_thresholds": {"board_above_cents": 20000},
        "reporting_interval": "on_task",
    },
    "CFO": {
        "title": "Chief Financial Officer",
        "objectives": ["preserve runway", "reject projects that violate unit economics"],
        "metrics": ["cash", "burn_rate", "payback_months", "worst_case_loss"],
        "permits": {
            "inspect_spend": ALLOW,
            "pause_budget": ALLOW,
            "reject_project": ALLOW,
            "approve_spend": ALLOW,
            "allocate_budget": DENY,
            "alter_revenue_numbers": DENY,
            "borrow_money": DENY,
            "bank_transfer": DENY,
        },
        "delegation_scope": [],
        "approval_thresholds": {},
        "reporting_interval": "daily",
    },
    "CMO": {
        "title": "Chief Marketing Officer",
        "objectives": ["find demand with buying intent", "acquire users at a price the product can carry"],
        "metrics": ["qualified_signals", "cac_cents", "conversion_pct"],
        "permits": {
            "web_search": ALLOW,
            "read_public_forum": ALLOW,
            "spend_ads": {"limit_cents": 5000, "hard_cents": 30000, "daily_cents": 5000},
            "send_email": APPROVAL,
            "mass_unsolicited_email": DENY,
            "deceptive_marketing": DENY,
            "publish_legal_terms": DENY,
        },
        "delegation_scope": ["ContentWriter"],
        "approval_thresholds": {},
        "reporting_interval": "daily",
    },
    "COO": {
        "title": "Chief Operating Officer",
        "objectives": ["run the cheapest experiment that can falsify the hypothesis"],
        "metrics": ["experiment_cost_cents", "cycle_time_days", "kill_decisions"],
        "permits": {
            "run_experiment": ALLOW,
            "spend_ops": {"limit_cents": 5000, "hard_cents": 10000},
            "publish_landing_page": ALLOW,
            "change_pricing": APPROVAL,
            "hire_human": APPROVAL,
            "mass_unsolicited_email": DENY,
        },
        "delegation_scope": ["OpsAnalyst"],
        "approval_thresholds": {},
        "reporting_interval": "daily",
    },
}

# charter clauses -> actions they forbid outright
CLAUSE_FORBIDS = {
    "no_debt": ["borrow_money", "issue_debt"],
    "no_deceptive_marketing": ["deceptive_marketing", "fake_reviews", "misleading_claims"],
    "no_mass_unsolicited_email": ["mass_unsolicited_email"],
    "no_legal_commitments_without_approval": ["sign_contract", "publish_legal_terms"],
    "no_bank_transfer": ["bank_transfer", "withdraw_funds"],
}


def spec(role):
    return ROLE_SPECS.get(role)


def default_charter(capital_cents=50000, max_experiment_cents=10000, board_threshold_cents=20000):
    return {
        "initial_capital_cents": int(capital_cents),
        "max_experiment_spend_cents": int(max_experiment_cents),
        "board_approval_threshold_cents": int(board_threshold_cents),
        "clauses": [
            "no_debt",
            "no_deceptive_marketing",
            "no_mass_unsolicited_email",
            "no_legal_commitments_without_approval",
            "kill_products_after_validation_failure",
        ],
        "mode": "moderate",
    }
