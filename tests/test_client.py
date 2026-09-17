from unittest.mock import MagicMock, patch

import pytest

from app.core.config import Settings
from app.openstack.client import ConfigurationError, OpenStackClient


@pytest.mark.parametrize("application_credential", [False, True])
def test_authentication_and_all_project_sdk_calls(application_credential):
    settings = Settings(
        _env_file=None,
        os_auth_url="https://keystone.example/v3",
        os_username="reader",
        os_password="fixture-secret",
        os_project_name="admin",
        os_application_credential_id="credential-id" if application_credential else "",
        os_application_credential_secret="app-secret" if application_credential else "",
    )
    with patch("app.openstack.client.Connection") as connection:
        sdk = MagicMock()
        connection.return_value = sdk
        client = OpenStackClient(settings)
        client.authenticate()
        client.projects()
        client.instances()
        client.volumes()
        sdk.authorize.assert_called_once()
        sdk.identity.projects.assert_called_once_with()
        sdk.compute.servers.assert_called_once_with(details=True, all_projects=True)
        sdk.block_storage.volumes.assert_called_once_with(details=True, all_projects=True)
        auth_session = connection.call_args.kwargs["session"]
        assert auth_session.verify is True
        assert auth_session.timeout == 30
        assert type(auth_session.auth).__name__ == (
            "ApplicationCredential" if application_credential else "Password"
        )
        assert "fixture-secret" not in repr(settings)


def test_missing_credentials_have_safe_error():
    with pytest.raises(ConfigurationError) as error:
        OpenStackClient(Settings(_env_file=None, os_auth_url="")).authenticate()
    assert str(error.value) == ""
