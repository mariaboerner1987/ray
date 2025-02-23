import os
import time

from typing import Dict, Tuple

from ray_release.logger import logger
from ray_release.util import ANYSCALE_HOST
from ray_release.cluster_manager.cluster_manager import ClusterManager
from ray_release.exception import CommandTimeout
from ray.job_submission import JobSubmissionClient, JobStatus


class JobManager:
    def __init__(self, cluster_manager: ClusterManager):
        self.job_id_pool: Dict[int, str] = dict()
        self.start_time: Dict[int, float] = dict()
        self.counter = 0
        self.cluster_manager = cluster_manager
        self.job_client = None
        self.last_job_id = None

    def _run_job(self, cmd_to_run, env_vars) -> int:
        self.counter += 1
        command_id = self.counter
        env = os.environ.copy()
        env["RAY_ADDRESS"] = self.cluster_manager.get_cluster_address(full=False)
        env.setdefault("ANYSCALE_HOST", ANYSCALE_HOST)

        full_cmd = " ".join(f"{k}={v}" for k, v in env_vars.items()) + " " + cmd_to_run
        logger.info(f"Executing {cmd_to_run} with {env_vars} via ray job submit")

        job_id = self.job_client.submit_job(
            # Entrypoint shell command to execute
            entrypoint=full_cmd,
        )
        self.last_job_id = job_id
        self.job_id_pool[command_id] = job_id
        self.start_time[command_id] = time.time()
        return command_id

    def _wait_job(self, command_id: int, timeout: int):
        start_time = time.monotonic()
        timeout_at = start_time + timeout
        next_status = start_time + 30

        while True:
            now = time.monotonic()
            if now >= timeout_at:
                raise CommandTimeout(
                    f"Cluster command timed out after {timeout} seconds."
                )

            if now >= next_status:
                logger.info(
                    f"... command still running ..."
                    f"({int(now - start_time)} seconds) ..."
                )
                next_status += 30
            status = self.job_client.get_job_status(self.job_id_pool[command_id])
            if status in {JobStatus.SUCCEEDED, JobStatus.STOPPED, JobStatus.FAILED}:
                break
            time.sleep(1)
        status = self.job_client.get_job_status(self.job_id_pool[command_id])
        # TODO(sang): Propagate JobInfo.error_type
        if status == JobStatus.SUCCEEDED:
            retcode = 0
        else:
            retcode = -1
        duration = time.time() - self.start_time[command_id]
        return retcode, duration

    def run_and_wait(self, cmd_to_run, env_vars, timeout: int = 120) -> Tuple[int, int]:
        if not self.job_client:
            self.job_client = JobSubmissionClient(
                self.cluster_manager.get_cluster_address(full=False)
            )
        cid = self._run_job(cmd_to_run, env_vars)
        return self._wait_job(cid, timeout)

    def get_last_logs(self):
        # return None
        if not self.job_client:
            self.job_client = JobSubmissionClient(
                self.cluster_manager.get_cluster_address(full=False)
            )
        return self.job_client.get_job_logs(self.last_job_id)
