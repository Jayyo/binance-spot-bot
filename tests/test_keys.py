import logging
import pickle

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from spotbot.keys import CredentialError, RedactingFilter, Secret, load_credentials

SEED = bytes.fromhex(
    "9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60"
)  # RFC 8032 TEST 1
KEY = "k" * 64
SECRET = "s" * 64


def pem_file(tmp_path, password=None):
    key = Ed25519PrivateKey.from_private_bytes(SEED)
    enc = (
        serialization.BestAvailableEncryption(password.encode())
        if password
        else serialization.NoEncryption()
    )
    path = tmp_path / "key.pem"
    path.write_bytes(
        key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, enc)
    )
    path.chmod(0o600)
    return path


def dotenv(tmp_path, text, mode=0o600):
    path = tmp_path / ".env"
    path.write_text(text)
    path.chmod(mode)
    return path


def test_testnet_never_reads_live_variables(tmp_path):
    env = {"BINANCE_API_KEY": KEY, "BINANCE_API_SECRET": SECRET}
    with pytest.raises(CredentialError, match="no testnet API key"):
        load_credentials("testnet", env, tmp_path / ".env")


def test_live_reads_its_own_variables(tmp_path):
    env = {"BINANCE_TESTNET_API_KEY": KEY, "BINANCE_TESTNET_API_SECRET": SECRET}
    with pytest.raises(CredentialError, match="no live API key"):
        load_credentials("live", env, tmp_path / ".env")
    creds = load_credentials("testnet", env, tmp_path / ".env")
    assert creds.signer.kind == "hmac" and creds.source == "environment"


def test_environment_wins_over_dotenv(tmp_path):
    path = dotenv(
        tmp_path, f"BINANCE_TESTNET_API_KEY={'f' * 64}\nBINANCE_TESTNET_API_SECRET={SECRET}\n"
    )
    creds = load_credentials("testnet", {"BINANCE_TESTNET_API_KEY": "e" * 64}, path)
    assert creds.api_key.reveal() == "e" * 64
    creds = load_credentials("testnet", {}, path)
    assert creds.api_key.reveal() == "f" * 64 and creds.source == str(path)


def test_loose_dotenv_permissions_warn(tmp_path, caplog):
    path = dotenv(
        tmp_path, f"BINANCE_TESTNET_API_KEY={KEY}\nBINANCE_TESTNET_API_SECRET={SECRET}\n", 0o644
    )
    with caplog.at_level(logging.WARNING):
        load_credentials("testnet", {}, path)
    assert "chmod 600" in caplog.text


def test_ambiguous_or_incomplete_signing_config_is_refused(tmp_path):
    key = pem_file(tmp_path)
    both = {
        "BINANCE_API_KEY": KEY,
        "BINANCE_API_SECRET": SECRET,
        "BINANCE_PRIVATE_KEY_PATH": str(key),
    }
    with pytest.raises(CredentialError, match="keep exactly one"):
        load_credentials("live", both, tmp_path / ".env")
    with pytest.raises(CredentialError, match="no signing secret"):
        load_credentials("live", {"BINANCE_API_KEY": KEY}, tmp_path / ".env")


def test_ed25519_key_file(tmp_path):
    env = {"BINANCE_API_KEY": KEY, "BINANCE_PRIVATE_KEY_PATH": str(pem_file(tmp_path))}
    creds = load_credentials("live", env, tmp_path / ".env")
    assert creds.signer.kind == "ed25519"
    assert len(creds.fingerprint) == 12


def test_wrong_key_password_does_not_leak_material(tmp_path):
    path = pem_file(tmp_path, password="right-password")
    env = {
        "BINANCE_API_KEY": KEY,
        "BINANCE_PRIVATE_KEY_PATH": str(path),
        "BINANCE_PRIVATE_KEY_PASSWORD": "wrong",
    }
    with pytest.raises(CredentialError) as err:
        load_credentials("live", env, tmp_path / ".env")
    assert "BEGIN" not in str(err.value) and "right-password" not in str(err.value)


@pytest.mark.parametrize(
    "bad", ["abc def0123456789ab", '"abcdef0123456789ab"', "abcdef0123456789ab\n", "short"]
)
def test_malformed_keys_are_refused_without_echoing_them(bad, tmp_path):
    env = {"BINANCE_API_KEY": bad, "BINANCE_API_SECRET": SECRET}
    with pytest.raises(CredentialError) as err:
        load_credentials("live", env, tmp_path / ".env")
    assert "BINANCE_API_KEY" in str(err.value)
    assert bad.strip('"\n ') not in str(err.value)


def test_secrets_do_not_print_or_pickle():
    s = Secret("super-secret-value")
    assert "super" not in repr(s) and "super" not in str(s) and "super" not in f"{[s]}"
    with pytest.raises(TypeError):
        pickle.dumps(s)
    creds = load_credentials("live", {"BINANCE_API_KEY": "v" * 64, "BINANCE_API_SECRET": "h" * 64})
    assert "h" * 64 not in repr(creds) and "v" * 64 not in repr(creds)


def test_redacting_filter_blanks_values_in_messages_and_args(caplog):
    logger = logging.getLogger("test.redact")
    caplog.handler.addFilter(RedactingFilter(["abcdefgh12345678"]))
    with caplog.at_level(logging.INFO, logger="test.redact"):
        logger.info("key=%s", "abcdefgh12345678")
        logger.info("inline abcdefgh12345678 value")
    assert "abcdefgh12345678" not in caplog.text
    assert caplog.text.count("***") == 2
