import time
from typing import Iterable, Optional
import logging

import numpy
from clone_client_env.comm_worker import CommWorker
from clone_client_env.ctrl_worker import CtrlWorker
import multiprocessing as mp

from clone_client_env.utils import client_connection

CLIENT_TIMEOUT = 0.0045  # 4.5ms
FULL_LOOSE_TIME = 0.5  # 500ms


class CloneEnvError(Exception):
    pass


class CloneEnv:
    def __init__(
        self,
        hostname="clonepiext",
        timeout: float = CLIENT_TIMEOUT,
        log_level: int = logging.ERROR,
    ):
        logging.basicConfig(level=log_level)
        self.no_actions = 0
        self._hostname = hostname
        self._timeout = timeout

        self._comm_pipe, self._ctrl_pipe = mp.Pipe()
        self._actions_queue = mp.Queue(maxsize=1)
        self._obs_lock = mp.Lock()

        self._current_obs = None
        self._comm_worker = None
        self._current_actions = None
        self._ctrl_worker = None

    @property
    def comm_worker(self) -> CommWorker:
        """Returns the communication worker"""
        if not self._comm_worker:
            raise CloneEnvError(
                "Communication worker is not initialized. Please connect to the robot first."
            )

        return self._comm_worker

    @property
    def ctrl_worker(self) -> CtrlWorker:
        """Returns the control worker"""
        if not self._ctrl_worker:
            raise CloneEnvError(
                "Control worker is not initialized. Please connect to the robot first."
            )

        return self._ctrl_worker

    def get_obs(self) -> Iterable[float]:
        """
        Returns current contraction reading for each muscle.

        Each reading is a positive value.
        Maximum value is determined by the pressure in the system.
        """
        if self.comm_worker.exception:
            err, trace = self.comm_worker.exception
            self.force_close()
            raise CloneEnvError(trace) from err

        with self._obs_lock:
            return numpy.array(self._current_obs)

    def step(self, actions: Iterable[float]) -> None:
        """
        Allows individual actuation of muscles

        Valid actuation values are [-1, 0, 1].
        -1: Loose muscle
        0: Do nothing
        1: Contract muscle

        Using values in-between does nothing, API is written for future planned compatibility.
        """
        self._actions_queue.put(actions)

        if self.comm_worker.exception:
            err, trace = self.comm_worker.exception
            self.force_close()
            raise CloneEnvError(trace) from err

    def _start_client(self) -> None:
        with client_connection(self._hostname) as (loop, client):
            self.no_actions = client.number_of_muscles
            loop.run_until_complete(client.start_pressuregen())
            loop.run_until_complete(client.wait_for_desired_pressure())

    def connect(self) -> None:
        """Connects to the robot"""
        self._start_client()
        self._current_obs = mp.Manager().list([0.0] * self.no_actions)
        self._current_actions = mp.Manager().list([0.0] * self.no_actions)

        self._comm_worker = CommWorker(
            self._hostname,
            self._current_obs,
            self._obs_lock,
            self._comm_pipe,
            self._timeout,
        )

        self._ctrl_worker = CtrlWorker(
            self._hostname,
            self._actions_queue,
            self._ctrl_pipe,
            self._timeout,
        )

        self.comm_worker.start()
        self.ctrl_worker.start()

    def force_close(self) -> None:
        """Force close connection to the robot"""
        self.comm_worker.terminate()
        self.ctrl_worker.terminate()

    def keep_step(self, actions: Iterable[float], period: float) -> None:
        """Keep sending the same values over `period` seconds"""
        start = time.time()
        while time.time() - start < period:
            self.step(actions)

    def loose_all(self, period: float = FULL_LOOSE_TIME) -> None:
        """Loose all muscles"""
        all_loose = numpy.zeros(self.no_actions) - 1
        self.keep_step(all_loose, period)

    def close(self) -> None:
        """Close connection and cleanly exit the robot"""
        self.reset()
        with client_connection(self._hostname) as (loop, client):
            loop.run_until_complete(client.stop_pressuregen())

        self.force_close()

    def reset(
        self, actions: Optional[Iterable[float]] = None, period: float = FULL_LOOSE_TIME
    ) -> None:
        """
        Resets everything (errors/ hardware errors/ warning) and brings back the hand to a neutral position,
        and resets the system back to normal. It blocks the execution on `period` seconds.

        By default reset means loose all muscles.

        """
        if actions is None:
            self.loose_all()
        else:
            self.keep_step(actions, period)

        self.get_obs()
