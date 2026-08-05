"""Entra ID（Azure AD）が発行した JWT アクセストークンの検証（標準ライブラリのみ）。

profile_ui Lambda が Function URL（authtype=NONE）で受けたリクエストの
`Authorization: Bearer <token>` を、Entra の JWKS 公開鍵で検証するために使う。

なぜ PyJWT / cryptography を使わないか:
  既存 Lambda（rotate_key / report_usage）は boto3 のみで依存ゼロ。cryptography は
  ネイティブ拡張のため macOS でビルドしたものが Lambda(Linux) で動かず、クロスビルドの
  段取りが要る。RS256（公開鍵での署名検証）は modexp + PKCS#1 v1.5 パディング照合だけで
  済み、hashlib(SHA-256) も標準にあるため、外部依存を足さず純 Python で検証する。
  ※ 検証するのは「公開鍵での署名照合」なので秘密は扱わない（タイミング攻撃対象なし）。

検証項目（Entra v2.0 トークン前提）:
  - 署名（RS256）: JWKS の kid 一致鍵で照合
  - exp / nbf: 有効期間（60 秒の許容ずれ）
  - iss: https://login.microsoftonline.com/<tid>/v2.0
  - tid: 期待テナント（= 認可の実体。テナント全員に開放する要件のため tid 一致で足りる）
  - aud: 期待クライアント（api://<client_id> または <client_id>）
"""

import base64
import hashlib
import json
import logging
import time
import urllib.request

logger = logging.getLogger()

# SHA-256 の DigestInfo（PKCS#1 v1.5 EMSA の DER プレフィックス）
_SHA256_DIGEST_INFO_PREFIX = bytes.fromhex("3031300d060960864801650304020105000420")

# 許容する時刻ずれ（秒）
_CLOCK_SKEW = 60

# JWKS はプロセス内でキャッシュ（TTL 経過 or 未知の kid で再取得）
_JWKS_CACHE: dict = {"keys": {}, "fetched_at": 0.0}
_JWKS_TTL = 3600.0


class AuthError(Exception):
    """トークン検証失敗。呼び出し側は 401 に変換する。"""


def _b64url_decode(segment: str) -> bytes:
    padding = "=" * (-len(segment) % 4)
    return base64.urlsafe_b64decode(segment + padding)


def _b64url_to_int(segment: str) -> int:
    return int.from_bytes(_b64url_decode(segment), "big")


def _fetch_jwks(tenant_id: str) -> dict:
    """Entra の JWKS を取得し {kid: (n, e)} を返す。"""
    url = f"https://login.microsoftonline.com/{tenant_id}/discovery/v2.0/keys"
    with urllib.request.urlopen(url, timeout=5) as resp:  # noqa: S310 - 固定の Microsoft ドメイン
        doc = json.loads(resp.read())
    keys = {}
    for k in doc.get("keys", []):
        if k.get("kty") == "RSA" and "n" in k and "e" in k and "kid" in k:
            keys[k["kid"]] = (_b64url_to_int(k["n"]), _b64url_to_int(k["e"]))
    if not keys:
        raise AuthError("JWKS に RSA 公開鍵が見つかりません")
    return keys


def _get_public_key(tenant_id: str, kid: str) -> tuple:
    now = time.time()
    cache = _JWKS_CACHE
    fresh = now - cache["fetched_at"] < _JWKS_TTL
    if not fresh or kid not in cache["keys"]:
        # 期限切れ、または未知の kid（鍵ローテ直後）なら取り直す
        cache["keys"] = _fetch_jwks(tenant_id)
        cache["fetched_at"] = now
    if kid not in cache["keys"]:
        raise AuthError(f"署名鍵が見つかりません (kid={kid})")
    return cache["keys"][kid]


def _verify_rs256(signing_input: bytes, signature: bytes, n: int, e: int) -> bool:
    """RSA 公開鍵 (n, e) で RS256 署名を検証（PKCS#1 v1.5）。"""
    k = (n.bit_length() + 7) // 8
    if len(signature) != k:
        return False
    # s^e mod n を復号し、EM = 0x00 || 0x01 || PS(0xFF..) || 0x00 || T を再構成して照合
    m = pow(int.from_bytes(signature, "big"), e, n)
    em = m.to_bytes(k, "big")
    digest = hashlib.sha256(signing_input).digest()
    expected = b"\x00\x01" + b"\xff" * (k - len(_SHA256_DIGEST_INFO_PREFIX) - len(digest) - 3) + b"\x00" + _SHA256_DIGEST_INFO_PREFIX + digest
    return em == expected


def verify_token(token: str, tenant_id: str, client_id: str) -> dict:
    """検証に成功したら claims(dict) を返す。失敗なら AuthError。"""
    try:
        header_b64, payload_b64, sig_b64 = token.split(".")
        header = json.loads(_b64url_decode(header_b64))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
        raise AuthError("JWT の形式が不正です")

    if header.get("alg") != "RS256":
        raise AuthError(f"想定外の alg: {header.get('alg')}")
    kid = header.get("kid")
    if not kid:
        raise AuthError("ヘッダーに kid がありません")

    n, e = _get_public_key(tenant_id, kid)
    signing_input = f"{header_b64}.{payload_b64}".encode()
    signature = _b64url_decode(sig_b64)
    if not _verify_rs256(signing_input, signature, n, e):
        raise AuthError("署名の検証に失敗しました")

    claims = json.loads(_b64url_decode(payload_b64))

    now = time.time()
    if now > claims.get("exp", 0) + _CLOCK_SKEW:
        raise AuthError("トークンの有効期限が切れています")
    if now < claims.get("nbf", 0) - _CLOCK_SKEW:
        raise AuthError("トークンがまだ有効ではありません")

    # iss は v2.0（login.microsoftonline.com/<tid>/v2.0）と v1.0（sts.windows.net/<tid>/）の
    # 両形式を許容する。カスタム API 向けアクセストークンは accessTokenAcceptedVersion が
    # 既定(null=v1)だと v1.0 形式で発行されるため、Entra 設定に依らず通るようにする。
    # 実質の認可は下の tid（テナント）一致で行う。
    expected_iss = {
        f"https://login.microsoftonline.com/{tenant_id}/v2.0",
        f"https://sts.windows.net/{tenant_id}/",
    }
    if claims.get("iss") not in expected_iss:
        raise AuthError("iss が一致しません")
    if claims.get("tid") != tenant_id:
        raise AuthError("tid（テナント）が一致しません")
    if claims.get("aud") not in (client_id, f"api://{client_id}"):
        raise AuthError("aud（クライアント）が一致しません")

    return claims
