# SPDX-License-Identifier: LGPL-2.1-or-later
#
# Copyright (C) 2019 Linaro Limited
# Author: Dan Rue <dan.rue@linaro.org>
#
# Copyright (C) 2019, 2021-2023 Collabora Limited
# Author: Guillaume Tucker <guillaume.tucker@collabora.com>
# Author: Michal Galka <michal.galka@collabora.com>

"""LAVA runtime implementation"""

from collections import namedtuple
import time
from urllib.parse import urljoin

import requests
import yaml

from kernelci.runtime import Runtime


# This will go away when adding get_html_log()
# pylint: disable=too-few-public-methods
class LogParser:
    """LAVA log parser

    This class can be used to parse LAVA logs as received in a callback, in
    YAML format via *log_data_yaml*.  It can then produce a plain text version
    with just the serial output from the test platform.
    """

    def __init__(self, log_data_yaml):
        self._raw_log = self._get_raw_log(log_data_yaml)

    @classmethod
    def _get_raw_log(cls, log_data_yaml):
        log = yaml.safe_load(log_data_yaml)
        raw_log = []
        for line in log:
            dtime, level, msg = (line.get(key) for key in ['dt', 'lvl', 'msg'])
            if not isinstance(msg, str):
                continue
            msg = msg.strip().replace('\x1b', '^[')
            if msg:
                raw_log.append((dtime, level, msg))
        return raw_log

    def get_text_log(self, output):
        """Get the plain text serial console output log from the plaform"""
        for _, level, msg in self._raw_log:
            if level == 'target':
                output.write(msg)
                output.write('\n')


class Callback:
    """LAVA callback handler"""

    # copied from lava-server/lava_scheduler_app/models.py
    SUBMITTED = 0
    RUNNING = 1
    COMPLETE = 2
    INCOMPLETE = 3
    CANCELED = 4
    CANCELING = 5

    # LAVA job result names
    LAVA_JOB_RESULT_NAMES = {
        COMPLETE: "pass",
        INCOMPLETE: "fail",
        CANCELED: "incomplete",
        CANCELING: "incomplete",
    }

    def __init__(self, data):
        """This class can be used to parse LAVA callback data"""
        self._data = data
        self._meta = None

    def get_data(self):
        """Get the raw callback data"""
        return self._data

    def get_device_id(self):
        """Get the ID of the tested device"""
        return self._data.get('actual_device_id')

    def get_meta(self, key):
        """Get a metadata value from the job definition"""
        if self._meta is None:
            self._meta = yaml.safe_load(self._data['definition'])['metadata']
        return self._meta.get(key)

    def get_job_status(self):
        """Get the job status"""
        # map over LAVA_JOB_RESULT_NAMES
        return self.LAVA_JOB_RESULT_NAMES.get(self._data['status'])

    def is_infra_error(self):
        """Determine wether the job has hit an infrastructure error"""
        lava_yaml = self._data['results']['lava']
        lava = yaml.safe_load(lava_yaml)
        stages = {stage['name']: stage for stage in lava}
        job_meta = stages['job']['metadata']
        return job_meta.get('error_type') == "Infrastructure"

    def _get_job_failure_metadata(self):
        """Get failed lava job metadata fields such as error type and
        error message"""
        lava_yaml = self._data['results']['lava']
        lava = yaml.safe_load(lava_yaml)
        stages = {stage['name']: stage for stage in lava}
        job_meta = stages.get('job', {}).get('metadata')
        return job_meta

    @classmethod
    def _get_login_case(cls, tests):
        tests_map = {test['name']: test for test in tests}
        login = (
            tests_map.get('auto-login-action') or tests_map.get('login-action')
        )
        result = login and login['result'] == 'pass'
        return 'pass' if result else 'fail'

    @classmethod
    def _get_kernelmsg_case(cls, tests):
        tests_map = {test['name']: test for test in tests}
        kernelmsg = tests_map.get('kernel-messages')
        result = kernelmsg and kernelmsg['result'] == 'pass'
        return 'pass' if result else 'fail'

    @classmethod
    def _get_suite_results(cls, tests):
        suite_results = {}
        for test in reversed(tests):
            test_set_name = test['metadata'].get('set')
            if test_set_name:
                test_cases = suite_results.setdefault(test_set_name, {})
            else:
                test_cases = suite_results
            test_cases[test['name']] = test['result']
        return suite_results

    def get_results(self):
        """Parse the results and return them as a plain dictionary"""
        results = {}
        for suite_name, suite_results in self._data['results'].items():
            tests = yaml.safe_load(suite_results)
            if suite_name == 'lava':
                results['login'] = self._get_login_case(tests)
                results['kernelmsg'] = self._get_kernelmsg_case(tests)
            else:
                suite_name = suite_name.partition("_")[2]
                results[suite_name] = self._get_suite_results(tests)
        return results

    def _get_stage_result(self, suite_name):
        lava_yaml = self._data['results']['lava']
        lava = yaml.safe_load(lava_yaml)
        stages = {stage['name']: stage for stage in lava}
        result = None
        for stage_name, stage_results in stages.items():
            stage_name = stage_name.partition("_")[2]
            if stage_name == suite_name:
                result = stage_results['result']
        return result

    def _get_results_hierarchy(self, results):
        hierarchy = []
        for name, value in results.items():
            node = {'name': name}
            child_nodes = []
            item = {'node': node, 'child_nodes': child_nodes}
            if isinstance(value, dict):
                item['child_nodes'] = self._get_results_hierarchy(value)
                node['result'] = self._get_stage_result(node['name'])
            elif isinstance(value, str):
                node['result'] = value
            hierarchy.append(item)
        return hierarchy

    def get_hierarchy(self, results, job_node):
        """Convert the plain results dictionary to a hierarchy for the API"""
        job_result = job_node['result']
        if job_result == "fail":
            job_meta = self._get_job_failure_metadata()
            if job_meta:
                job_node['data']['error_code'] = job_meta.get('error_type')
                job_node['data']['error_msg'] = job_meta.get('error_msg')
            else:
                print(f"Job failure metadata not found for node: {job_node['id']}")

        return {
            'node': {
                'name': job_node['name'],
                'result': job_result,
                'artifacts': {},
                'data': job_node['data'],
            },
            'child_nodes': self._get_results_hierarchy(results),
        }

    def get_log_parser(self):
        """Get a LogParser object from the callback data"""
        return LogParser(self._data['log'])

    def to_file(self, filename):
        """Write the callback data to a JSON file"""
        with open(filename, 'w', encoding='utf-8') as file:
            file.write(self._data)


class LAVA(Runtime):
    """Runtime implementation to run jobs in a LAVA lab

    LAVA is a framework for running tests on real hardware or QEMU.  This
    provides the basic features needed to generate a job defintion, submit it
    and wait for the job to complete.

    This currently only supports the REST API v0.2 and doesn't look for online
    devices or aliases.  It also doesn't take into account the callback
    parameters so results can't be sent to the API yet.  It requiers an API
    token to be provided to the constructor.  The user name is not used.
    """
    API_VERSION = 'v0.2'
    RestAPIServer = namedtuple('RestAPIServer', ['url', 'session'])

    def __init__(self, configs, **kwargs):
        super().__init__(configs, **kwargs)
        self._server = self._connect()

    def get_params(self, job, api_config=None):
        params = super().get_params(job, api_config)
        if params:
            params['notify'] = self.config.notify
        return params

    def generate(self, job, params):
        template = self._get_template(job.config)
        try:
            rendered = template.render(params)
        # jinja2.exceptions.UndefinedError
        except Exception as exc:  # pylint: disable=broad-except
            platform_params = params['platform_config'].params
            print(f"Error rendering job template: {exc}, {params}" +
                  f"{exc}, {params} {platform_params}")
            return None

        # yaml round-trip to process e.g. multi-line commands
        return yaml.dump(yaml.load(rendered, Loader=yaml.CLoader))

    def submit(self, job_path):
        with open(job_path, 'r', encoding='utf-8') as job_file:
            job = job_file.read()
            job_id = self._submit(job)
            return job_id

    def get_job_id(self, job_object):
        job_id = int(job_object)
        return job_id

    def wait(self, job_object):
        job_id = int(job_object)
        job_url = urljoin(self._server.url, '/'.join(['jobs', str(job_id)]))
        while True:
            resp = self._server.session.get(job_url)
            resp.raise_for_status()
            data = resp.json()
            if data['state'] == 'Finished':
                health = data['health']
                return 0 if health == 'Complete' else 1
            time.sleep(3)

    def _connect(self):
        rest_url = f'{self.config.url}/api/{self.API_VERSION}/'
        rest_api = self.RestAPIServer(rest_url, requests.Session())
        rest_api.session.params = {'format': 'json', 'limit': '256'}
        rest_api.session.headers = {
            'authorization': f'Token {self._token}',
            'content-type': 'application/json',
        }
        return rest_api

    def _submit(self, job):
        jobs_url = urljoin(self._server.url, 'jobs/')
        job_data = {
            'definition': job,
        }
        resp = self._server.session.post(
            jobs_url, json=job_data, allow_redirects=False
        )
        resp.raise_for_status()
        return resp.json()['job_ids'][0]


def get_runtime(runtime_config, **kwargs):
    """Get a LAVA runtime object"""
    return LAVA(runtime_config, **kwargs)
