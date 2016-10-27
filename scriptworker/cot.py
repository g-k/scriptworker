#!/usr/bin/env python
"""Chain of Trust artifact validation and creation.

Attributes:
    log (logging.Logger): the log object for this module.
"""
import asyncio
from frozendict import frozendict
import json
import logging
import os
import re
from urllib.parse import unquote, urljoin, urlparse
from scriptworker.client import validate_json_schema
from scriptworker.config import freeze_values
from scriptworker.constants import DEFAULT_CONFIG
from scriptworker.exceptions import CoTError, ScriptWorkerException, ScriptWorkerGPGException
from scriptworker.gpg import get_body, GPG, sign
from scriptworker.task import download_artifacts, get_decision_task_id, get_task_id
from scriptworker.utils import filepaths_in_dir, format_json, get_hash, raise_future_exceptions
from taskcluster.exceptions import TaskclusterFailure

log = logging.getLogger(__name__)


# TODO support the rest of the task types... balrog, apkpush, beetmover, hgpush, etc.
VALID_TASK_TYPES = (
    'build',
    'decision',
    'docker-image',
    'signing',
)


# TODO ChainOfTrust {{{1
class ChainOfTrust(object):
    """
    """
    # TODO docstrings
    def __init__(self, context, name, task_id=None):
        self.name = name
        self.task_id = task_id or get_task_id(context.claim_task)
        self.task = context.task
        self.decision_task_id = get_decision_task_id(self.task)
        self.context = context
        self.links = []
        # populate self.links
        loop = asyncio.get_event_loop()
        loop.run_until_complete(
            build_task_dependencies(self, self.task, self.name, self.task_id)
        )

    def dependent_task_ids(self):
        """
        """
        return [x.task_id for x in self.links]

    def is_try(self):
        """
        """
        result = False
        for link in self.links:
            if link.is_try:
                result = True
                break
        return result

    def get_link(self, task_id):
        """
        """
        links = [x for x in self.links if x.task_id == task_id]
        if len(links) != 1:
            raise CoTError("No single Link matches task_id {}!\n{}".format(task_id, self.dependent_task_ids()))
        return links[0]


# TODO LinkOfTrust {{{1
class LinkOfTrust(object):
    """Each LinkOfTrust represents a task in the Chain of Trust and its status.

    Attributes:
    """
    # TODO docstrings
    _task = None
    _cot = None
    decision_task_id = None
    worker_class = None
    task_type = None
    # status = None  # TODO automate status going to False or True?
    # messages = []
    # errors = []
    # tests_to_run = []
    # tests_completed = []

    def __init__(self, context, name, task_id):
        self.name = name
        self.task_type = guess_task_type(self.name)
        self.task_id = task_id
        self.cot_dir = os.path.join(
            context.config['artifact_dir'], 'cot', self.task_id
        )

    def _set(self, prop_name, value):
        prev = getattr(self, prop_name)
        if prev is not None:
            raise CoTError(
                "LinkOfTrust {}: re-setting {} to {} when it is already set to {}!".format(
                    str(self.name), prop_name, value, prev
                )
            )
        return setattr(self, prop_name, value)

    @property
    def task(self):
        """ TODO
        """
        return self._task

    @task.setter
    def task(self, task):
        freeze_values(task)
        self._set('_task', frozendict(task))
        self.decision_task_id = get_decision_task_id(self.task)
        self.worker_class = guess_worker_class(self.task, self.name)
        self.is_try = is_try(self)
        # TODO add tests to run

    @property
    def cot(self):
        """
        """
        return self._cot

    @cot.setter
    def cot(self, cot):
        freeze_values(cot)
        self._set('_cot', frozendict(cot))
        # TODO add tests to run


# cot generation {{{1
# get_cot_artifacts {{{2
def get_cot_artifacts(context):
    """Generate the artifact relative paths and shas for the chain of trust

    Args:
        context (scriptworker.context.Context): the scriptworker context.

    Returns:
        dict: a dictionary of {"path/to/artifact": {"hash_alg": "..."}, ...}
    """
    artifacts = {}
    filepaths = filepaths_in_dir(context.config['artifact_dir'])
    hash_alg = context.config['chain_of_trust_hash_algorithm']
    for filepath in sorted(filepaths):
        path = os.path.join(context.config['artifact_dir'], filepath)
        sha = get_hash(path, hash_alg=hash_alg)
        artifacts[filepath] = {hash_alg: sha}
    return artifacts


# get_cot_environment {{{2
def get_cot_environment(context):
    """Get environment information for the chain of trust artifact.

    Args:
        context (scriptworker.context.Context): the scriptworker context.

    Returns:
        dict: the environment info.
    """
    env = {}
    # TODO
    return env


# generate_cot_body {{{2
def generate_cot_body(context):
    """Generate the chain of trust dictionary.

    This is the unsigned and unformatted chain of trust artifact contents.

    Args:
        context (scriptworker.context.Context): the scriptworker context.

    Returns:
        dict: the unsignd and unformatted chain of trust artifact contents.

    Raises:
        ScriptWorkerException: on error.
    """
    try:
        cot = {
            'artifacts': get_cot_artifacts(context),
            'chainOfTrustVersion': 1,
            'runId': context.claim_task['runId'],
            'task': context.task,
            'taskId': context.claim_task['status']['taskId'],
            'workerGroup': context.claim_task['workerGroup'],
            'workerId': context.config['worker_id'],
            'workerType': context.config['worker_type'],
            'environment': get_cot_environment(context),
        }
    except (KeyError, ) as exc:
        raise ScriptWorkerException("Can't generate chain of trust! {}".format(str(exc)))

    return cot


# generate_cot {{{2
def generate_cot(context, path=None):
    """Format and sign the cot body, and write to disk

    Args:
        context (scriptworker.context.Context): the scriptworker context.
        path (str, optional): The path to write the chain of trust artifact to.
            If None, this is artifact_dir/public/chainOfTrust.json.asc.
            Defaults to None.

    Returns:
        str: the contents of the chain of trust artifact.

    Raises:
        ScriptWorkerException: on schema error.
    """
    body = generate_cot_body(context)
    try:
        with open(context.config['cot_schema_path'], "r") as fh:
            schema = json.load(fh)
    except (IOError, ValueError) as e:
        raise ScriptWorkerException(
            "Can't read schema file {}: {}".format(context.config['cot_schema_path'], str(e))
        )
    validate_json_schema(body, schema, name="chain of trust")
    body = format_json(body)
    path = path or os.path.join(context.config['artifact_dir'], "public", "chainOfTrust.json.asc")
    if context.config['sign_chain_of_trust']:
        body = sign(GPG(context), body)
    with open(path, "w") as fh:
        print(body, file=fh, end="")
    return body


# cot verification {{{1
# guess_worker_class {{{2
def guess_worker_class(task, name):
    """Given a task, determine which worker class it was run on.

    Currently there are no task markers for generic-worker and
    taskcluster-worker hasn't been rolled out.  Those need to be populated here
    once they're ready.

    * docker-worker: `task.payload.image` is not None
    * check for scopes beginning with the worker type name.

    Args:
        task (dict): the task definition to check.
        name (str): the name of the task, used for error message strings.

    Returns:
        str: the worker type.

    Raises:
        CoTError: on inability to determine the worker type
    """
    worker_type = {'worker_type': None}

    def _set_worker_type(wt):
        if worker_type['worker_type'] is not None and worker_type['worker_type'] != wt:
            raise CoTError("guess_worker_class: {} was {} and now looks like {}!\n{}".format(name, worker_type['worker_type'], wt, task))
        worker_type['worker_type'] = wt

    if task['payload'].get("image"):
        _set_worker_type("docker-worker")
    # TODO config for these scriptworker checks?
    if task['provisionerId'] in ("scriptworker-prov-v1", ):
        _set_worker_type("scriptworker")
    if task['workerType'] in ("signing-linux-v1", ):
        _set_worker_type("scriptworker")

    for scope in task['scopes']:
        if scope.startswith("docker-worker:"):
            _set_worker_type("docker-worker")

    if worker_type['worker_type'] is None:
        raise CoTError("guess_worker_class: can't find a type for {}!\n{}".format(name, task))
    return worker_type['worker_type']


def guess_task_type(name):
    """Guess the task type of the task.

    Args:
        name (str): the name of the task.

    Returns:
        str: the task_type.

    Raises:
        CoTError: on invalid task_type.
    """
    parts = name.split(':')
    task_type = parts[-1]
    if task_type.startswith('build'):
        task_type = 'build'
    if task_type not in VALID_TASK_TYPES:
        raise CoTError(
            "Invalid task type for {}!".format(name)
        )
    return task_type


# is_try {{{2
def _is_try_url(url):
    parsed = urlparse(url)
    path = unquote(parsed.path).lstrip('/')
    parts = path.split('/')
    if parts[0] == "try":
        return True
    return False


def is_try(link):
    """Determine if a task is a 'try' task (restricted privs).

    XXX do we want this, or just do this behavior for any non-allowlisted repo?

    This checks for the following things::

        * `task.payload.env.GECKO_HEAD_REPOSITORY` == "https://hg.mozilla.org/try/"
        * `task.payload.env.MH_BRANCH` == "try"
        * `task.metadata.source` == "https://hg.mozilla.org/try/..."
        * `task.schedulerId` in ("gecko-level-1", )

    Args:
        link (LinkOfTrust): the link to check.

    Returns:
        bool: True if it's try
    """
    result = False
    task = link.task
    if task['payload']['env'].get("GECKO_HEAD_REPOSITORY"):
        result = result or _is_try_url(task['payload']['env']['GECKO_HEAD_REPOSITORY'])
    if task['payload']['env'].get("MH_BRANCH"):
        result = result or task['payload']['env']['MH_BRANCH'] == 'try'
    if task['metadata'].get('source'):
        result = result or _is_try_url(task['metadata']['source'])
    result = result or task['schedulerId'] in ("gecko-level-1", )
    return result


# check_interactive_docker_worker {{{2
def check_interactive_docker_worker(task, name):
    """Given a task, make sure the task was not defined as interactive.

    * `task.payload.features.interactive` must be absent or False.
    * `task.payload.env.TASKCLUSTER_INTERACTIVE` must be absent or False.

    Args:
        task (dict): the task definition to check.
        name (str): the name of the task, used for error message strings.

    Returns:
        list: the list of error messages.  Success is an empty list.
    """
    messages = []
    try:
        if task['payload']['features'].get('interactive'):
            messages.append("{} is interactive: task.payload.features.interactive!".format(name))
        if task['payload']['env'].get('TASKCLUSTER_INTERACTIVE'):
            messages.append("{} is interactive: task.payload.env.TASKCLUSTER_INTERACTIVE!".format(name))
    except KeyError:
        messages.append("check_interactive_docker_worker: {} task definition is malformed!".format(name))
    return messages


# check_docker_image_sha {{{2
def check_docker_image_sha(context, cot, name):
    """Verify that pre-built docker shas are in allowlists.

    Decision and docker-image tasks use pre-built docker images from docker hub.
    Verify that these pre-built docker image shas are in the allowlists.

    Args:
        context (scriptworker.context.Context): the scriptworker context.
        cot (dict): the chain of trust json dict.
        name (str): the name of the task.  This must be in
            `context.config['docker_image_allowlists']`.

    Raises:
        CoTError: on failure.
        KeyError: on malformed config / cot
    """
    # XXX we will need some way to allow trusted developers to update these
    # allowlists
    if cot['environment']['imageHash'] not in context.config['docker_image_allowlists'][name]:
        raise CoTError("{} docker imageHash {} not in the allowlist!\n{}".format(name, cot['environment']['imageHash'], cot))


# find_task_dependencies {{{2
def find_task_dependencies(task, name, task_id):
    """Find the taskIds of the chain of trust dependencies of a given task.

    Args:
        task (dict): the task definition to inspect.
        name (str): the name of the task, for logging and naming children.
        task_id (str): the taskId of the task.

    Returns:
        dict: mapping dependent task `name` to dependent task `taskId`.
    """
    log.info("find_task_dependencies {}".format(name))
    decision_task_id = get_decision_task_id(task)
    decision_key = '{}:decision'.format(name)
    dep_dict = {}
    for key, val in task['extra'].get('chainOfTrust', {}).get('inputs', {}).items():
        dep_dict['{}:{}'.format(name, key)] = val
    # XXX start hack: remove once all signing tasks have task.extra.chainOfTrust.inputs
    if 'unsignedArtifacts' in task['payload']:
        build_ids = []
        for url in task['payload']['unsignedArtifacts']:
            parts = urlparse(url)
            path = unquote(parts.path)
            m = re.search(DEFAULT_CONFIG['valid_artifact_path_regexes'][0], path)
            path_info = m.groupdict()
            if path_info['taskId'] not in build_ids:
                build_ids.append(path_info['taskId'])
        for count, build_id in enumerate(build_ids):
            dep_dict['{}:build{}'.format(name, count)] = build_id
    # XXX end hack
    if decision_task_id != task_id:
        dep_dict[decision_key] = decision_task_id
    log.info(dep_dict)
    return dep_dict


# build_task_dependencies {{{2
async def build_task_dependencies(chain, task, name, my_task_id):
    """Recursively build the task dependencies of a task.

    Args:
        chain (ChainOfTrust): the chain of trust to add to.
        task (dict): the task definition to operate on.
        name (str): the name of the task to operate on.
        my_task_id (str): the taskId of the task to operate on.

    Raises:
        KeyError: on failure.
    """
    log.info("build_task_dependencies {}".format(name))
    if name.count(':') > 5:
        raise CoTError("Too deep recursion!\n{}".format(name))
    deps = find_task_dependencies(task, name, my_task_id)
    task_names = sorted(deps.keys())
    # make sure we deal with the decision task first, or we may populate
    # signing:build0:decision before signing:decision
    decision_key = "{}:decision".format(name)
    if decision_key in task_names:
        task_names = [decision_key] + sorted([x for x in task_names if x != decision_key])
    for task_name in task_names:
        task_id = deps[task_name]
        if task_id not in chain.dependent_task_ids():
            link = LinkOfTrust(chain.context, task_name, task_id)
            try:
                task_defn = await chain.context.queue.task(task_id)
                link.task = task_defn
                chain.links.append(link)
                await build_task_dependencies(chain, task_defn, task_name, task_id)
            except TaskclusterFailure as exc:
                raise CoTError(str(exc))


# get_artifact_url {{{2
def get_artifact_url(context, task_id, path):
    """Get a TaskCluster artifact url.

    Args:
        context (scriptworker.context.Context): the scriptworker context
        task_id (str): the task id of the task that published the artifact
        path (str): the relative path of the artifact

    Returns:
        str: the artifact url

    Raises:
        TaskClusterFailure: on failure.
    """
    url = urljoin(
        context.queue.options['baseUrl'],
        context.queue.makeRoute('getLatestArtifact', replDict={
            'taskId': task_id,
            'name': 'public/chainOfTrust.json.asc'
        })
    )
    return url


# download_cot {{{2
async def download_cot(chain):
    """Download the signed chain of trust artifacts.

    Args:
        chain (ChainOfTrust): the chain of trust to add to.

    Raises:
        DownloadError: on failure.
    """
    tasks = []
    for link in chain.links:
        # don't try to download the current task's chain of trust artifact,
        # which hasn't been created / uploaded yet
        if link.task_id == chain.task_id:
            continue
        task_id = link.task_id
        url = get_artifact_url(chain.context, task_id, 'public/chainOfTrust.json.asc')
        parent_dir = link.cot_dir
        tasks.append(
            download_artifacts(
                chain.context, [url], parent_dir=parent_dir,
                valid_artifact_task_ids=[task_id]
            )
        )
    # XXX catch DownloadError and raise CoTError?
    await raise_future_exceptions(tasks)


# download_cot_artifacts {{{2
async def download_cot_artifacts(chain, task_id, paths):
    """Download artifacts and verify their SHAs against the chain of trust.

    Args:
        chain (ChainOfTrust): the chain of trust object
        task_id (str): the task ID to download from
        paths (list): the list of artifacts to download

    Returns:
        list: the full paths of the artifacts

    Raises:
        CoTError: on failure.
    """
    full_paths = []
    urls = []
    link = chain.get_link(task_id)
    for path in paths:
        if path not in link.cot['artifacts']:
            raise CoTError("path {} not in {} chain of trust artifacts!".format(path, link.name))
        url = get_artifact_url(chain.context, task_id, path)
        urls.append(url)
    await download_artifacts(
        chain.context, urls, parent_dir=link.cot_dir, valid_artifact_task_ids=[task_id]
    )
    for path in paths:
        full_path = os.path.join(link.cot_dir, path)
        full_paths.append(full_path)
        for alg, expected_sha in link.cot['artifacts'][path].items():
            real_sha = get_hash(full_path, hash_alg=alg)
            if expected_sha != real_sha:
                raise CoTError("BAD HASH: Expected {} {}; got {}!".format(alg, expected_sha, real_sha))
    return full_paths


# verify_cot_signatures {{{2
def verify_cot_signatures(chain):
    """Verify the signatures of the chain of trust artifacts populated in `download_cot`.

    Populate each link.cot with the chain of trust json body.

    Args:
        chain (ChainOfTrust): the chain of trust to add to.

    Raises:
        CoTError: on failure.
    """
    for link in chain.links:
        if link.task_id == chain.task_id:
            continue
        path = os.path.join(link.cot_dir, 'public/chainOfTrust.json.asc')
        gpg = GPG(
            chain.context,
            gpg_home=os.path.join(
                chain.context.config['base_gpg_home_dir'], link.worker_class
            )
        )
        try:
            with open(path, "r") as fh:
                contents = fh.read()
        except OSError as exc:
            raise CoTError("Can't read {}: {}!".format(path, str(exc)))
        try:
            # XXX remove verify_sig pref and kwarg when pubkeys are in git repo
            body = get_body(
                gpg, contents,
                verify_sig=chain.context.config['verify_cot_signature']
            )
        except ScriptWorkerGPGException as exc:
            raise CoTError("GPG Error verifying chain of trust for {}: {}!".format(path, str(exc)))
        link.cot = body
