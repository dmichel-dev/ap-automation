from functools import lru_cache

from pydantic import SecretStr
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Runtime configuration, read from environment variables (see .env.example)."""

    ap_database_url: SecretStr
    core_api_token: SecretStr
    mistral_api_key: SecretStr = SecretStr("")
    mistral_ocr_model: str = "mistral-ocr-latest"
    confidence_threshold: float = 0.85
    home_currency: str = "USD"
    intake_email_alias: str = "invoices@davidmichel.dev"  # only emails to this address are processed
    file_storage_dir: str = "/data/files"
    file_retention_days: int = 30
    # Who gets the daily summary email (comma-separated list)
    daily_summary_to: str = "david@davidmichel.dev"
    # Keys the fingerprints that compare bank accounts across invoices (empty: processing refuses to run)
    bank_fingerprint_key: SecretStr = SecretStr("")
    # Company accounting policy (docs/validation.md): amounts in the home currency
    cutoff_materiality: str = "1000"  # prior-period service at or above this → review
    capex_threshold: str = "2500"  # item cost at or above this → possible fixed asset, review

    # Accounting (QuickBooks Online). Account IDs are the sandbox defaults; see docs/integrations.md
    qbo_api_host: str = "https://sandbox-quickbooks.api.intuit.com"
    qbo_realm_id: str = ""  # QuickBooks company ID (not a secret, but kept out of git)
    qbo_withholding_account_id: str = ""  # liability for taxes withheld at source (e.g. IRPF)
    qbo_tax_recoverable_account_id: str = ""  # only if a tax rule is "recoverable"
    tax_treatment_file: str = ""  # default: app/tax_treatment.json
    qbo_bank_account_id: str = "35"  # Checking (mapped to the Plaid account)
    qbo_write_off_account_id: str = ""  # "Payment differences": open parts of under-paid bills written off
    # "Run now" webhooks: the shared secret requests are signed with (empty: every request is refused)
    run_now_token: SecretStr = SecretStr("")
    posting_max_attempts: int = 5
    posting_lease_minutes: int = 10

    # Bank matching (deterministic rules, docs/matching.md)
    match_amount_abs_tolerance: str = "1.00"  # in the account currency
    match_amount_pct_tolerance: str = "0.005"  # 0.5 %; the smaller of the two applies
    match_days_before_invoice: int = 3
    match_days_after_due: int = 30
    match_default_terms_days: int = 30  # when the invoice has no due date
    match_name_min_similarity: float = 0.6
    match_fx_pct_tolerance: str = "0.03"  # 3 % of the bill converted at the reference rate
    match_fx_max_rate_age_days: int = 4  # a rate older than this before the transaction can't be used

    # Public demo (docs/demo.md): off unless switched on, and nothing starts without a hash key
    demo_enabled: bool = False
    demo_hash_key: SecretStr = SecretStr("")  # keys the client-address and sender digests
    demo_session_minutes: int = 60
    demo_starts_per_hour: int = 3  # per client address
    demo_starts_per_day: int = 10
    demo_active_sessions: int = 30
    demo_files_per_session: int = 5  # all channels together
    demo_max_file_bytes: int = 5 * 1024 * 1024
    demo_max_pages: int = 3
    demo_pages_per_day: int = 200  # OCR pages for visitors' own files (samples cost nothing)
    demo_pages_per_month: int = 2000
    demo_emails_per_sender_day: int = 10


@lru_cache
def get_settings() -> Settings:
    return Settings()
