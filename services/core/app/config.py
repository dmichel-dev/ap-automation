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
    file_storage_dir: str = "/data/files"
    file_retention_days: int = 30

    # Accounting (QuickBooks Online). Account IDs are the sandbox defaults; see docs/integrations.md
    qbo_api_host: str = "https://sandbox-quickbooks.api.intuit.com"
    qbo_realm_id: str = ""  # QuickBooks company ID (not a secret, but kept out of git)
    qbo_expense_account_id: str = "20"  # Supplies
    qbo_tax_account_id: str = "21"  # Taxes & Licenses
    qbo_bank_account_id: str = "35"  # Checking (mapped to the Plaid account)
    posting_max_attempts: int = 5
    posting_lease_minutes: int = 10

    # Bank matching (deterministic rules, docs/matching.md)
    match_amount_abs_tolerance: str = "1.00"  # in the account currency
    match_amount_pct_tolerance: str = "0.005"  # 0.5 %; the smaller of the two applies
    match_days_before_invoice: int = 3
    match_days_after_due: int = 30
    match_default_terms_days: int = 30  # when the invoice has no due date
    match_name_min_similarity: float = 0.6


@lru_cache
def get_settings() -> Settings:
    return Settings()
