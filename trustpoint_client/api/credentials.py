from __future__ import annotations
import subprocess
import re
import secrets

from typing import TYPE_CHECKING

from trustpoint_devid_module.serializer import (
    CertificateSerializer,
    CertificateCollectionSerializer,
    PublicKeySerializer,
    PrivateKeySerializer,
    CredentialSerializer
)
from cryptography.hazmat.primitives import serialization
from trustpoint_client.enums import (
    CertificateFormat,
    CertificateCollectionFormat,
    PublicKeyFormat,
    PrivateKeyFormat
)

from pathlib import Path

from trustpoint_client.api.schema import CertificateType
from trustpoint_client.api.schema import PkiProtocol
from trustpoint_client.api.oid import NameOid
from trustpoint_client.api.schema import CredentialModel
from cryptography import x509

if TYPE_CHECKING:
    from trustpoint_client.api.schema import InventoryModel
    from trustpoint_devid_module.service_interface import DevIdModule

class TrustpointClientCredential:

    inventory: InventoryModel
    devid_module: DevIdModule
    default_domain: str

    inventory_file_path: Path
    generate_new_key: callable
    _store_inventory: callable

    def credential_exists(self, domain: str, unique_name: str) -> bool:
        if domain not in self.inventory.domains:
            return False
        if unique_name == 'domain-credential':
            return True
        if unique_name not in self.inventory.domains[domain].credentials:
            return False
        return True

    def domain_exists(self, domain: str) -> bool:
        if domain not in self.inventory.domains:
            return False
        return True

    def _get_domain_header(self, domain: str) -> dict[str, str]:
        return {
            'Domain': domain,
            'Device': self.inventory.domains[domain].domain_config.device,
            'Serial-Number': self.inventory.domains[domain].domain_config.serial_number,
            'Is-Default-Domain': domain == self.default_domain,
            'Signature Suite': self.inventory.domains[domain].domain_config.signature_suite.value,
            'PKI Protocol': self.inventory.domains[domain].domain_config.pki_protocol.value,
            '# Credentials (excl. LDevID)': len(self.inventory.domains[domain].credentials)
        }

    @staticmethod
    def _get_credential_info(credential: CredentialModel) -> dict[str, str]:
        return {
            'Certificate Subject': credential.subject,
            'Certificate Certificate-Type': credential.certificate_type.value,
            'Certificate Not-Valid-Before': credential.not_valid_before,
            'Certificate Not-Valid-After': credential.not_valid_after,
            'Certificate Expires-in': credential.not_valid_after - credential.not_valid_before
        }

    def _get_verbose_credential_info(self, credential: CredentialModel) -> dict[str, None | str]:

        cert = self.devid_module.inventory.devid_certificates[
            credential.certificate_index
        ].certificate.decode()

        public_key = self.devid_module.inventory.devid_keys[
            credential.key_index
        ].public_key.decode()

        cert_chain = ''.join(cert.decode() for cert in self.devid_module.inventory.devid_certificates[
            credential.certificate_index
        ].certificate_chain)

        return self._get_credential_info(credential) | {
            f'Credential Certificate': cert,
            f'Credential Public-Key': public_key,
            f'Credential Certificate Chain': cert_chain,
        }

    def list_credential(
            self,
            domain: None | str,
            unique_name: None | str,
            verbose: bool) -> dict[str, dict[str, None | str | dict[str, None | str]]]:

        if domain is None and self.default_domain is None:
            raise ValueError('No default domain is configured. Nothing to list.')

        result = {
            'header': self._get_domain_header(domain),
            'credentials': {

            }
        }

        if domain is None:
            domain = self.default_domain

        if unique_name is None:

            if not verbose:
                result['credentials']['domain-credential'] = self._get_credential_info(
                    credential=self.inventory.domains[self.default_domain].ldevid_credential)

                for name, credential in self.inventory.domains[self.default_domain].credentials.items():
                    result['credentials'][name] = self._get_credential_info(credential)
                return result

            else:
                result['credentials']['domain-credential'] = self._get_verbose_credential_info(
                    credential=self.inventory.domains[self.default_domain].ldevid_credential)

                for name, credential in self.inventory.domains[self.default_domain].credentials.items():
                    result['credentials'][name] = self._get_verbose_credential_info(credential)
                return result

        if unique_name == 'domain-credential':
            credential = self.inventory.domains[domain].ldevid_credential
        else:
            credential = self.inventory.domains[domain].credentials[unique_name]

        if not verbose:
            result['credentials'][unique_name] = self._get_credential_info(
                credential=credential
            )
            return result

        result['credentials'][unique_name] = self._get_verbose_credential_info(
            credential=credential
        )
        return result

    def delete_credential(self, domain: None | str, unique_name: str) -> bool:
        if domain is None:
            domain = self.default_domain

        if unique_name == 'domain-credential':
            raise ValueError('The Domain Credential cannot be deleted unless the domain itself is removed.')

        inventory = self.inventory
        domain_model = inventory.domains[domain]
        credential_model = domain_model.credentials[unique_name]
        key_index = credential_model.key_index

        self.devid_module.delete_ldevid_key(key_index)
        del inventory.domains[domain].credentials[unique_name]
        self._store_inventory(inventory)
        return True

    def export_credential_as_pkcs12(
            self, domain: None | str, unique_name: str, password: None | bytes) -> (bytes, bytes):
        if domain is None:
            domain = self.default_domain
        if not self.credential_exists(domain, unique_name):
            raise ValueError(f'Credential {unique_name} does not exist for domain {domain}.')

        if password is None:
            password = secrets.token_urlsafe(12)
        if len(password) < 12:
            raise ValueError('Password must be at least 8 characters.')
        password = password.encode()

        credential = self.inventory.domains[domain].credentials[unique_name]

        cert = self.devid_module.inventory.devid_certificates[
            credential.certificate_index
        ].certificate

        priv_key = self.devid_module.inventory.devid_keys[
            credential.key_index
        ].private_key

        cert_chain = self.devid_module.inventory.devid_certificates[
            credential.certificate_index
        ].certificate_chain

        credential_serializer = CredentialSerializer(credential=(priv_key, cert, cert_chain))

        pkcs12_bytes = credential_serializer.as_pkcs12(password=password, friendly_name=b'')

        return pkcs12_bytes, password

    def export_certificate(self, domain: None | str, unique_name: str, cert_format: CertificateFormat) -> bytes:
        if domain is None:
            domain = self.default_domain
        if not self.credential_exists(domain, unique_name):
            raise ValueError(f'Credential {unique_name} does not exist for domain {domain}.')

        credential = self.inventory.domains[domain].credentials[unique_name]

        cert = self.devid_module.inventory.devid_certificates[
            credential.certificate_index
        ].certificate

        certificate_serializer = CertificateSerializer(cert)

        if cert_format == CertificateFormat.PEM:
            return certificate_serializer.as_pem()
        elif cert_format == CertificateFormat.DER:
            return certificate_serializer.as_pem()
        elif cert_format == CertificateFormat.PKCS7_PEM:
            return certificate_serializer.as_pkcs7_pem()
        elif cert_format == CertificateFormat.PKCS7_DER:
            return certificate_serializer.as_pkcs7_der()
        else:
            raise ValueError(f'Certificate format {cert_format.value} is not supported.')

    def export_certificate_chain(
            self,
            domain: None | str,
            unique_name: str,
            cert_chain_format: CertificateCollectionFormat) -> bytes:

        if domain is None:
            domain = self.default_domain
        if not self.credential_exists(domain, unique_name):
            raise ValueError(f'Credential {unique_name} does not exist for domain {domain}.')

        credential = self.inventory.domains[domain].credentials[unique_name]

        cert = self.devid_module.inventory.devid_certificates[
            credential.certificate_index
        ].certificate_chain

        certificate_collection_serializer = CertificateCollectionSerializer(cert)

        if cert_chain_format == CertificateCollectionFormat.PEM:
            return certificate_collection_serializer.as_pem()
        elif cert_chain_format == CertificateCollectionFormat.PKCS7_PEM:
            return certificate_collection_serializer.as_pkcs7_pem()
        elif cert_chain_format == CertificateCollectionFormat.PKCS7_DER:
            return certificate_collection_serializer.as_pkcs7_der()
        else:
            raise ValueError(f'Certificate chain format {cert_chain_format.value} is not supported.')

    def export_public_key(
            self,
            domain: None | str,
            unique_name: str,
            public_key_format: PublicKeyFormat) -> bytes:

        if domain is None:
            domain = self.default_domain
        if not self.credential_exists(domain, unique_name):
            raise ValueError(f'Credential {unique_name} does not exist for domain {domain}.')

        credential = self.inventory.domains[domain].credentials[unique_name]

        pub_key = self.devid_module.inventory.devid_keys[
            credential.key_index
        ].public_key

        public_key_serializer = PublicKeySerializer(pub_key)

        if public_key_format == PublicKeyFormat.PEM:
            return public_key_serializer.as_pem()
        elif public_key_format == PublicKeyFormat.DER:
            return public_key_serializer.as_der()
        else:
            raise ValueError(f'Public key format {public_key_format.value} is not supported.')

    def export_private_key(
            self,
            domain: None | str,
            unique_name: str,
            password: None | bytes,
            private_key_format: PrivateKeyFormat) -> (bytes, bytes):

        if domain is None:
            domain = self.default_domain
        if not self.credential_exists(domain, unique_name):
            raise ValueError(f'Credential {unique_name} does not exist for domain {domain}.')

        if password is None:
            password = secrets.token_urlsafe(12)
        if len(password) < 12:
            raise ValueError('Password must be at least 8 characters.')
        password = password.encode()

        credential = self.inventory.domains[domain].credentials[unique_name]

        priv_key = self.devid_module.inventory.devid_keys[
            credential.key_index
        ].private_key

        private_key_serializer = PrivateKeySerializer(priv_key)
        if private_key_format == PrivateKeyFormat.PKCS1_PEM:
            return private_key_serializer.as_pkcs1_pem(password=password), password
        elif private_key_format == PrivateKeyFormat.PKCS8_PEM:
            return private_key_serializer.as_pkcs8_pem(password=password), password
        elif private_key_format == PrivateKeyFormat.PKCS8_DER:
            return private_key_serializer.as_pkcs8_der(password=password), password
        elif private_key_format == PrivateKeyFormat.PKCS12:
            return private_key_serializer.as_pkcs12(password=password, friendly_name=b''), password
        else:
            raise ValueError(f'Private key format {private_key_format.value} is not supported.')

    def request_generic(self, domain: None | str, unique_name: str, subject: list[str]) -> None:
        if domain is None:
            domain = self.default_domain
        if not self.domain_exists(domain):
            raise ValueError(f'Domain {domain} does not exist.')

        if unique_name in self.inventory.domains[domain].credentials:
            raise ValueError(f'Credentials with unique name {unique_name} already exists for domain {domain}.')

        pattern = re.compile(r'^[a-zA-Z]+[a-zA-Z0-9_-]+$')
        match = pattern.match(unique_name)
        if match is None:
            raise ValueError(
                'The unique name must start with a letter and '
                'must only contain letters, digits, underscores and hyphens.\n')

        if self.inventory.domains[domain].domain_config.pki_protocol == PkiProtocol.CMP:
            return self._request_generic_via_cmp(domain, unique_name, subject)

    def _request_generic_via_cmp(self, domain: str, unique_name: str, subject: list[str]) -> None:
        inventory = self.inventory
        inventory_domain = inventory.domains[domain]

        subject_entries = {}
        for entry in subject:
            attribute_type, attribute_value = entry.split(':', 1)
            name_oid = NameOid.get_by_name(attribute_type)
            if name_oid is None:
                pattern = re.compile(r'^([0-2])((\.0)|(\.[1-9][0-9]*))*$')
                match = pattern.match(attribute_type)
                if match is None:
                    raise ValueError(f'Found an invalid subject attribute type: {attribute_type}.')
                oid = attribute_type
            else:
                oid = name_oid.dotted_string
            subject_entries[oid] = attribute_value

        # The serial number of the device is not included by default, due to possible privacy concerns.
        subject_cmp_str = f'/2.5.4.65=trustpoint.generic-cert.{domain}.{unique_name}'
        for key, value in subject_entries.items():
            subject_cmp_str += f'/{key}={value}'

        key_index = inventory_domain.ldevid_credential.key_index
        cert_index = inventory_domain.ldevid_credential.certificate_index

        key = self.devid_module.inventory.devid_keys[key_index].private_key
        cert = self.devid_module.inventory.devid_certificates[cert_index].certificate

        key_path = self.inventory_file_path.parent / 'key.pem'
        cert_path = self.inventory_file_path.parent / 'cert.pem'

        key_path.write_bytes(key)
        cert_path.write_bytes(cert)

        new_key_path = self.inventory_file_path.parent / 'new_key.pem'
        new_cert_path = self.inventory_file_path.parent / 'new_cert.pem'
        new_cert_chain_path = self.inventory_file_path.parent / 'new_cert_chain.pem'

        new_private_key = self.generate_new_key(inventory_domain.domain_config.signature_suite)
        new_key_path.write_bytes(
            new_private_key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.TraditionalOpenSSL,
                encryption_algorithm=serialization.NoEncryption()
            ))

        trustpoint_host = inventory_domain.domain_config.trustpoint_host
        trustpoint_port = inventory_domain.domain_config.trustpoint_port

        cmd = (
            f'openssl cmp '
            f'-cmd ir '
            f'-server https://{trustpoint_host}:{trustpoint_port} '
            f'-path /.well-known/cmp/p/{domain}/initialization/ '
            f'-newkey {new_key_path} '
            f'-key {key_path} '
            f'-cert {cert_path} '
            f'-certout {new_cert_path} '
            f'-chainout {new_cert_chain_path} '
            f'-implicit_confirm -disable_confirm '
            f'-unprotected_errors '
            f'-tls_used '
            f'-subject {subject_cmp_str}'
        )

        try:
            subprocess.check_output(cmd, shell=True, stderr=subprocess.STDOUT)
        except subprocess.CalledProcessError as exception_:
            raise ValueError(f'CMP request failed: {exception_}')

        enrolled_private_key = new_key_path.read_bytes()
        enrolled_cert = new_cert_path.read_bytes()
        enrolled_cert_chain = new_cert_chain_path.read_bytes()

        loaded_cert = x509.load_pem_x509_certificate(enrolled_cert)
        enrolled_subject = loaded_cert.subject.rfc4514_string()

        new_key_index = self.devid_module.insert_ldevid_key(enrolled_private_key)
        self.devid_module.enable_devid_key(new_key_index)

        new_cert_index = self.devid_module.insert_ldevid_certificate(enrolled_cert)
        self.devid_module.enable_devid_certificate(new_cert_index)

        self.devid_module.insert_ldevid_certificate_chain(new_cert_index, enrolled_cert_chain)

        new_credential = CredentialModel(
            unique_name=unique_name,
            certificate_index=new_cert_index,
            key_index=new_key_index,
            subject=enrolled_subject,
            certificate_type=CertificateType.GENERIC,
            not_valid_before=loaded_cert.not_valid_before_utc,
            not_valid_after=loaded_cert.not_valid_after_utc
        )

        inventory_domain.credentials[unique_name] = new_credential
        self._store_inventory(inventory)
