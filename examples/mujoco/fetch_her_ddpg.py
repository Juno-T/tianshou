#!/usr/bin/env python3

import argparse
import datetime
import os
import pprint
from typing import Any, Dict, Optional, Sequence, Tuple, Union

import gym
import numpy as np
import torch
from torch.utils.tensorboard import SummaryWriter

from tianshou.data import Collector, HERReplayBuffer, HERVectorReplayBuffer
from tianshou.data.batch import Batch
from tianshou.env import ShmemVectorEnv, TruncatedAsTerminated
from tianshou.exploration import GaussianNoise
from tianshou.policy import DDPGPolicy
from tianshou.trainer import offpolicy_trainer
from tianshou.utils import TensorboardLogger, WandbLogger
from tianshou.utils.net.common import Net
from tianshou.utils.net.continuous import Actor, Critic


def get_args():
    # python3 fetch_her_ddpg.py --task FetchReach-v3 --seed 0 --horizon 50
    parser = argparse.ArgumentParser()
    parser.add_argument("--task", type=str, default="FetchReach-v3")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--buffer-size", type=int, default=1000000)
    parser.add_argument("--hidden-sizes", type=int, nargs="*", default=[256, 256])
    parser.add_argument("--actor-lr", type=float, default=1e-3)
    parser.add_argument("--critic-lr", type=float, default=1e-3)
    parser.add_argument("--gamma", type=float, default=0.99)
    parser.add_argument("--tau", type=float, default=0.005)
    parser.add_argument("--exploration-noise", type=float, default=0.1)
    parser.add_argument("--start-timesteps", type=int, default=25000)
    parser.add_argument("--epoch", type=int, default=200)
    parser.add_argument("--step-per-epoch", type=int, default=5000)
    parser.add_argument("--step-per-collect", type=int, default=1)
    parser.add_argument("--update-per-step", type=int, default=1)
    parser.add_argument("--n-step", type=int, default=1)
    parser.add_argument("--batch-size", type=int, default=256)
    parser.add_argument("--horizon", type=int, default=50)
    parser.add_argument("--future-k", type=int, default=8)
    parser.add_argument("--training-num", type=int, default=1)
    parser.add_argument("--test-num", type=int, default=10)
    parser.add_argument("--logdir", type=str, default="log")
    parser.add_argument("--render", type=float, default=0.)
    parser.add_argument(
        "--device", type=str, default="cuda" if torch.cuda.is_available() else "cpu"
    )
    parser.add_argument("--resume-path", type=str, default=None)
    parser.add_argument("--resume-id", type=str, default=None)
    parser.add_argument(
        "--logger",
        type=str,
        default="tensorboard",
        choices=["tensorboard", "wandb"],
    )
    parser.add_argument("--wandb-project", type=str, default="HER-benchmark")
    parser.add_argument(
        "--watch",
        default=False,
        action="store_true",
        help="watch the play of pre-trained policy only",
    )
    return parser.parse_args()


def make_fetch_env(task, training_num, test_num):
    env = TruncatedAsTerminated(gym.make(task))
    train_envs = ShmemVectorEnv(
        [lambda: TruncatedAsTerminated(gym.make(task)) for _ in range(training_num)]
    )
    test_envs = ShmemVectorEnv(
        [lambda: TruncatedAsTerminated(gym.make(task)) for _ in range(test_num)]
    )
    return env, train_envs, test_envs


class DictStateNet(Net):

    def __init__(
        self, state_shape: Dict[str, Union[int, Sequence[int]]], keys: Sequence[str],
        **kwargs
    ) -> None:
        self.keys = keys
        self.original_shape = state_shape
        flat_state_shape = []
        for k in self.keys:
            flat_state_shape.append(int(np.prod(state_shape[k])))
        super().__init__(sum(flat_state_shape), **kwargs)

    def preprocess_obs(self, obs):
        if isinstance(obs, dict) or (isinstance(obs, Batch) and self.keys[0] in obs):
            if self.original_shape[self.keys[0]] == obs[self.keys[0]].shape:
                # No batch dim
                new_obs = torch.Tensor([obs[k] for k in self.keys]).flatten()
                # new_obs = torch.Tensor([obs[k] for k in self.keys]).reshape(1, -1)
            else:
                bsz = obs[self.keys[0]].shape[0]
                new_obs = torch.cat(
                    [torch.Tensor(obs[k].reshape(bsz, -1)) for k in self.keys], axis=1
                )
        else:
            new_obs = obs
        return new_obs

    def forward(
        self,
        obs: Union[Dict[str, Union[np.ndarray, torch.Tensor]], Union[np.ndarray,
                                                                     torch.Tensor]],
        state: Any = None,
        info: Dict[str, Any] = {},
    ) -> Tuple[torch.Tensor, Any]:
        return super().forward(self.preprocess_obs(obs), state, info)


class GoalStateCritic(Critic):

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    def forward(
        self,
        obs: Union[np.ndarray, torch.Tensor],
        act: Optional[Union[np.ndarray, torch.Tensor]] = None,
        info: Dict[str, Any] = {},
    ) -> torch.Tensor:
        return super().forward(self.preprocess.preprocess_obs(obs), act, info)


def test_ddpg(args=get_args()):
    env, train_envs, test_envs = make_fetch_env(
        args.task, args.training_num, args.test_num
    )
    args.state_shape = {
        'observation': env.observation_space['observation'].shape,
        'achieved_goal': env.observation_space['achieved_goal'].shape,
        'desired_goal': env.observation_space['desired_goal'].shape,
    }
    args.action_shape = env.action_space.shape or env.action_space.n
    args.max_action = env.action_space.high[0]
    args.exploration_noise = args.exploration_noise * args.max_action
    print("Observations shape:", args.state_shape)
    print("Actions shape:", args.action_shape)
    print("Action range:", np.min(env.action_space.low), np.max(env.action_space.high))
    # seed
    np.random.seed(args.seed)
    torch.manual_seed(args.seed)
    # model
    net_a = DictStateNet(
        args.state_shape,
        keys=['observation', 'achieved_goal', 'desired_goal'],
        hidden_sizes=args.hidden_sizes,
        device=args.device
    )
    actor = Actor(
        net_a, args.action_shape, max_action=args.max_action, device=args.device
    ).to(args.device)
    actor_optim = torch.optim.Adam(actor.parameters(), lr=args.actor_lr)
    net_c = DictStateNet(
        args.state_shape,
        keys=['observation', 'achieved_goal', 'desired_goal'],
        action_shape=args.action_shape,
        hidden_sizes=args.hidden_sizes,
        concat=True,
        device=args.device,
    )
    critic = GoalStateCritic(net_c, device=args.device).to(args.device)
    critic_optim = torch.optim.Adam(critic.parameters(), lr=args.critic_lr)
    policy = DDPGPolicy(
        actor,
        actor_optim,
        critic,
        critic_optim,
        tau=args.tau,
        gamma=args.gamma,
        exploration_noise=GaussianNoise(sigma=args.exploration_noise),
        estimation_step=args.n_step,
        action_space=env.action_space,
    )

    # load a previous policy
    if args.resume_path:
        policy.load_state_dict(torch.load(args.resume_path, map_location=args.device))
        print("Loaded agent from: ", args.resume_path)

    # collector
    def compute_reward_fn(ag: np.ndarray, g: np.ndarray):
        return env.compute_reward(ag, g, {})

    if args.training_num > 1:
        buffer = HERVectorReplayBuffer(
            args.buffer_size,
            len(train_envs),
            compute_reward_fn=compute_reward_fn,
            horizon=args.horizon,
            future_k=args.future_k,
        )
    else:
        buffer = HERReplayBuffer(
            args.buffer_size,
            compute_reward_fn=compute_reward_fn,
            horizon=args.horizon,
            future_k=args.future_k,
        )
    train_collector = Collector(policy, train_envs, buffer, exploration_noise=True)
    test_collector = Collector(policy, test_envs)
    train_collector.collect(n_step=args.start_timesteps, random=True)

    # log
    now = datetime.datetime.now().strftime("%y%m%d-%H%M%S")
    args.algo_name = "ddpg"
    log_name = os.path.join(args.task, args.algo_name, str(args.seed), now)
    log_path = os.path.join(args.logdir, log_name)

    # logger
    if args.logger == "wandb":
        logger = WandbLogger(
            save_interval=1,
            name=log_name.replace(os.path.sep, "__"),
            run_id=args.resume_id,
            config=args,
            project=args.wandb_project,
        )
    writer = SummaryWriter(log_path)
    writer.add_text("args", str(args))
    if args.logger == "tensorboard":
        logger = TensorboardLogger(writer)
    else:  # wandb
        logger.load(writer)

    def save_best_fn(policy):
        torch.save(policy.state_dict(), os.path.join(log_path, "policy.pth"))

    if not args.watch:
        # trainer
        result = offpolicy_trainer(
            policy,
            train_collector,
            test_collector,
            args.epoch,
            args.step_per_epoch,
            args.step_per_collect,
            args.test_num,
            args.batch_size,
            save_best_fn=save_best_fn,
            logger=logger,
            update_per_step=args.update_per_step,
            test_in_train=False,
        )
        pprint.pprint(result)

    # Let's watch its performance!
    policy.eval()
    test_envs.seed(args.seed)
    test_collector.reset()
    result = test_collector.collect(n_episode=args.test_num, render=args.render)
    print(f'Final reward: {result["rews"].mean()}, length: {result["lens"].mean()}')


if __name__ == "__main__":
    test_ddpg()