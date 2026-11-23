"""局域网自签证书：复用、换地址重签、成对校验。"""

from __future__ import annotations

from pathlib import Path

from app.infra import tls


def _san(cert_path: Path) -> tuple[set[str], set[str]]:
    from cryptography import x509

    cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
    san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    return (
        set(san.get_values_for_type(x509.DNSName)),
        {str(item) for item in san.get_values_for_type(x509.IPAddress)},
    )


def test_a_cert_is_reused_while_the_addresses_match(tmp_path: Path) -> None:
    cert, key = tls.ensure_cert(tmp_path, ["192.168.1.10"])
    before = cert.read_bytes()
    again, _ = tls.ensure_cert(tmp_path, ["192.168.1.10"])
    assert again == cert and key.is_file()
    assert again.read_bytes() == before


def test_a_new_address_forces_a_new_cert(tmp_path: Path) -> None:
    cert, _ = tls.ensure_cert(tmp_path, ["192.168.1.10"])
    before = cert.read_bytes()
    renewed, _ = tls.ensure_cert(tmp_path, ["192.168.1.11"])
    assert renewed.read_bytes() != before
    dns, ips = _san(renewed)
    assert "192.168.1.11" in ips and "localhost" in dns


def test_a_mismatched_pair_is_replaced(tmp_path: Path) -> None:
    cert, key = tls.ensure_cert(tmp_path, ["192.168.1.10"])
    before = cert.read_bytes()
    # 模拟上次写盘写到一半：key 是新的、cert 还是旧的。下一次必须认出来重签。
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    replacement = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    key.write_bytes(
        replacement.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    renewed, _ = tls.ensure_cert(tmp_path, ["192.168.1.10"])
    assert renewed.read_bytes() != before
    assert tls._pair_ok(renewed, key)
