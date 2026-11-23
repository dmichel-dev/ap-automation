"""Static reference data: ISO codes, currency symbols, date conventions, legal forms.

Kept in code (not fetched) so normalization is deterministic and works offline.
"""

# ISO 4217 active currency codes usable on invoices
# (precious metals, testing and fund codes like XAU, XTS or XXX are left out)
ISO_4217 = frozenset(
    """
AED AFN ALL AMD ANG AOA ARS AUD AWG AZN BAM BBD BDT BGN BHD BIF BMD BND BOB BRL BSD BTN BWP
BYN BZD CAD CDF CHF CLP CNY COP CRC CUP CVE CZK DJF DKK DOP DZD EGP ERN ETB EUR FJD FKP GBP
GEL GHS GIP GMD GNF GTQ GYD HKD HNL HTG HUF IDR ILS INR IQD IRR ISK JMD JOD JPY KES KGS KHR
KMF KPW KRW KWD KYD KZT LAK LBP LKR LRD LSL LYD MAD MDL MGA MKD MMK MNT MOP MRU MUR MVR MWK
MXN MYR MZN NAD NGN NIO NOK NPR NZD OMR PAB PEN PGK PHP PKR PLN PYG QAR RON RSD RUB RWF SAR
SBD SCR SDG SEK SGD SHP SLE SOS SRD SSP STN SVC SYP SZL THB TJS TMT TND TOP TRY TTD TWD TZS
UAH UGX USD UYU UZS VES VND VUV WST XAF XCD XCG XOF XPF YER ZAR ZMW ZWG
""".split()
)

# Number of decimal places (ISO 4217 minor units); everything else has 2
MINOR_UNITS = {
    **dict.fromkeys("BIF CLP DJF GNF ISK JPY KMF KRW PYG RWF UGX VND VUV XAF XOF XPF".split(), 0),
    **dict.fromkeys("BHD IQD JOD KWD LYD OMR TND".split(), 3),
}


def minor_units(currency: str | None) -> int:
    return MINOR_UNITS.get(currency or "", 2)


# ISO 3166-1 alpha-2 (plus XK, Kosovo, which appears on real invoices)
ISO_3166 = frozenset(
    """
AD AE AF AG AI AL AM AO AQ AR AS AT AU AW AX AZ BA BB BD BE BF BG BH BI BJ BL BM BN BO BQ BR
BS BT BV BW BY BZ CA CC CD CF CG CH CI CK CL CM CN CO CR CU CV CW CX CY CZ DE DJ DK DM DO DZ
EC EE EG EH ER ES ET FI FJ FK FM FO FR GA GB GD GE GF GG GH GI GL GM GN GP GQ GR GS GT GU GW
GY HK HM HN HR HT HU ID IE IL IM IN IO IQ IR IS IT JE JM JO JP KE KG KH KI KM KN KP KR KW KY
KZ LA LB LC LI LK LR LS LT LU LV LY MA MC MD ME MF MG MH MK ML MM MN MO MP MQ MR MS MT MU MV
MW MX MY MZ NA NC NE NF NG NI NL NO NP NR NU NZ OM PA PE PF PG PH PK PL PM PN PR PS PT PW PY
QA RE RO RS RU RW SA SB SC SD SE SG SH SI SJ SK SL SM SN SO SR SS ST SV SX SY SZ TC TD TF TG
TH TJ TK TL TM TN TO TR TT TV TW TZ UA UG UM US UY UZ VA VC VE VG VI VN VU WF WS YE YT ZA ZM
ZW XK
""".split()
)

# EU VAT number prefixes that differ from the ISO country code
VAT_PREFIX_TO_COUNTRY = {"EL": "GR", "XI": "GB"}

# --- Currency symbols ----------------------------------------------------------
# Symbols that always mean the same currency (keys uppercase, trailing dot removed before lookup)
CURRENCY_SYMBOLS = {
    "€": "EUR",
    "₹": "INR",
    "₩": "KRW",
    "₽": "RUB",
    "₺": "TRY",
    "₫": "VND",
    "₱": "PHP",
    "₪": "ILS",
    "₴": "UAH",
    "₦": "NGN",
    "฿": "THB",
    "₸": "KZT",
    "₾": "GEL",
    "₼": "AZN",
    "ZŁ": "PLN",
    "KČ": "CZK",
    "FT": "HUF",
    "LEI": "RON",
    "R$": "BRL",
    "US$": "USD",
    "C$": "CAD",
    "CA$": "CAD",
    "A$": "AUD",
    "AU$": "AUD",
    "NZ$": "NZD",
    "HK$": "HKD",
    "S$": "SGD",
    "MX$": "MXN",
    "NT$": "TWD",
    "円": "JPY",
    "元": "CNY",
    "RMB": "CNY",
    "CN¥": "CNY",
    "JP¥": "JPY",
    "RM": "MYR",
    "RP": "IDR",
}

# Symbols whose meaning depends on the supplier's country (None = cannot tell)
AMBIGUOUS_SYMBOLS: dict[str, dict[str, str]] = {
    "$": {
        **dict.fromkeys(["US", "PR", "GU", "VI", "AS", "MP", "UM", "EC", "SV", "PA", "PW", "FM", "MH", "TL"], "USD"),
        "CA": "CAD",
        "AU": "AUD",
        "NZ": "NZD",
        "SG": "SGD",
        "HK": "HKD",
        "MX": "MXN",
        "AR": "ARS",
        "CL": "CLP",
        "CO": "COP",
        "TW": "TWD",
        "UY": "UYU",
        "DO": "DOP",
    },
    "¥": {"JP": "JPY", "CN": "CNY"},
    "KR": {"SE": "SEK", "NO": "NOK", "DK": "DKK", "FO": "DKK", "GL": "DKK", "IS": "ISK"},
    "FR": {"CH": "CHF", "LI": "CHF"},
    "R": {"ZA": "ZAR"},
    "RS": {"IN": "INR", "PK": "PKR", "LK": "LKR", "NP": "NPR"},
    "£": {"EG": "EGP"},  # otherwise GBP, see below
}
AMBIGUOUS_SYMBOL_DEFAULT = {"£": "GBP"}

# --- Dates ---------------------------------------------------------------------
# Countries where numeric dates are written month first (US and territories / associated states).
# Canada mixes conventions, so it gets no suggestion. Every other known country: day first.
MONTH_FIRST_COUNTRIES = frozenset(["US", "PR", "GU", "VI", "AS", "MP", "UM", "FM", "MH", "PW"])
NO_DATE_ORDER_SUGGESTION = frozenset(["CA"])

# Month names (accents removed, lowercase). Unambiguous prefixes are derived automatically.
_MONTH_NAMES = {
    1: "january janvier januar janner enero gennaio janeiro januari",
    2: "february fevrier februar febrero febbraio fevereiro februari",
    3: "march mars marz maerz marzo marco maart",
    4: "april avril abril aprile",
    5: "may mai mayo maggio maio mei",
    6: "june juin juni junio giugno junho",
    7: "july juillet juli julio luglio julho",
    8: "august aout agosto augustus",
    9: "september septembre septiembre setiembre settembre setembro",
    10: "october octobre oktober octubre ottobre outubro",
    11: "november novembre noviembre novembro",
    12: "december decembre dezember diciembre dicembre dezembro",
}


def _build_month_lookup() -> dict[str, int]:
    candidates: dict[str, set[int]] = {}
    for month, names in _MONTH_NAMES.items():
        for name in names.split():
            for k in range(3, len(name) + 1):
                candidates.setdefault(name[:k], set()).add(month)
    return {prefix: months.pop() for prefix, months in candidates.items() if len(months) == 1}


MONTH_LOOKUP = _build_month_lookup()

# --- Supplier names --------------------------------------------------------------
# Legal-form tokens removed for the duplicate key (compared after casefold, punctuation removed)
LEGAL_FORM_TOKENS = frozenset(
    """
llc inc incorporated ltd limited corp corporation co company plc llp lp pllc
gmbh ag kg ohg ug ev mbh kgaa sa sas sasu sarl eurl sci snc scop bv nv vof spa srl srls snc
sl slu sll ab as asa oy oyj aps sro spol zoo sp kft zrt nyrt doo dd ad ood eood lda
pty pte bhd sdn tbk kk yk pvt
""".split()
)
# Legal forms written without spaces in CJK names
LEGAL_FORM_SUBSTRINGS = (
    "株式会社",
    "有限会社",
    "合同会社",
    "(株)",
    "（株）",
    "有限公司",
    "股份有限公司",
    "주식회사",
    "(주)",
)
