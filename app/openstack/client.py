from typing import Protocol

from keystoneauth1 import session
from keystoneauth1.identity import v3
from openstack.connection import Connection

from app.core.config import Settings


class ConfigurationError(Exception):
    pass


class InventoryClient(Protocol):
    def authenticate(self): ...
    def projects(self): ...
    def instances(self): ...
    def volumes(self): ...
    def flavor(self, flavor_id: str): ...
    def close(self): ...


class OpenStackClient:
    """Read-only SDK boundary. Explicit sessions avoid implicit clouds.yaml credentials."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.connection = None

    def authenticate(self):
        s = self.settings
        if not s.os_auth_url:
            raise ConfigurationError()
        if s.os_application_credential_id or s.os_application_credential_secret.get_secret_value():
            if (
                not s.os_application_credential_id
                or not s.os_application_credential_secret.get_secret_value()
            ):
                raise ConfigurationError()
            auth = v3.ApplicationCredential(
                auth_url=s.os_auth_url,
                application_credential_id=s.os_application_credential_id,
                application_credential_secret=s.os_application_credential_secret.get_secret_value(),
            )
        else:
            if not all((s.os_username, s.os_password.get_secret_value(), s.os_project_name)):
                raise ConfigurationError()
            auth = v3.Password(
                auth_url=s.os_auth_url,
                username=s.os_username,
                password=s.os_password.get_secret_value(),
                project_name=s.os_project_name,
                user_domain_name=s.os_user_domain_name,
                project_domain_name=s.os_project_domain_name,
            )
        auth_session = session.Session(
            auth=auth, verify=s.os_cacert or True, timeout=s.os_api_timeout_seconds
        )
        self.connection = Connection(
            session=auth_session,
            region_name=s.os_region_name,
            interface=s.os_interface,
            compute_api_version=s.os_compute_api_version,
            block_storage_api_version="3",
            identity_api_version="3",
            connect_retries=1,
            status_code_retries=1,
        )
        self.connection.authorize()

    def projects(self):
        return self.connection.identity.projects()

    def instances(self):
        # Generators traverse SDK pagination; consumers must exhaust successfully.
        return self.connection.compute.servers(details=True, all_projects=True)

    def volumes(self):
        return self.connection.block_storage.volumes(details=True, all_projects=True)

    def flavor(self, flavor_id):
        return self.connection.compute.get_flavor(flavor_id)

    def close(self):
        if self.connection:
            self.connection.close()
