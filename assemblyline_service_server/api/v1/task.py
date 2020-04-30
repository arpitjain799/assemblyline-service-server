import time
from typing import cast, Dict, Any

from assemblyline.common.dict_utils import flatten, unflatten

from assemblyline.common.isotime import now_as_iso
from flask import request

from assemblyline.common import forge
from assemblyline.common.attack_map import attack_map
from assemblyline.common.constants import SERVICE_STATE_HASH, ServiceStatus
from assemblyline.common.forge import CachedObject
from assemblyline.odm import construct_safe
from assemblyline.odm.messages.service_heartbeat import Metrics
from assemblyline.odm.messages.task import Task as ServiceTask
from assemblyline.odm.models.error import Error
from assemblyline.odm.models.heuristic import Heuristic
from assemblyline.odm.models.result import Result
from assemblyline.odm.models.tagging import Tagging
from assemblyline.remote.datatypes.exporting_counter import export_metrics_once
from assemblyline.remote.datatypes.hash import ExpiringHash
from assemblyline_core.dispatching.client import DispatchClient
from assemblyline_service_server.api.base import make_subapi_blueprint, make_api_response, api_login
from assemblyline_service_server.config import LOGGER, STORAGE, config
from assemblyline_service_server.helper.heuristics import get_heuristics

status_table = ExpiringHash(SERVICE_STATE_HASH, ttl=60*30)
dispatch_client = DispatchClient(STORAGE)
heuristics = cast(Dict[str, Heuristic], CachedObject(get_heuristics, refresh=300))
tag_whitelister = forge.get_tag_whitelister(log=LOGGER)

SUB_API = 'task'
task_api = make_subapi_blueprint(SUB_API, api_version=1)
task_api._doc = "Perform operations on service tasks"


@task_api.route("/", methods=["GET"])
@api_login()
def get_task(client_info):
    """

    Header:
    {'container_id': abcd...123
     'service_name': 'Extract',
     'service_version': '4.0.1',
     'service_tool_version': '
     'timeout': '30'

    }

    Result example:
    {'keep_alive': true}

    """
    service_name = client_info['service_name']
    service_version = client_info['service_version']
    client_id = client_info['client_id']
    timeout = int(float(request.headers.get('timeout', 30)))
    # Add a little extra to the status timeout so that the service has a chance to retry before we start to
    # suspect it of slacking off
    status_table.set(client_id, (service_name, ServiceStatus.Idle, time.time() + timeout + 5))

    cache_miss = False

    task = dispatch_client.request_work(client_id, service_name, service_version, timeout=timeout)

    if not task:
        # No task found in service queue
        return make_api_response(dict(task=False))

    try:
        result_key = Result.help_build_key(sha256=task.fileinfo.sha256,
                                           service_name=service_name,
                                           service_version=service_version,
                                           service_tool_version=client_info['service_tool_version'],
                                           is_empty=False,
                                           task=task)
        service_data = dispatch_client.service_data[service_name]

        # If we are allowed, try to see if the result has been cached
        if not task.ignore_cache and not service_data.disable_cache:
            result = STORAGE.result.get_if_exists(result_key)
            if result:
                dispatch_client.service_finished(task.sid, result_key, result)
                return make_api_response(dict(task=False))

            result = STORAGE.emptyresult.get_if_exists(f"{result_key}.e")
            if result:
                result = STORAGE.create_empty_result_from_key(result_key)
                dispatch_client.service_finished(task.sid, f"{result_key}.e", result)
                return make_api_response(dict(task=False))

            # No luck with the cache, lets dispatch the task to a client
            cache_miss = True

        status_table.set(client_id, (service_name, ServiceStatus.Running, time.time() + service_data.timeout))
        return make_api_response(dict(task=task.as_primitives()))
    finally:
        export_metrics_once(service_name, Metrics, dict(execute=1, cache_miss=int(cache_miss)),
                            host=client_id, counter_type='service')


@task_api.route("/", methods=["POST"])
@api_login()
def task_finished(client_info):
    """
    Header:
    {'client_id': 'abcd...123',
    }


    Data Block:
    {'exec_time': 300,
     'task': {},
     'result': ''
    }
    """
    data = request.json
    exec_time = data.get('exec_time')

    try:
        task = ServiceTask(data['task'])

        if 'result' in data:  # Task created a result
            result = data['result']
            missing_files = handle_task_result(exec_time, task, result, client_info)
            if missing_files:
                return make_api_response(dict(success=False, missing_files=missing_files))
            return make_api_response(dict(success=True))

        elif 'error' in data:  # Task created an error
            error = data['error']
            handle_task_error(exec_time, task, error, client_info)
            return make_api_response(dict(success=True))
        else:
            return make_api_response("", "No result or error provided by service.", 400)

    except ValueError as e:  # Catch errors when building Task or Result model
        return make_api_response("", e, 400)


class InvalidHeuristicException(Exception):
    pass


class Heuristic(object):
    def __init__(self, heur_id, attack_ids, signatures, frequency):
        # Validate heuristic
        definition = heuristics.get(heur_id)
        if not definition:
            raise InvalidHeuristicException(f"Heuristic with ID '{heur_id}' does not exist, skipping...")

        # Set defaults
        self.heur_id = heur_id
        self.attack_ids = []
        self.score = 0
        self.name = definition.name
        self.classification = definition.classification

        # Show only attack_ids that are valid
        attack_ids = attack_ids or []
        for a_id in attack_ids:
            if a_id in set(attack_map.keys()):
                self.attack_ids.append(a_id)
            else:
                LOGGER.warning(f"Invalid attack_id '{a_id}' for heuristic '{heur_id}'. Ignoring it.")

        # Calculate the score for the signatures
        self.signatures = signatures or {}
        for sig_name, freq in signatures.items():
            if sig_name in definition.signature_score_map:
                self.score += definition.signature_score_map[sig_name] * freq
            else:
                self.score += definition.score * freq

        # Calculate the score for the heuristic frequency
        self.score += definition.score * frequency

        # Check scoring boundaries
        self.score = max(definition.score, self.score)
        if definition.max_score:
            self.score = min(self.score, definition.max_score)


def handle_task_result(exec_time: int, task: ServiceTask, result: Dict[str, Any], client_info: Dict[str, str]):
    service_name = client_info['service_name']
    client_id = client_info['client_id']

    # Add scores to the heuristics, if any section set a heuristic
    total_score = 0
    for section in result['result']['sections']:
        if section.get('heuristic'):
            heur_id = f"{client_info['service_name'].upper()}.{str(section['heuristic']['heur_id'])}"
            attack_ids = section['heuristic'].pop('attack_ids', [])
            signatures = section['heuristic'].pop('signatures', [])
            frequency = section['heuristic'].pop('frequency', 0)

            try:
                # Validate the heuristic and recalculate its score
                heuristic = Heuristic(heur_id, attack_ids, signatures, frequency)

                # Assign the newly computed heuristic to the section
                section['heuristic'] = dict(
                    heur_id=heur_id,
                    score=heuristic.score,
                    name=heuristic.name,
                    attack=[],
                    signature=[]
                )
                total_score += heuristic.score

                # Assign the multiple attack IDs to the heuristic
                for attack_id in heuristic.attack_ids:
                    attack_item = dict(
                        attack_id=attack_id,
                        pattern=attack_map[attack_id]['name'],
                        categories=attack_map[attack_id]['categories']
                    )
                    section['heuristic']['attack'].append(attack_item)

                # Assign the multiple signatures to the heuristic
                for sig_name, freq in heuristic.signatures.items():
                    signature_item = dict(
                        name=sig_name,
                        frequency=freq
                    )
                    section['heuristic']['signature'].append(signature_item)
            except InvalidHeuristicException as e:
                section['heuristic'] = None
                LOGGER.warning(str(e))

    # Update the total score of the result
    result['result']['score'] = total_score

    # Add timestamps for creation, archive and expiry
    result['created'] = now_as_iso()
    result['archive_ts'] = now_as_iso(config.datastore.ilm.days_until_archive * 24 * 60 * 60)
    if task.ttl:
        result['expiry_ts'] = now_as_iso(task.ttl * 24 * 60 * 60)

    # Pop the temporary submission data
    temp_submission_data = result.pop('temp_submission_data', None)

    # Process the tag values
    for section in result['result']['sections']:
        # Perform tag whitelisting
        section['tags'] = unflatten(tag_whitelister.get_validated_tag_map(flatten(section['tags'])))

        section['tags'], dropped = construct_safe(Tagging, section.get('tags', {}))

        if dropped:
            LOGGER.warning(f"[{task.sid}] Invalid tag data from {client_info['service_name']}: {dropped}")

    result = Result(result)

    with forge.get_filestore() as f_transport:
        missing_files = []
        for file in (result.response.extracted + result.response.supplementary):
            if STORAGE.file.get_if_exists(file.sha256) is None or not f_transport.exists(file.sha256):
                missing_files.append(file.sha256)
        if missing_files:
            return missing_files

    result_key = result.build_key(service_tool_version=result.response.service_tool_version, task=task)
    dispatch_client.service_finished(task.sid, result_key, result, temp_submission_data)

    # Metrics

    if result.result.score > 0:
        export_metrics_once(service_name, Metrics, dict(scored=1), host=client_id, counter_type='service')
    else:
        export_metrics_once(service_name, Metrics, dict(not_scored=1), host=client_id, counter_type='service')

    LOGGER.info(f"[{task.sid}] {client_info['client_id']} - {client_info['service_name']} "
                f"successfully completed task {f' in {exec_time}ms' if exec_time else ''}")


def handle_task_error(exec_time: int, task: ServiceTask, error: Dict[str, Any], client_info: Dict[str, str]) -> None:
    service_name = client_info['service_name']
    client_id = client_info['client_id']

    LOGGER.info(f"[{task.sid}] {client_info['client_id']} - {client_info['service_name']} "
                f"failed to complete task {f' in {exec_time}ms' if exec_time else ''}")

    # Add timestamps for creation, archive and expiry
    error['created'] = now_as_iso()
    error['archive_ts'] = now_as_iso(config.datastore.ilm.days_until_archive * 24 * 60 * 60)
    if task.ttl:
        error['expiry_ts'] = now_as_iso(task.ttl * 24 * 60 * 60)

    error = Error(error)
    error_key = error.build_key(service_tool_version=error.response.service_tool_version, task=task)
    dispatch_client.service_failed(task.sid, error_key, error)

    # Metrics
    if error.response.status == 'FAIL_RECOVERABLE':
        export_metrics_once(service_name, Metrics, dict(fail_recoverable=1), host=client_id, counter_type='service')
    else:
        export_metrics_once(service_name, Metrics, dict(fail_nonrecoverable=1), host=client_id, counter_type='service')
