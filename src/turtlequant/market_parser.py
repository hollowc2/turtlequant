"""Market parser — regex + rule templates → (asset, strike, expiry, option_type).

Classifies Polymarket question text into:
  - European digital: "Will BTC be above $X on [date]?" → P(S_T > K)
  - Barrier (touch): "Will BTC reach/hit $X before [date]?" → P(max(S_t) > K)
  - Unclassified: logged to corpus file for review; trade skipped

Expiry is parsed from question text, with market.resolution_time as
authoritative fallback.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Option type
# ---------------------------------------------------------------------------


class OptionType(StrEnum):
    EUROPEAN = "european"  # P(S_T > K) at expiry
    BARRIER = "barrier"  # P(max(S_t) > K) for any t in [0, T]  — upside touch
    BARRIER_DOWN = (
        "barrier_down"  # P(min(S_t) < K) for any t in [0, T] — downside touch
    )
    EUROPEAN_PUT = "european_put"  # P(S_T < K) at expiry


# ---------------------------------------------------------------------------
# Parsed result
# ---------------------------------------------------------------------------


@dataclass
class MarketParams:
    """Extracted parameters from a Polymarket question."""

    asset: str  # "btc", "eth", "sol"
    strike: float  # USD strike price
    expiry: datetime  # UTC
    option_type: OptionType
    raw_question: str = ""


# ---------------------------------------------------------------------------
# Regex patterns
# ---------------------------------------------------------------------------

# Recognised assets (case-insensitive)
_ASSET_PAT = r"(BTC|Bitcoin|ETH|Ethereum|SOL|Solana|XRP|Ripple)"

# Strike: $75,000, $75k, $1.5M, $2T or "1 million". Keep the suffix inside the
# capture so _parse_strike can scale compact values correctly. The trailing
# lookahead stops backtracking into a shorter number, and rejects percentages
# ("dominance above 60%") outright.
_STRIKE_PAT = (
    r"\$?([\d,]+(?:\.\d+)?"
    r"(?:\s*(?:[kKmMbBtT]|thousand|million|billion|trillion)\b)?)"
    r"(?![\d,]|\.\d|\s*%)"
)
_STRIKE_MULTIPLIERS: dict[str, float] = {
    "k": 1e3,
    "thousand": 1e3,
    "m": 1e6,
    "million": 1e6,
    "b": 1e9,
    "billion": 1e9,
    "t": 1e12,
    "trillion": 1e12,
}

# Questions that mention an asset and a number but are not a plain price
# threshold on that asset: market cap, dominance, ETF flows, network stats,
# ratio/flip markets, ranges, and "X or Y first" races (not a single touch).
_REJECT_RE = re.compile(
    r"\b(?:market\s*cap|mcap|dominance|etfs?|inflows?|outflows?|hash\s*rate|gas|"
    r"supply|treasur(?:y|ies)|reserves?|holdings?|hold|flip(?:s|pening)?|"
    r"outperform|between|first)\b"
    r"|\bbefore\s+(?:it\s+)?(?:hits?|reach(?:es)?|dips?|falls?|drops?|touch(?:es)?)\b"
    r"|\bor\s+(?:dip|fall|drop|sink|reach|hit|touch)",
    re.IGNORECASE,
)

# Accept a parsed strike only within this multiple of live spot. Anything
# outside is a misparse (wrong units, non-price market), not a tradeable edge.
MIN_STRIKE_TO_SPOT = 0.25
MAX_STRIKE_TO_SPOT = 4.0

# Date phrase: "March 30", "March 30, 2025", "2025-03-30", "end of March", "March 16-22"
_DATE_PAT = (
    r"("
    r"\d{4}-\d{2}-\d{2}"  # ISO date
    r"|[A-Za-z]+ \d{1,2}-\d{1,2}(?:,? \d{4})?"  # "March 16-22" date range — end date used as expiry
    r"|[A-Za-z]+ \d{1,2}(?:,? \d{4})?"  # "March 30" or "March 30, 2025"
    r"|(?:end of |the end of )[A-Za-z]+"  # "end of March"
    r"|Q[1-4] \d{4}"  # "Q1 2025"
    r"|[A-Za-z]+ \d{4}"  # "March 2026" (month + year only)
    r"|[A-Za-z]+"  # bare month name: "March", "April"
    r")"
)

# European patterns: "above", "over", "exceed", "at or above", "higher than", "greater than"
_EUROPEAN_RE = re.compile(
    rf"Will\s+(?:the\s+price\s+of\s+)?{_ASSET_PAT}\s+(?:be\s+)?(?:at\s+or\s+)?(?:above|over|exceed|higher\s+than|greater\s+than)\s+{_STRIKE_PAT}"
    rf".*?(?:on|at|by)\s+{_DATE_PAT}",
    re.IGNORECASE | re.DOTALL,
)

# Barrier UP patterns: "reach", "hit", "touch", "cross", "break above"
_BARRIER_RE = re.compile(
    rf"Will\s+{_ASSET_PAT}\s+(?:ever\s+)?(?:reach|hit|touch|cross|break(?:\s+above)?)\s+{_STRIKE_PAT}"
    rf".*?(?:(?:before|by|in)\s+)?{_DATE_PAT}",
    re.IGNORECASE | re.DOTALL,
)

# Barrier DOWN patterns: "dip to", "fall to", "drop to", "fall below", "drop below"
# These are PUT barriers (P(min S_t < K)) — stored as OptionType.BARRIER_DOWN
_BARRIER_DOWN_RE = re.compile(
    rf"Will\s+{_ASSET_PAT}\s+(?:ever\s+)?(?:dip|fall|drop|sink|crash|decline)\s+(?:to|below|under)\s+{_STRIKE_PAT}"
    rf".*?(?:(?:before|by|in|on)\s+)?{_DATE_PAT}",
    re.IGNORECASE | re.DOTALL,
)

# Polymarket's short-form template: "Ethereum above 2,600 on September 23, 8PM ET?"
# Anchored so a stray asset mention elsewhere in a question cannot match.
_EUROPEAN_SIMPLE_RE = re.compile(
    rf"^\s*(?:Will\s+)?(?:the\s+price\s+of\s+)?{_ASSET_PAT}(?:\s+price)?\s+(?:be\s+|close\s+|trade\s+)?"
    rf"(?:above|over)\s+{_STRIKE_PAT}\s+(?:by|on|at)\s+{_DATE_PAT}",
    re.IGNORECASE,
)

# Asset normalization map
_ASSET_MAP: dict[str, str] = {
    "btc": "btc",
    "bitcoin": "btc",
    "eth": "eth",
    "ethereum": "eth",
    "sol": "sol",
    "solana": "sol",
    "xrp": "xrp",
    "ripple": "xrp",
}

# Corpus file for unclassified markets; None disables it (dry-run, tests).
# Each distinct question is written once, so the file stays bounded by the
# market universe instead of growing every scan.
_CORPUS_FILE: Path | None = Path("unclassified_markets.jsonl")
_CORPUS_SEEN: set[str] | None = None


def set_corpus_file(path: Path | None) -> None:
    """Point the unclassified-question corpus at ``path``, or disable it."""
    global _CORPUS_FILE, _CORPUS_SEEN
    _CORPUS_FILE = path
    _CORPUS_SEEN = None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def parse_market(
    question: str, resolution_time: datetime | None = None
) -> MarketParams | None:
    """Parse a Polymarket question into MarketParams.

    Returns None if the question cannot be classified. Also logs
    unclassified questions to unclassified_markets.jsonl.

    Args:
        question: The raw question text from the Polymarket API.
        resolution_time: The market's authoritative resolution UTC timestamp.
            If provided, overrides any date parsed from the question text.
    """
    question = question.strip()
    if _REJECT_RE.search(question):
        _log_unclassified(question)
        return None

    # Try European pattern first
    params = _try_european(question, resolution_time)
    if params is not None:
        return params

    # Try barrier UP pattern
    params = _try_barrier(question, resolution_time)
    if params is not None:
        return params

    # Try barrier DOWN pattern
    params = _try_barrier_down(question, resolution_time)
    if params is not None:
        return params

    # Try simple "above X by date" fallback
    params = _try_simple(question, resolution_time)
    if params is not None:
        return params

    # Log unclassified for corpus building
    _log_unclassified(question)
    return None


# ---------------------------------------------------------------------------
# Pattern matchers
# ---------------------------------------------------------------------------


def _try_european(
    question: str, resolution_time: datetime | None
) -> MarketParams | None:
    m = _EUROPEAN_RE.search(question)
    if m is None:
        return None
    asset_raw, strike_raw, date_raw = m.group(1), m.group(2), m.group(3)
    return _build_params(
        question, asset_raw, strike_raw, date_raw, OptionType.EUROPEAN, resolution_time
    )


def _try_barrier(
    question: str, resolution_time: datetime | None
) -> MarketParams | None:
    m = _BARRIER_RE.search(question)
    if m is None:
        return None
    asset_raw, strike_raw, date_raw = m.group(1), m.group(2), m.group(3)
    return _build_params(
        question, asset_raw, strike_raw, date_raw, OptionType.BARRIER, resolution_time
    )


def _try_barrier_down(
    question: str, resolution_time: datetime | None
) -> MarketParams | None:
    m = _BARRIER_DOWN_RE.search(question)
    if m is None:
        return None
    asset_raw, strike_raw, date_raw = m.group(1), m.group(2), m.group(3)
    return _build_params(
        question,
        asset_raw,
        strike_raw,
        date_raw,
        OptionType.BARRIER_DOWN,
        resolution_time,
    )


def _try_simple(question: str, resolution_time: datetime | None) -> MarketParams | None:
    m = _EUROPEAN_SIMPLE_RE.search(question)
    if m is None:
        return None
    asset_raw, strike_raw, date_raw = m.group(1), m.group(2), m.group(3)
    return _build_params(
        question, asset_raw, strike_raw, date_raw, OptionType.EUROPEAN, resolution_time
    )


def _build_params(
    question: str,
    asset_raw: str,
    strike_raw: str,
    date_raw: str,
    option_type: OptionType,
    resolution_time: datetime | None,
) -> MarketParams | None:
    asset = _ASSET_MAP.get(asset_raw.lower())
    if asset is None:
        return None

    strike = _parse_strike(strike_raw)
    if strike is None or strike <= 0:
        return None

    # Use resolution_time as authoritative expiry if available
    if resolution_time is not None:
        expiry = resolution_time
    else:
        expiry = _parse_date(date_raw)
        if expiry is None:
            return None

    # Sanity check: expiry must be in the future
    if expiry <= datetime.now(UTC):
        logger.debug("Skipping already-expired market: %s", question[:80])
        return None

    return MarketParams(
        asset=asset,
        strike=strike,
        expiry=expiry,
        option_type=option_type,
        raw_question=question,
    )


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------


def _parse_strike(raw: str) -> float | None:
    """Parse '$75,000', '$75k', '$1.5M', '1 million' or '75000' → USD float."""
    m = re.fullmatch(r"([\d.]+)\s*([A-Za-z]*)", raw.replace(",", "").strip())
    if m is None:
        return None
    number, suffix = m.groups()
    multiplier = _STRIKE_MULTIPLIERS.get(suffix.lower(), None) if suffix else 1.0
    if multiplier is None:
        return None
    try:
        return float(number) * multiplier
    except ValueError:
        return None


def strike_is_plausible(strike: float, spot: float) -> bool:
    """True when strike/spot is within the band of real price-threshold markets."""
    if spot <= 0 or strike <= 0:
        return False
    return MIN_STRIKE_TO_SPOT <= strike / spot <= MAX_STRIKE_TO_SPOT


def _parse_date(raw: str) -> datetime | None:
    """Parse date string from question text to UTC datetime."""
    from dateutil import parser as dp

    if not raw:
        return None

    raw = raw.strip()

    # Handle "March 16-22" date ranges → use the end date as expiry
    range_match = re.match(r"([A-Za-z]+)\s+\d{1,2}-(\d{1,2})(?:,?\s*(\d{4}))?", raw)
    if range_match:
        month, end_day, year = (
            range_match.group(1),
            range_match.group(2),
            range_match.group(3),
        )
        raw = f"{month} {end_day}" + (f", {year}" if year else "")

    # Handle "end of March" → last day of that month
    end_of = re.match(r"(?:end of |the end of )([A-Za-z]+)", raw, re.IGNORECASE)
    if end_of:
        month_name = end_of.group(1)
        raw = f"last day of {month_name} {datetime.now(UTC).year}"

    # Handle "Q1 2025" → end of that quarter
    q_match = re.match(r"Q([1-4])\s+(\d{4})", raw, re.IGNORECASE)
    if q_match:
        quarter, year = int(q_match.group(1)), int(q_match.group(2))
        month = quarter * 3
        raw = f"{year}-{month:02d}-01"  # will be parsed as start; adjust to end
        try:
            dt = dp.parse(raw)
            # Move to end of quarter month
            import calendar

            last_day = calendar.monthrange(dt.year, dt.month)[1]
            dt = dt.replace(day=last_day)
            return dt.replace(tzinfo=UTC)
        except Exception:
            return None

    # Try dateutil for everything else
    try:
        # If no year in string, assume current year
        if not re.search(r"\d{4}", raw):
            raw = f"{raw} {datetime.now(UTC).year}"
        dt = dp.parse(raw, default=datetime(datetime.now(UTC).year, 1, 1))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        # If parsed date is in the past, try next year
        if dt < datetime.now(UTC):
            dt = dt.replace(year=dt.year + 1)
        return dt.astimezone(UTC)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Corpus logging
# ---------------------------------------------------------------------------


def _log_unclassified(question: str) -> None:
    """Append unclassified question to corpus file for weekly manual review."""
    global _CORPUS_SEEN
    if _CORPUS_FILE is None:
        return
    try:
        if _CORPUS_SEEN is None:
            _CORPUS_SEEN = _read_corpus_questions(_CORPUS_FILE)
        if question in _CORPUS_SEEN:
            return
        entry = {
            "question": question,
            "ts": datetime.now(UTC).isoformat(),
        }
        with _CORPUS_FILE.open("a") as f:
            f.write(json.dumps(entry) + "\n")
        _CORPUS_SEEN.add(question)
    except Exception as exc:
        logger.debug("Could not write unclassified corpus: %s", exc)


def _read_corpus_questions(path: Path) -> set[str]:
    if not path.exists():
        return set()
    seen: set[str] = set()
    with path.open() as f:
        for line in f:
            try:
                question = json.loads(line).get("question")
            except (json.JSONDecodeError, AttributeError):
                continue
            if isinstance(question, str):
                seen.add(question)
    return seen
