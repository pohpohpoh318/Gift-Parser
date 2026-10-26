#!/usr/bin/env python3
"""Telegram Gift Parser (single-file edition).

    python main.py              run the parser (what Railway starts)
    python main.py test-alert   send a clearly labelled test alert through the real pipeline
    python main.py check        verify credentials and the live Telegram data source
    python main.py session      one-time interactive login that prints TELEGRAM_SESSION

Detects newly upgraded Telegram collectible gifts by probing t.me/nft/<Collection>-<N+1> with a Telethon
user session, and sends alerts through the Bot API. All configuration comes from environment variables.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys

__version__ = "1.0.0"



# ==========================================================================
# models
# ==========================================================================
"""Plain data models shared by every module (no Telegram library imports)."""

import re
from dataclasses import asdict, dataclass
from typing import Any, Dict, Optional

_NON_ALNUM = re.compile(r"[^0-9A-Za-z]+")


def normalize_name(name: str) -> str:
    """Lower-case alphanumeric form used to compare collection names."""
    return _NON_ALNUM.sub("", name or "").lower()


def derive_slug_prefix(title: str) -> str:
    """Best-effort slug prefix for a collection title ("Plush Pepe" -> "PlushPepe").

    This is only a *guess*; the detector verifies it against Telegram before
    trusting it (see ``detector.py``).
    """
    return _NON_ALNUM.sub("", title or "")


@dataclass(frozen=True)
class Trait:
    """One collectible attribute (model, symbol/pattern or backdrop)."""

    name: str
    permille: Optional[int] = None  # rarity in 1/1000, as reported by Telegram
    tier: Optional[str] = None  # 'uncommon' | 'rare' | 'epic' | 'legendary' when Telegram sends a tier

    def rarity_text(self) -> Optional[str]:
        parts = []
        if self.permille is not None:
            parts.append(f"{self.permille / 10:g}%")
        if self.tier:
            parts.append(self.tier.capitalize())
        return " · ".join(parts) if parts else None


@dataclass(frozen=True)
class UniqueGift:
    """A collectible (upgraded) gift, e.g. https://t.me/nft/PlushPepe-1234."""

    slug: str
    title: str
    num: int
    gift_id: Optional[int] = None  # id of the regular gift type
    unique_id: Optional[int] = None  # id of this specific collectible
    model: Optional[Trait] = None
    symbol: Optional[Trait] = None
    backdrop: Optional[Trait] = None
    model_document_id: Optional[int] = None
    issued: Optional[int] = None  # how many of this type were upgraded so far
    total: Optional[int] = None  # how many can be / were upgraded
    crafted: bool = False
    burned: bool = False
    owner_id: Optional[int] = None  # current owner when detected (the upgrader, unless it moved instantly)
    owner_username: Optional[str] = None  # public @username, without the @
    owner_name: Optional[str] = None  # display name (or the name Telegram shows when the owner is hidden)
    owner_is_channel: bool = False

    @property
    def link(self) -> str:
        return f"https://t.me/nft/{self.slug}"

    @property
    def prefix(self) -> str:
        return self.slug.rsplit("-", 1)[0]

    def traits(self) -> Dict[str, Optional[Trait]]:
        return {"model": self.model, "symbol": self.symbol, "backdrop": self.backdrop}

    def rarest(self) -> Optional[tuple]:
        """(label, trait) of the trait with the lowest permille, if any is known."""
        known = [(k, t) for k, t in self.traits().items() if t is not None and t.permille is not None]
        if not known:
            return None
        return min(known, key=lambda kt: kt[1].permille)  # type: ignore[arg-type,return-value]

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "UniqueGift":
        d = dict(d)
        for key in ("model", "symbol", "backdrop"):
            if d.get(key):
                d[key] = Trait(**d[key])
            else:
                d[key] = None
        return cls(**d)


@dataclass(frozen=True)
class CatalogGift:
    """A regular gift type from the Telegram gift shop (payments.getStarGifts)."""

    gift_id: int
    title: Optional[str] = None
    stars: Optional[int] = None
    upgrade_stars: Optional[int] = None  # set => this type can be upgraded to collectibles
    limited: bool = False
    sold_out: bool = False
    availability_total: Optional[int] = None
    availability_remains: Optional[int] = None
    availability_resale: Optional[int] = None
    resell_min_stars: Optional[int] = None
    auction: bool = False
    require_premium: bool = False
    birthday: bool = False

    @property
    def upgradable(self) -> bool:
        return self.upgrade_stars is not None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "CatalogGift":
        return cls(**d)


@dataclass(frozen=True)
class Floor:
    """Cheapest current Telegram *resale* listings (Stars only)."""

    collection_stars: Optional[int] = None
    collection_listed: Optional[int] = None
    model_stars: Optional[int] = None
    model_listed: Optional[int] = None

    @property
    def has_data(self) -> bool:
        return self.collection_stars is not None or self.model_stars is not None


# ==========================================================================
# config
# ==========================================================================
"""Configuration management.

Every setting is read from environment variables (never hardcoded).  The
``ENV_SPECS`` registry below is the single source of truth for variable names;
``tests/test_consistency.py`` verifies that ``.env.example`` and ``README.md``
document every one of them.
"""

import os
import re
from dataclasses import dataclass, field
from typing import Dict, List, Mapping, Optional, Tuple



class ConfigError(Exception):
    """Raised when the environment configuration is missing or invalid."""


@dataclass(frozen=True)
class EnvSpec:
    name: str
    default: str  # human-readable default ("" = none)
    needed_by: str  # "bot" | "user" | "optional"
    description: str


ENV_SPECS: Tuple[EnvSpec, ...] = (
    # --- required: Bot API (sends alerts) ---------------------------------
    EnvSpec("TELEGRAM_BOT_TOKEN", "", "bot", "Bot token from @BotFather (sends the alerts)."),
    EnvSpec("TELEGRAM_CHAT_ID", "", "bot", "Channel/group id (-100...) or @channelusername that receives alerts."),
    # --- required: user session (detects gifts) ---------------------------
    EnvSpec("TELEGRAM_API_ID", "", "user", "Numeric api_id from https://my.telegram.org."),
    EnvSpec("TELEGRAM_API_HASH", "", "user", "api_hash from https://my.telegram.org."),
    EnvSpec("TELEGRAM_SESSION", "", "user", "Telethon StringSession created with generate_session.py."),
    # --- optional ---------------------------------------------------------
    EnvSpec("TELEGRAM_MESSAGE_THREAD_ID", "", "optional", "Forum topic id if TELEGRAM_CHAT_ID is a forum supergroup."),
    EnvSpec("ADMIN_IDS", "your own id if TELEGRAM_CHAT_ID is a private chat", "optional", "Telegram user IDs (comma list) allowed to use the /settings panel."),
    EnvSpec("PANEL_ENABLED", "true", "optional", "Enable the /settings control panel in the bot chat."),
    EnvSpec("DATABASE_PATH", "$RAILWAY_VOLUME_MOUNT_PATH/gift_parser.sqlite3 or ./data/gift_parser.sqlite3", "optional", "SQLite file path. Put it on a Railway volume."),
    EnvSpec("LOG_LEVEL", "INFO", "optional", "DEBUG, INFO, WARNING or ERROR."),
    EnvSpec("COLLECTIONS_INCLUDE", "", "optional", "Comma list of collection names to watch (empty = all)."),
    EnvSpec("COLLECTIONS_EXCLUDE", "", "optional", "Comma list of collection names to ignore."),
    EnvSpec("EXTRA_COLLECTIONS", "", "optional", "Slug prefixes to watch even if missing from the catalog (e.g. PlushPepe)."),
    EnvSpec("SLUG_PREFIX_OVERRIDES", "", "optional", "Fix wrong slug prefixes: 'Title=Prefix,Title2=Prefix2'."),
    EnvSpec("RARITY_MAX_PERMILLE", "0", "optional", "Only alert if a trait rarity is <= this value in 1/1000 (15 = 1.5%). 0 = off."),
    EnvSpec("RARITY_ATTRIBUTES", "model,symbol,backdrop", "optional", "Which traits the rarity filter looks at."),
    EnvSpec("RARITY_TIERS", "", "optional", "Alert if a trait has one of these tiers: uncommon,rare,epic,legendary."),
    EnvSpec("MAX_GIFT_NUMBER", "0", "optional", "Only alert for gift numbers <= this value. 0 = off."),
    EnvSpec("ALERT_UPGRADES", "true", "optional", "Send alerts for newly upgraded gifts."),
    EnvSpec("ALERT_NEW_GIFTS", "true", "optional", "Send alerts for new gift types / gifts that become upgradable."),
    EnvSpec("HOT_INTERVAL_SECONDS", "1.5", "optional", "Poll interval for collections with very recent upgrades."),
    EnvSpec("WARM_INTERVAL_SECONDS", "6", "optional", "Poll interval for collections with upgrades in the warm window."),
    EnvSpec("COLD_INTERVAL_SECONDS", "30", "optional", "Poll interval for quiet collections."),
    EnvSpec("HOT_WINDOW_SECONDS", "900", "optional", "A collection is 'hot' this long after its last upgrade."),
    EnvSpec("WARM_WINDOW_SECONDS", "21600", "optional", "A collection is 'warm' this long after its last upgrade."),
    EnvSpec("MAX_REQUESTS_PER_SECOND", "10", "optional", "Global cap on Telegram requests from the user session."),
    EnvSpec("CATCHUP_MAX_ALERTS", "25", "optional", "After downtime, alert at most this many missed gifts per collection."),
    EnvSpec("CATALOG_POLL_INTERVAL_SECONDS", "20", "optional", "How often to check the gift catalog for new gifts."),
    EnvSpec("FLOOR_ENABLED", "true", "optional", "Add Telegram-resale floor prices (Stars) to alerts."),
    EnvSpec("FLOOR_TIMEOUT_SECONDS", "2.5", "optional", "Never delay an alert longer than this waiting for a floor price."),
    EnvSpec("FLOOR_CACHE_SECONDS", "45", "optional", "Floor price cache lifetime."),
    EnvSpec("ALERT_BATCH_MAX", "5", "optional", "Max gifts merged into one message when alerts back up."),
    EnvSpec("PENDING_MAX_AGE_SECONDS", "900", "optional", "Unsent alerts older than this are dropped after a restart."),
    EnvSpec("SHUTDOWN_GRACE_SECONDS", "10", "optional", "Time allowed to flush alerts on SIGTERM."),
    EnvSpec("STATS_INTERVAL_SECONDS", "60", "optional", "How often a statistics line is logged."),
    EnvSpec("DRY_RUN", "false", "optional", "Detect and log, but never send alerts."),
    EnvSpec("SEND_TEST_ALERT_ON_START", "false", "optional", "Send one clearly-labelled test alert at startup."),
)

ALL_ENV_NAMES: Tuple[str, ...] = tuple(s.name for s in ENV_SPECS)

_TRUE = {"1", "true", "yes", "on", "y", "t"}
_FALSE = {"0", "false", "no", "off", "n", "f", ""}
_VALID_TRAITS = ("model", "symbol", "backdrop")
_VALID_TIERS = ("uncommon", "rare", "epic", "legendary")
_VALID_LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")


@dataclass(frozen=True)
class Settings:
    # secrets (never printed)
    bot_token: str = field(repr=False, default="")
    api_hash: str = field(repr=False, default="")
    session: str = field(repr=False, default="")
    # identifiers
    chat_id: str = ""
    thread_id: Optional[int] = None
    api_id: Optional[int] = None
    # storage / logging
    database_path: str = "./data/gift_parser.sqlite3"
    database_path_source: str = "default"
    on_railway: bool = False
    volume_mount_path: Optional[str] = None
    log_level: str = "INFO"
    admin_ids: Tuple[int, ...] = ()
    panel_enabled: bool = True
    # filters
    collections_include: Tuple[str, ...] = ()
    collections_exclude: Tuple[str, ...] = ()
    extra_collections: Tuple[str, ...] = ()
    slug_prefix_overrides: Dict[str, str] = field(default_factory=dict)
    rarity_max_permille: int = 0
    rarity_attributes: Tuple[str, ...] = _VALID_TRAITS
    rarity_tiers: Tuple[str, ...] = ()
    max_gift_number: int = 0
    # behaviour
    alert_upgrades: bool = True
    alert_new_gifts: bool = True
    hot_interval: float = 1.5
    warm_interval: float = 6.0
    cold_interval: float = 30.0
    hot_window: float = 900.0
    warm_window: float = 21600.0
    max_rps: float = 10.0
    catchup_max_alerts: int = 25
    catalog_poll_interval: float = 20.0
    floor_enabled: bool = True
    floor_timeout: float = 2.5
    floor_cache_seconds: float = 45.0
    alert_batch_max: int = 5
    pending_max_age: float = 900.0
    shutdown_grace: float = 10.0
    stats_interval: float = 60.0
    dry_run: bool = False
    send_test_alert_on_start: bool = False

    @property
    def rarity_filter_active(self) -> bool:
        return self.rarity_max_permille > 0 or bool(self.rarity_tiers)

    def secrets(self) -> List[str]:
        return [s for s in (self.bot_token, self.api_hash, self.session) if s]

    def describe(self) -> List[str]:
        """Human-readable, secret-free summary used in startup logs."""

        def flag(v: object) -> str:
            return "set" if v else "MISSING"

        return [
            f"bot token: {flag(self.bot_token)} | chat id: {self.chat_id or 'MISSING'}"
            f"{f' (topic {self.thread_id})' if self.thread_id else ''}",
            f"user session: api_id {flag(self.api_id)}, api_hash {flag(self.api_hash)}, session {flag(self.session)}",
            f"database: {self.database_path} (from {self.database_path_source})",
            f"control panel: {'on' if self.panel_enabled and self.admin_ids else 'off'} (admins: {len(self.admin_ids)})",
            f"collections include={list(self.collections_include) or 'ALL'} exclude={list(self.collections_exclude) or 'none'} "
            f"extra={list(self.extra_collections) or 'none'}",
            f"rarity filter: max_permille={self.rarity_max_permille or 'off'} tiers={list(self.rarity_tiers) or 'off'} "
            f"attrs={list(self.rarity_attributes)} | max gift number={self.max_gift_number or 'off'}",
            f"polling: hot={self.hot_interval}s warm={self.warm_interval}s cold={self.cold_interval}s "
            f"max_rps={self.max_rps} catalog={self.catalog_poll_interval}s",
            f"alerts: upgrades={self.alert_upgrades} new_gifts={self.alert_new_gifts} floor={self.floor_enabled} "
            f"batch_max={self.alert_batch_max} dry_run={self.dry_run}",
        ]


# --------------------------------------------------------------------------
# parsing helpers
# --------------------------------------------------------------------------


def _raw(env: Mapping[str, str], name: str) -> Optional[str]:
    v = env.get(name)
    if v is None:
        return None
    v = v.strip()
    return v if v != "" else None


def _int(env: Mapping[str, str], name: str, default: int, minimum: int = 0) -> int:
    raw = _raw(env, name)
    if raw is None:
        return default
    try:
        val = int(raw)
    except ValueError:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from None
    if val < minimum:
        raise ConfigError(f"{name} must be >= {minimum}, got {val}")
    return val


def _float(env: Mapping[str, str], name: str, default: float, minimum: float = 0.0) -> float:
    raw = _raw(env, name)
    if raw is None:
        return default
    try:
        val = float(raw)
    except ValueError:
        raise ConfigError(f"{name} must be a number, got {raw!r}") from None
    if val < minimum:
        raise ConfigError(f"{name} must be >= {minimum}, got {val}")
    return val


def _bool(env: Mapping[str, str], name: str, default: bool) -> bool:
    raw = env.get(name)
    if raw is None or raw.strip() == "":
        return default
    low = raw.strip().lower()
    if low in _TRUE:
        return True
    if low in _FALSE:
        return False
    raise ConfigError(f"{name} must be true or false, got {raw!r}")


def _list(env: Mapping[str, str], name: str) -> Tuple[str, ...]:
    raw = _raw(env, name)
    if raw is None:
        return ()
    parts = re.split(r"[,\n;]", raw)
    return tuple(p.strip() for p in parts if p.strip())


def _choices(env: Mapping[str, str], name: str, valid: Tuple[str, ...], default: Tuple[str, ...]) -> Tuple[str, ...]:
    items = tuple(i.lower() for i in _list(env, name))
    if not items:
        return default
    bad = [i for i in items if i not in valid]
    if bad:
        raise ConfigError(f"{name} contains unknown value(s) {bad}; valid: {', '.join(valid)}")
    return items


def _overrides(env: Mapping[str, str]) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for item in _list(env, "SLUG_PREFIX_OVERRIDES"):
        if "=" not in item:
            raise ConfigError(f"SLUG_PREFIX_OVERRIDES entry {item!r} must look like 'Title=Prefix'")
        title, prefix = (x.strip() for x in item.split("=", 1))
        if not title or not re.fullmatch(r"[0-9A-Za-z]+", prefix):
            raise ConfigError(f"SLUG_PREFIX_OVERRIDES entry {item!r}: prefix must be letters/digits only")
        out[normalize_name(title)] = prefix
    return out


def _chat_id(raw: str) -> str:
    if re.fullmatch(r"-?\d+", raw) or re.fullmatch(r"@[A-Za-z][A-Za-z0-9_]{3,}", raw):
        return raw
    raise ConfigError(
        "TELEGRAM_CHAT_ID must be a numeric id such as -1001234567890 or a public @channelusername, "
        f"got {raw!r}"
    )


def _admin_ids(env: Mapping[str, str], chat_id: str) -> Tuple[int, ...]:
    ids = []
    for item in _list(env, "ADMIN_IDS"):
        if not re.fullmatch(r"\d+", item):
            raise ConfigError(f"ADMIN_IDS must be numeric Telegram user IDs, got {item!r}")
        ids.append(int(item))
    if re.fullmatch(r"\d+", chat_id or "") and int(chat_id) not in ids:  # positive id = a private chat with you
        ids.append(int(chat_id))
    return tuple(ids)


def resolve_database_path(env: Mapping[str, str]) -> Tuple[str, str]:
    explicit = _raw(env, "DATABASE_PATH")
    if explicit:
        return explicit, "DATABASE_PATH"
    mount = _raw(env, "RAILWAY_VOLUME_MOUNT_PATH")
    if mount:
        return os.path.join(mount, "gift_parser.sqlite3"), "RAILWAY_VOLUME_MOUNT_PATH"
    return os.path.join(".", "data", "gift_parser.sqlite3"), "default"


def load_settings(
    environ: Optional[Mapping[str, str]] = None,
    *,
    need_bot: bool = True,
    need_user: bool = True,
) -> Settings:
    """Build :class:`Settings` from the environment, validating everything."""
    env = os.environ if environ is None else environ

    missing: List[str] = []

    def required(name: str) -> str:
        v = _raw(env, name)
        if v is None:
            missing.append(name)
            return ""
        return v

    bot_token = chat_id = ""
    if need_bot:
        bot_token = required("TELEGRAM_BOT_TOKEN")
        chat_id = required("TELEGRAM_CHAT_ID")
    api_hash = session = ""
    api_id_raw = ""
    if need_user:
        api_id_raw = required("TELEGRAM_API_ID")
        api_hash = required("TELEGRAM_API_HASH")
        session = required("TELEGRAM_SESSION")
    if missing:
        raise ConfigError(
            "Missing required environment variable(s): "
            + ", ".join(missing)
            + ". Add them in Railway > your service > Variables."
        )

    if bot_token and not re.fullmatch(r"\d{5,}:[A-Za-z0-9_-]{20,}", bot_token):
        raise ConfigError("TELEGRAM_BOT_TOKEN does not look like a bot token (expected '123456:ABC-DEF...')")
    if chat_id:
        chat_id = _chat_id(chat_id)

    api_id: Optional[int] = None
    if api_id_raw:
        try:
            api_id = int(api_id_raw)
        except ValueError:
            raise ConfigError("TELEGRAM_API_ID must be a number (from https://my.telegram.org)") from None

    thread_raw = _raw(env, "TELEGRAM_MESSAGE_THREAD_ID")
    thread_id: Optional[int] = None
    if thread_raw:
        try:
            thread_id = int(thread_raw)
        except ValueError:
            raise ConfigError("TELEGRAM_MESSAGE_THREAD_ID must be an integer") from None

    log_level = (_raw(env, "LOG_LEVEL") or "INFO").upper()
    if log_level not in _VALID_LOG_LEVELS:
        raise ConfigError(f"LOG_LEVEL must be one of {', '.join(_VALID_LOG_LEVELS)}")

    db_path, db_source = resolve_database_path(env)
    on_railway = any(k.startswith("RAILWAY_") for k in env.keys())

    hot = _float(env, "HOT_INTERVAL_SECONDS", 1.5, 0.2)
    warm = _float(env, "WARM_INTERVAL_SECONDS", 6.0, 0.2)
    cold = _float(env, "COLD_INTERVAL_SECONDS", 30.0, 0.2)
    if not (hot <= warm <= cold):
        raise ConfigError("Intervals must satisfy HOT_INTERVAL_SECONDS <= WARM_INTERVAL_SECONDS <= COLD_INTERVAL_SECONDS")

    return Settings(
        bot_token=bot_token,
        api_hash=api_hash,
        session=session,
        chat_id=chat_id,
        thread_id=thread_id,
        api_id=api_id,
        database_path=db_path,
        database_path_source=db_source,
        on_railway=on_railway,
        volume_mount_path=_raw(env, "RAILWAY_VOLUME_MOUNT_PATH"),
        log_level=log_level,
        admin_ids=_admin_ids(env, chat_id),
        panel_enabled=_bool(env, "PANEL_ENABLED", True),
        collections_include=_list(env, "COLLECTIONS_INCLUDE"),
        collections_exclude=_list(env, "COLLECTIONS_EXCLUDE"),
        extra_collections=_list(env, "EXTRA_COLLECTIONS"),
        slug_prefix_overrides=_overrides(env),
        rarity_max_permille=_int(env, "RARITY_MAX_PERMILLE", 0, 0),
        rarity_attributes=_choices(env, "RARITY_ATTRIBUTES", _VALID_TRAITS, _VALID_TRAITS),
        rarity_tiers=_choices(env, "RARITY_TIERS", _VALID_TIERS, ()),
        max_gift_number=_int(env, "MAX_GIFT_NUMBER", 0, 0),
        alert_upgrades=_bool(env, "ALERT_UPGRADES", True),
        alert_new_gifts=_bool(env, "ALERT_NEW_GIFTS", True),
        hot_interval=hot,
        warm_interval=warm,
        cold_interval=cold,
        hot_window=_float(env, "HOT_WINDOW_SECONDS", 900.0, 1.0),
        warm_window=_float(env, "WARM_WINDOW_SECONDS", 21600.0, 1.0),
        max_rps=_float(env, "MAX_REQUESTS_PER_SECOND", 10.0, 0.5),
        catchup_max_alerts=_int(env, "CATCHUP_MAX_ALERTS", 25, 1),
        catalog_poll_interval=_float(env, "CATALOG_POLL_INTERVAL_SECONDS", 20.0, 2.0),
        floor_enabled=_bool(env, "FLOOR_ENABLED", True),
        floor_timeout=_float(env, "FLOOR_TIMEOUT_SECONDS", 2.5, 0.1),
        floor_cache_seconds=_float(env, "FLOOR_CACHE_SECONDS", 45.0, 0.0),
        alert_batch_max=_int(env, "ALERT_BATCH_MAX", 5, 1),
        pending_max_age=_float(env, "PENDING_MAX_AGE_SECONDS", 900.0, 10.0),
        shutdown_grace=_float(env, "SHUTDOWN_GRACE_SECONDS", 10.0, 1.0),
        stats_interval=_float(env, "STATS_INTERVAL_SECONDS", 60.0, 5.0),
        dry_run=_bool(env, "DRY_RUN", False),
        send_test_alert_on_start=_bool(env, "SEND_TEST_ALERT_ON_START", False),
    )


# ==========================================================================
# logging_setup
# ==========================================================================
"""Logging with automatic secret redaction.

Railway captures stdout, so everything is logged there.  Any configured secret
(bot token, api_hash, session string) and anything shaped like a bot token is
replaced before the line is written, including inside tracebacks.
"""

import logging
import re
import sys
from typing import Iterable

_BOT_TOKEN_RE = re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{30,}\b")
_BOT_URL_RE = re.compile(r"/bot[^/\s]+/")


class RedactingFormatter(logging.Formatter):
    def __init__(self, secrets: Iterable[str] = (), fmt: str | None = None, datefmt: str | None = None) -> None:
        super().__init__(fmt, datefmt)
        # longest first so a secret that contains another is fully removed
        self._secrets = sorted({s for s in secrets if s and len(s) >= 4}, key=len, reverse=True)

    def redact(self, text: str) -> str:
        for secret in self._secrets:
            if secret in text:
                text = text.replace(secret, "***")
        text = _BOT_URL_RE.sub("/bot***/", text)
        return _BOT_TOKEN_RE.sub("***", text)

    def format(self, record: logging.LogRecord) -> str:
        return self.redact(super().format(record))


def setup_logging(level: str = "INFO", secrets: Iterable[str] = ()) -> None:
    """Configure the root logger (idempotent)."""
    try:
        sys.stdout.reconfigure(line_buffering=True)  # type: ignore[attr-defined]
    except Exception:  # pragma: no cover - very old/odd stdout
        pass
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        RedactingFormatter(
            secrets,
            fmt="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%SZ",
        )
    )
    handler.formatter.converter = _utc  # type: ignore[union-attr]
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
    root.addHandler(handler)
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    # httpx logs full request URLs (which contain the bot token) at INFO.
    for noisy in ("httpx", "httpcore", "hpack"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    # Telethon is chatty at INFO about reconnects; keep warnings and errors.
    logging.getLogger("telethon").setLevel(logging.WARNING)


def _utc(ts: float | None = None):
    import time

    return time.gmtime(ts)


# ==========================================================================
# ratelimit
# ==========================================================================
"""Rate limiting and retry/backoff helpers."""

import asyncio
import random
import time
from typing import Awaitable, Callable


def backoff_delay(attempt: int, base: float = 1.0, cap: float = 60.0, jitter: float = 0.2) -> float:
    """Exponential backoff with jitter: base * 2**(attempt-1), capped."""
    attempt = max(1, attempt)
    delay = min(cap, base * (2 ** (attempt - 1)))
    return delay * (1.0 + random.uniform(-jitter, jitter))


class RateLimiter:
    """Spacing-based limiter shared by all probes of the user session.

    ``acquire()`` reserves the next free slot, so requests are evenly spread at
    ``rate`` per second.  On a FLOOD_WAIT the whole limiter is paused and the
    rate is halved (``penalize``); it then recovers 5% per ``recover()`` call.
    """

    def __init__(
        self,
        rate: float,
        *,
        min_rate: float = 0.5,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if rate <= 0:
            raise ValueError("rate must be positive")
        self.max_rate = rate
        self.rate = rate
        self.min_rate = min(min_rate, rate)
        self._clock = clock
        self._sleep = sleep
        self._next_free = 0.0
        self.total_wait = 0.0
        self.acquired = 0

    async def acquire(self) -> float:
        now = self._clock()
        slot = max(self._next_free, now)
        self._next_free = slot + 1.0 / self.rate
        wait = slot - now
        self.acquired += 1
        if wait > 0:
            self.total_wait += wait
            await self._sleep(wait)
        return wait

    def penalize(self, seconds: float) -> None:
        now = self._clock()
        self._next_free = max(self._next_free, now + max(0.0, seconds))
        self.rate = max(self.min_rate, self.rate * 0.5)

    def recover(self) -> None:
        if self.rate < self.max_rate:
            self.rate = min(self.max_rate, self.rate * 1.05)


# ==========================================================================
# db
# ==========================================================================
"""SQLite persistence: duplicate detection, collection progress, alert outbox.

All access goes through one connection guarded by a lock and executed in a
worker thread, so the asyncio loop is never blocked.  The database lives on
the Railway volume (see README) so that state survives restarts/redeploys.
"""

import asyncio
import json
import logging
import os
import sqlite3
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple, TypeVar


log_db = logging.getLogger("giftparser.db")

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- One row per collection (gift type) we probe for new upgrades.
CREATE TABLE IF NOT EXISTS collections (
    key             TEXT PRIMARY KEY,           -- normalized alphanumeric lowercase name
    gift_id         INTEGER,                    -- id of the regular gift type (if known)
    title           TEXT NOT NULL,
    slug_prefix     TEXT NOT NULL,              -- e.g. PlushPepe  (slug = PlushPepe-<num>)
    prefix_verified INTEGER NOT NULL DEFAULT 0, -- 1 once Telegram confirmed the prefix
    bootstrapped    INTEGER NOT NULL DEFAULT 0, -- 1 once the silent baseline was taken
    last_num        INTEGER NOT NULL DEFAULT 0, -- highest gift number already handled
    known_issued    INTEGER NOT NULL DEFAULT 0, -- availability_issued last reported by Telegram
    last_found_at   REAL,                       -- unix time of the last new upgrade seen
    created_at      REAL NOT NULL
);

-- Every collectible ever detected; PRIMARY KEY on slug is the dedupe guarantee.
CREATE TABLE IF NOT EXISTS seen_gifts (
    slug          TEXT PRIMARY KEY,
    gift_id       INTEGER,
    num           INTEGER NOT NULL,
    title         TEXT NOT NULL,
    detected_at   REAL NOT NULL,
    passed_filter INTEGER NOT NULL,
    filter_reason TEXT
);
CREATE INDEX IF NOT EXISTS idx_seen_detected ON seen_gifts(detected_at);

-- Last known state of the regular gift catalog (payments.getStarGifts).
CREATE TABLE IF NOT EXISTS catalog_gifts (
    gift_id       INTEGER PRIMARY KEY,
    title         TEXT,
    stars         INTEGER,
    upgrade_stars INTEGER,
    sold_out      INTEGER NOT NULL DEFAULT 0,
    first_seen_at REAL NOT NULL,
    last_seen_at  REAL NOT NULL
);

-- Alert outbox. PRIMARY KEY on key makes enqueueing idempotent.
CREATE TABLE IF NOT EXISTS alerts (
    key        TEXT PRIMARY KEY,   -- gift slug, or 'catalog:<event>:<gift_id>'
    kind       TEXT NOT NULL,      -- 'upgrade' | 'catalog'
    status     TEXT NOT NULL,      -- pending | sent | failed | expired | dry_run
    payload    TEXT NOT NULL,      -- JSON
    created_at REAL NOT NULL,
    sent_at    REAL,
    attempts   INTEGER NOT NULL DEFAULT 0,
    last_error TEXT
);
CREATE INDEX IF NOT EXISTS idx_alerts_status ON alerts(status, created_at);

-- Alert rules edited from the Telegram control panel (JSON per rule).
CREATE TABLE IF NOT EXISTS rules (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    data       TEXT NOT NULL,
    created_at REAL NOT NULL
);

-- Trait names observed on detected gifts; they feed the panel's pick-lists.
CREATE TABLE IF NOT EXISTS seen_traits (
    kind      TEXT NOT NULL,      -- model | symbol | backdrop
    coll      TEXT NOT NULL,      -- normalized collection key
    name      TEXT NOT NULL,
    n         INTEGER NOT NULL DEFAULT 0,
    last_seen REAL NOT NULL,
    PRIMARY KEY (kind, coll, name)
);
"""

T = TypeVar("T")


class Database:
    def __init__(self, path: str) -> None:
        self.path = path
        self._conn: Optional[sqlite3.Connection] = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------ lifecycle
    async def open(self) -> None:
        await asyncio.to_thread(self._open_sync)

    def _open_sync(self) -> None:
        directory = os.path.dirname(os.path.abspath(self.path))
        os.makedirs(directory, exist_ok=True)
        conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.executescript(SCHEMA)
        row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        if row is None:
            conn.execute("INSERT INTO meta(key,value) VALUES('schema_version', ?)", (str(SCHEMA_VERSION),))
        elif int(row["value"]) > SCHEMA_VERSION:
            raise RuntimeError(
                f"Database schema version {row['value']} is newer than this program supports ({SCHEMA_VERSION})"
            )
        self._conn = conn

    async def close(self) -> None:
        await asyncio.to_thread(self._close_sync)

    def _close_sync(self) -> None:
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                except sqlite3.Error:  # pragma: no cover
                    pass
                self._conn.close()
                self._conn = None

    async def _run(self, fn: Callable[[sqlite3.Connection], T]) -> T:
        def locked() -> T:
            with self._lock:
                if self._conn is None:
                    raise RuntimeError("database is not open")
                return fn(self._conn)

        return await asyncio.to_thread(locked)

    # ----------------------------------------------------------------- meta
    async def get_meta(self, key: str) -> Optional[str]:
        def op(c: sqlite3.Connection) -> Optional[str]:
            r = c.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
            return r["value"] if r else None

        return await self._run(op)

    async def set_meta(self, key: str, value: str) -> None:
        def op(c: sqlite3.Connection) -> None:
            c.execute(
                "INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )

        await self._run(op)

    # ---------------------------------------------------------- collections
    async def upsert_collection(
        self, key: str, title: str, slug_prefix: str, gift_id: Optional[int], now: Optional[float] = None
    ) -> bool:
        """Insert a collection if unknown. Returns True if it was newly created.

        Existing rows keep their progress; a verified prefix is never overwritten.
        """
        ts = now if now is not None else time.time()

        def op(c: sqlite3.Connection) -> bool:
            existing = c.execute("SELECT 1 FROM collections WHERE key=?", (key,)).fetchone()
            c.execute(
                """INSERT INTO collections(key,gift_id,title,slug_prefix,created_at) VALUES(?,?,?,?,?)
                   ON CONFLICT(key) DO UPDATE SET
                     gift_id = COALESCE(excluded.gift_id, collections.gift_id),
                     title = excluded.title,
                     slug_prefix = CASE WHEN collections.prefix_verified=1
                                        THEN collections.slug_prefix ELSE excluded.slug_prefix END""",
                (key, gift_id, title, slug_prefix, ts),
            )
            return existing is None

        return await self._run(op)

    async def load_collections(self) -> List[Dict[str, Any]]:
        def op(c: sqlite3.Connection) -> List[Dict[str, Any]]:
            return [dict(r) for r in c.execute("SELECT * FROM collections ORDER BY title")]

        return await self._run(op)

    async def update_collection(self, key: str, **fields: Any) -> None:
        allowed = {
            "slug_prefix",
            "prefix_verified",
            "bootstrapped",
            "last_num",
            "known_issued",
            "last_found_at",
            "gift_id",
        }
        bad = set(fields) - allowed
        if bad:
            raise ValueError(f"unknown collection fields: {bad}")
        if not fields:
            return
        sets = ", ".join(f"{k}=?" for k in fields)

        def op(c: sqlite3.Connection) -> None:
            c.execute(f"UPDATE collections SET {sets} WHERE key=?", (*fields.values(), key))

        await self._run(op)

    # ------------------------------------------------------------ detections
    async def record_detection(
        self, gift: UniqueGift, passed_filter: bool, reason: str = "", now: Optional[float] = None
    ) -> bool:
        """Atomically record a gift; returns True only the FIRST time it is seen.

        If it passed the filters, an ``alerts`` outbox row is created in the same
        transaction, so a crash can never lose or duplicate the alert decision.
        """
        ts = now if now is not None else time.time()
        payload = json.dumps(gift.to_dict(), separators=(",", ":"))

        def op(c: sqlite3.Connection) -> bool:
            c.execute("BEGIN IMMEDIATE")
            try:
                cur = c.execute(
                    "INSERT OR IGNORE INTO seen_gifts(slug,gift_id,num,title,detected_at,passed_filter,filter_reason)"
                    " VALUES(?,?,?,?,?,?,?)",
                    (gift.slug, gift.gift_id, gift.num, gift.title, ts, 1 if passed_filter else 0, reason),
                )
                if cur.rowcount == 0:
                    c.execute("COMMIT")
                    return False
                if passed_filter:
                    c.execute(
                        "INSERT OR IGNORE INTO alerts(key,kind,status,payload,created_at) VALUES(?,?,?,?,?)",
                        (gift.slug, "upgrade", "pending", payload, ts),
                    )
                c.execute("COMMIT")
                return True
            except BaseException:
                c.execute("ROLLBACK")
                raise

        return await self._run(op)

    async def has_seen(self, slug: str) -> bool:
        def op(c: sqlite3.Connection) -> bool:
            return c.execute("SELECT 1 FROM seen_gifts WHERE slug=?", (slug,)).fetchone() is not None

        return await self._run(op)

    # --------------------------------------------------------------- catalog
    async def load_catalog(self) -> Dict[int, Dict[str, Any]]:
        def op(c: sqlite3.Connection) -> Dict[int, Dict[str, Any]]:
            return {r["gift_id"]: dict(r) for r in c.execute("SELECT * FROM catalog_gifts")}

        return await self._run(op)

    async def upsert_catalog(self, gift: CatalogGift, now: Optional[float] = None) -> None:
        ts = now if now is not None else time.time()

        def op(c: sqlite3.Connection) -> None:
            c.execute(
                """INSERT INTO catalog_gifts(gift_id,title,stars,upgrade_stars,sold_out,first_seen_at,last_seen_at)
                   VALUES(?,?,?,?,?,?,?)
                   ON CONFLICT(gift_id) DO UPDATE SET title=excluded.title, stars=excluded.stars,
                     upgrade_stars=excluded.upgrade_stars, sold_out=excluded.sold_out,
                     last_seen_at=excluded.last_seen_at""",
                (gift.gift_id, gift.title, gift.stars, gift.upgrade_stars, 1 if gift.sold_out else 0, ts, ts),
            )

        await self._run(op)

    # ---------------------------------------------------------------- alerts
    async def enqueue_alert(self, key: str, kind: str, payload: Dict[str, Any], now: Optional[float] = None) -> bool:
        """Idempotently queue an alert. Returns True if it was newly queued."""
        ts = now if now is not None else time.time()

        def op(c: sqlite3.Connection) -> bool:
            cur = c.execute(
                "INSERT OR IGNORE INTO alerts(key,kind,status,payload,created_at) VALUES(?,?,?,?,?)",
                (key, kind, "pending", json.dumps(payload, separators=(",", ":")), ts),
            )
            return cur.rowcount > 0

        return await self._run(op)

    async def mark_alert(
        self, key: str, status: str, error: Optional[str] = None, now: Optional[float] = None, add_attempt: bool = False
    ) -> None:
        ts = now if now is not None else time.time()

        def op(c: sqlite3.Connection) -> None:
            c.execute(
                "UPDATE alerts SET status=?, last_error=?, sent_at=CASE WHEN ? IN ('sent','dry_run') THEN ? ELSE sent_at END,"
                " attempts=attempts+? WHERE key=?",
                (status, error, status, ts, 1 if add_attempt else 0, key),
            )

        await self._run(op)

    async def load_pending_alerts(self, max_age: float, now: Optional[float] = None) -> Tuple[List[Dict[str, Any]], int]:
        """Return (still-fresh pending alerts, number expired by age)."""
        ts = now if now is not None else time.time()

        def op(c: sqlite3.Connection) -> Tuple[List[Dict[str, Any]], int]:
            cur = c.execute(
                "UPDATE alerts SET status='expired', last_error='too old after restart'"
                " WHERE status='pending' AND created_at < ?",
                (ts - max_age,),
            )
            expired = cur.rowcount
            rows = [dict(r) for r in c.execute("SELECT * FROM alerts WHERE status='pending' ORDER BY created_at, rowid")]
            return rows, expired

        return await self._run(op)

    async def alert_status(self, key: str) -> Optional[str]:
        def op(c: sqlite3.Connection) -> Optional[str]:
            r = c.execute("SELECT status FROM alerts WHERE key=?", (key,)).fetchone()
            return r["status"] if r else None

        return await self._run(op)

    # ----------------------------------------------------------------- rules
    async def add_rule(self, data: Dict[str, Any]) -> int:
        def op(c: sqlite3.Connection) -> int:
            cur = c.execute("INSERT INTO rules(data,created_at) VALUES(?,?)", (json.dumps(data), time.time()))
            return int(cur.lastrowid)

        return await self._run(op)

    async def save_rule(self, rule_id: int, data: Dict[str, Any]) -> None:
        def op(c: sqlite3.Connection) -> None:
            c.execute("UPDATE rules SET data=? WHERE id=?", (json.dumps(data), rule_id))

        await self._run(op)

    async def delete_rule(self, rule_id: int) -> None:
        def op(c: sqlite3.Connection) -> None:
            c.execute("DELETE FROM rules WHERE id=?", (rule_id,))

        await self._run(op)

    async def load_rules(self) -> List[Tuple[int, Dict[str, Any]]]:
        def op(c: sqlite3.Connection) -> List[Tuple[int, Dict[str, Any]]]:
            return [(r["id"], json.loads(r["data"])) for r in c.execute("SELECT id, data FROM rules ORDER BY id")]

        return await self._run(op)

    # ---------------------------------------------------------------- traits
    async def note_traits(self, coll: str, model: Optional[str], symbol: Optional[str], backdrop: Optional[str]) -> None:
        now = time.time()
        rows = [(k, coll, n, now) for k, n in (("model", model), ("symbol", symbol), ("backdrop", backdrop)) if n]

        def op(c: sqlite3.Connection) -> None:
            c.executemany(
                "INSERT INTO seen_traits(kind,coll,name,n,last_seen) VALUES(?,?,?,1,?)"
                " ON CONFLICT(kind,coll,name) DO UPDATE SET n=n+1, last_seen=excluded.last_seen",
                [(k, cl, nm, ts) for k, cl, nm, ts in rows],
            )

        if rows:
            await self._run(op)

    @staticmethod
    def _coll_clause(coll_keys: Optional[List[str]]) -> Tuple[str, List[str]]:
        if not coll_keys:
            return "", []
        return " AND coll IN (" + ",".join("?" * len(coll_keys)) + ")", list(coll_keys)

    async def list_traits(self, kind: str, coll_keys: Optional[List[str]], limit: int, offset: int) -> List[Tuple[str, int]]:
        clause, args = self._coll_clause(coll_keys)

        def op(c: sqlite3.Connection) -> List[Tuple[str, int]]:
            q = (f"SELECT name, SUM(n) AS total FROM seen_traits WHERE kind=?{clause} "
                 "GROUP BY name ORDER BY total DESC, name LIMIT ? OFFSET ?")
            return [(r["name"], r["total"]) for r in c.execute(q, (kind, *args, limit, offset))]

        return await self._run(op)

    async def count_traits(self, kind: str, coll_keys: Optional[List[str]]) -> int:
        clause, args = self._coll_clause(coll_keys)

        def op(c: sqlite3.Connection) -> int:
            q = f"SELECT COUNT(DISTINCT name) AS n FROM seen_traits WHERE kind=?{clause}"
            return int(c.execute(q, (kind, *args)).fetchone()["n"])

        return await self._run(op)

    # ----------------------------------------------------------- maintenance
    async def prune(self, older_than_days: float = 60.0, now: Optional[float] = None) -> int:
        cutoff = (now if now is not None else time.time()) - older_than_days * 86400

        def op(c: sqlite3.Connection) -> int:
            a = c.execute("DELETE FROM seen_gifts WHERE detected_at < ?", (cutoff,)).rowcount
            b = c.execute("DELETE FROM alerts WHERE created_at < ? AND status != 'pending'", (cutoff,)).rowcount
            return a + b

        return await self._run(op)

    async def counts(self) -> Dict[str, int]:
        def op(c: sqlite3.Connection) -> Dict[str, int]:
            out: Dict[str, int] = {}
            for table in ("collections", "seen_gifts", "catalog_gifts", "alerts"):
                out[table] = c.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
            out["alerts_pending"] = c.execute("SELECT COUNT(*) AS n FROM alerts WHERE status='pending'").fetchone()["n"]
            return out

        return await self._run(op)


# ==========================================================================
# source
# ==========================================================================
"""Interface between the detector and the Telegram *user session*.

The detector, catalog watcher and alert pipeline only depend on this abstract
interface, so they can be unit-tested with an in-memory fake (no network, no
Telethon).  The real implementation is ``telethon_source.TelethonSource``.
"""

from typing import List, Optional



class SourceError(Exception):
    """Base class for gift-source failures."""


class FatalSourceError(SourceError):
    """Unrecoverable setup problem (e.g. Telethon missing/too old): retrying cannot help."""


class TransientSourceError(SourceError):
    """Network trouble / timeouts / Telegram 5xx: retry with backoff."""


class RateLimitedError(SourceError):
    """Telegram asked us to wait (FLOOD_WAIT)."""

    def __init__(self, seconds: float, detail: str = "") -> None:
        super().__init__(f"rate limited for {seconds}s {detail}".strip())
        self.seconds = float(seconds)


class SessionInvalidError(SourceError):
    """The user session is revoked/expired/duplicated: a human must act."""


class GiftSource:
    """Abstract data source for gift information."""

    async def connect(self) -> None:  # pragma: no cover - interface
        raise NotImplementedError

    async def disconnect(self) -> None:  # pragma: no cover - interface
        raise NotImplementedError

    async def ensure_connected(self) -> None:  # pragma: no cover - interface
        raise NotImplementedError

    async def get_catalog(self) -> Optional[List[CatalogGift]]:
        """Return the gift catalog, or None if unchanged since the last call."""
        raise NotImplementedError  # pragma: no cover

    async def get_unique(self, slug: str) -> Optional[UniqueGift]:
        """Resolve a collectible by slug ('PlushPepe-12'); None if it does not exist."""
        raise NotImplementedError  # pragma: no cover

    async def find_resale_sample(self, gift_id: int) -> Optional[UniqueGift]:
        """Any collectible of this gift type currently on resale (authoritative slug prefix + counters)."""
        raise NotImplementedError  # pragma: no cover

    async def get_floor(self, gift_id: int, model_document_id: Optional[int]) -> Floor:
        """Cheapest Telegram resale listings (Stars) for the collection and model."""
        raise NotImplementedError  # pragma: no cover


# ==========================================================================
# tl_parse
# ==========================================================================
"""Convert Telethon TL objects into the plain models in ``models.py``.

Parsing is duck-typed (by class name and attribute) on purpose:

* it does not import Telethon, so it is unit-testable without it;
* it tolerates schema differences between layers (e.g. ``rarity`` object in
  newer layers vs ``rarity_permille`` int in older ones).

Field names follow https://core.telegram.org/constructor/starGiftUnique and
https://core.telegram.org/constructor/starGift
"""

from typing import Any, Iterable, List, Optional


_RARITY_PREFIX = "StarGiftAttributeRarity"


def _cls(obj: Any) -> str:
    return type(obj).__name__


def _opt_int(v: Any) -> Optional[int]:
    if v is None or isinstance(v, bool):
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def parse_rarity(attr: Any) -> tuple:
    """Return (permille, tier) for an attribute, either may be None."""
    rarity = getattr(attr, "rarity", None)
    permille: Optional[int] = None
    tier: Optional[str] = None
    if rarity is not None:
        permille = _opt_int(getattr(rarity, "permille", None))
        if permille is None:
            name = _cls(rarity)
            if name.startswith(_RARITY_PREFIX) and len(name) > len(_RARITY_PREFIX):
                tier = name[len(_RARITY_PREFIX):].lower()
    if permille is None and tier is None:
        permille = _opt_int(getattr(attr, "rarity_permille", None))  # older layers
    return permille, tier


def _trait(attr: Any) -> Trait:
    permille, tier = parse_rarity(attr)
    return Trait(name=str(getattr(attr, "name", "") or ""), permille=permille, tier=tier)


def stars_amount(amount: Any) -> Optional[int]:
    """Whole Stars from a StarsAmount (ignores TON amounts)."""
    if amount is None:
        return None
    if _cls(amount) == "StarsAmount":
        return _opt_int(getattr(amount, "amount", None))
    if _cls(amount) == "StarsTonAmount":
        return None
    return None


def first_stars_price(resell: Any) -> Optional[int]:
    """Stars price out of ``starGiftUnique.resell_amount`` (vector) or legacy ``resell_stars``."""
    if resell is None:
        return None
    if isinstance(resell, (list, tuple)):
        for item in resell:
            s = stars_amount(item)
            if s is not None:
                return s
        return None
    return stars_amount(resell) if not isinstance(resell, int) else int(resell)


def _peer(peer: Any) -> tuple:
    """(kind, id) for a Peer object (or a bare int in older layers)."""
    if peer is None:
        return None, None
    if isinstance(peer, int) and not isinstance(peer, bool):
        return "user", peer
    name = _cls(peer)
    if name == "PeerUser":
        return "user", _opt_int(getattr(peer, "user_id", None))
    if name == "PeerChannel":
        return "channel", _opt_int(getattr(peer, "channel_id", None))
    if name == "PeerChat":
        return "channel", _opt_int(getattr(peer, "chat_id", None))
    return None, None


def _public_username(entity: Any) -> Optional[str]:
    uname = getattr(entity, "username", None)
    if uname:
        return str(uname)
    for u in getattr(entity, "usernames", None) or []:
        if getattr(u, "active", True) and getattr(u, "username", None):
            return str(u.username)
    return None


def parse_owner(tl: Any, users: Any = None, chats: Any = None) -> dict:
    """Owner info of a collectible. Only what Telegram actually provides; unknown stays None."""
    kind, oid = _peer(getattr(tl, "owner_id", None))
    out: dict = {"owner_id": oid, "owner_username": None, "owner_name": None, "owner_is_channel": kind == "channel"}
    if oid is not None:
        for entity in (chats if kind == "channel" else users) or []:
            if _opt_int(getattr(entity, "id", None)) == oid:
                out["owner_username"] = _public_username(entity)
                full = " ".join(p for p in (getattr(entity, "first_name", None), getattr(entity, "last_name", None)) if p)
                out["owner_name"] = full or getattr(entity, "title", None) or None
                break
    else:
        hidden = getattr(tl, "owner_name", None)  # shown by Telegram when the owner chose to stay hidden
        out["owner_name"] = str(hidden) if hidden else None
    return out


def parse_unique(tl: Any, users: Any = None, chats: Any = None) -> UniqueGift:
    """Parse a ``StarGiftUnique`` into :class:`UniqueGift` (``users``/``chats`` resolve the owner)."""
    if _cls(tl) not in ("StarGiftUnique",):
        raise ValueError(f"expected StarGiftUnique, got {_cls(tl)}")
    model = symbol = backdrop = None
    model_doc: Optional[int] = None
    for attr in getattr(tl, "attributes", None) or []:
        name = _cls(attr)
        if name == "StarGiftAttributeModel" and model is None:
            model = _trait(attr)
            model_doc = _opt_int(getattr(getattr(attr, "document", None), "id", None))
        elif name == "StarGiftAttributePattern" and symbol is None:
            symbol = _trait(attr)
        elif name == "StarGiftAttributeBackdrop" and backdrop is None:
            backdrop = _trait(attr)
    slug = str(getattr(tl, "slug", "") or "")
    num = _opt_int(getattr(tl, "num", None))
    if not slug or num is None:
        raise ValueError("StarGiftUnique without slug/num")
    return UniqueGift(
        slug=slug,
        title=str(getattr(tl, "title", "") or slug.rsplit("-", 1)[0]),
        num=num,
        gift_id=_opt_int(getattr(tl, "gift_id", None)),
        unique_id=_opt_int(getattr(tl, "id", None)),
        model=model,
        symbol=symbol,
        backdrop=backdrop,
        model_document_id=model_doc,
        issued=_opt_int(getattr(tl, "availability_issued", None)),
        total=_opt_int(getattr(tl, "availability_total", None)),
        crafted=bool(getattr(tl, "crafted", False)),
        burned=bool(getattr(tl, "burned", False)),
        **parse_owner(tl, users, chats),
    )


def parse_catalog_gift(tl: Any) -> Optional[CatalogGift]:
    """Parse a regular ``StarGift`` (returns None for collectibles/unknown)."""
    if _cls(tl) != "StarGift":
        return None
    gid = _opt_int(getattr(tl, "id", None))
    if gid is None:
        return None
    title = getattr(tl, "title", None)
    return CatalogGift(
        gift_id=gid,
        title=str(title) if title else None,
        stars=_opt_int(getattr(tl, "stars", None)),
        upgrade_stars=_opt_int(getattr(tl, "upgrade_stars", None)),
        limited=bool(getattr(tl, "limited", False)),
        sold_out=bool(getattr(tl, "sold_out", False)),
        availability_total=_opt_int(getattr(tl, "availability_total", None)),
        availability_remains=_opt_int(getattr(tl, "availability_remains", None)),
        availability_resale=_opt_int(getattr(tl, "availability_resale", None)),
        resell_min_stars=_opt_int(getattr(tl, "resell_min_stars", None)),
        auction=bool(getattr(tl, "auction", False)),
        require_premium=bool(getattr(tl, "require_premium", False)),
        birthday=bool(getattr(tl, "birthday", False)),
    )


def parse_catalog(gifts: Iterable[Any]) -> List[CatalogGift]:
    out: List[CatalogGift] = []
    for g in gifts:
        parsed = parse_catalog_gift(g)
        if parsed is not None:
            out.append(parsed)
    return out


def min_resale_stars(gifts: Iterable[Any]) -> Optional[int]:
    """Cheapest Stars price among resale listings (list is sorted ascending by price)."""
    prices = []
    for g in gifts:
        p = first_stars_price(getattr(g, "resell_amount", None))
        if p is None:
            p = first_stars_price(getattr(g, "resell_stars", None))
        if p is not None:
            prices.append(p)
    return min(prices) if prices else None


# ==========================================================================
# telethon_source
# ==========================================================================
"""Real gift source: a Telethon *user* session talking MTProto.

Why a user session?  Telegram's Bot API has NO method that reveals gifts that
other people upgrade (it only reports gifts a bot or its business account
receives).  The MTProto methods below, used by the official apps, are only
available to user accounts ("Only users can use this method"):

* payments.getStarGifts          catalog of gift types (+ hash-based caching)
* payments.getUniqueStarGift     resolve a collectible by slug ``Name-N``
* payments.getResaleStarGifts    market listings, used for floor prices

Everything Telethon-specific lives in this file and is imported lazily.
"""

import asyncio
import logging
import re
from typing import Any, List, Optional


log_telethon_source = logging.getLogger("giftparser.telegram")

_FATAL_AUTH = (
    "AUTHKEYUNREGISTERED",
    "AUTHKEYDUPLICATED",
    "AUTHKEYINVALID",
    "SESSIONREVOKED",
    "SESSIONEXPIRED",
    "USERDEACTIVATED",
    "SESSIONPASSWORDNEEDED",
)
_FLOOD_RE = re.compile(r"FLOOD_WAIT_?(\d+)|wait of (\d+) seconds", re.IGNORECASE)


class SlugNotFound(SourceError):
    """STARGIFT_SLUG_INVALID: the collectible does not exist (yet)."""


class SlugBurned(SourceError):
    """STARGIFT_ALREADY_BURNED: the number exists but the gift was burned (cannot be resolved)."""


def classify_exception(exc: BaseException) -> Exception:
    """Map any exception raised while calling Telegram to one of our error types.

    Works from class names/messages so it does not need Telethon's error
    classes (they are generated dynamically and vary between versions).
    """
    blob_raw = f"{type(exc).__name__} {getattr(exc, 'message', '')} {exc}"
    blob = blob_raw.upper()
    squashed = blob.replace("_", "").replace(" ", "")

    if "STARGIFT_ALREADY_BURNED" in blob or "STARGIFTALREADYBURNED" in squashed:
        return SlugBurned(str(exc))
    if "STARGIFT_SLUG_INVALID" in blob or "STARGIFTSLUGINVALID" in squashed:
        return SlugNotFound(str(exc))
    if "FLOODWAIT" in squashed or "FLOOD_WAIT" in blob or "SLOWMODEWAIT" in squashed:
        secs = getattr(exc, "seconds", None)
        if secs is None:
            m = _FLOOD_RE.search(blob_raw)
            secs = int(next(g for g in m.groups() if g)) if m else 30
        return RateLimitedError(float(secs), type(exc).__name__)
    if any(tag in squashed for tag in _FATAL_AUTH):
        return SessionInvalidError(
            f"{type(exc).__name__}: the Telegram session is no longer valid. Create a new one with "
            "generate_session.py and update TELEGRAM_SESSION. (AUTH_KEY_DUPLICATED means the same session "
            "string is being used by two running processes at once.)"
        )
    if isinstance(exc, (ConnectionError, TimeoutError, asyncio.TimeoutError, OSError, EOFError)):
        return TransientSourceError(f"{type(exc).__name__}: {exc}")
    if any(tag in squashed for tag in ("TIMEOUT", "RPCCALLFAIL", "INTERNAL", "SERVERERROR", "MSGWAIT", "CONNECTION")):
        return TransientSourceError(f"{type(exc).__name__}: {exc}")
    code = getattr(exc, "code", None)
    if isinstance(code, int) and code >= 500:
        return TransientSourceError(f"{type(exc).__name__}: {exc}")
    return SourceError(f"{type(exc).__name__}: {exc}")


class TelethonSource(GiftSource):
    RPC_TIMEOUT = 20.0

    def __init__(self, api_id: int, api_hash: str, session: str) -> None:
        self._api_id = api_id
        self._api_hash = api_hash
        self._session = session
        self._client: Any = None
        self._fn: Any = None  # telethon.tl.functions
        self._types: Any = None  # telethon.tl.types
        self._catalog_hash = 0
        self._connect_lock = asyncio.Lock()

    # ------------------------------------------------------------ lifecycle
    def _import(self) -> None:
        if self._fn is not None:
            return
        try:
            from telethon import TelegramClient  # noqa: F401
            from telethon.sessions import StringSession  # noqa: F401
            from telethon.tl import functions, types
        except ImportError as exc:  # pragma: no cover
            raise FatalSourceError("Telethon is not installed: pip install -r requirements.txt") from exc
        missing = [
            n
            for n in ("GetStarGiftsRequest", "GetUniqueStarGiftRequest", "GetResaleStarGiftsRequest")
            if not hasattr(functions.payments, n)
        ]
        if missing:
            raise FatalSourceError(
                "The installed Telethon is too old: missing " + ", ".join(missing) + ". "
                "Run: pip install -U 'telethon>=1.45.0'"
            )
        self._fn, self._types = functions, types

    def _make_client(self) -> Any:
        from telethon import TelegramClient
        from telethon.sessions import StringSession

        return TelegramClient(
            StringSession(self._session),
            self._api_id,
            self._api_hash,
            receive_updates=False,  # we only issue requests; no update stream is needed
            flood_sleep_threshold=0,  # surface FLOOD_WAIT so our limiter handles/logs it
            connection_retries=10,
            retry_delay=2,
            auto_reconnect=True,
            request_retries=2,
            timeout=15,
            device_model="Gift Parser (Railway)",
            system_version="Linux",
            app_version="1.0",
        )

    async def connect(self) -> None:
        self._import()
        async with self._connect_lock:
            if self._client is None:
                self._client = self._make_client()
            if not self._client.is_connected():
                try:
                    await self._client.connect()
                except Exception as exc:
                    raise classify_exception(exc) from exc
            try:
                authorized = await self._client.is_user_authorized()
            except Exception as exc:
                raise classify_exception(exc) from exc
            if not authorized:
                raise SessionInvalidError(
                    "TELEGRAM_SESSION is not authorized (revoked, expired or wrong api_id/api_hash). "
                    "Generate a new one with generate_session.py using the SAME api_id/api_hash."
                )

    async def ensure_connected(self) -> None:
        if self._client is None or not self._client.is_connected():
            log_telethon_source.warning("Telegram connection is down; reconnecting")
            await self.connect()
            log_telethon_source.info("Telegram connection restored")

    async def disconnect(self) -> None:
        if self._client is not None:
            try:
                await asyncio.wait_for(self._client.disconnect(), timeout=5)
            except Exception as exc:  # pragma: no cover
                log_telethon_source.debug("error while disconnecting: %s", exc)

    async def whoami(self) -> str:
        me = await self._call(self._fn.users.GetUsersRequest(id=[self._types.InputUserSelf()]))
        user = me[0]
        return f"id={getattr(user, 'id', '?')} bot={bool(getattr(user, 'bot', False))}"

    # ----------------------------------------------------------------- core
    async def _call(self, request: Any) -> Any:
        if self._client is None:
            raise TransientSourceError("not connected")
        try:
            return await asyncio.wait_for(self._client(request), timeout=self.RPC_TIMEOUT)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise classify_exception(exc) from exc

    # --------------------------------------------------------------- queries
    async def get_catalog(self) -> Optional[List[CatalogGift]]:
        result = await self._call(self._fn.payments.GetStarGiftsRequest(hash=self._catalog_hash))
        if type(result).__name__ == "StarGiftsNotModified":
            return None
        self._catalog_hash = int(getattr(result, "hash", 0) or 0)
        return parse_catalog(getattr(result, "gifts", []) or [])

    async def get_unique(self, slug: str) -> Optional[UniqueGift]:
        try:
            result = await self._call(self._fn.payments.GetUniqueStarGiftRequest(slug=slug))
        except SlugNotFound:
            return None
        except SlugBurned:
            # The number was used but the gift was burned: Telegram refuses to show it. Return a
            # placeholder so the detector steps over this number instead of getting stuck on it.
            prefix, _, num = slug.rpartition("-")
            return UniqueGift(slug=slug, title=prefix, num=int(num), burned=True)
        return parse_unique(result.gift, getattr(result, "users", None), getattr(result, "chats", None))

    def _resale_request(self, gift_id: int, limit: int, model_document_id: Optional[int] = None) -> Any:
        kwargs: dict = {"gift_id": gift_id, "offset": "", "limit": limit}
        if model_document_id is not None:
            kwargs["attributes"] = [self._types.StarGiftAttributeIdModel(document_id=model_document_id)]
        return kwargs

    async def _resale(self, gift_id: int, limit: int, *, by_price: bool, model_document_id: Optional[int] = None) -> Any:
        kwargs = self._resale_request(gift_id, limit, model_document_id)
        if by_price:
            kwargs["sort_by_price"] = True
            kwargs["stars_only"] = True
        try:
            return await self._call(self._fn.payments.GetResaleStarGiftsRequest(**kwargs))
        except TypeError:
            # a Telethon build without the newer flag: retry with the minimal call
            kwargs.pop("stars_only", None)
            return await self._call(self._fn.payments.GetResaleStarGiftsRequest(**kwargs))

    async def find_resale_sample(self, gift_id: int) -> Optional[UniqueGift]:
        result = await self._resale(gift_id, 1, by_price=False)
        for g in getattr(result, "gifts", []) or []:
            try:
                return parse_unique(g)
            except ValueError:
                continue
        return None

    async def get_floor(self, gift_id: int, model_document_id: Optional[int]) -> Floor:
        coll = await self._resale(gift_id, 3, by_price=True)
        coll_gifts = list(getattr(coll, "gifts", []) or [])
        floor_c = min_resale_stars(coll_gifts)
        listed_c = getattr(coll, "count", None) if floor_c is not None else None
        floor_m = listed_m = None
        if model_document_id is not None:
            try:
                mdl = await self._resale(gift_id, 3, by_price=True, model_document_id=model_document_id)
                floor_m = min_resale_stars(getattr(mdl, "gifts", []) or [])
                listed_m = getattr(mdl, "count", None) if floor_m is not None else None
            except SourceError as exc:
                log_telethon_source.debug("model floor lookup failed: %s", exc)
        return Floor(
            collection_stars=floor_c,
            collection_listed=int(listed_c) if listed_c is not None else None,
            model_stars=floor_m,
            model_listed=int(listed_m) if listed_m is not None else None,
        )


# ==========================================================================
# runtime
# ==========================================================================
"""Rules and switches edited at runtime from the Telegram control panel.

A *rule* describes gifts you want alerts for.  Every field that is set must
match (AND); a field left empty means "any".  A gift alerts if ANY enabled rule
matches.  With no rules at all, every upgrade alerts (subject to the filters in
the environment variables).  Everything is stored in SQLite, so it survives
restarts and redeploys.
"""

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple


log_runtime = logging.getLogger("giftparser.runtime")

LIST_FIELDS = ("collections", "models", "backdrops", "symbols")


def _norm(names: List[str]) -> Set[str]:
    return {normalize_name(n) for n in names if n}


@dataclass
class Rule:
    id: int = 0
    enabled: bool = True
    collections: List[str] = field(default_factory=list)
    models: List[str] = field(default_factory=list)
    backdrops: List[str] = field(default_factory=list)
    symbols: List[str] = field(default_factory=list)
    max_permille: Optional[int] = None  # the rarest trait must be at or below this (per-mille)
    min_num: Optional[int] = None
    max_num: Optional[int] = None

    def matches(self, gift: UniqueGift) -> bool:
        if not self.enabled:
            return False
        if self.collections and not ({normalize_name(gift.title), normalize_name(gift.prefix)} & _norm(self.collections)):
            return False
        for names, trait in ((self.models, gift.model), (self.backdrops, gift.backdrop), (self.symbols, gift.symbol)):
            if names and (trait is None or normalize_name(trait.name) not in _norm(names)):
                return False
        if self.max_permille is not None:
            rarest = gift.rarest()
            if rarest is None or rarest[1].permille > self.max_permille:
                return False
        if self.min_num is not None and gift.num < self.min_num:
            return False
        if self.max_num is not None and gift.num > self.max_num:
            return False
        return True

    def to_dict(self) -> Dict[str, Any]:
        return {
            "enabled": self.enabled, "collections": self.collections, "models": self.models,
            "backdrops": self.backdrops, "symbols": self.symbols, "max_permille": self.max_permille,
            "min_num": self.min_num, "max_num": self.max_num,
        }

    @classmethod
    def from_dict(cls, rule_id: int, d: Dict[str, Any]) -> "Rule":
        return cls(
            id=rule_id, enabled=bool(d.get("enabled", True)),
            collections=list(d.get("collections") or []), models=list(d.get("models") or []),
            backdrops=list(d.get("backdrops") or []), symbols=list(d.get("symbols") or []),
            max_permille=d.get("max_permille"), min_num=d.get("min_num"), max_num=d.get("max_num"),
        )

    def is_empty(self) -> bool:
        return not any((self.collections, self.models, self.backdrops, self.symbols)) and self.max_permille is None \
            and self.min_num is None and self.max_num is None

    def short(self) -> str:
        """One-line label for lists."""
        bits = []
        if self.collections:
            bits.append("/".join(self.collections[:2]) + ("…" if len(self.collections) > 2 else ""))
        for label, names in (("model", self.models), ("bg", self.backdrops), ("sym", self.symbols)):
            if names:
                bits.append(f"{label}:" + "/".join(names[:2]) + ("…" if len(names) > 2 else ""))
        if self.max_permille is not None:
            bits.append(f"≤{self.max_permille / 10:g}%")
        if self.min_num is not None or self.max_num is not None:
            bits.append(f"#{self.min_num or 1}–{self.max_num or '∞'}")
        return " · ".join(bits) or "everything"


class Runtime:
    """In-memory view of the panel settings, persisted through the database."""

    FLAGS = ("paused", "alert_new_gifts", "floor_enabled")

    def __init__(self, settings: Settings, db: Database) -> None:
        self.db = db
        self.rules: Dict[int, Rule] = {}
        self.paused = False
        self.alert_new_gifts = settings.alert_new_gifts
        self.floor_enabled = settings.floor_enabled

    async def load(self) -> None:
        self.rules = {rid: Rule.from_dict(rid, data) for rid, data in await self.db.load_rules()}
        for name in self.FLAGS:
            value = await self.db.get_meta(f"rt_{name}")
            if value is not None:
                setattr(self, name, value == "1")
        log_runtime.info("Control panel settings loaded: %d rule(s), paused=%s", len(self.rules), self.paused)

    async def set_flag(self, name: str, value: bool) -> None:
        if name not in self.FLAGS:
            raise ValueError(name)
        setattr(self, name, value)
        await self.db.set_meta(f"rt_{name}", "1" if value else "0")

    async def create_rule(self) -> Rule:
        rule = Rule()
        rule.id = await self.db.add_rule(rule.to_dict())
        self.rules[rule.id] = rule
        return rule

    async def save_rule(self, rule: Rule) -> None:
        await self.db.save_rule(rule.id, rule.to_dict())
        self.rules[rule.id] = rule

    async def delete_rule(self, rule_id: int) -> None:
        await self.db.delete_rule(rule_id)
        self.rules.pop(rule_id, None)

    def active_rules(self) -> List[Rule]:
        return [r for r in self.rules.values() if r.enabled]

    def evaluate(self, gift: UniqueGift) -> Tuple[bool, str]:
        if self.paused:
            return False, "alerts paused"
        if not self.rules:
            return True, ""
        active = self.active_rules()
        if not active:
            return False, "all rules switched off"
        if any(r.matches(gift) for r in active):
            return True, ""
        return False, "no rule matched"

    def polled_collections(self) -> Optional[Set[str]]:
        """Normalized collections that need probing, or None for 'all of them'."""
        if not self.rules:
            return None
        active = self.active_rules()
        if any(not r.collections for r in active):
            return None
        return {normalize_name(c) for r in active for c in r.collections}

    def should_poll(self, *names: str) -> bool:
        wanted = self.polled_collections()
        return wanted is None or bool(wanted & {normalize_name(n) for n in names if n})


# ==========================================================================
# filters
# ==========================================================================
"""Collection / rarity / number filters."""

from typing import Iterable, Optional, Tuple



class GiftFilter:
    def __init__(self, settings: Settings, runtime: Optional[Runtime] = None) -> None:
        self.s = settings
        self.runtime = runtime
        self._include = {normalize_name(x) for x in settings.collections_include}
        self._exclude = {normalize_name(x) for x in settings.collections_exclude}
        self._extra = {normalize_name(x) for x in settings.extra_collections}

    @property
    def has_include_list(self) -> bool:
        return bool(self._include)

    def collection_allowed(self, *names: str) -> bool:
        """True if any of the names (title, slug prefix, key) passes include/exclude."""
        norm = {normalize_name(n) for n in names if n}
        if norm & self._exclude:
            return False
        if self._include:
            # extra collections are explicit requests, so they are watched too
            return bool(norm & self._include) or bool(norm & self._extra)
        return True

    @property
    def paused(self) -> bool:
        return bool(self.runtime and self.runtime.paused)

    @property
    def alert_new_gifts(self) -> bool:
        return self.runtime.alert_new_gifts if self.runtime else self.s.alert_new_gifts

    def should_poll(self, *names: str) -> bool:
        """False for collections no enabled panel rule cares about (saves requests)."""
        return self.runtime.should_poll(*names) if self.runtime else True

    def evaluate(self, gift: UniqueGift) -> Tuple[bool, str]:
        """Return (passes, reason). ``reason`` explains a rejection."""
        if not self.collection_allowed(gift.title, gift.prefix):
            return False, "collection filtered"
        if gift.burned:
            return False, "burned"
        if self.s.max_gift_number and gift.num > self.s.max_gift_number:
            return False, f"number {gift.num} > MAX_GIFT_NUMBER"
        if self.s.rarity_filter_active and not self._rarity_ok(gift, self.s.rarity_attributes):
            return False, "rarity filter"
        if self.runtime is not None:
            return self.runtime.evaluate(gift)
        return True, ""

    def _rarity_ok(self, gift: UniqueGift, attrs: Iterable[str]) -> bool:
        traits = gift.traits()
        for label in attrs:
            t = traits.get(label)
            if t is None:
                continue
            if self.s.rarity_max_permille and t.permille is not None and t.permille <= self.s.rarity_max_permille:
                return True
            if self.s.rarity_tiers and t.tier and t.tier.lower() in self.s.rarity_tiers:
                return True
        return False


# ==========================================================================
# formatting
# ==========================================================================
"""Alert message formatting (Telegram HTML parse mode)."""

import html
from typing import Optional



def esc(text: object) -> str:
    return html.escape(str(text), quote=False)


def _trait_line(label: str, trait: Optional[Trait]) -> Optional[str]:
    if trait is None or not trait.name:
        return None
    line = f"{label}: <b>{esc(trait.name)}</b>"
    r = trait.rarity_text()
    if r:
        line += f" ({esc(r)})"
    return line


def _stars(n: int) -> str:
    return f"{n:,} ★"


def format_owner(gift: UniqueGift) -> Optional[str]:
    """'Upgraded by' line: @username if public, else a name/ID link, else nothing."""
    if gift.owner_username:
        kind = " (channel)" if gift.owner_is_channel else ""
        return f'Upgraded by: <a href="https://t.me/{esc(gift.owner_username)}">@{esc(gift.owner_username)}</a>{kind}'
    if gift.owner_id and not gift.owner_is_channel:
        label = esc(gift.owner_name or "user")
        return f'Upgraded by: <a href="tg://user?id={gift.owner_id}">{label}</a> (no public @username)'
    if gift.owner_name:
        return f"Upgraded by: {esc(gift.owner_name)} (owner hidden)"
    return None


def format_floor(floor: Optional[Floor]) -> Optional[str]:
    if floor is None or not floor.has_data:
        return None
    parts = []
    if floor.collection_stars is not None:
        s = f"collection {_stars(floor.collection_stars)}"
        if floor.collection_listed is not None:
            s += f" ({floor.collection_listed:,} listed)"
        parts.append(s)
    if floor.model_stars is not None:
        s = f"this model {_stars(floor.model_stars)}"
        if floor.model_listed is not None:
            s += f" ({floor.model_listed:,} listed)"
        parts.append(s)
    return "Floor (Telegram resale, Stars only): " + " · ".join(parts)


def format_upgrade_alert(gift: UniqueGift, floor: Optional[Floor] = None, *, test: bool = False) -> str:
    lines = []
    if test:
        lines.append("🧪 <b>TEST ALERT</b> — pipeline check, this is not a real gift")
    lines.append("🎁 <b>New collectible gift upgraded</b>")
    lines.append(f"<b>{esc(gift.title)} #{gift.num:,}</b>")
    coll = f"Collection: {esc(gift.title)}"
    if gift.gift_id:
        coll += f" (type ID <code>{gift.gift_id}</code>)"
    lines.append(coll)
    if gift.unique_id:
        lines.append(f"Gift ID: <code>{gift.unique_id}</code>")
    owner = format_owner(gift)
    if owner:
        lines.append(owner)
    for label, trait in (("Model", gift.model), ("Backdrop", gift.backdrop), ("Symbol", gift.symbol)):
        line = _trait_line(label, trait)
        if line:
            lines.append(line)
    rarest = gift.rarest()
    if rarest is not None:
        lines.append(f"Rarest trait: {rarest[0]} ({esc(rarest[1].rarity_text())})")
    if gift.issued is not None:
        supply = f"{gift.issued:,}"
        if gift.total:
            supply += f" / {gift.total:,}"
        lines.append(f"Upgraded so far: {supply}")
    if gift.crafted:
        lines.append("Crafted gift")
    floor_line = format_floor(floor)
    if floor_line:
        lines.append(esc(floor_line))
    lines.append(f'🔗 <a href="{esc(gift.link)}">{esc(gift.link.replace("https://", ""))}</a>')
    return "\n".join(lines)


def format_catalog_alert(gift: CatalogGift, event: str, *, test: bool = False) -> str:
    title = gift.title or f"Gift {gift.gift_id}"
    if event == "upgradable":
        head = "🔓 <b>Gift can now be upgraded to a collectible</b>"
    else:
        head = "🆕 <b>New gift available</b>"
    lines = []
    if test:
        lines.append("🧪 <b>TEST ALERT</b> — pipeline check, this is not a real gift")
    lines.append(head)
    lines.append(f"<b>{esc(title)}</b> (ID <code>{gift.gift_id}</code>)")
    if gift.stars is not None:
        lines.append(f"Price: {_stars(gift.stars)}")
    if gift.limited and gift.availability_total:
        remains = gift.availability_remains
        left = f"{remains:,} of " if remains is not None else ""
        lines.append(f"Limited supply: {left}{gift.availability_total:,}")
    if gift.upgrade_stars is not None:
        lines.append(f"Upgrade cost: {_stars(gift.upgrade_stars)} → collectible")
    flags = [n for n, v in (("auction", gift.auction), ("Premium only", gift.require_premium), ("sold out", gift.sold_out)) if v]
    if flags:
        lines.append("Flags: " + ", ".join(flags))
    lines.append("Open Telegram → Gifts to see it.")
    return "\n".join(lines)


# ==========================================================================
# bot_api
# ==========================================================================
"""Telegram *Bot API* client (used ONLY for sending alerts).

Authentication here is the bot token from @BotFather.  This is completely
separate from the user session in ``telethon_source.py`` that detects gifts.
The HTTP layer is injectable so the retry logic is unit-tested without network.
"""

import asyncio
import logging
import re
from typing import Any, Awaitable, Callable, Dict, Optional, Tuple


log_bot_api = logging.getLogger("giftparser.botapi")

API_BASE = "https://api.telegram.org"


class BotApiError(Exception):
    def __init__(self, message: str, status: Optional[int] = None, retryable: bool = False) -> None:
        super().__init__(message)
        self.status = status
        self.retryable = retryable


class BotApiAuthError(BotApiError):
    """Invalid bot token (HTTP 401)."""


class HttpxTransport:
    """Default transport: one persistent HTTP/1.1 keep-alive connection pool."""

    def __init__(self, timeout: float = 15.0) -> None:
        import httpx  # lazy so tests don't need it

        self._httpx = httpx
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=10.0))

    async def post(self, url: str, payload: Dict[str, Any]) -> Tuple[int, Optional[Dict[str, Any]]]:
        resp = await self._client.post(url, json=payload)
        try:
            data = resp.json()
        except ValueError:
            data = None
        return resp.status_code, data

    async def close(self) -> None:
        await self._client.aclose()


_TAG_RE = re.compile(r"<[^>]+>")


def html_to_plain(text: str) -> str:
    import html

    return html.unescape(_TAG_RE.sub("", text))


class BotApiClient:
    def __init__(
        self,
        token: str,
        chat_id: str,
        thread_id: Optional[int] = None,
        *,
        transport: Any = None,
        max_attempts: int = 6,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._token = token
        self.chat_id = chat_id
        self.thread_id = thread_id
        self._transport = transport
        self._max_attempts = max_attempts
        self._sleep = sleep

    def _t(self) -> Any:
        if self._transport is None:
            self._transport = HttpxTransport()
        return self._transport

    async def close(self) -> None:
        t = self._transport
        if t is not None and hasattr(t, "close"):
            await t.close()

    async def _call(self, method: str, payload: Dict[str, Any]) -> Any:
        url = f"{API_BASE}/bot{self._token}/{method}"
        last_error = "unknown error"
        for attempt in range(1, self._max_attempts + 1):
            try:
                status, data = await self._t().post(url, payload)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # network failure, timeout, TLS...
                last_error = f"network error: {type(exc).__name__}"
                delay = backoff_delay(attempt, base=1.0, cap=20.0)
                log_bot_api.warning("Bot API %s failed (%s); retry %d/%d in %.1fs", method, last_error, attempt, self._max_attempts, delay)
                await self._sleep(delay)
                continue

            if status == 200 and data and data.get("ok"):
                return data.get("result")

            desc = (data or {}).get("description", f"HTTP {status}")
            params = (data or {}).get("parameters") or {}
            last_error = f"{status} {desc}"

            if status == 429:
                wait = float(params.get("retry_after", 5)) + 0.5
                log_bot_api.warning("Bot API rate limit on %s: waiting %.1fs (attempt %d/%d)", method, wait, attempt, self._max_attempts)
                await self._sleep(min(wait, 120.0))
                continue
            if status >= 500:
                delay = backoff_delay(attempt, base=1.0, cap=20.0)
                log_bot_api.warning("Bot API %s server error %s; retry in %.1fs", method, last_error, delay)
                await self._sleep(delay)
                continue
            if status == 401:
                raise BotApiAuthError(f"Bot token rejected by Telegram ({desc}). Check TELEGRAM_BOT_TOKEN.", 401)
            if "migrate_to_chat_id" in params:
                raise BotApiError(
                    f"The group was upgraded to a supergroup. Set TELEGRAM_CHAT_ID={params['migrate_to_chat_id']}", status
                )
            raise BotApiError(f"Bot API {method} rejected: {last_error}", status)
        raise BotApiError(f"Bot API {method} failed after {self._max_attempts} attempts: {last_error}", None, retryable=True)

    async def get_me(self) -> Dict[str, Any]:
        return await self._call("getMe", {})

    async def get_chat(self) -> Dict[str, Any]:
        return await self._call("getChat", {"chat_id": self.chat_id})

    # ----- helpers used by the control panel
    async def get_updates(self, offset: int, timeout: int = 10) -> list:
        return await self._call(
            "getUpdates", {"offset": offset, "timeout": timeout, "allowed_updates": ["message", "callback_query"]}
        ) or []

    async def send_to(self, chat_id: Any, text: str, reply_markup: Optional[dict] = None) -> int:
        payload: Dict[str, Any] = {
            "chat_id": chat_id, "text": text, "parse_mode": "HTML", "link_preview_options": {"is_disabled": True},
        }
        if reply_markup:
            payload["reply_markup"] = reply_markup
        return int((await self._call("sendMessage", payload))["message_id"])

    async def edit_message(self, chat_id: Any, message_id: int, text: str, reply_markup: Optional[dict] = None) -> None:
        payload: Dict[str, Any] = {
            "chat_id": chat_id, "message_id": message_id, "text": text, "parse_mode": "HTML",
            "link_preview_options": {"is_disabled": True},
        }
        if reply_markup:
            payload["reply_markup"] = reply_markup
        try:
            await self._call("editMessageText", payload)
        except BotApiError as exc:
            if "not modified" not in str(exc).lower():
                raise

    async def answer_callback(self, callback_id: str, text: Optional[str] = None, alert: bool = False) -> None:
        payload: Dict[str, Any] = {"callback_query_id": callback_id}
        if text:
            payload["text"] = text
            payload["show_alert"] = alert
        try:
            await self._call("answerCallbackQuery", payload)
        except BotApiError as exc:
            log_bot_api.debug("answerCallbackQuery failed: %s", exc)

    async def call(self, method: str, payload: Optional[Dict[str, Any]] = None) -> Any:
        return await self._call(method, payload or {})

    async def send_message(self, text: str, *, link_preview_url: Optional[str] = None) -> int:
        """Send an HTML message; returns the message id.

        ``link_preview_url`` shows a preview card for that URL (the t.me/nft
        link renders the gift artwork); None disables previews.
        """
        payload: Dict[str, Any] = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": "HTML",
            "link_preview_options": (
                {"url": link_preview_url, "prefer_small_media": False} if link_preview_url else {"is_disabled": True}
            ),
        }
        if self.thread_id:
            payload["message_thread_id"] = self.thread_id
        try:
            result = await self._call("sendMessage", payload)
        except BotApiError as exc:
            if exc.status == 400 and "parse entities" in str(exc).lower():
                log_bot_api.error("Telegram could not parse the HTML alert; resending as plain text")
                payload.pop("parse_mode")
                payload["text"] = html_to_plain(text)
                result = await self._call("sendMessage", payload)
            else:
                raise
        return int(result["message_id"])


# ==========================================================================
# stats
# ==========================================================================
"""Tiny counters object logged periodically (visible in Railway logs)."""

import time
from dataclasses import dataclass, field


@dataclass
class Stats:
    started: float = field(default_factory=time.time)
    probes: int = 0
    probe_hits: int = 0
    probe_misses: int = 0
    probe_errors: int = 0
    flood_waits: int = 0
    detections: int = 0
    alerts_sent: int = 0
    alerts_failed: int = 0
    catalog_checks: int = 0
    reconnects: int = 0
    last_probe_ok: float = 0.0
    last_alert_ok: float = 0.0
    _snap_probes: int = 0
    _snap_time: float = field(default_factory=time.time)

    def rps_since_last(self) -> float:
        now = time.time()
        dt = max(0.001, now - self._snap_time)
        rps = (self.probes - self._snap_probes) / dt
        self._snap_probes, self._snap_time = self.probes, now
        return rps


# ==========================================================================
# alerts
# ==========================================================================
"""Alert pipeline: outbox -> (optional floor lookup) -> batching -> Bot API.

Guarantees
* An alert is first written to the SQLite outbox (``alerts`` table, in the same
  transaction that recorded the detection), so nothing is lost or duplicated if
  the process restarts between detection and delivery.
* Items are sent in detection order.  When many pile up (Telegram allows ~20
  messages/minute per group) they are merged into batches of up to
  ``ALERT_BATCH_MAX`` gifts per message.
* Floor-price lookup can never delay an alert longer than FLOOR_TIMEOUT_SECONDS.
* Delivery is at-least-once: a crash in the instant between Telegram accepting
  a message and the database being updated can re-send that one alert after a
  restart.  The window is milliseconds wide and documented in the README.
"""

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple


log_alerts = logging.getLogger("giftparser.alerts")

MAX_SEND_ATTEMPTS = 4
MAX_MESSAGE_CHARS = 3800
SEPARATOR = "\n\n──────────\n\n"


@dataclass
class AlertItem:
    key: str
    kind: str  # 'upgrade' | 'catalog'
    gift: Optional[UniqueGift] = None
    catalog: Optional[CatalogGift] = None
    catalog_event: str = "new"
    created: float = 0.0
    attempts: int = 0
    test: bool = False

    @classmethod
    def from_row(cls, row: Dict[str, Any]) -> "AlertItem":
        import json

        payload = json.loads(row["payload"])
        if row["kind"] == "upgrade":
            return cls(key=row["key"], kind="upgrade", gift=UniqueGift.from_dict(payload),
                       created=row["created_at"], attempts=row["attempts"])
        return cls(
            key=row["key"],
            kind="catalog",
            catalog=CatalogGift.from_dict(payload["gift"]),
            catalog_event=payload.get("event", "new"),
            created=row["created_at"],
            attempts=row["attempts"],
        )


class FloorProvider:
    """Floor prices from Telegram's own resale market, cached and time-boxed."""

    def __init__(self, source: GiftSource, settings: Settings, runtime: Any = None) -> None:
        self._source = source
        self._settings_enabled = settings.floor_enabled
        self._runtime = runtime
        self._ttl = settings.floor_cache_seconds
        self._timeout = settings.floor_timeout
        self._cache: Dict[Tuple[int, Optional[int]], Tuple[float, Floor]] = {}

    async def get(self, gift: UniqueGift) -> Optional[Floor]:
        enabled = self._runtime.floor_enabled if self._runtime is not None else self._settings_enabled
        if not enabled or gift.gift_id is None:
            return None
        key = (gift.gift_id, gift.model_document_id)
        hit = self._cache.get(key)
        now = time.monotonic()
        if hit and now - hit[0] < self._ttl:
            return hit[1]
        try:
            floor = await asyncio.wait_for(self._source.get_floor(gift.gift_id, gift.model_document_id), self._timeout)
        except asyncio.TimeoutError:
            log_alerts.info("floor lookup for %s timed out after %.1fs; sending alert without it", gift.slug, self._timeout)
            return hit[1] if hit else None
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log_alerts.warning("floor lookup for %s failed: %s", gift.slug, exc)
            return hit[1] if hit else None
        self._cache[key] = (now, floor)
        if len(self._cache) > 500:
            self._cache.pop(next(iter(self._cache)))
        return floor


class AlertDispatcher:
    def __init__(
        self,
        settings: Settings,
        db: Database,
        bot: Optional[BotApiClient],
        floors: Optional[FloorProvider],
        stats: Stats,
    ) -> None:
        self.s = settings
        self.db = db
        self.bot = bot
        self.floors = floors
        self.stats = stats
        self._queue: "asyncio.Queue[AlertItem]" = asyncio.Queue()
        self._stop = asyncio.Event()
        self._inflight = 0

    # -------------------------------------------------------------- intake
    async def load_pending(self) -> int:
        rows, expired = await self.db.load_pending_alerts(self.s.pending_max_age)
        if expired:
            log_alerts.warning("Dropped %d unsent alert(s) older than %ds from before the restart", expired, int(self.s.pending_max_age))
        for row in rows:
            self._queue.put_nowait(AlertItem.from_row(row))
        if rows:
            log_alerts.info("Re-queued %d unsent alert(s) from before the restart", len(rows))
        return len(rows)

    async def enqueue_upgrade(self, gift: UniqueGift) -> None:
        """Called after ``record_detection`` already stored the outbox row."""
        self._queue.put_nowait(AlertItem(key=gift.slug, kind="upgrade", gift=gift, created=time.time()))

    async def enqueue_catalog(self, gift: CatalogGift, event: str) -> bool:
        key = f"catalog:{event}:{gift.gift_id}"
        ok = await self.db.enqueue_alert(key, "catalog", {"gift": gift.to_dict(), "event": event})
        if ok:
            self._queue.put_nowait(AlertItem(key=key, kind="catalog", catalog=gift, catalog_event=event, created=time.time()))
        return ok

    @property
    def backlog(self) -> int:
        return self._queue.qsize() + self._inflight

    # ---------------------------------------------------------- formatting
    async def render(self, item: AlertItem) -> str:
        if item.kind == "upgrade" and item.gift is not None:
            floor = await self.floors.get(item.gift) if self.floors else None
            return format_upgrade_alert(item.gift, floor, test=item.test)
        if item.catalog is not None:
            return format_catalog_alert(item.catalog, item.catalog_event, test=item.test)
        raise ValueError(f"malformed alert {item.key}")

    # ----------------------------------------------------------------- loop
    async def run(self) -> None:
        log_alerts.info("Alert dispatcher started (batch max %d, dry_run=%s)", self.s.alert_batch_max, self.s.dry_run)
        while True:
            if self._stop.is_set() and self._queue.empty():
                return
            try:
                first = await asyncio.wait_for(self._queue.get(), timeout=0.5)
            except asyncio.TimeoutError:
                continue
            batch = [first]
            while len(batch) < self.s.alert_batch_max:
                try:
                    batch.append(self._queue.get_nowait())
                except asyncio.QueueEmpty:
                    break
            self._inflight = len(batch)
            try:
                await self._deliver(batch)
            finally:
                self._inflight = 0

    async def _deliver(self, batch: List[AlertItem]) -> None:
        texts: List[Tuple[AlertItem, str]] = []
        for item in batch:
            try:
                texts.append((item, await self.render(item)))
            except Exception:
                log_alerts.exception("Could not render alert %s; marking failed", item.key)
                await self.db.mark_alert(item.key, "failed", "render error", add_attempt=True)
                self.stats.alerts_failed += 1

        # split into messages that fit Telegram's size limit
        messages: List[List[Tuple[AlertItem, str]]] = []
        current: List[Tuple[AlertItem, str]] = []
        size = 0
        for pair in texts:
            if current and size + len(pair[1]) + len(SEPARATOR) > MAX_MESSAGE_CHARS:
                messages.append(current)
                current, size = [], 0
            current.append(pair)
            size += len(pair[1]) + len(SEPARATOR)
        if current:
            messages.append(current)

        for group in messages:
            await self._send_group(group)

    async def _send_group(self, group: List[Tuple[AlertItem, str]]) -> None:
        text = SEPARATOR.join(t for _, t in group)
        names = ", ".join(self._label(i) for i, _ in group)
        preview = None
        if len(group) == 1 and group[0][0].gift is not None:
            preview = group[0][0].gift.link  # t.me/nft link renders the gift artwork

        if self.s.dry_run or self.bot is None:
            log_alerts.info("[DRY_RUN] would send alert for %s:\n%s", names, text)
            for item, _ in group:
                await self.db.mark_alert(item.key, "dry_run")
            return

        started = time.time()
        try:
            await self.bot.send_message(text, link_preview_url=preview)
        except BotApiAuthError:
            log_alerts.error("Bot token rejected; alerts cannot be delivered. Fix TELEGRAM_BOT_TOKEN.")
            await self._fail_group(group, "bot token rejected", final=True)
            return
        except BotApiError as exc:
            log_alerts.error("Failed to send alert for %s: %s", names, exc)
            await self._fail_group(group, str(exc), final=not exc.retryable)
            return
        except Exception:
            log_alerts.exception("Unexpected error sending alert for %s", names)
            await self._fail_group(group, "unexpected error", final=False)
            return

        for item, _ in group:
            await self.db.mark_alert(item.key, "sent", add_attempt=True)
            self.stats.alerts_sent += 1
        self.stats.last_alert_ok = time.time()
        log_alerts.info("Alert sent for %s (%.2fs)", names, time.time() - started)
        await asyncio.sleep(0.3)  # gentle spacing between consecutive messages

    async def _fail_group(self, group: List[Tuple[AlertItem, str]], error: str, final: bool) -> None:
        for item, _ in group:
            item.attempts += 1
            if final or item.attempts >= MAX_SEND_ATTEMPTS:
                await self.db.mark_alert(item.key, "failed", error, add_attempt=True)
                self.stats.alerts_failed += 1
                log_alerts.error("Giving up on alert %s after %d attempt(s): %s", item.key, item.attempts, error)
            else:
                await self.db.mark_alert(item.key, "pending", error, add_attempt=True)
                asyncio.get_running_loop().call_later(
                    backoff_delay(item.attempts, base=5.0, cap=60.0), self._queue.put_nowait, item
                )

    @staticmethod
    def _label(item: AlertItem) -> str:
        if item.gift is not None:
            return item.gift.slug
        return item.key

    # ------------------------------------------------------------- shutdown
    def request_stop(self) -> None:
        self._stop.set()

    async def drain(self, timeout: float) -> None:
        """Wait up to ``timeout`` seconds for queued alerts to be delivered."""
        deadline = time.monotonic() + timeout
        while self.backlog and time.monotonic() < deadline:
            await asyncio.sleep(0.1)
        if self.backlog:
            log_alerts.warning("%d alert(s) not delivered before shutdown; they stay queued in the database", self.backlog)

    # ---------------------------------------------------------- test alert
    async def send_test_alert(self) -> None:
        """Push a clearly labelled synthetic alert through the *real* pipeline."""

        gift = UniqueGift(
            slug="TestGift-1", title="Test Gift", num=1, gift_id=0, unique_id=0,
            model=Trait("Sample Model", 15), backdrop=Trait("Sample Backdrop", 20), symbol=Trait("Sample Symbol", 30),
            issued=1, total=1000,
        )
        item = AlertItem(key="test", kind="upgrade", gift=gift, test=True, created=time.time())
        text = format_upgrade_alert(gift, None, test=True)  # no invented prices
        catalog_item = AlertItem(
            key="test-catalog", kind="catalog", test=True, created=time.time(), catalog_event="new",
            catalog=CatalogGift(gift_id=0, title="Test Gift", stars=50, upgrade_stars=300, limited=True,
                                availability_total=5000, availability_remains=4000),
        )
        ctext = format_catalog_alert(catalog_item.catalog, "new", test=True)  # type: ignore[arg-type]
        if self.s.dry_run or self.bot is None:
            log_alerts.info("[DRY_RUN] test alerts:\n%s\n\n%s", text, ctext)
            return
        await self.bot.send_message(text, link_preview_url=None)
        await self.bot.send_message(ctext)
        log_alerts.info("Test alerts sent to chat %s", self.s.chat_id)


# ==========================================================================
# detector
# ==========================================================================
"""Upgrade detector: finds newly upgraded gifts by sequential slug probing.

How detection works (verified against core.telegram.org, see README)
--------------------------------------------------------------------
Telegram has NO push update and NO Bot API method announcing that *someone
else* upgraded a gift.  What it does provide is ``payments.getUniqueStarGift``,
which resolves any collectible by its public slug ``<Collection>-<number>``
(the same data as https://t.me/nft/<slug>).  Numbers are assigned
sequentially when a gift is upgraded, and every result carries
``availability_issued`` (how many of that type have been upgraded).  So the
next upgrade of a collection whose highest known number is N appears as
``<Collection>-(N+1)``; we ask for exactly that slug and an error
(STARGIFT_SLUG_INVALID) means "not upgraded yet".

Latency is therefore the poll interval of the collection.  To keep it low
without hammering Telegram, each collection is polled at an adaptive rate:

    hot   (upgrade seen in the last HOT_WINDOW)     every HOT_INTERVAL
    warm  (upgrade seen in the last WARM_WINDOW)    every WARM_INTERVAL
    cold  (otherwise)                               every COLD_INTERVAL

and all probes share one global rate limiter (MAX_REQUESTS_PER_SECOND) that
backs off automatically on FLOOD_WAIT.
"""

import asyncio
import logging
import random
import time
from dataclasses import dataclass
from typing import Awaitable, Callable, Dict, List, Optional


log_detector = logging.getLogger("giftparser.detector")

BURST = 6  # how many known-to-exist numbers are fetched concurrently when catching up
GAP_RETRIES = 3  # polls to wait for a number that Telegram says should exist
LOOKAHEAD = 8  # numbers examined past a suspected gap
RESOLVE_RETRY_SECONDS = 1800.0  # re-try verifying an unverified slug prefix this often


@dataclass
class CollectionState:
    key: str
    title: str
    prefix: str
    gift_id: Optional[int] = None
    verified: bool = False
    bootstrapped: bool = False
    last_num: int = 0
    known_issued: int = 0
    last_found_at: Optional[float] = None
    gap_misses: int = 0
    last_resolve_attempt: float = 0.0

    @classmethod
    def from_row(cls, row: dict) -> "CollectionState":
        return cls(
            key=row["key"],
            title=row["title"],
            prefix=row["slug_prefix"],
            gift_id=row["gift_id"],
            verified=bool(row["prefix_verified"]),
            bootstrapped=bool(row["bootstrapped"]),
            last_num=row["last_num"],
            known_issued=row["known_issued"],
            last_found_at=row["last_found_at"],
        )


AlertCallback = Callable[[UniqueGift], Awaitable[None]]


class UpgradeDetector:
    def __init__(
        self,
        settings: Settings,
        source: GiftSource,
        db: Database,
        gift_filter: GiftFilter,
        limiter: RateLimiter,
        stats: Stats,
        on_alert: AlertCallback,
    ) -> None:
        self.s = settings
        self.source = source
        self.db = db
        self.filter = gift_filter
        self.limiter = limiter
        self.stats = stats
        self.on_alert = on_alert
        self.collections: Dict[str, CollectionState] = {}
        self._tasks: Dict[str, asyncio.Task] = {}
        self._stop = asyncio.Event()
        self._running = False
        self.fatal: Optional[BaseException] = None

    # ------------------------------------------------------------ registry
    async def load_from_db(self) -> int:
        n = 0
        for row in await self.db.load_collections():
            st = CollectionState.from_row(row)
            if not self.filter.collection_allowed(st.title, st.prefix, st.key):
                continue
            override = self.s.slug_prefix_overrides.get(normalize_name(st.title))
            if override and override != st.prefix:
                log_detector.warning("Collection %s: applying SLUG_PREFIX_OVERRIDES %r -> %r (re-baselining)", st.title, st.prefix, override)
                st.prefix, st.verified, st.bootstrapped, st.last_num, st.known_issued = override, False, False, 0, 0
                await self.db.update_collection(
                    st.key, slug_prefix=override, prefix_verified=0, bootstrapped=0, last_num=0, known_issued=0
                )
            self.collections[st.key] = st
            n += 1
        return n

    async def add_collection(self, title: str, gift_id: Optional[int] = None, prefix: Optional[str] = None) -> Optional[CollectionState]:
        """Register (idempotently) a collection to watch."""
        key = normalize_name(prefix or title)
        if not key:
            return None
        if not self.filter.collection_allowed(title, prefix or "", key):
            return None
        existing = self.collections.get(key)
        if existing is not None:
            if gift_id and not existing.gift_id:
                existing.gift_id = gift_id
                await self.db.update_collection(key, gift_id=gift_id)
            return existing
        guess = self.s.slug_prefix_overrides.get(normalize_name(title)) or prefix or derive_slug_prefix(title)
        await self.db.upsert_collection(key, title, guess, gift_id)
        row = next(r for r in await self.db.load_collections() if r["key"] == key)
        st = CollectionState.from_row(row)
        if st.prefix != guess and not st.verified:
            st.prefix = guess
        self.collections[key] = st
        log_detector.info("Tracking new collection %r (slug prefix guess %r, gift id %s)", title, st.prefix, gift_id)
        if self._running and key not in self._tasks:
            self._spawn(st)
        return st

    # ------------------------------------------------------------- running
    def _spawn(self, st: CollectionState) -> None:
        self._tasks[st.key] = asyncio.create_task(self._collection_loop(st), name=f"collection:{st.key}")

    async def run(self) -> None:
        self._running = True
        for st in list(self.collections.values()):
            self._spawn(st)
        log_detector.info("Upgrade detector running for %d collection(s)", len(self.collections))
        await self._stop.wait()
        for t in self._tasks.values():
            t.cancel()
        await asyncio.gather(*self._tasks.values(), return_exceptions=True)
        self._tasks.clear()
        self._running = False

    def request_stop(self) -> None:
        self._stop.set()

    async def _sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)

    def interval_for(self, st: CollectionState, now: Optional[float] = None) -> float:
        now = time.time() if now is None else now
        if not st.bootstrapped or not st.verified or st.last_found_at is None:
            base = self.s.cold_interval
        else:
            age = now - st.last_found_at
            if age <= self.s.hot_window:
                base = self.s.hot_interval
            elif age <= self.s.warm_window:
                base = self.s.warm_interval
            else:
                base = self.s.cold_interval
        return base * random.uniform(0.9, 1.1)

    async def _collection_loop(self, st: CollectionState) -> None:
        # spread the first probes of many collections over one cold interval
        await self._sleep(random.uniform(0, min(self.s.cold_interval, 10.0)))
        failures = 0
        while not self._stop.is_set():
            if not self.filter.should_poll(st.title, st.prefix, st.key):
                # No enabled panel rule needs this collection: stop probing it. It re-baselines
                # silently when a rule wants it again, so old gifts never alert.
                if st.bootstrapped:
                    st.bootstrapped = False
                    await self.db.update_collection(st.key, bootstrapped=0)
                await self._sleep(3.0)
                continue
            try:
                if not st.bootstrapped:
                    await self.bootstrap(st)
                else:
                    if not st.verified:
                        await self._maybe_resolve_prefix(st)
                    await self.poll_once(st)
                failures = 0
            except asyncio.CancelledError:
                raise
            except SessionInvalidError as exc:
                self.fatal = exc
                self._stop.set()
                log_detector.critical("%s", exc)
                return
            except Exception as exc:
                failures += 1
                self.stats.probe_errors += 1
                log_detector.warning("Collection %s: %s: %s (failure #%d)", st.title, type(exc).__name__, exc, failures)
                await self._sleep(backoff_delay(failures, base=2.0, cap=60.0))
                continue
            await self._sleep(self.interval_for(st))

    # --------------------------------------------------------------- probing
    async def _fetch(self, prefix: str, num: int) -> Optional[UniqueGift]:
        """One rate-limited, retried lookup of ``<prefix>-<num>``."""
        slug = f"{prefix}-{num}"
        attempt = 0
        while True:
            await self.limiter.acquire()
            try:
                gift = await self.source.get_unique(slug)
            except RateLimitedError as exc:
                self.stats.flood_waits += 1
                self.limiter.penalize(exc.seconds)
                log_detector.warning("FLOOD_WAIT %.0fs on %s: pausing all probes, rate now %.1f/s", exc.seconds, slug, self.limiter.rate)
                continue
            except TransientSourceError as exc:
                attempt += 1
                self.stats.probe_errors += 1
                if attempt == 1 or attempt % 10 == 0:
                    log_detector.warning("Network problem probing %s (%s); retrying with backoff (attempt %d)", slug, exc, attempt)
                await self._sleep(backoff_delay(attempt, base=1.0, cap=30.0))
                continue
            self.stats.probes += 1
            self.stats.last_probe_ok = time.time()
            if gift is None:
                self.stats.probe_misses += 1
            else:
                self.stats.probe_hits += 1
            return gift

    async def _fetch_window(self, st: CollectionState, start: int) -> List[UniqueGift]:
        """Fetch ``start`` (and, when Telegram says more exist, a burst after it).

        Returns the consecutive run of existing gifts beginning at ``start``.
        """
        known_more = st.known_issued - start + 1  # numbers we believe exist from `start`
        count = max(1, min(BURST, known_more))
        results = await asyncio.gather(*(self._fetch(st.prefix, start + i) for i in range(count)))
        out: List[UniqueGift] = []
        for g in results:
            if g is None:
                break
            out.append(g)
        return out

    async def _accept(self, st: CollectionState, num: int, gift: UniqueGift) -> None:
        passes, reason = self.filter.evaluate(gift)
        is_new = await self.db.record_detection(gift, passes, reason)
        st.last_num = max(st.last_num, num)
        st.known_issued = max(st.known_issued, gift.issued or 0)
        st.last_found_at = time.time()
        st.gap_misses = 0
        if gift.gift_id and not st.gift_id:
            st.gift_id = gift.gift_id
        newly_verified = not st.verified
        st.verified = True
        await self.db.update_collection(
            st.key, last_num=st.last_num, known_issued=st.known_issued, last_found_at=st.last_found_at,
            prefix_verified=1, gift_id=st.gift_id,
        )
        if newly_verified:
            log_detector.info("Collection %s: slug prefix %r confirmed by Telegram", st.title, st.prefix)
        if not is_new:
            log_detector.debug("Already seen %s; not alerting again", gift.slug)
            return
        self.stats.detections += 1
        try:  # feeds the panel's pick-lists; must never break detection
            await self.db.note_traits(
                st.key, gift.model.name if gift.model else None,
                gift.symbol.name if gift.symbol else None, gift.backdrop.name if gift.backdrop else None,
            )
        except Exception as exc:  # pragma: no cover
            log_detector.debug("could not record traits: %s", exc)
        traits = " / ".join(
            f"{k}={t.name}" for k, t in gift.traits().items() if t is not None and t.name
        )
        if passes and self.s.alert_upgrades:
            log_detector.info("DETECTED upgrade %s (%s) -> alerting", gift.slug, traits)
            await self.on_alert(gift)
        else:
            log_detector.info("DETECTED upgrade %s (%s) -> not alerting (%s)", gift.slug, traits, reason or "ALERT_UPGRADES=false")

    async def poll_once(self, st: CollectionState) -> int:
        """Check for new upgrades once; returns the number of new gifts found."""
        found = 0
        while not self._stop.is_set():
            nxt = st.last_num + 1
            gifts = await self._fetch_window(st, nxt)
            if not gifts:
                await self._handle_missing(st, nxt)
                break
            st.known_issued = max(st.known_issued, max((g.issued or 0) for g in gifts))
            cap = self.s.catchup_max_alerts
            if st.known_issued - st.last_num > cap:
                new_last = st.known_issued - cap
                log_detector.warning(
                    "Collection %s: %d upgrade(s) happened while not watching; skipping to #%d "
                    "(alerting only the most recent %d, CATCHUP_MAX_ALERTS)",
                    st.title, new_last - st.last_num, new_last, cap,
                )
                st.last_num = new_last
                await self.db.update_collection(st.key, last_num=new_last, known_issued=st.known_issued)
                continue
            for i, g in enumerate(gifts):
                await self._accept(st, nxt + i, g)
                found += 1
            # Telegram says nothing newer exists: wait for the next scheduled poll
            if st.known_issued <= st.last_num:
                break
        return found

    async def _handle_missing(self, st: CollectionState, nxt: int) -> None:
        """``<prefix>-nxt`` does not exist. Detect numbering gaps."""
        if not st.known_issued or st.known_issued < nxt:
            st.gap_misses = 0
            return
        st.gap_misses += 1
        if st.gap_misses < GAP_RETRIES:
            return
        ahead = await asyncio.gather(*(self._fetch(st.prefix, nxt + i) for i in range(1, LOOKAHEAD + 1)))
        for offset, g in enumerate(ahead, start=1):
            if g is not None:
                log_detector.warning(
                    "Collection %s: #%d..#%d cannot be resolved although Telegram counts %d upgrades; skipping gap",
                    st.title, nxt, nxt + offset - 1, st.known_issued,
                )
                st.last_num = nxt + offset - 1
                st.gap_misses = 0
                await self.db.update_collection(st.key, last_num=st.last_num)
                return
        log_detector.warning("Collection %s: expected #%d (issued=%d) but it is not resolvable; resetting expectation", st.title, nxt, st.known_issued)
        st.known_issued = st.last_num
        st.gap_misses = 0
        await self.db.update_collection(st.key, known_issued=st.known_issued)

    # -------------------------------------------------------------- bootstrap
    async def _resale_sample(self, st: CollectionState) -> Optional[UniqueGift]:
        st.last_resolve_attempt = time.time()
        if st.gift_id is None:
            return None
        await self.limiter.acquire()
        try:
            return await self.source.find_resale_sample(st.gift_id)
        except RateLimitedError as exc:
            self.limiter.penalize(exc.seconds)
        except SourceError as exc:
            log_detector.debug("resale lookup for %s failed: %s", st.title, exc)
        return None

    async def _maybe_resolve_prefix(self, st: CollectionState) -> None:
        """Periodically retry verifying an unverified prefix using the resale market."""
        if st.gift_id is None or time.time() - st.last_resolve_attempt < RESOLVE_RETRY_SECONDS:
            return
        sample = await self._resale_sample(st)
        if sample is None:
            return
        log_detector.warning(
            "Collection %s: Telegram's resale market shows upgrades (e.g. %s) while prefix %r was unverified; re-baselining with prefix %r",
            st.title, sample.slug, st.prefix, sample.prefix,
        )
        st.prefix, st.bootstrapped = sample.prefix, False
        await self.db.update_collection(st.key, slug_prefix=sample.prefix, bootstrapped=0)

    async def bootstrap(self, st: CollectionState) -> None:
        """Take a *silent* baseline so that old gifts never trigger alerts.

        1. Resolve gift #1 of the collection with the guessed prefix.  If it is
           missing, ask Telegram's resale market for a real collectible of this
           type: that reveals the true slug prefix and the current counters.
        2. ``availability_issued`` says how many already exist; probe forward
           from there until a number is missing.
        """
        first = await self._fetch(st.prefix, 1)
        anchor = first
        if first is None:
            sample = await self._resale_sample(st)
            if sample is not None:
                if sample.prefix != st.prefix:
                    log_detector.warning("Collection %s: slug prefix corrected %r -> %r using Telegram's resale listing", st.title, st.prefix, sample.prefix)
                    st.prefix = sample.prefix
                    await self.db.update_collection(st.key, slug_prefix=sample.prefix)
                anchor = await self._fetch(st.prefix, 1) or sample
        if anchor is None:
            st.last_num = 0
            st.known_issued = 0
            st.verified = False
            st.bootstrapped = True
            await self.db.update_collection(st.key, last_num=0, known_issued=0, bootstrapped=1, prefix_verified=0)
            log_detector.info(
                "Collection %s: no upgrades found under prefix %r yet (new collection, or the prefix needs "
                "SLUG_PREFIX_OVERRIDES). Watching for #1.", st.title, st.prefix,
            )
            return
        if anchor.issued is None:  # e.g. #1 was burned: no counters, so ask the resale market for some
            sample = await self._resale_sample(st)
            if sample is not None and sample.issued:
                anchor = sample
        base = max(anchor.issued or 0, anchor.num, 1)
        if anchor.gift_id and not st.gift_id:
            st.gift_id = anchor.gift_id
        for _ in range(2000):  # safety cap
            nxt = await self._fetch(st.prefix, base + 1)
            if nxt is None:
                break
            base = max(base + 1, nxt.issued or 0)
        st.last_num = base
        st.known_issued = max(base, anchor.issued or 0)
        st.verified = True
        st.bootstrapped = True
        await self.db.update_collection(
            st.key, last_num=base, known_issued=st.known_issued, bootstrapped=1, prefix_verified=1, gift_id=st.gift_id
        )
        log_detector.info("Collection %s: baseline set at #%d (prefix %r verified); only newer upgrades will alert", st.title, base, st.prefix)

    # ------------------------------------------------------------ reporting
    def tier_counts(self) -> Dict[str, int]:
        now = time.time()
        out = {"hot": 0, "warm": 0, "cold": 0, "unbootstrapped": 0, "unverified": 0}
        for st in self.collections.values():
            if not st.bootstrapped:
                out["unbootstrapped"] += 1
                continue
            if not st.verified:
                out["unverified"] += 1
            if st.last_found_at is None:
                out["cold"] += 1
            elif now - st.last_found_at <= self.s.hot_window:
                out["hot"] += 1
            elif now - st.last_found_at <= self.s.warm_window:
                out["warm"] += 1
            else:
                out["cold"] += 1
        return out


# ==========================================================================
# catalog
# ==========================================================================
"""Catalog watcher: new gift types and gifts that become upgradable.

Uses ``payments.getStarGifts`` with Telegram's own ``hash`` so an unchanged
catalog costs one tiny "not modified" response.  The very first run only
records a silent baseline (no alert flood); afterwards a gift type that was
never seen before, or one that gains an upgrade price (``upgrade_stars``),
raises one alert, de-duplicated through the database.
"""

import asyncio
import logging
import random
from typing import Awaitable, Callable, Optional


log_catalog = logging.getLogger("giftparser.catalog")

BASELINE_KEY = "catalog_baselined"


class CatalogWatcher:
    def __init__(
        self,
        settings: Settings,
        source: GiftSource,
        db: Database,
        detector: UpgradeDetector,
        dispatcher: AlertDispatcher,
        gift_filter: GiftFilter,
        limiter: RateLimiter,
        stats: Stats,
    ) -> None:
        self.s = settings
        self.source = source
        self.db = db
        self.detector = detector
        self.dispatcher = dispatcher
        self.filter = gift_filter
        self.limiter = limiter
        self.stats = stats
        self._stop = asyncio.Event()
        self.fatal: Optional[BaseException] = None

    def request_stop(self) -> None:
        self._stop.set()

    async def register_extra_collections(self) -> None:
        for name in self.s.extra_collections:
            await self.detector.add_collection(name, prefix=name)

    async def poll_once(self) -> int:
        """Returns the number of catalog alerts queued."""
        await self.limiter.acquire()
        gifts = await self.source.get_catalog()
        self.stats.catalog_checks += 1
        if gifts is None:
            return 0
        baselined = await self.db.get_meta(BASELINE_KEY) == "1"
        known = await self.db.load_catalog()
        alerts = 0
        new_types = 0
        for g in gifts:
            prev = known.get(g.gift_id)
            event: Optional[str] = None
            if prev is None:
                event = "new"
                new_types += 1
            elif prev["upgrade_stars"] is None and g.upgrade_stars is not None:
                event = "upgradable"
            await self.db.upsert_catalog(g)

            title = g.title
            # Only gift types that can have collectibles are probed: upgradable now, sold out
            # (older types keep being upgraded), or already on the resale market.
            if title and (g.upgradable or g.availability_resale or g.sold_out):
                await self.detector.add_collection(title, gift_id=g.gift_id)

            if event and baselined and self.filter.alert_new_gifts and not self.filter.paused and self._wanted(g):
                if await self.dispatcher.enqueue_catalog(g, event):
                    alerts += 1
                    log_catalog.info("Catalog event %r for %s (id %d)", event, g.title, g.gift_id)
        if not baselined:
            await self.db.set_meta(BASELINE_KEY, "1")
            log_catalog.info("Catalog baseline recorded: %d gift type(s). Only changes from now on will alert.", len(gifts))
        elif new_types:
            log_catalog.info("Catalog changed: %d new gift type(s)", new_types)
        return alerts

    def _wanted(self, gift: CatalogGift) -> bool:
        return self.filter.collection_allowed(gift.title or "")

    async def run(self) -> None:
        log_catalog.info("Catalog watcher running (every %.0fs)", self.s.catalog_poll_interval)
        failures = 0
        while not self._stop.is_set():
            try:
                await self.poll_once()
                failures = 0
                delay = self.s.catalog_poll_interval * random.uniform(0.9, 1.1)
            except asyncio.CancelledError:
                raise
            except SessionInvalidError as exc:
                self.fatal = exc
                log_catalog.critical("%s", exc)
                return
            except RateLimitedError as exc:
                self.stats.flood_waits += 1
                self.limiter.penalize(exc.seconds)
                log_catalog.warning("FLOOD_WAIT %.0fs while reading the gift catalog", exc.seconds)
                delay = exc.seconds
            except Exception as exc:
                failures += 1
                log_catalog.warning("Catalog check failed: %s: %s (failure #%d)", type(exc).__name__, exc, failures)
                delay = backoff_delay(failures, base=3.0, cap=120.0)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass


# ==========================================================================
# panel
# ==========================================================================
"""Telegram control panel: change what you get alerts for, from the bot chat.

Open your bot and send /settings.  Everything is done with buttons (plus an
optional "type it" mode), and is stored in SQLite so it survives redeploys.

How it receives your taps: the Bot API's getUpdates long-polling, so no web
server or webhook is needed.  Only the Telegram user IDs in ADMIN_IDS may use
it; everyone else is ignored.

Model: a *rule* = collections + models + backdrops + symbols + rarity limit +
number range.  A gift alerts if any enabled rule matches; with no rules every
upgrade alerts.
"""

import asyncio
import logging
import re
import time
from typing import Any, Dict, List, Optional, Tuple


log_panel = logging.getLogger("giftparser.panel")

PAGE = 8
# field code -> (label, Rule attribute, trait kind in seen_traits or None)
FIELDS: Dict[str, Tuple[str, str, Optional[str]]] = {
    "c": ("Collections", "collections", None),
    "m": ("Models", "models", "model"),
    "b": ("Backdrops", "backdrops", "backdrop"),
    "s": ("Symbols", "symbols", "symbol"),
    "r": ("Rarity", "max_permille", None),
    "n": ("Min number", "min_num", None),
    "x": ("Max number", "max_num", None),
}
LIST_CODES = ("c", "m", "b", "s")
RARITY_PRESETS = (5, 10, 20, 50, 100)  # per-mille => 0.5%, 1%, 2%, 5%, 10%

View = Tuple[str, dict]


def kb(rows: List[List[Tuple[str, str]]]) -> dict:
    return {"inline_keyboard": [[{"text": t, "callback_data": d} for t, d in row if len(d.encode()) <= 64] for row in rows]}


def onoff(value: bool) -> str:
    return "ON" if value else "OFF"


class ControlPanel:
    def __init__(
        self,
        settings: Settings,
        bot: BotApiClient,
        db: Database,
        runtime: Runtime,
        detector: UpgradeDetector,
        stats: Stats,
    ) -> None:
        self.s = settings
        self.bot = bot
        self.db = db
        self.rt = runtime
        self.detector = detector
        self.stats = stats
        self.admins = set(settings.admin_ids)
        self._awaiting: Dict[int, Tuple[int, str]] = {}
        self._offset = 0
        self._stop = asyncio.Event()

    def request_stop(self) -> None:
        self._stop.set()

    # --------------------------------------------------------------- polling
    async def run(self) -> None:
        if not self.admins:
            log_panel.warning("Control panel disabled: no admin. Set ADMIN_IDS (your Telegram user id).")
            return
        log_panel.info("Control panel running. Admins: %d. Send /settings to the bot.", len(self.admins))
        await self._setup()
        failures = 0
        while not self._stop.is_set():
            try:
                updates = await self.bot.get_updates(self._offset, timeout=10)
                failures = 0
            except BotApiAuthError:
                raise
            except BotApiError as exc:
                failures += 1
                if failures == 1 or failures % 10 == 0:
                    log_panel.warning("Panel could not fetch updates: %s (another copy of the bot running? attempt %d)", exc, failures)
                await asyncio.sleep(min(30, 2 * failures))
                continue
            for update in updates:
                if not isinstance(update, dict):
                    continue
                self._offset = max(self._offset, int(update.get("update_id", 0)) + 1)
                try:
                    await self.handle_update(update)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log_panel.exception("Panel failed to handle an update")

    async def _setup(self) -> None:
        for method, payload in (
            ("deleteWebhook", {"drop_pending_updates": False}),  # a webhook would block getUpdates
            ("setMyCommands", {"commands": [
                {"command": "settings", "description": "Open the alert settings panel"},
                {"command": "status", "description": "Show bot status"},
            ]}),
        ):
            try:
                await self.bot.call(method, payload)
            except BotApiError as exc:
                log_panel.debug("%s failed: %s", method, exc)

    # --------------------------------------------------------------- updates
    async def handle_update(self, update: Dict[str, Any]) -> None:
        if update.get("callback_query"):
            await self._on_callback(update["callback_query"])
        elif update.get("message"):
            await self._on_message(update["message"])

    async def _on_message(self, msg: Dict[str, Any]) -> None:
        uid = int((msg.get("from") or {}).get("id", 0))
        chat_id = msg["chat"]["id"]
        text = (msg.get("text") or "").strip()
        if uid not in self.admins:
            if text.startswith("/start"):
                await self.bot.send_to(chat_id, "This is a private gift-alert bot. Its settings are restricted to its owner.")
            return
        command = text.split()[0].split("@")[0].lower() if text.startswith("/") else ""
        if command == "/cancel":
            self._awaiting.pop(uid, None)
            await self.bot.send_to(chat_id, "Cancelled.")
        elif command in ("/start", "/settings", "/panel", "/menu"):
            self._awaiting.pop(uid, None)
            await self._send_view(chat_id, await self.view_home())
        elif command == "/status":
            await self._send_view(chat_id, await self.view_status())
        elif command == "/rules":
            await self._send_view(chat_id, await self.view_rules())
        elif command:
            await self.bot.send_to(chat_id, "Unknown command. Try /settings.")
        elif uid in self._awaiting and text:
            await self._apply_text(uid, chat_id, text)
        else:
            await self.bot.send_to(chat_id, "Send /settings to open the panel.")

    async def _send_view(self, chat_id: Any, view: View) -> None:
        await self.bot.send_to(chat_id, view[0], view[1])

    async def _on_callback(self, cb: Dict[str, Any]) -> None:
        uid = int((cb.get("from") or {}).get("id", 0))
        cb_id = cb["id"]
        if uid not in self.admins:
            await self.bot.answer_callback(cb_id, "Not allowed", alert=True)
            return
        message = cb.get("message") or {}
        chat_id = (message.get("chat") or {}).get("id")
        message_id = message.get("message_id")
        data = cb.get("data") or ""
        if chat_id is None or message_id is None:
            await self.bot.answer_callback(cb_id)
            return
        try:
            view, toast = await self._route(uid, data)
        except Exception:
            log_panel.exception("Panel action %r failed", data)
            await self.bot.answer_callback(cb_id, "Something went wrong, see logs", alert=True)
            return
        await self.bot.answer_callback(cb_id, toast)
        if view is not None:
            await self.bot.edit_message(chat_id, message_id, view[0], view[1])

    # --------------------------------------------------------------- routing
    async def _route(self, uid: int, data: str) -> Tuple[Optional[View], Optional[str]]:
        parts = data.split(":", 4)
        action = parts[0]
        if action != "rx":
            self._awaiting.pop(uid, None)

        if action in ("home", "noop"):
            return (await self.view_home(), None) if action == "home" else (None, None)
        if action == "status":
            return await self.view_status(), None
        if action == "rules":
            return await self.view_rules(), None
        if action == "flag":  # flag:<name>
            name = parts[1]
            await self.rt.set_flag(name, not getattr(self.rt, name))
            return await self.view_home(), f"{name.replace('_', ' ')}: {onoff(getattr(self.rt, name))}"
        if action == "rnew":
            rule = await self.rt.create_rule()
            return await self.view_rule(rule.id), f"Rule #{rule.id} created"

        rid = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
        rule = self.rt.rules.get(rid)
        if rule is None:
            return await self.view_rules(), "That rule no longer exists"

        if action == "r":
            return await self.view_rule(rid), None
        if action == "rt":
            rule.enabled = not rule.enabled
            await self.rt.save_rule(rule)
            return await self.view_rule(rid), f"Rule {onoff(rule.enabled)}"
        if action == "rd":
            return (f"Delete <b>rule #{rid}</b>?\n{esc(rule.short())}",
                    kb([[("🗑 Yes, delete", f"rdc:{rid}"), ("Cancel", f"r:{rid}")]])), None
        if action == "rdc":
            await self.rt.delete_rule(rid)
            return await self.view_rules(), "Deleted"

        code = parts[2] if len(parts) > 2 else ""
        if code not in FIELDS:
            return await self.view_rule(rid), None
        attr = FIELDS[code][1]
        if action == "rf":
            return await self.view_field(rid, code), None
        if action == "rp":
            return await self.view_pick(rid, code, int(parts[3]) if len(parts) > 3 else 0), None
        if action == "ri":  # toggle one item in a list field
            page, name = int(parts[3]), parts[4]
            current: List[str] = list(getattr(rule, attr))
            match = [c for c in current if normalize_name(c) == normalize_name(name)]
            if match:
                current.remove(match[0])
            else:
                current.append(name)
            setattr(rule, attr, current)
            await self.rt.save_rule(rule)
            return await self.view_pick(rid, code, page), None
        if action == "rc":
            setattr(rule, attr, [] if code in LIST_CODES else None)
            await self.rt.save_rule(rule)
            return await self.view_rule(rid), "Cleared"
        if action == "rs":  # rarity preset
            rule.max_permille = int(parts[3])
            await self.rt.save_rule(rule)
            return await self.view_rule(rid), f"Rarity ≤ {rule.max_permille / 10:g}%"
        if action == "rx":  # switch to typing mode
            self._awaiting[uid] = (rid, code)
            hints = {
                "r": "Send the rarity limit in percent, e.g. 2 (= rarest trait at most 2%).",
                "n": "Send the minimum gift number, e.g. 1.",
                "x": "Send the maximum gift number, e.g. 1000.",
            }
            hint = hints.get(code, "Send the names separated by commas, e.g. Onyx Black, Black.")
            return (f"⌨️ <b>{FIELDS[code][0]}</b> for rule #{rid}\n{hint}\n\nSend /cancel to stop.",
                    kb([[("⬅ Back", f"rf:{rid}:{code}")]])), None
        return await self.view_rule(rid), None

    # ----------------------------------------------------------------- typing
    async def _apply_text(self, uid: int, chat_id: Any, text: str) -> None:
        rid, code = self._awaiting.pop(uid)
        rule = self.rt.rules.get(rid)
        if rule is None:
            await self.bot.send_to(chat_id, "That rule no longer exists. Send /settings.")
            return
        attr = FIELDS[code][1]
        note = ""
        if code in LIST_CODES:
            names = [p.strip() for p in re.split(r"[,\n;]", text) if p.strip()]
            if code == "c":
                known = {normalize_name(st.title): st.title for st in self.detector.collections.values()}
                fixed = [known.get(normalize_name(n), n) for n in names]
                unknown = [n for n in names if normalize_name(n) not in known]
                if unknown:
                    note = "\n⚠️ Not a known collection (saved anyway): " + esc(", ".join(unknown))
                names = fixed
            setattr(rule, attr, names)
        elif code == "r":
            m = re.fullmatch(r"\s*(\d+(?:[.,]\d+)?)\s*%?\s*", text)
            pct = float(m.group(1).replace(",", ".")) if m else -1
            if not 0 < pct <= 100:
                self._awaiting[uid] = (rid, code)
                await self.bot.send_to(chat_id, "I need a percentage between 0 and 100, e.g. 2. Try again or /cancel.")
                return
            rule.max_permille = max(1, int(round(pct * 10)))
        else:
            if not re.fullmatch(r"\s*\d{1,9}\s*", text):
                self._awaiting[uid] = (rid, code)
                await self.bot.send_to(chat_id, "I need a whole number, e.g. 1000. Try again or /cancel.")
                return
            setattr(rule, attr, int(text))
        await self.rt.save_rule(rule)
        text_view, markup = await self.view_rule(rid)
        await self.bot.send_to(chat_id, "✅ Saved." + note + "\n\n" + text_view, markup)

    # ----------------------------------------------------------------- views
    async def view_home(self) -> View:
        rules = list(self.rt.rules.values())
        active = [r for r in rules if r.enabled]
        if not rules:
            rule_line = "No rules yet → you get alerts for <b>every</b> upgrade."
        else:
            rule_line = f"{len(active)} of {len(rules)} rule(s) on → alerts only for matching gifts."
        polled = self.rt.polled_collections()
        scope = "all collections" if polled is None else f"{len(polled)} collection(s) (the rest are not scanned)"
        text = (
            "<b>⚙️ Gift alert settings</b>\n"
            f"Alerts: <b>{'⏸ PAUSED' if self.rt.paused else '▶ ACTIVE'}</b>\n"
            f"{rule_line}\nScanning: {scope}"
        )
        rows = [
            [("▶ Resume alerts" if self.rt.paused else "⏸ Pause alerts", "flag:paused")],
            [(f"📋 Rules ({len(rules)})", "rules"), ("➕ New rule", "rnew")],
            [(f"🆕 New-gift alerts: {onoff(self.rt.alert_new_gifts)}", "flag:alert_new_gifts")],
            [(f"💰 Floor prices: {onoff(self.rt.floor_enabled)}", "flag:floor_enabled")],
            [("📊 Status", "status")],
        ]
        return text, kb(rows)

    async def view_rules(self) -> View:
        rules = sorted(self.rt.rules.values(), key=lambda r: r.id)
        if not rules:
            return ("<b>📋 Rules</b>\nNo rules yet. Without rules you get every upgrade.",
                    kb([[("➕ New rule", "rnew")], [("⬅ Back", "home")]]))
        lines = ["<b>📋 Rules</b> (a gift alerts if <i>any</i> ON rule matches)"]
        rows: List[List[Tuple[str, str]]] = []
        for r in rules:
            lines.append(f"{'✅' if r.enabled else '⏹'} #{r.id}: {esc(r.short())}")
            rows.append([(f"{'✅' if r.enabled else '⏹'} #{r.id} {r.short()}"[:40], f"r:{r.id}")])
        rows.append([("➕ New rule", "rnew"), ("⬅ Back", "home")])
        return "\n".join(lines), kb(rows)

    async def view_rule(self, rid: int) -> View:
        r = self.rt.rules[rid]

        def lst(names: List[str]) -> str:
            return esc(", ".join(names)) if names else "any"

        number = "any" if r.min_num is None and r.max_num is None else f"#{r.min_num or 1} – {('#' + str(r.max_num)) if r.max_num else 'no limit'}"
        text = (
            f"<b>Rule #{rid}</b> {'✅ ON' if r.enabled else '⏹ OFF'}\n"
            f"Collections: <b>{lst(r.collections)}</b>\n"
            f"Models: <b>{lst(r.models)}</b>\n"
            f"Backdrops: <b>{lst(r.backdrops)}</b>\n"
            f"Symbols: <b>{lst(r.symbols)}</b>\n"
            f"Rarity: <b>{'rarest trait ≤ ' + format(r.max_permille / 10, 'g') + '%' if r.max_permille is not None else 'any'}</b>\n"
            f"Number: <b>{number}</b>\n\n"
            "A gift must match <i>every</i> field that is set (empty = any)."
        )
        if r.is_empty():
            text += "\n⚠️ This rule is empty, so it matches every gift."
        rows = [
            [("Collections", f"rf:{rid}:c"), ("Models", f"rf:{rid}:m")],
            [("Backdrops", f"rf:{rid}:b"), ("Symbols", f"rf:{rid}:s")],
            [("Rarity", f"rf:{rid}:r"), ("Min #", f"rf:{rid}:n"), ("Max #", f"rf:{rid}:x")],
            [("⏹ Turn off" if r.enabled else "✅ Turn on", f"rt:{rid}"), ("🗑 Delete", f"rd:{rid}")],
            [("⬅ Rules", "rules")],
        ]
        return text, kb(rows)

    async def view_field(self, rid: int, code: str) -> View:
        r = self.rt.rules[rid]
        label, attr, _kind = FIELDS[code]
        value = getattr(r, attr)
        if code in LIST_CODES:
            shown = esc(", ".join(value)) if value else "any"
            rows = [[("📋 Pick from list", f"rp:{rid}:{code}:0"), ("⌨️ Type names", f"rx:{rid}:{code}")],
                    [("🧹 Clear (any)", f"rc:{rid}:{code}"), ("⬅ Back", f"r:{rid}")]]
        elif code == "r":
            shown = f"rarest trait ≤ {value / 10:g}%" if value is not None else "any"
            rows = [[(f"≤{p / 10:g}%", f"rs:{rid}:r:{p}") for p in RARITY_PRESETS],
                    [("⌨️ Type percent", f"rx:{rid}:r"), ("🧹 Clear", f"rc:{rid}:r")],
                    [("⬅ Back", f"r:{rid}")]]
        else:
            shown = f"#{value}" if value is not None else "any"
            rows = [[("⌨️ Type number", f"rx:{rid}:{code}"), ("🧹 Clear", f"rc:{rid}:{code}")], [("⬅ Back", f"r:{rid}")]]
        return f"<b>Rule #{rid} · {label}</b>\nCurrent: <b>{shown}</b>", kb(rows)

    async def _pick_items(self, rule: Rule, code: str, page: int) -> Tuple[List[str], int, str]:
        """Names on this page, total count, and a hint if the list is short/empty."""
        if code == "c":
            names = sorted({st.title for st in self.detector.collections.values()}, key=str.lower)
            return names[page * PAGE:(page + 1) * PAGE], len(names), ""
        kind = FIELDS[code][2] or ""
        keys = [normalize_name(c) for c in rule.collections] if code in ("m", "s") and rule.collections else None
        total = await self.db.count_traits(kind, keys)
        items = await self.db.list_traits(kind, keys, PAGE, page * PAGE)
        hint = ""
        if total == 0:
            hint = ("\nNo names seen yet: the list fills up as the bot detects upgrades. "
                    "Use <b>Type names</b> in the meantime.")
        elif code in ("m", "s") and not rule.collections:
            hint = "\nTip: set a collection first to see only its names."
        return [n for n, _c in items], total, hint

    async def view_pick(self, rid: int, code: str, page: int) -> View:
        r = self.rt.rules[rid]
        label, attr, _kind = FIELDS[code]
        page = max(0, page)
        names, total, hint = await self._pick_items(r, code, page)
        pages = max(1, -(-total // PAGE))
        if page >= pages:
            page = pages - 1
            names, total, hint = await self._pick_items(r, code, page)
        chosen = {normalize_name(x) for x in getattr(r, attr)}
        rows: List[List[Tuple[str, str]]] = []
        row: List[Tuple[str, str]] = []
        for name in names:
            mark = "✅ " if normalize_name(name) in chosen else ""
            row.append((f"{mark}{name}"[:30], f"ri:{rid}:{code}:{page}:{name}"))
            if len(row) == 2:
                rows.append(row)
                row = []
        if row:
            rows.append(row)
        nav: List[Tuple[str, str]] = []
        if page > 0:
            nav.append(("◀", f"rp:{rid}:{code}:{page - 1}"))
        nav.append((f"{page + 1}/{pages}", "noop"))
        if page + 1 < pages:
            nav.append(("▶", f"rp:{rid}:{code}:{page + 1}"))
        rows.append(nav)
        rows.append([("🧹 Clear", f"rc:{rid}:{code}"), ("✔ Done", f"r:{rid}")])
        selected = esc(", ".join(getattr(r, attr))) if getattr(r, attr) else "any"
        return f"<b>Rule #{rid} · {label}</b>\nTap to select/unselect. Selected: <b>{selected}</b>{hint}", kb(rows)

    async def view_status(self) -> View:
        tiers = self.detector.tier_counts()
        st = self.stats
        up = int(time.time() - st.started)
        text = (
            "<b>📊 Status</b>\n"
            f"Uptime: {up // 3600}h {(up % 3600) // 60}m\n"
            f"Alerts: {'⏸ paused' if self.rt.paused else '▶ active'} · rules: {len(self.rt.rules)}\n"
            f"Collections: hot {tiers['hot']} · warm {tiers['warm']} · cold {tiers['cold']}"
            f" · baselining {tiers['unbootstrapped']}\n"
            f"Requests: {st.probes:,} (errors {st.probe_errors:,}, flood waits {st.flood_waits})\n"
            f"Upgrades detected: {st.detections:,}\n"
            f"Alerts sent: {st.alerts_sent:,} · failed: {st.alerts_failed:,}"
        )
        return text, kb([[("🔄 Refresh", "status"), ("⬅ Back", "home")]])


# ==========================================================================
# app
# ==========================================================================
"""Application wiring: startup diagnostics, supervised tasks, graceful shutdown."""

import asyncio
import logging
import os
import platform
import signal
import sys
import time
from typing import Any, Awaitable, Callable, Optional


log_app = logging.getLogger("giftparser.app")

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_CONFIG = 2
EXIT_SESSION = 3

WATCHDOG_INTERVAL = 15.0
STALL_RECONNECT_SECONDS = 150.0


def storage_warnings(settings: Settings) -> list:
    """Messages about where the database lives (Railway volume checks)."""
    out = []
    db = os.path.abspath(settings.database_path)
    mount = settings.volume_mount_path
    if settings.on_railway:
        if not mount:
            out.append(
                "NO RAILWAY VOLUME DETECTED: the database is on the container's ephemeral disk and is wiped on every "
                "redeploy. The bot will still avoid spamming old gifts (it re-baselines silently), but gifts upgraded "
                "while it restarts can be missed. Attach a volume at /data and set DATABASE_PATH=/data/gift_parser.sqlite3."
            )
        elif not db.startswith(os.path.abspath(mount).rstrip("/") + "/"):
            out.append(
                f"DATABASE_PATH ({db}) is not inside the Railway volume mounted at {mount}; the database will NOT persist. "
                f"Use DATABASE_PATH={mount}/gift_parser.sqlite3"
            )
    return out


class Application:
    def __init__(
        self,
        settings: Settings,
        *,
        source: GiftSource,
        bot: Optional[BotApiClient],
        db: Optional[Database] = None,
        install_signals: bool = True,
    ) -> None:
        self._install_signals = install_signals
        self.s = settings
        self.source = source
        self.bot = bot
        self.db = db or Database(settings.database_path)
        self.stats = Stats()
        self.stop = asyncio.Event()
        self.exit_code = EXIT_OK
        self.limiter = RateLimiter(settings.max_rps)
        self.runtime = Runtime(settings, self.db)
        self.filter = GiftFilter(settings, self.runtime)
        self.floors = FloorProvider(source, settings, self.runtime)
        self.dispatcher = AlertDispatcher(settings, self.db, bot, self.floors, self.stats)
        self.detector = UpgradeDetector(
            settings, source, self.db, self.filter, self.limiter, self.stats, self.dispatcher.enqueue_upgrade
        )
        self.catalog = CatalogWatcher(
            settings, source, self.db, self.detector, self.dispatcher, self.filter, self.limiter, self.stats
        )
        self.panel: Optional[ControlPanel] = None
        if settings.panel_enabled and settings.admin_ids and bot is not None and not settings.dry_run:
            self.panel = ControlPanel(settings, bot, self.db, self.runtime, self.detector, self.stats)
        self._tasks: list = []

    # ---------------------------------------------------------------- banner
    def log_banner(self) -> None:
        log_app.info("=" * 70)
        log_app.info("Telegram Gift Parser v%s starting", __version__)
        log_app.info("Python %s on %s", platform.python_version(), platform.platform())
        railway = {k: os.environ[k] for k in ("RAILWAY_PROJECT_NAME", "RAILWAY_ENVIRONMENT_NAME", "RAILWAY_SERVICE_NAME", "RAILWAY_DEPLOYMENT_ID", "RAILWAY_REPLICA_ID") if k in os.environ}
        if railway:
            log_app.info("Railway: %s", ", ".join(f"{k.replace('RAILWAY_', '').lower()}={v}" for k, v in railway.items()))
        for line in self.s.describe():
            log_app.info("config | %s", line)
        for warning in storage_warnings(self.s):
            log_app.warning("%s", warning)
        if self.s.on_railway and self.s.volume_mount_path:
            log_app.info("Persistent volume mounted at %s", self.s.volume_mount_path)
        if os.environ.get("RAILWAY_DEPLOYMENT_OVERLAP_SECONDS", "0") not in ("", "0"):
            log_app.warning(
                "RAILWAY_DEPLOYMENT_OVERLAP_SECONDS is set: two deployments would use the same Telegram session at the "
                "same time, which Telegram answers with AUTH_KEY_DUPLICATED. Remove that variable."
            )
        log_app.info("=" * 70)

    # ---------------------------------------------------------------- helpers
    async def _sleep_or_stop(self, seconds: float) -> bool:
        """Sleep; returns True if a stop was requested meanwhile."""
        try:
            await asyncio.wait_for(self.stop.wait(), timeout=seconds)
            return True
        except asyncio.TimeoutError:
            return False

    def fail(self, code: int, message: str) -> None:
        log_app.critical("%s", message)
        self.exit_code = code
        self.stop.set()

    async def _supervise(self, name: str, factory: Callable[[], Awaitable[Any]]) -> None:
        attempt = 0
        while not self.stop.is_set():
            started = time.monotonic()
            try:
                await factory()
                return
            except asyncio.CancelledError:
                raise
            except SessionInvalidError as exc:
                self.fail(EXIT_SESSION, str(exc))
                return
            except Exception:
                attempt = 1 if time.monotonic() - started > 300 else attempt + 1
                delay = backoff_delay(attempt, base=2.0, cap=60.0)
                log_app.exception("Task %r crashed; restarting in %.0fs", name, delay)
                if await self._sleep_or_stop(delay):
                    return

    def install_signal_handlers(self) -> None:
        loop = asyncio.get_running_loop()

        def handler(signame: str) -> None:
            if not self.stop.is_set():
                log_app.info("Received %s: shutting down gracefully", signame)
                self.stop.set()

        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, handler, sig.name)
            except (NotImplementedError, RuntimeError):  # Windows
                signal.signal(sig, lambda *_a, _n=sig.name: loop.call_soon_threadsafe(handler, _n))

    # ----------------------------------------------------------------- startup
    async def validate_bot(self) -> None:
        if self.bot is None or self.s.dry_run:
            log_app.info("Bot API validation skipped (DRY_RUN)")
            return
        try:
            me = await self.bot.get_me()
        except BotApiAuthError as exc:
            raise ConfigError(str(exc)) from exc
        except BotApiError as exc:
            log_app.error("Could not reach the Bot API: %s (will keep trying when sending alerts)", exc)
            return
        log_app.info("Bot API OK: sending as @%s", me.get("username"))
        try:
            chat = await self.bot.get_chat()
            log_app.info("Alert destination OK: %s (%s)", chat.get("title") or chat.get("username") or chat.get("id"), chat.get("type"))
        except BotApiError as exc:
            log_app.error(
                "Cannot access TELEGRAM_CHAT_ID=%s: %s. Add the bot to the channel/group as an administrator that can "
                "post messages. Alerts will fail until this is fixed.", self.s.chat_id, exc,
            )

    async def connect_source(self) -> None:
        attempt = 0
        while not self.stop.is_set():
            try:
                await self.source.connect()
                log_app.info("Telegram user session connected and authorized")
                return
            except (SessionInvalidError, FatalSourceError):
                raise
            except SourceError as exc:
                attempt += 1
                delay = backoff_delay(attempt, base=2.0, cap=60.0)
                log_app.warning("Telegram connect failed (%s); retry in %.0fs", exc, delay)
                if await self._sleep_or_stop(delay):
                    return

    # ---------------------------------------------------------------- tasks
    async def _watchdog(self) -> None:
        """Keep the connection alive and recover from silent stalls."""
        while not self.stop.is_set():
            if await self._sleep_or_stop(WATCHDOG_INTERVAL):
                return
            self.limiter.recover()
            try:
                await self.source.ensure_connected()
            except SessionInvalidError:
                raise
            except SourceError as exc:
                log_app.warning("Reconnect attempt failed: %s", exc)
                continue
            idle = time.time() - self.stats.last_probe_ok if self.stats.last_probe_ok else 0
            if self.detector.collections and self.stats.last_probe_ok and idle > STALL_RECONNECT_SECONDS:
                log_app.warning("No successful Telegram request for %.0fs; forcing a reconnect", idle)
                self.stats.reconnects += 1
                try:
                    await self.source.disconnect()
                    await self.source.connect()
                    self.stats.last_probe_ok = time.time()
                except SessionInvalidError:
                    raise
                except SourceError as exc:
                    log_app.warning("Forced reconnect failed: %s", exc)

    async def _stats_loop(self) -> None:
        while not self.stop.is_set():
            if await self._sleep_or_stop(self.s.stats_interval):
                return
            st = self.stats
            tiers = self.detector.tier_counts()
            log_app.info(
                "STATS up=%ds | collections hot=%d warm=%d cold=%d pending_baseline=%d unverified_prefix=%d | "
                "probes=%d (%.1f/s, limit %.1f/s) hits=%d misses=%d errors=%d floodwaits=%d | "
                "detections=%d alerts_sent=%d alerts_failed=%d backlog=%d | catalog_checks=%d reconnects=%d",
                time.time() - st.started, tiers["hot"], tiers["warm"], tiers["cold"], tiers["unbootstrapped"], tiers["unverified"],
                st.probes, st.rps_since_last(), self.limiter.rate, st.probe_hits, st.probe_misses, st.probe_errors,
                st.flood_waits, st.detections, st.alerts_sent, st.alerts_failed, self.dispatcher.backlog,
                st.catalog_checks, st.reconnects,
            )

    async def _maintenance(self) -> None:
        while not self.stop.is_set():
            removed = await self.db.prune()
            if removed:
                log_app.info("Pruned %d old database row(s)", removed)
            if await self._sleep_or_stop(6 * 3600):
                return

    # ------------------------------------------------------------------- main
    async def run(self) -> int:
        self.log_banner()
        if self._install_signals:
            self.install_signal_handlers()
        try:
            await self.db.open()
            counts = await self.db.counts()
            log_app.info("Database ready at %s: %s", self.s.database_path, counts)
            await self.runtime.load()
            await self.validate_bot()
            await self.connect_source()
            if self.stop.is_set():
                return self.exit_code

            loaded = await self.detector.load_from_db()
            await self.catalog.register_extra_collections()
            log_app.info("Loaded %d collection(s) from the database", loaded)
            await self.dispatcher.load_pending()

            if self.s.send_test_alert_on_start:
                try:
                    await self.dispatcher.send_test_alert()
                except Exception:
                    log_app.exception("Startup test alert failed")

            self._tasks = [
                asyncio.create_task(self._supervise("dispatcher", self.dispatcher.run), name="dispatcher"),
                asyncio.create_task(self._supervise("detector", self.detector.run), name="detector"),
                asyncio.create_task(self._supervise("catalog", self.catalog.run), name="catalog"),
                asyncio.create_task(self._supervise("watchdog", self._watchdog), name="watchdog"),
                asyncio.create_task(self._supervise("stats", self._stats_loop), name="stats"),
                asyncio.create_task(self._supervise("maintenance", self._maintenance), name="maintenance"),
            ]
            if self.panel is not None:
                self._tasks.append(asyncio.create_task(self._supervise("panel", self.panel.run), name="panel"))
            elif self.s.panel_enabled:
                log_app.warning("Control panel is OFF: set ADMIN_IDS (or use your own numeric id as TELEGRAM_CHAT_ID)")
            log_app.info("All components started. Watching for new gifts.")
            await self.stop.wait()
        except ConfigError as exc:
            self.exit_code = EXIT_CONFIG
            log_app.critical("Configuration problem: %s", exc)
        except SessionInvalidError as exc:
            self.exit_code = EXIT_SESSION
            log_app.critical("%s", exc)
        except FatalSourceError as exc:
            self.exit_code = EXIT_ERROR
            log_app.critical("%s", exc)
        except Exception:
            self.exit_code = EXIT_ERROR
            log_app.exception("Fatal error during startup")
        finally:
            await self.shutdown()
        for fatal in (self.detector.fatal, self.catalog.fatal):
            if fatal is not None and self.exit_code == EXIT_OK:
                self.exit_code = EXIT_SESSION
        return self.exit_code

    async def shutdown(self) -> None:
        log_app.info("Shutting down...")
        self.stop.set()
        self.detector.request_stop()
        self.catalog.request_stop()
        self.dispatcher.request_stop()
        if self.panel is not None:
            self.panel.request_stop()
        try:
            await self.dispatcher.drain(self.s.shutdown_grace)
        except Exception:  # pragma: no cover
            log_app.exception("error while draining alerts")
        for t in self._tasks:
            if not t.done():
                t.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        for closer in (self.source.disconnect, self._close_bot, self.db.close):
            try:
                await asyncio.wait_for(closer(), timeout=5)
            except Exception as exc:
                log_app.debug("error during shutdown step: %s", exc)
        log_app.info("Shutdown complete (exit code %d)", self.exit_code)

    async def _close_bot(self) -> None:
        if self.bot is not None:
            await self.bot.close()


# ==========================================================================
# check
# ==========================================================================
"""``python main.py check``: live end-to-end verification against Telegram.

Run it once after configuring credentials (locally or via ``railway run``).  It
exercises every external dependency and prints PASS/FAIL per step, so you know
the detection data source really works *before* relying on the bot.
"""

import sys
from typing import Callable, List, Optional, Tuple


OK = "PASS"
BAD = "FAIL"


async def run_check(settings: Settings) -> int:
    results: List[Tuple[str, bool, str]] = []

    def record(name: str, ok: bool, detail: str = "") -> None:
        results.append((name, ok, detail))
        print(f"[{OK if ok else BAD}] {name}" + (f": {detail}" if detail else ""))
        sys.stdout.flush()

    # 1. Bot API
    bot = BotApiClient(settings.bot_token, settings.chat_id, settings.thread_id)
    try:
        me = await bot.get_me()
        record("Bot API token (getMe)", True, f"@{me.get('username')}")
        try:
            chat = await bot.get_chat()
            record("Alert chat reachable (getChat)", True, f"{chat.get('title') or chat.get('username')} [{chat.get('type')}]")
        except Exception as exc:
            record("Alert chat reachable (getChat)", False, f"{exc} -> add the bot as an admin that can post")
    except Exception as exc:
        record("Bot API token (getMe)", False, str(exc))
    finally:
        await bot.close()

    # 2. Database
    db = Database(settings.database_path)
    try:
        await db.open()
        await db.set_meta("check", "ok")
        assert await db.get_meta("check") == "ok"
        record("SQLite database read/write", True, f"{settings.database_path} (from {settings.database_path_source})")
    except Exception as exc:
        record("SQLite database read/write", False, str(exc))
    finally:
        await db.close()

    # 3. User session + gift data
    src = TelethonSource(settings.api_id or 0, settings.api_hash, settings.session)
    try:
        await src.connect()
        record("User session authorized (Telethon)", True, await src.whoami())
        catalog = await src.get_catalog()
        upgradable = [g for g in (catalog or []) if g.upgradable and g.title]
        record("Gift catalog (payments.getStarGifts)", bool(catalog), f"{len(catalog or [])} gift types, {len(upgradable)} upgradable")

        sample = None
        for g in upgradable:
            sample = await src.get_unique(f"{derive_slug_prefix(g.title or '')}-1")
            if sample:
                break
        if sample is None and upgradable:
            sample = await src.find_resale_sample(upgradable[0].gift_id)
        if sample is not None:
            traits = {k: (t.name if t else None) for k, t in sample.traits().items()}
            record("Collectible lookup (payments.getUniqueStarGift)", True,
                   f"{sample.slug}: {traits}, issued={sample.issued}, owner=@{sample.owner_username or '?'} (id {sample.owner_id}), link={sample.link}")
            ok_traits = all(traits.values())
            record("Attributes parsed (model/backdrop/symbol)", ok_traits, "" if ok_traits else "some attributes missing")
            rar = [t.rarity_text() for t in sample.traits().values() if t]
            record("Rarity data present", any(rar), ", ".join(str(r) for r in rar))
            miss = await src.get_unique(f"{sample.prefix}-{10**9}")
            record("Non-existent slug reported as 'not found'", miss is None)
            if sample.gift_id:
                floor = await src.get_floor(sample.gift_id, sample.model_document_id)
                record("Floor price lookup (resale market)", True,
                       f"collection={floor.collection_stars} model={floor.model_stars} (None = nothing listed)")
        else:
            record("Collectible lookup (payments.getUniqueStarGift)", False, "no sample collectible could be resolved")
    except SourceError as exc:
        record("User session / gift data", False, str(exc))
    except Exception as exc:
        record("User session / gift data", False, f"{type(exc).__name__}: {exc}")
    finally:
        await src.disconnect()

    failed = [r for r in results if not r[1]]
    print()
    print("ALL CHECKS PASSED" if not failed else f"{len(failed)} CHECK(S) FAILED")
    return 0 if not failed else 1


# ==========================================================================
# entry point
# ==========================================================================
async def create_session() -> int:
    """Interactive one-time login that prints TELEGRAM_SESSION (run on a computer/Colab/Termux)."""
    import getpass

    try:
        from telethon import TelegramClient
        from telethon.sessions import StringSession
    except ImportError:
        print("Telethon is not installed. Run: pip install telethon")
        return 1
    api_id = os.environ.get("TELEGRAM_API_ID") or input("api_id (from https://my.telegram.org): ").strip()
    api_hash = os.environ.get("TELEGRAM_API_HASH") or getpass.getpass("api_hash (input hidden): ").strip()
    try:
        api_id_int = int(api_id)
    except ValueError:
        print("api_id must be a number")
        return 1
    client = TelegramClient(StringSession(), api_id_int, api_hash, device_model="Gift Parser (Railway)")
    await client.start()  # interactive: phone -> code -> optional 2FA password
    me = await client.get_me()
    session = client.session.save()
    await client.disconnect()
    print()
    print(f"Logged in as: {getattr(me, 'first_name', '')} (id {me.id})")
    print("=" * 72)
    print("TELEGRAM_SESSION (copy everything between the lines, keep it SECRET):")
    print("-" * 72)
    print(session)
    print("-" * 72)
    print("Paste it into Railway -> your service -> Variables -> TELEGRAM_SESSION.")
    print("Do NOT run two copies of the bot with the same session string at the same time.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="main.py", description="Telegram Gift Parser")
    p.add_argument("command", nargs="?", default="run", choices=["run", "test-alert", "check", "session"])
    p.add_argument("--version", action="version", version=f"gift-parser {__version__}")
    return p


async def _run(command: str, settings: Settings) -> int:
    if command == "check":
        return await run_check(settings)
    bot = BotApiClient(settings.bot_token, settings.chat_id, settings.thread_id)
    if command == "test-alert":
        dispatcher = AlertDispatcher(settings, Database(settings.database_path), bot, None, Stats())
        try:
            await dispatcher.send_test_alert()
        finally:
            await bot.close()
        print("Test alert sent. Check your channel/group." if not settings.dry_run else "DRY_RUN: logged only.")
        return 0
    source = TelethonSource(settings.api_id or 0, settings.api_hash, settings.session)
    app = Application(settings, source=source, bot=bot)
    return await app.run()


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "session":
        return asyncio.run(create_session())
    try:  # local convenience; Railway injects real environment variables
        from dotenv import load_dotenv

        load_dotenv(override=False)
    except ImportError:
        pass
    try:
        settings = load_settings(need_user=args.command != "test-alert")
    except ConfigError as exc:
        print(f"CONFIGURATION ERROR: {exc}", flush=True)
        return 2
    setup_logging(settings.log_level, settings.secrets())
    try:
        return asyncio.run(_run(args.command, settings))
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        logging.getLogger("giftparser").critical("Fatal: %s: %s", type(exc).__name__, exc, exc_info=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
