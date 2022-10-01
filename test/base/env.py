import random
import time
from copy import deepcopy

import gym
import networkx as nx
import numpy as np
from gym.spaces import Box, Dict, Discrete, MultiDiscrete, Tuple

from tianshou.data import Batch


class MyTestEnv(gym.Env):
    """This is a "going right" task. The task is to go right ``size`` steps.
    """

    def __init__(
        self,
        size,
        sleep=0,
        dict_state=False,
        recurse_state=False,
        ma_rew=0,
        multidiscrete_action=False,
        random_sleep=False,
        array_state=False
    ):
        assert dict_state + recurse_state + array_state <= 1, \
            "dict_state / recurse_state / array_state can be only one true"
        self.size = size
        self.sleep = sleep
        self.random_sleep = random_sleep
        self.dict_state = dict_state
        self.recurse_state = recurse_state
        self.array_state = array_state
        self.ma_rew = ma_rew
        self._md_action = multidiscrete_action
        # how many steps this env has stepped
        self.steps = 0
        if dict_state:
            self.observation_space = Dict(
                {
                    "index": Box(shape=(1, ), low=0, high=size - 1),
                    "rand": Box(shape=(1, ), low=0, high=1, dtype=np.float64)
                }
            )
        elif recurse_state:
            self.observation_space = Dict(
                {
                    "index":
                    Box(shape=(1, ), low=0, high=size - 1),
                    "dict":
                    Dict(
                        {
                            "tuple":
                            Tuple(
                                (
                                    Discrete(2),
                                    Box(shape=(2, ), low=0, high=1, dtype=np.float64)
                                )
                            ),
                            "rand":
                            Box(shape=(1, 2), low=0, high=1, dtype=np.float64)
                        }
                    )
                }
            )
        elif array_state:
            self.observation_space = Box(shape=(4, 84, 84), low=0, high=255)
        else:
            self.observation_space = Box(shape=(1, ), low=0, high=size - 1)
        if multidiscrete_action:
            self.action_space = MultiDiscrete([2, 2])
        else:
            self.action_space = Discrete(2)
        self.terminated = False
        self.index = 0

    def reset(self, state=0, seed=None):
        super().reset(seed=seed)
        self.terminated = False
        self.do_sleep()
        self.index = state
        return self._get_state(), {'key': 1, 'env': self}

    def _get_reward(self):
        """Generate a non-scalar reward if ma_rew is True."""
        end_flag = int(self.terminated)
        if self.ma_rew > 0:
            return [end_flag] * self.ma_rew
        return end_flag

    def _get_state(self):
        """Generate state(observation) of MyTestEnv"""
        if self.dict_state:
            return {
                'index': np.array([self.index], dtype=np.float32),
                'rand': self.np_random.random(1)
            }
        elif self.recurse_state:
            return {
                'index': np.array([self.index], dtype=np.float32),
                'dict': {
                    "tuple": (np.array([1], dtype=int), self.np_random.random(2)),
                    "rand": self.np_random.random((1, 2))
                }
            }
        elif self.array_state:
            img = np.zeros([4, 84, 84], int)
            img[3, np.arange(84), np.arange(84)] = self.index
            img[2, np.arange(84)] = self.index
            img[1, :, np.arange(84)] = self.index
            img[0] = self.index
            return img
        else:
            return np.array([self.index], dtype=np.float32)

    def do_sleep(self):
        if self.sleep > 0:
            sleep_time = random.random() if self.random_sleep else 1
            sleep_time *= self.sleep
            time.sleep(sleep_time)

    def step(self, action):
        self.steps += 1
        if self._md_action:
            action = action[0]
        if self.terminated:
            raise ValueError('step after done !!!')
        self.do_sleep()
        if self.index == self.size:
            self.terminated = True
            return self._get_state(), self._get_reward(), self.terminated, False, {}
        if action == 0:
            self.index = max(self.index - 1, 0)
            return self._get_state(), self._get_reward(), self.terminated, False, \
                {'key': 1, 'env': self} if self.dict_state else {}
        elif action == 1:
            self.index += 1
            self.terminated = self.index == self.size
            return self._get_state(), self._get_reward(), \
                self.terminated, False, {'key': 1, 'env': self}


class NXEnv(gym.Env):

    def __init__(self, size, obs_type, feat_dim=32):
        self.size = size
        self.feat_dim = feat_dim
        self.graph = nx.Graph()
        self.graph.add_nodes_from(list(range(size)))
        assert obs_type in ["array", "object"]
        self.obs_type = obs_type

    def _encode_obs(self):
        if self.obs_type == "array":
            return np.stack([v["data"] for v in self.graph._node.values()])
        return deepcopy(self.graph)

    def reset(self):
        graph_state = np.random.rand(self.size, self.feat_dim)
        for i in range(self.size):
            self.graph.nodes[i]["data"] = graph_state[i]
        return self._encode_obs(), {}

    def step(self, action):
        next_graph_state = np.random.rand(self.size, self.feat_dim)
        for i in range(self.size):
            self.graph.nodes[i]["data"] = next_graph_state[i]
        return self._encode_obs(), 1.0, 0, 0, {}


class MyGoalEnv(MyTestEnv):

    def __init__(self, *args, **kwargs):
        assert kwargs.get("dict_state", 0) + kwargs.get("recurse_state", 0) == 0, \
            "dict_state / recurse_state not supported"
        super().__init__(*args, **kwargs)
        obs, _ = super().reset(state=0)
        obs, _, _, _, _ = super().step(1)
        self._goal = obs * self.size
        super_obsv = self.observation_space
        self.observation_space = Box(
            shape=(super_obsv.shape[0] * 3, *super_obsv.shape[1:]),
            low=0,
            high=self.size
        )

    def reset(self, *args, **kwargs):
        obs, info = super().reset(*args, **kwargs)
        new_obs = np.concatenate([obs, obs, self._goal], axis=0)
        return new_obs, info

    def step(self, *args, **kwargs):
        obs_next, rew, terminated, truncated, info = super().step(*args, **kwargs)
        new_obs_next = np.concatenate([obs_next, obs_next, self._goal], axis=0)
        return new_obs_next, rew, terminated, truncated, info

    def deconstruct_obs_fn(self, obs: np.ndarray) -> Batch:
        """Deconstruct observation into observation, acheived_goal, goal
        obs: shape(bsz, *observation_shape)
        return: Batch(
            o=shape(bsz, *o.shape),
            ag=shape(bsz, *ag.shape),
            g=shape(bsz, *g.shape)
        )
        """
        state_sz = 1
        if self.array_state:
            state_sz = 4
        return Batch(
            o=obs[:, :state_sz],
            ag=obs[:, state_sz:2 * state_sz],
            g=obs[:, 2 * state_sz:],
        )

    def flatten_obs_fn(self, obs: Batch) -> np.ndarray:
        """Reconstruct observation
        obs: Batch(
            o=shape(bsz, *o.shape),
            ag=shape(bsz, *ag.shape),
            g=shape(bsz, *g.shape)
        )
        return: shape(bsz, *observation_shape)
        """
        return np.concatenate((obs.o, obs.ag, obs.g), axis=1)

    def compute_reward_fn(self, obs: Batch) -> np.ndarray:
        """Compute rewards from deconstructed obs
        obs: Batch(
            o=shape(bsz, *o.shape),
            ag=shape(bsz, *ag.shape),
            g=shape(bsz, *g.shape)
        )
        return: shape(bsz,)
        """
        ag_sum = obs.ag.reshape(obs.ag.shape[0], -1).sum(axis=1)
        g_sum = obs.g.reshape(obs.g.shape[0], -1).sum(axis=1)
        return (ag_sum == g_sum)
