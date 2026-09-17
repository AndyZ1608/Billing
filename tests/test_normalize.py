import copy
from datetime import UTC
from uuid import UUID

import pytest
from openstack.block_storage.v3.volume import Volume
from openstack.compute.v2.server import Server
from openstack.identity.v3.project import Project

from app.openstack.normalize import MalformedResource, normalize_instance, normalize_project, normalize_volume
from tests.fakes import FakeClient


def test_sdk_instances_and_volume_boot():
    fake = FakeClient()
    instance = normalize_instance(Server(**fake.data["instances"][2]), fake.flavor)
    assert instance["vcpus"] == 8
    assert instance["root_disk_gb"] == 0
    assert instance["ephemeral_disk_gb"] == 20
    assert instance["boot_source"] == "volume"
    assert instance["created_at_openstack"].tzinfo == UTC
    assert instance["quality_issues"] == []


def test_deleted_flavor_retains_unknown_dimensions():
    fake = FakeClient()
    vm = copy.deepcopy(fake.data["instances"][0])
    vm["flavor"] = {"id": "deleted"}
    result = normalize_instance(vm, fake.flavor)
    assert result["vcpus"] is None
    assert result["ram_mb"] is None
    assert "flavor_unavailable" in result["quality_issues"]


def test_embedded_flavor_does_not_require_catalog_lookup():
    fake = FakeClient()
    vm = fake.data["instances"][0]
    del vm["flavor"]["id"]  # Nova >= 2.47 may omit flavor UUID.
    result = normalize_instance(vm, lambda _: pytest.fail("unnecessary lookup"))
    assert result["vcpus"] == 2
    assert result["flavor_id"] is None
    assert result["flavor_name"] == "Small"


def test_legacy_flavor_and_invalid_timestamp():
    fake = FakeClient()
    vm = fake.data["instances"][0]
    vm.update(flavor={"id": "legacy"}, created_at="bad date")
    result = normalize_instance(
        vm, lambda _: {"vcpus": 2, "ram": 512, "disk": 10, "OS-FLV-EXT-DATA:ephemeral": 0, "name": "legacy"}
    )
    assert result["ram_mb"] == 512
    assert result["ephemeral_disk_gb"] == 0
    assert result["created_at_openstack"] is None
    assert "invalid_timestamp" in result["quality_issues"]


def test_unknown_boot_source_is_not_guessed_from_attachment():
    fake = FakeClient()
    vm = fake.data["instances"][0]
    del vm["image"]
    vm["attached_volumes"] = [{"id": "some-data-volume"}]
    result = normalize_instance(vm, fake.flavor)
    assert result["boot_source"] == "unknown"
    assert result["root_disk_gb"] is None


def test_sdk_volume_aliases_and_false_bootable():
    raw = FakeClient().data["volumes"][0]
    raw["os-vol-tenant-attr:tenant_id"] = raw.pop("project_id")
    result = normalize_volume(Volume(**raw))
    assert result["project_id"] == UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
    assert result["bootable"] is False


def test_identity_hex_uuid_and_rename():
    result = normalize_project(Project(id="a" * 32, name="Renamed", is_enabled=False))
    assert result["project_id"] == UUID("a" * 32)
    assert result["enabled"] is False
    assert result["project_name"] == "Renamed"


@pytest.mark.parametrize("value", [None, -1, 0.5, "garbage", True, 2**40])
def test_bad_dimensions_are_unknown(value):
    raw = FakeClient().data["volumes"][0]
    raw["size"] = value
    result = normalize_volume(raw)
    assert result["size_gb"] is None
    assert "unknown_size" in result["quality_issues"]


def test_missing_identity_is_rejected():
    with pytest.raises(MalformedResource):
        normalize_volume({"id": "bad", "project_id": "also-bad"})
