# -*- coding: utf-8 -*-
#
# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.

import hashlib
import imp
import importlib
import os
import sys
import textwrap
import zipfile
from collections import namedtuple
from datetime import datetime, timedelta

from croniter import croniter, CroniterBadCronError, CroniterBadDateError, CroniterNotAlphaError
from sqlalchemy import or_

from airflow import configuration, settings
from airflow.dag.base_dag import BaseDagBag
from airflow.exceptions import AirflowDagCycleException
from airflow.executors import get_default_executor
from airflow.models.serialized_dag import SerializedDagModel
from airflow.stats import Stats
from airflow.utils import timezone
from airflow.utils.dag_processing import list_py_file_paths, correct_maybe_zipped
from airflow.utils.db import provide_session
from airflow.utils.helpers import pprinttable
from airflow.utils.log.logging_mixin import LoggingMixin
from airflow.utils.state import State
from airflow.utils.timeout import timeout


class DagBag(BaseDagBag, LoggingMixin):
    """
    A dagbag is a collection of dags, parsed out of a folder tree and has high
    level configuration settings, like what database to use as a backend and
    what executor to use to fire off tasks. This makes it easier to run
    distinct environments for say production and development, tests, or for
    different teams or security profiles. What would have been system level
    settings are now dagbag level so that one system can run multiple,
    independent settings sets.

    :param dag_folder: the folder to scan to find DAGs
    :type dag_folder: unicode
    :param executor: the executor to use when executing task instances
        in this DagBag
    :param include_examples: whether to include the examples that ship
        with airflow or not
    :type include_examples: bool
    :param has_logged: an instance boolean that gets flipped from False to True after a
        file has been skipped. This is to prevent overloading the user with logging
        messages about skipped files. Therefore only once per DagBag is a file logged
        being skipped.
    """

    # static class variables to detetct dag cycle
    CYCLE_NEW = 0
    CYCLE_IN_PROGRESS = 1
    CYCLE_DONE = 2

    def __init__(
            self,
            dag_folder=None,
            executor=None,
            include_examples=configuration.conf.getboolean('core', 'LOAD_EXAMPLES'),
            safe_mode=configuration.conf.getboolean('core', 'DAG_DISCOVERY_SAFE_MODE'),
            dagcached_enabled=False):

        # do not use default arg in signature, to fix import cycle on plugin load
        if executor is None:
            executor = get_default_executor()
        dag_folder = dag_folder or settings.DAGS_FOLDER
        self.dag_folder = dag_folder
        self.dags = {}
        # the file's last modified timestamp when we last read it
        self.file_last_changed = {}
        self.executor = executor
        self.import_errors = {}
        self.has_logged = False
        self.dagcached_enabled = dagcached_enabled

        self.collect_dags(
            dag_folder=dag_folder,
            include_examples=include_examples,
            safe_mode=safe_mode)

    def size(self):
        """
        :return: the amount of dags contained in this dagbag
        """
        return len(self.dags)

    @property
    def dag_ids(self):
        return self.dags.keys()

    def get_dag(self, dag_id, from_file_only=False):
        """
        Gets the DAG out of the dictionary, and refreshes it if expired

        :param from_file_only: returns a DAG loaded from file.
        :type from_file_only: bool
        """
        from airflow.models.dag import DagModel  # Avoid circular import

        # Only read DAGs from DB if this dagbag is dagcached_enabled.
        # from_file_only is an exception, currently it is for renderring templates
        # in UI only. Because functions are gone in serialized DAGs, DAGs must be
        # imported from files.
        # FIXME: this exception should be removed in future, then webserver can be
        # decoupled from DAG files.
        if self.dagcached_enabled and not from_file_only:
            return self.dags.get(dag_id)

        # If asking for a known subdag, we want to refresh the parent
        dag = None
        root_dag_id = dag_id
        if dag_id in self.dags:
            dag = self.dags[dag_id]
            if dag.is_subdag:
                root_dag_id = dag.parent_dag.dag_id

        # Needs to load from file for a dagcached_enabled dagbag.
        enforce_from_file = False
        if self.dagcached_enabled and dag is not None:
            from airflow.dag.serialization.serialized_dag import SerializedDAG
            enforce_from_file = isinstance(dag, SerializedDAG)

        # If the dag corresponding to root_dag_id is absent or expired
        orm_dag = DagModel.get_current(root_dag_id)
        if (orm_dag and (
                root_dag_id not in self.dags or
                (
                    orm_dag.last_expired and
                    dag.last_loaded < orm_dag.last_expired
                )
        )) or enforce_from_file:
            # Reprocess source file
            found_dags = self.process_file(
                filepath=correct_maybe_zipped(orm_dag.fileloc), only_if_updated=False)

            # If the source file no longer exports `dag_id`, delete it from self.dags
            if found_dags and dag_id in [found_dag.dag_id for found_dag in found_dags]:
                return self.dags[dag_id]
            elif dag_id in self.dags:
                del self.dags[dag_id]
        return self.dags.get(dag_id)

    def process_file(self, filepath, only_if_updated=True, safe_mode=True):
        """
        Given a path to a python module or zip file, this method imports
        the module and look for dag objects within it.
        """
        from airflow.models.dag import DAG  # Avoid circular import

        found_dags = []

        # if the source file no longer exists in the DB or in the filesystem,
        # return an empty list
        # todo: raise exception?
        if filepath is None or not os.path.isfile(filepath):
            return found_dags

        try:
            # This failed before in what may have been a git sync
            # race condition
            file_last_changed_on_disk = datetime.fromtimestamp(os.path.getmtime(filepath))
            if only_if_updated \
                    and filepath in self.file_last_changed \
                    and file_last_changed_on_disk == self.file_last_changed[filepath]:
                return found_dags

        except Exception as e:
            self.log.exception(e)
            return found_dags

        mods = []
        is_zipfile = zipfile.is_zipfile(filepath)
        if not is_zipfile:
            if safe_mode:
                with open(filepath, 'rb') as file:
                    content = file.read()
                    if not all([s in content for s in (b'DAG', b'airflow')]):
                        self.file_last_changed[filepath] = file_last_changed_on_disk
                        # Don't want to spam user with skip messages
                        if not self.has_logged:
                            self.has_logged = True
                            self.log.info(
                                "File %s assumed to contain no DAGs. Skipping.",
                                filepath)
                        return found_dags

            self.log.debug("Importing %s", filepath)
            org_mod_name, _ = os.path.splitext(os.path.split(filepath)[-1])
            mod_name = ('unusual_prefix_' +
                        hashlib.sha1(filepath.encode('utf-8')).hexdigest() +
                        '_' + org_mod_name)

            if mod_name in sys.modules:
                del sys.modules[mod_name]

            with timeout(configuration.conf.getint('core', "DAGBAG_IMPORT_TIMEOUT")):
                try:
                    m = imp.load_source(mod_name, filepath)
                    mods.append(m)
                except Exception as e:
                    self.log.exception("Failed to import: %s", filepath)
                    self.import_errors[filepath] = str(e)
                    self.file_last_changed[filepath] = file_last_changed_on_disk

        else:
            zip_file = zipfile.ZipFile(filepath)
            for mod in zip_file.infolist():
                head, _ = os.path.split(mod.filename)
                mod_name, ext = os.path.splitext(mod.filename)
                if not head and (ext == '.py' or ext == '.pyc'):
                    if mod_name == '__init__':
                        self.log.warning("Found __init__.%s at root of %s", ext, filepath)
                    if safe_mode:
                        with zip_file.open(mod.filename) as zf:
                            self.log.debug("Reading %s from %s", mod.filename, filepath)
                            content = zf.read()
                            if not all([s in content for s in (b'DAG', b'airflow')]):
                                self.file_last_changed[filepath] = (
                                    file_last_changed_on_disk)
                                # todo: create ignore list
                                # Don't want to spam user with skip messages
                                if not self.has_logged:
                                    self.has_logged = True
                                    self.log.info(
                                        "File %s assumed to contain no DAGs. Skipping.",
                                        filepath)

                    if mod_name in sys.modules:
                        del sys.modules[mod_name]

                    try:
                        sys.path.insert(0, filepath)
                        m = importlib.import_module(mod_name)
                        mods.append(m)
                    except Exception as e:
                        self.log.exception("Failed to import: %s", filepath)
                        self.import_errors[filepath] = str(e)
                        self.file_last_changed[filepath] = file_last_changed_on_disk

        for m in mods:
            for dag in list(m.__dict__.values()):
                if isinstance(dag, DAG):
                    if not dag.full_filepath:
                        dag.full_filepath = filepath
                        if dag.fileloc != filepath and not is_zipfile:
                            dag.fileloc = filepath
                    try:
                        dag.is_subdag = False
                        self.bag_dag(dag, parent_dag=dag, root_dag=dag)
                        if isinstance(dag._schedule_interval, str):
                            croniter(dag._schedule_interval)
                        found_dags.append(dag)
                        found_dags += dag.subdags
                    except (CroniterBadCronError,
                            CroniterBadDateError,
                            CroniterNotAlphaError) as cron_e:
                        self.log.exception("Failed to bag_dag: %s", dag.full_filepath)
                        self.import_errors[dag.full_filepath] = \
                            "Invalid Cron expression: " + str(cron_e)
                        self.file_last_changed[dag.full_filepath] = \
                            file_last_changed_on_disk
                    except AirflowDagCycleException as cycle_exception:
                        self.log.exception("Failed to bag_dag: %s", dag.full_filepath)
                        self.import_errors[dag.full_filepath] = str(cycle_exception)
                        self.file_last_changed[dag.full_filepath] = \
                            file_last_changed_on_disk

        self.file_last_changed[filepath] = file_last_changed_on_disk
        return found_dags

    @provide_session
    def kill_zombies(self, session=None):
        """
        Fail zombie tasks, which are tasks that haven't
        had a heartbeat for too long, in the current DagBag.

        :param session: DB session.
        :type session: sqlalchemy.orm.session.Session
        """
        # Avoid circular import
        from airflow.models.taskinstance import TaskInstance as TI
        from airflow.jobs import LocalTaskJob as LJ

        # How many seconds do we wait for tasks to heartbeat before mark them as zombies.
        zombie_threshold_secs = (
            configuration.getint('scheduler', 'scheduler_zombie_task_threshold'))
        limit_dttm = timezone.utcnow() - timedelta(
            seconds=zombie_threshold_secs)
        self.log.debug("Failing jobs without heartbeat after %s", limit_dttm)

        tis = (
            session.query(TI)
            .join(LJ, TI.job_id == LJ.id)
            .filter(TI.state == State.RUNNING)
            .filter(TI.dag_id.in_(self.dags))
            .filter(
                or_(
                    LJ.state != State.RUNNING,
                    LJ.latest_heartbeat < limit_dttm,
                )
            ).all()
        )
        for ti in tis:
            self.log.info("Detected zombie job with dag_id %s, task_id %s, and execution date %s",
                          ti.dag_id, ti.task_id, ti.execution_date.isoformat())
            ti.test_mode = configuration.getboolean('core', 'unit_test_mode')
            ti.task = self.dags[ti.dag_id].get_task(ti.task_id)
            ti.handle_failure("{} detected as zombie".format(ti),
                              ti.test_mode, ti.get_template_context())
            self.log.info('Marked zombie job %s as %s', ti, ti.state)
            Stats.incr('zombies_killed')
        session.commit()

    def bag_dag(self, dag, parent_dag, root_dag):
        """
        Adds the DAG into the bag, recurses into sub dags.
        Throws AirflowDagCycleException if a cycle is detected in this dag or its subdags
        """

        dag.test_cycle()  # throws if a task cycle is found

        dag.resolve_template_files()
        dag.last_loaded = timezone.utcnow()

        for task in dag.tasks:
            settings.policy(task)

        subdags = dag.subdags

        try:
            for subdag in subdags:
                subdag.full_filepath = dag.full_filepath
                subdag.parent_dag = dag
                subdag.is_subdag = True
                self.bag_dag(subdag, parent_dag=dag, root_dag=root_dag)

            self.dags[dag.dag_id] = dag
            self.log.debug('Loaded DAG %s', dag)
        except AirflowDagCycleException as cycle_exception:
            # There was an error in bagging the dag. Remove it from the list of dags
            self.log.exception('Exception bagging dag: %s', dag.dag_id)
            # Only necessary at the root level since DAG.subdags automatically
            # performs DFS to search through all subdags
            if dag == root_dag:
                for subdag in subdags:
                    if subdag.dag_id in self.dags:
                        del self.dags[subdag.dag_id]
            raise cycle_exception

    def collect_dags(
            self,
            dag_folder=None,
            only_if_updated=True,
            include_examples=configuration.conf.getboolean('core', 'LOAD_EXAMPLES'),
            safe_mode=configuration.conf.getboolean('core', 'DAG_DISCOVERY_SAFE_MODE'),
            dagcached_enabled=False):
        """
        Given a file path or a folder, this method looks for python modules,
        imports them and adds them to the dagbag collection.

        Note that if a ``.airflowignore`` file is found while processing
        the directory, it will behave much like a ``.gitignore``,
        ignoring files that match any of the regex patterns specified
        in the file.

        **Note**: The patterns in .airflowignore are treated as
        un-anchored regexes, not shell-like glob patterns.
        """
        if self.dagcached_enabled:
            self.collect_dags_from_db()
            return

        self.log.info("Filling up the DagBag from %s", dag_folder)
        start_dttm = timezone.utcnow()
        dag_folder = dag_folder or self.dag_folder
        # Used to store stats around DagBag processing
        stats = []
        FileLoadStat = namedtuple(
            'FileLoadStat', "file duration dag_num task_num dags")

        dag_folder = correct_maybe_zipped(dag_folder)

        dags_by_name = {}

        for filepath in list_py_file_paths(dag_folder, safe_mode=safe_mode,
                                           include_examples=include_examples):
            try:
                ts = timezone.utcnow()
                found_dags = self.process_file(
                    filepath, only_if_updated=only_if_updated,
                    safe_mode=safe_mode)
                dag_ids = [dag.dag_id for dag in found_dags]
                dag_id_names = str(dag_ids)

                td = timezone.utcnow() - ts
                td = td.total_seconds() + (
                    float(td.microseconds) / 1000000)
                dags_by_name[dag_id_names] = dag_ids
                stats.append(FileLoadStat(
                    filepath.replace(dag_folder, ''),
                    td,
                    len(found_dags),
                    sum([len(dag.tasks) for dag in found_dags]),
                    dag_id_names,
                ))
            except Exception as e:
                self.log.exception(e)
        Stats.gauge(
            'collect_dags', (timezone.utcnow() - start_dttm).total_seconds(), 1)
        Stats.gauge(
            'dagbag_size', len(self.dags), 1)
        Stats.gauge(
            'dagbag_import_errors', len(self.import_errors), 1)
        self.dagbag_stats = sorted(
            stats, key=lambda x: x.duration, reverse=True)
        for file_stat in self.dagbag_stats:
            dag_ids = dags_by_name[file_stat.dags]
            if file_stat.dag_num >= 1:
                # if we found multiple dags per file, the stat is 'dag_id1 _ dag_id2'
                dag_names = '_'.join(dag_ids)
                Stats.timing('dag.loading-duration.{}'.
                             format(dag_names),
                             file_stat.duration)

    def collect_dags_from_db(self):
        """Collects DAGs from database."""
        start_dttm = timezone.utcnow()
        # DAG post-pcocessing steps such as self.bag_dag and croniter are not needed as
        # they are done by scheduler before serialization.
        # The dagbag contains all rows in serialized_dag table. Deleted DAGs are deleted
        # from the table by the scheduler job.
        self.log.info("Filling up the DagBag from database")
        self.dags = SerializedDagModel.read_all_dags()
        Stats.gauge(
            'collect_dags', (timezone.utcnow() - start_dttm).total_seconds(), 1)
        Stats.gauge('dagbag_size', len(self.dags), 1)

    def dagbag_report(self):
        """Prints a report around DagBag loading stats"""
        report = textwrap.dedent("""\n
        -------------------------------------------------------------------
        DagBag loading stats for {dag_folder}
        -------------------------------------------------------------------
        Number of DAGs: {dag_num}
        Total task number: {task_num}
        DagBag parsing time: {duration}
        {table}
        """)
        stats = self.dagbag_stats
        return report.format(
            dag_folder=self.dag_folder,
            duration=sum([o.duration for o in stats]),
            dag_num=sum([o.dag_num for o in stats]),
            task_num=sum([o.task_num for o in stats]),
            table=pprinttable(stats),
        )
