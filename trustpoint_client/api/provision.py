from __future__ import annotations

import requests
import hashlib
import hmac
from pathlib import Path
import urllib3
from typing import TYPE_CHECKING

from cryptography.x509 import oid
from trustpoint_devid_module.serializer import CredentialSerializer

if TYPE_CHECKING:
    from typing import Any
    from trustpoint_devid_module.service_interface import DevIdModule

from trustpoint_client.api.schema import SignatureSuite, CertificateType

HMAC_SIGNATURE_HTTP_HEADER = 'hmac-signature'

from trustpoint_client.api.schema import PkiProtocol
from trustpoint_client.api.schema import DomainModel, CredentialModel
from trustpoint_client.api.schema import DomainConfigModel
from cryptography.hazmat.primitives.asymmetric import rsa, ec
from cryptography.hazmat.primitives import hashes
from cryptography import x509
from cryptography.hazmat.primitives import serialization

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from typing import Union
    PrivateKey = Union[rsa.RSAPrivateKey, ec.EllipticCurvePrivateKey]

class TrustpointClientProvision:

    devid_module: DevIdModule
    inventory: property
    default_domain: property
    _store_inventory: callable

    def provision_auto(self, otp: str, device: str, host: str, port: int = 443,
                       extra_data: dict | None = None) -> dict:

        provision_data = {
            'otp': otp,
            'device': device,
            'host': host,
            'port': port
        }

        if extra_data: # trust store and protocol info already provided (e.g. by zero-touch demo)
            try:
                provision_data['trust-store'] = extra_data['trust-store']
                provision_data['domain'] = extra_data['domain']
                provision_data['signature-suite'] = SignatureSuite(extra_data['signature-suite'])
                provision_data['pki-protocol'] = PkiProtocol(extra_data['pki-protocol'])
            except KeyError as e:
                raise ValueError(f'extra_data provided, but does not contain required key {e}.')
        else:
            self._provision_get_trust_store(provision_data=provision_data)
        tls_trust_store_path = (Path(__file__).parent / Path('tls_trust_store.pem'))
        tls_trust_store_path.write_text(provision_data['trust-store'])
        provision_data['crypto-key'] = self.generate_new_key(provision_data['signature-suite'])
        self._provision_get_ldevid(provision_data=provision_data, tls_trust_store_path=tls_trust_store_path)
        self._provision_get_ldevid_chain(provision_data=provision_data, tls_trust_store_path=tls_trust_store_path)
        tls_trust_store_path.unlink()

        loaded_cert = x509.load_pem_x509_certificate(provision_data['ldevid'].encode())
        provision_data['ldevid-subject'] = loaded_cert.subject.rfc4514_string()
        provision_data['ldevid-certificate-type'] = CertificateType.LDEVID
        provision_data['ldevid-not-valid-before'] = loaded_cert.not_valid_before_utc
        provision_data['ldevid-not-valid-after'] = loaded_cert.not_valid_after_utc
        provision_data['ldevid-expires-in'] = provision_data['ldevid-not-valid-after'] \
                                              - provision_data['ldevid-not-valid-before']
        provision_data['serial-number'] = loaded_cert.subject.get_attributes_for_oid(
            oid.NameOID.SERIAL_NUMBER)[0].value

        self._store_ldevid_in_inventory(provision_data=provision_data)

        result = {
            'Device': provision_data['device'],
            'Serial-Number': provision_data['serial-number'],
            'Host': provision_data['host'],
            'Port': provision_data['port'],
            'PKI-Protocol': provision_data['pki-protocol'].value,
            'Signature-Suite': provision_data['signature-suite'].value,
            'LDevID Subject': provision_data['ldevid-subject'],
            'LDevID Certificate Type': provision_data['ldevid-certificate-type'].value,
            'LDevID Not-Valid-Before': provision_data['ldevid-not-valid-before'],
            'LDevID Not-Valid-After': provision_data['ldevid-not-valid-after'],
            'LDevID Expires-In': provision_data['ldevid-expires-in']
        }

        if result['Host'] == 'localhost':
            result['Host'] = '127.0.0.1'

        return result

    def _provision_get_trust_store(self, provision_data: dict[str, Any]) -> None:
        host = provision_data['host']
        url_extension = provision_data['device']
        otp = provision_data['otp'].encode()
        salt = provision_data['device'].encode()
        port = provision_data['port']

        # We do not yet check the TLS server certificate, thus verify=False is set on purpose here
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        response = requests.get(
            f'https://{host}:{port}/api/onboarding/trust-store/{url_extension}',
            verify=False,
            timeout=10)
        if not HMAC_SIGNATURE_HTTP_HEADER in response.headers:
            raise ValueError('HMAC missing in HTTP header.')

        provision_data['domain'] = response.headers['domain']
        provision_data['signature-suite'] = SignatureSuite(response.headers['signature-suite'])
        provision_data['pki-protocol'] = PkiProtocol(response.headers['pki-protocol'])

        pbkdf2_iter = 1000000
        derived_key = hashlib.pbkdf2_hmac('sha256', otp, salt, pbkdf2_iter, dklen=32)
        calculated_hmac = hmac.new(derived_key, response.content, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(calculated_hmac, response.headers[HMAC_SIGNATURE_HTTP_HEADER]):
            raise RuntimeError('HMACs do not match.')

        provision_data['trust-store'] = response.content.decode()

    @staticmethod
    def generate_new_key(signature_suite: SignatureSuite) -> PrivateKey:
        rsa_public_exponent = 65537
        if signature_suite == SignatureSuite.RSA2048:
            return rsa.generate_private_key(public_exponent=rsa_public_exponent, key_size=2048)
        if signature_suite == SignatureSuite.RSA3072:
            return rsa.generate_private_key(public_exponent=rsa_public_exponent, key_size=3072)
        if signature_suite == SignatureSuite.RSA4096:
            return rsa.generate_private_key(public_exponent=rsa_public_exponent, key_size=4096)
        if signature_suite == SignatureSuite.SECP256R1:
            return ec.generate_private_key(curve=ec.SECP256R1())
        if signature_suite == SignatureSuite.SECP384R1:
            return ec.generate_private_key(curve=ec.SECP384R1())

        raise ValueError('Algorithm not supported.')

    @staticmethod
    def _provision_get_ldevid(provision_data: dict[str, Any], tls_trust_store_path: Path) -> None:
        host = provision_data['host']
        url_extension = provision_data['device']
        otp = provision_data['otp'].encode()
        salt = provision_data['device'].encode()
        port = provision_data['port']
        key = provision_data['crypto-key']

        if provision_data['signature-suite'] == SignatureSuite.SECP384R1:
            hash_algo = hashes.SHA384
        else:
            hash_algo = hashes.SHA256

        csr_builder = x509.CertificateSigningRequestBuilder()
        csr_builder = csr_builder.subject_name(
            x509.Name(
                [
                    x509.NameAttribute(
                        x509.NameOID.COMMON_NAME,'Trustpoint LDevID')]))
        csr = csr_builder.sign(key, hash_algo()).public_bytes(serialization.Encoding.PEM)

        # Let Trustpoint sign our CSR (auth via OTP and salt as username via HTTP basic auth)
        files = {'ldevid.csr': csr}

        ldevid_response = requests.post(
            f'https://{host}:{port}/api/onboarding/ldevid/' + url_extension,
            auth=(salt, otp),
            files=files,
            verify=tls_trust_store_path,
            timeout=10
        )
        if ldevid_response.status_code != 200:
            error_message = 'Server returned HTTP code ' + str(ldevid_response.status_code)
            raise ValueError(error_message)

        provision_data['ldevid'] = ldevid_response.content.decode()

    @staticmethod
    def _provision_get_ldevid_chain(provision_data: dict[str, Any], tls_trust_store_path: Path) -> None:
        host = provision_data['host']
        url_extension = provision_data['device']
        port = provision_data['port']

        cert_chain = requests.get(
            f'https://{host}:{port}/api/onboarding/ldevid/cert-chain/{url_extension}',
            verify=tls_trust_store_path,
            # cert=('ldevid.pem', 'ldevid-private-key.pem'),
            timeout=10,
        )
        if cert_chain.status_code != 200:
            exc_msg = 'Server returned HTTP code ' + str(cert_chain.status_code)
            raise ValueError(exc_msg)

        provision_data['ldevid-cert-chain'] = cert_chain.content.decode()

    def _store_ldevid_in_inventory(self, provision_data: dict[str, Any]) -> None:

        ldevid_key_index = self.devid_module.insert_ldevid_key(provision_data['crypto-key'])
        self.devid_module.enable_devid_key(ldevid_key_index)
        ldevid_certificate_index = self.devid_module.insert_ldevid_certificate(provision_data['ldevid'])
        self.devid_module.enable_devid_certificate(ldevid_certificate_index)
        self.devid_module.insert_ldevid_certificate_chain(
            ldevid_certificate_index, provision_data['ldevid-cert-chain'])

        inventory = self.inventory
        ldevid_credential = CredentialModel(
            unique_name='domain-credential',
            certificate_index=ldevid_certificate_index,
            key_index=ldevid_key_index,
            subject=provision_data['ldevid-subject'],
            certificate_type=provision_data['ldevid-certificate-type'],
            not_valid_before=provision_data['ldevid-not-valid-before'],
            not_valid_after=provision_data['ldevid-not-valid-after']
        )

        if provision_data['host'] == 'localhost':
            trustpoint_host = '127.0.0.1'
        else:
            trustpoint_host = provision_data['host']

        domain_config = DomainConfigModel(
            device=provision_data['device'],
            serial_number=provision_data['serial-number'],
            domain=provision_data['domain'],
            trustpoint_host=trustpoint_host,
            trustpoint_port=provision_data['port'],
            signature_suite=provision_data['signature-suite'],
            pki_protocol=provision_data['pki-protocol'],
            tls_trust_store=provision_data['trust-store']
        )

        inventory.domains[provision_data['domain']] = DomainModel(
            domain_config=domain_config,
            ldevid_credential=ldevid_credential,
            credentials={},
            trust_stores={},
        )
        self._store_inventory(inventory)

        if self.default_domain is None:
            self.default_domain = provision_data['domain']

    def provision_manual(
            self,
            trustpoint_host: str,
            trustpoint_port: int,
            pki_protocol: PkiProtocol,
            credential: CredentialSerializer) -> dict:

        cert = credential.credential_certificate.as_crypto()
        err_msg = 'Certificate does not seem to be an LDevID issued by a Trustpoint.'
        try:
            serial = cert.subject.get_attributes_for_oid(x509.NameOID.SERIAL_NUMBER)[0].value
            pseudonym = cert.subject.get_attributes_for_oid(x509.OID_PSEUDONYM)[0].value
            domain = cert.subject.get_attributes_for_oid(x509.OID_DN_QUALIFIER)[0].value.split('.')
            if domain[0].lower() != 'trustpoint':
                raise ValueError(err_msg)
            domain = domain[-1]
        except KeyError as exception:
            raise ValueError(err_msg) from exception

        if domain in self.inventory.domains:
            raise ValueError(f'Domain with unique name {domain} already exists.')

        private_key = credential.credential_private_key.as_crypto()
        cert = credential.credential_certificate.as_crypto()
        cert_chain = credential.additional_certificates.as_crypto()


        ldevid_key_index = self.devid_module.insert_ldevid_key(private_key)
        self.devid_module.enable_devid_key(ldevid_key_index)
        ldevid_certificate_index = self.devid_module.insert_ldevid_certificate(cert)
        self.devid_module.enable_devid_certificate(ldevid_certificate_index)
        self.devid_module.insert_ldevid_certificate_chain(
            ldevid_certificate_index, cert_chain)

        inventory = self.inventory
        ldevid_credential = CredentialModel(
            unique_name='domain-credential',
            certificate_index=ldevid_certificate_index,
            key_index=ldevid_key_index,
            subject=cert.subject.rfc4514_string(),
            certificate_type=CertificateType.LDEVID,
            not_valid_before=cert.not_valid_before_utc,
            not_valid_after=cert.not_valid_after_utc
        )

        if trustpoint_host == 'localhost':
            trustpoint_host = '127.0.0.1'

        domain_config = DomainConfigModel(
            device=pseudonym,
            serial_number=serial,
            domain=domain,
            trustpoint_host=trustpoint_host,
            trustpoint_port=trustpoint_port,
            signature_suite=SignatureSuite.get_signature_suite_by_public_key(cert.public_key()),
            pki_protocol=pki_protocol,
            tls_trust_store='None'
        )

        inventory.domains[domain] = DomainModel(
            domain_config=domain_config,
            ldevid_credential=ldevid_credential,
            credentials={},
            trust_stores={},
        )
        self._store_inventory(inventory)

        if self.default_domain is None:
            self.default_domain = domain

        return {
            'Device': pseudonym,
            'Serial-Number': serial,
            'Host': trustpoint_host,
            'Port': trustpoint_port,
            'PKI-Protocol': pki_protocol.value,
            'Signature-Suite': SignatureSuite.get_signature_suite_by_public_key(cert.public_key()).value,
            'LDevID Subject': cert.subject.rfc4514_string(),
            'LDevID Certificate Type': CertificateType.LDEVID.value,
            'LDevID Not-Valid-Before': cert.not_valid_before_utc,
            'LDevID Not-Valid-After': cert.not_valid_after_utc,
            'LDevID Expires-In': cert.not_valid_after_utc - cert.not_valid_before_utc
        }