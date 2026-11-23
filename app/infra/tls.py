"""自签证书：局域网 https。

浏览器只在 https 或 localhost 下把麦克风交给页面（`navigator.mediaDevices` 在
http://192.168.x.x 上直接是 undefined）。这台服务默认只听回环；一旦对局域网开放，
就用这里签的一张自签证书继续监听同一个端口，其它设备的麦克风才可用。

证书不随包装：首次需要时按当前地址现签一张，落在数据目录的 `certs/` 里，
换机器、换 IP 都重新签。浏览器会提示"不受信任"——自签就是这样，接受一次即可；
它只为把请求变成安全上下文，不证明服务器身份。
"""

from __future__ import annotations

import datetime as dt
import ipaddress
import logging
from pathlib import Path

from app.infra.atomic import write_bytes

__all__ = ["CertError", "ensure_cert"]

LOG = logging.getLogger("voicehub.tls")

#: 自签证书的有效期。浏览器对超过 825 天的自签证书会直接拒绝。
DAYS = 820
#: 离到期不足这个天数就重签，免得用户某天突然打不开。
RENEW_WITHIN = 30


class CertError(RuntimeError):
    """签不出证书：调用方据此回落到 http，并把原因说给人听。"""


def _san_names(hosts: list[str]) -> tuple[list[str], list[ipaddress._BaseAddress]]:
    dns: list[str] = ["localhost"]
    ips: list[ipaddress._BaseAddress] = [
        ipaddress.ip_address("127.0.0.1"),
        ipaddress.ip_address("::1"),
    ]
    for raw in hosts:
        text = str(raw or "").strip()
        if not text:
            continue
        try:
            ips.append(ipaddress.ip_address(text))
        except ValueError:
            if text not in dns:
                dns.append(text)
    return dns, ips


def _valid_for(path: Path, hosts: list[str]) -> bool:
    """现有证书还能用吗：看得见、没快到期、SAN 覆盖当前地址。

    只看到期会在换 IP 后继续发旧证书：浏览器报名称不匹配，用户以为服务坏了。
    """
    try:
        from cryptography import x509
    except ImportError:  # pragma: no cover - cryptography 不在时走 ensure_cert 的报错
        return False
    try:
        cert = x509.load_pem_x509_certificate(path.read_bytes())
    except (OSError, ValueError):
        return False
    if cert.not_valid_after_utc - dt.datetime.now(dt.UTC) <= dt.timedelta(days=RENEW_WITHIN):
        return False
    try:
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    except x509.ExtensionNotFound:
        return False
    dns, ips = _san_names(hosts)
    have_dns = set(san.get_values_for_type(x509.DNSName))
    have_ips = set(san.get_values_for_type(x509.IPAddress))
    return set(dns) <= have_dns and set(ips) <= have_ips


def _pair_ok(cert_path: Path, key_path: Path) -> bool:
    """证书与私钥是不是同一对；上次写盘写到一半时这里会拦住。"""
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import serialization
    except ImportError:  # pragma: no cover
        return False
    try:
        cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
        key = serialization.load_pem_private_key(key_path.read_bytes(), password=None)
        return (
            cert.public_key().public_bytes(
                serialization.Encoding.DER,
                serialization.PublicFormat.SubjectPublicKeyInfo,
            )
            == key.public_key().public_bytes(
                serialization.Encoding.DER,
                serialization.PublicFormat.SubjectPublicKeyInfo,
            )
        )
    except (OSError, ValueError, TypeError):
        return False


def ensure_cert(root: Path, hosts: list[str]) -> tuple[Path, Path]:
    """保证 `root` 下有一张对 `hosts` 有效的自签证书，返回 (cert, key)。

    已有且没过期、SAN 覆盖当前地址、且证书与私钥成对就复用；否则重签。
    签不出来时抛 :class:`CertError`，调用方回落到 http。
    """
    cert_path = Path(root) / "server.crt"
    key_path = Path(root) / "server.key"
    if (
        cert_path.is_file()
        and key_path.is_file()
        and _valid_for(cert_path, hosts)
        and _pair_ok(cert_path, key_path)
    ):
        return cert_path, key_path

    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID
    except ImportError as exc:  # pragma: no cover - 依赖缺了才走这里
        raise CertError(
            "没有 cryptography，签不了自签证书：先 pip install cryptography。"
        ) from exc

    dns, ips = _san_names(hosts)
    now = dt.datetime.now(dt.UTC)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name(
        [
            x509.NameAttribute(NameOID.COMMON_NAME, dns[0]),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "VoiceHub"),
        ]
    )
    builder = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=5))
        .not_valid_after(now + dt.timedelta(days=DAYS))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.SubjectAlternativeName(
                [x509.DNSName(name) for name in dns]
                + [x509.IPAddress(ip) for ip in ips]
            ),
            critical=False,
        )
    )
    cert = builder.sign(key, hashes.SHA256())

    key_bytes = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    )
    cert_bytes = cert.public_bytes(serialization.Encoding.PEM)
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    # 两个文件各写各的，必然有一小段"新 key + 旧 cert"的窗口。下次启动由
    # _pair_ok 认出这种半对证书并重签；写盘本身用 atomic 的唯一临时名与退避。
    try:
        write_bytes(key_path, key_bytes)
        write_bytes(cert_path, cert_bytes)
    except OSError as exc:
        raise CertError(f"证书写不进数据目录：{exc}") from exc

    LOG.info(
        "已签发自签证书：%s（%s）",
        cert_path,
        ", ".join(dns + [str(ip) for ip in ips]),
    )
    return cert_path, key_path
