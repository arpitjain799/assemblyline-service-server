from contextlib import contextmanager

import hashlib
from unittest.mock import patch, MagicMock

import pytest
from assemblyline.odm.models.heuristic import Heuristic

from assemblyline.odm.randomizer import random_minimal_obj

from assemblyline.odm.models.service import Service

from assemblyline.common import forge
from assemblyline.odm import randomizer
from assemblyline_service_server.config import AUTH_KEY
from assemblyline_service_server import app


headers = {
    'Container-Id': randomizer.get_random_hash(12),
    'X-APIKey': AUTH_KEY,
    # 'Service-Name': randomizer.get_random_service_name(),
    # 'Service-Version': randomizer.get_random_service_version(),
    'Service-Tool-Version': randomizer.get_random_hash(64),
    'X-Forwarded-For': '127.0.0.1',
}


@pytest.fixture()
def client():
    client = app.app.test_client()
    yield client


@pytest.fixture(scope='function')
def storage():
    ds = MagicMock()
    with patch('assemblyline_service_server.api.v1.service.STORAGE', ds):
        yield ds


def test_register_existing_service(client, storage):
    service = random_minimal_obj(Service)

    headers['Service-Name'] = service.name
    headers['Service-Version'] = service.version

    result = client.post("/api/v1/service/register/", headers=headers, json=service.as_primitives())
    assert result.ok
    assert storage.heuristic.save.call_count == 0

    assert result.json['api_response']['keep_alive'] is True
    assert len(result.json['api_response']['new_heuristics']) == 0


def test_register_bad_service(client, storage):
    service = random_minimal_obj(Service).as_primitives()

    headers['Service-Name'] = service['name']
    headers['Service-Version'] = service['version']
    del service['name']

    result = client.post("/api/v1/service/register/", headers=headers, json=service)
    assert not result.ok


def test_register_new_service(client, storage):
    storage.service.get_if_exists.return_value = False
    storage.service_delta.get_if_exists.return_value = False

    service = random_minimal_obj(Service)

    headers['Service-Name'] = service.name
    headers['Service-Version'] = service.version

    result = client.post("/api/v1/service/register/", headers=headers, json=service.as_primitives())
    assert result.ok
    assert storage.heuristic.save.call_count == 0
    assert storage.service.save.call_count == 1
    assert storage.service_delta.save.call_count == 1

    assert result.json['api_response']['keep_alive'] is False
    assert len(result.json['api_response']['new_heuristics']) == 0



def test_register_new_service_version(client, storage):
    storage.service_delta.get_if_exists.return_value = False

    service = random_minimal_obj(Service)

    headers['Service-Name'] = service.name
    headers['Service-Version'] = service.version

    result = client.post("/api/v1/service/register/", headers=headers, json=service.as_primitives())
    assert result.ok
    assert storage.heuristic.save.call_count == 0
    assert storage.service.save.call_count == 0
    assert storage.service_delta.save.call_count == 1

    assert result.json['api_response']['keep_alive'] is True
    assert len(result.json['api_response']['new_heuristics']) == 0


def test_register_new_heuristics(client, storage):
    storage.heuristic.get_if_exists.return_value = None

    service = random_minimal_obj(Service)
    service = service.as_primitives()
    service['heuristics'] = [random_minimal_obj(Heuristic).as_primitives()]

    headers['Service-Name'] = service['name']
    headers['Service-Version'] = service['version']

    result = client.post("/api/v1/service/register/", headers=headers, json=service)
    assert result.ok
    assert storage.heuristic.save.call_count == 1

    assert result.json['api_response']['keep_alive'] is True
    assert len(result.json['api_response']['new_heuristics']) == 1
    assert service['heuristics'][0]['heur_id'] in result.json['api_response']['new_heuristics'][0]


def test_register_existing_heuristics(client, storage):
    service = random_minimal_obj(Service)
    service = service.as_primitives()
    service['heuristics'] = [random_minimal_obj(Heuristic).as_primitives()]

    headers['Service-Name'] = service['name']
    headers['Service-Version'] = service['version']

    result = client.post("/api/v1/service/register/", headers=headers, json=service)
    assert result.ok
    assert storage.heuristic.save.call_count == 0

    assert result.json['api_response']['keep_alive'] is True
    assert len(result.json['api_response']['new_heuristics']) == 0


def test_register_bad_heuristics(client, storage):
    service = random_minimal_obj(Service)
    service = service.as_primitives()
    service['heuristics'] = [random_minimal_obj(Heuristic).as_primitives()]
    service['heuristics'][0]['description'] = None

    headers['Service-Name'] = service['name']
    headers['Service-Version'] = service['version']

    result = client.post("/api/v1/service/register/", headers=headers, json=service)
    assert not result.ok
