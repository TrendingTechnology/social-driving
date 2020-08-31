import math
import random
from collections import deque
from itertools import product
from glob import glob

import numpy as np
import torch

from gym.spaces import Box, Discrete, Tuple
from sdriving.environments.intersection import (
    MultiAgentRoadIntersectionBicycleKinematicsEnvironment,
)
from sdriving.tsim import (
    SplineModel,
    BicycleKinematicsModel,
    angle_normalize,
    BatchedVehicle,
    intervehicle_collision_check,
)
from sdriving.nuscenes import NuscenesWorld


class MultiAgentNuscenesIntersectionDrivingEnvironment(
    MultiAgentRoadIntersectionBicycleKinematicsEnvironment
):
    def __init__(
        self,
        map_path: str,
        npoints: int = 360,
        horizon: int = 300,
        timesteps: int = 10,
        history_len: int = 5,
        time_green: int = 100,
        nagents: int = 4,
        device: torch.device = torch.device("cpu"),
        lidar_noise: float = 0.0,
    ):
        self.npoints = npoints
        self.history_len = history_len
        self.time_green = time_green
        self.device = device
        self.worlds = []

        for path in glob(map_path):
            self.worlds.append(NuscenesWorld(path))

        world = random.choice(self.worlds)
        super(
            MultiAgentRoadIntersectionBicycleKinematicsEnvironment, self
        ).__init__(world, nagents, horizon, timesteps, device)

        self.queue1 = None
        self.queue2 = None
        self.lidar_noise = lidar_noise

        bool_buffer = torch.ones(self.nagents * 4, self.nagents * 4)
        for i in range(0, self.nagents * 4, 4):
            bool_buffer[i : (i + 4), i : (i + 4)] -= 1
        self.bool_buffer = bool_buffer.bool()

        self.buffered_ones = torch.ones(self.nagents, 1, device=self.device)

    def get_action_space(self):
        self.max_accln = 1.5
        self.normalization_factor = torch.as_tensor([self.max_accln])
        return Box(
            low=np.array([-self.max_accln]),
            high=np.array([self.max_accln]),
        )

    def _get_distance_from_goal(self):
        a_id = self.get_agent_ids_list()[0]
        vehicle = self.agents[a_id]
        dist = vehicle.distance_from_destination()
        path_distance = self.dynamics.distance_proxy - self.dynamics.distances
        return torch.where(dist == 0, self.buffered_ones, 1 / path_distance)

    def get_state(self):
        a_ids = self.get_agent_ids_list()
        a_id = a_ids[0]
        ts = self.world.get_all_traffic_signal().unsqueeze(1)
        vehicle = self.agents[a_id]
        head = torch.cat([self.agents[v].optimal_heading() for v in a_ids])

        inv_dist = self._get_distance_from_goal()

        speed = vehicle.speed

        obs = torch.cat([ts, speed / self.dynamics.v_lim, head, inv_dist], -1)
        lidar = 1 / self.world.get_lidar_data_all_vehicles(self.npoints)

        if self.lidar_noise > 0:
            lidar *= torch.rand_like(lidar) > self.lidar_noise

        if self.history_len > 1:
            while len(self.queue1) <= self.history_len - 1:
                self.queue1.append(obs)
                self.queue2.append(lidar)
            self.queue1.append(obs)
            self.queue2.append(lidar)

            return (
                torch.cat(list(self.queue1), dim=-1),
                torch.cat(list(self.queue2), dim=-1),
            )
        else:
            return obs, lidar

    def _get_distance_rwd_from_goal(self):
        return self.dynamics.distance_proxy - self.dynamics.distances

    def get_reward(self, new_collisions: torch.Tensor, action: torch.Tensor):
        a_ids = self.get_agent_ids_list()
        a_id = a_ids[0]
        vehicle = self.agents[a_id]

        # Distance from destination
        # A bit hacky, but doesn't matter if the agent only goes forward
        distances = self._get_distance_rwd_from_goal()

        # Agent Speeds
        speeds = vehicle.speed

        # Action Regularization
        if self.cached_actions is not None:
            smoothness = (
                (action - self.cached_actions).pow(2)
                / (2 * self.normalization_factor).pow(2)
            ).sum(-1, keepdim=True)
        else:
            smoothness = 0.0

        # Goal Reach Bonus
        reached_goal = distances <= 5.0
        not_completed = ~self.completion_vector
        goal_reach_bonus = (not_completed * reached_goal).float()
        self.completion_vector = self.completion_vector + reached_goal
        for v in a_ids:
            self.agents[v].destination = self.agents[
                v
            ].position * self.completion_vector + self.agents[
                v
            ].destination * (
                ~self.completion_vector
            )

        distances *= not_completed / self.original_distances

        # Collision
        new_collisions = ~self.collision_vector * new_collisions
        penalty = (
            new_collisions.float()
            + new_collisions
            * distances
            * (self.horizon - self.nsteps - 1)
            / self.horizon
        )

        self.collision_vector += new_collisions

        return (
            -(distances + smoothness) * (~self.collision_vector) / self.horizon
            - (speeds / 8.0).abs() * self.completion_vector / self.horizon
            - penalty
            + goal_reach_bonus
        )

    def add_vehicles_to_world(self):
        vehicle = None
        dims = torch.as_tensor([[4.48, 2.2]])
        self.cps = []
        idxs = []
        for _ in range(self.nagents):
            successful_placement = False
            while not successful_placement:
                (
                    idx,
                    (spos, epos, orient, dorient, cps),
                ) = self.world.sample_new_vehicle_position()
                if vehicle is None:
                    vehicle = BatchedVehicle(
                        position=spos,
                        orientation=orient,
                        destination=epos,
                        dest_orientation=dorient,
                        dimensions=dims,
                        initial_speed=torch.zeros(1, 1),
                        name="agent",
                    )
                    break
                else:
                    successful_placement = vehicle.add_vehicle(
                        position=spos,
                        orientation=orient,
                        destination=epos,
                        dest_orientation=dorient,
                        dimensions=dims,
                        initial_speed=torch.zeros(1, 1),
                    )
            idxs.append(idx)
            self.cps.append(cps)
        self.cps = torch.cat(self.cps)

        vehicle.add_bool_buffer(self.bool_buffer)

        self.world.add_vehicle(vehicle, idxs)
        self.store_dynamics(vehicle)
        self.agents[vehicle.name] = vehicle

        self.original_distances = self._get_original_distances()

    def _get_original_distances(self):
        return self.dynamics.distance_proxy

    def store_dynamics(self, vehicle):
        self.dynamics = SplineModel(
            self.cps, v_lim=torch.ones(self.nagents) * 8.0
        )

    def reset(self):
        # Keep the environment fixed for now
        self.world = random.choice(self.worlds)
        self.world.reset()
        self.add_vehicles_to_world()

        self.queue1 = deque(maxlen=self.history_len)
        self.queue2 = deque(maxlen=self.history_len)

        return super(
            MultiAgentRoadIntersectionBicycleKinematicsEnvironment, self
        ).reset()


class MultiAgentNuscenesIntersectionDrivingDiscreteEnvironment(
    MultiAgentNuscenesIntersectionDrivingEnvironment
):
    def configure_action_space(self):
        self.max_accln = 1.5
        self.action_list = torch.arange(
            -self.max_accln, self.max_accln + 0.05, step=0.25
        ).unsqueeze(1)

    def get_action_space(self):
        self.normalization_factor = torch.as_tensor([self.max_accln])
        return Discrete(self.action_list.size(0))

    def discrete_to_continuous_actions(self, action: torch.Tensor):
        return self.action_list[action]


class MultiAgentNuscenesIntersectionBicycleKinematicsEnvironment(
    MultiAgentNuscenesIntersectionDrivingEnvironment
):
    def store_dynamics(self, vehicle):
        self.dynamics = BicycleKinematicsModel(
            dim=vehicle.dimensions[:, 0], v_lim=torch.ones(self.nagents) * 8.0
        )

    def get_action_space(self):
        self.max_accln = 1.5
        self.max_steering = 0.1
        self.normalization_factor = torch.as_tensor(
            [self.max_steering, self.max_accln]
        )
        return Box(
            low=np.array([-self.max_steering, -self.max_accln]),
            high=np.array([self.max_steering, self.max_accln]),
        )

    def _get_distance_from_goal(self):
        a_id = self.get_agent_ids_list()[0]
        vehicle = self.agents[a_id]
        dist = vehicle.distance_from_destination().clamp(min=1.0)
        rval = 1 / dist
        return rval

    def _get_distance_rwd_from_goal(self):
        # This might give us incorrect results. We should use the nearest neighbor of
        # the predefined splines for this
        a_id = self.get_agent_ids_list()[0]
        vehicle = self.agents[a_id]
        dist = vehicle.distance_from_destination()
        return dist

    def _get_original_distances(self):
        a_id = self.get_agent_ids_list()[0]
        vehicle = self.agents[a_id]
        dist = vehicle.distance_from_destination()
        return dist


class MultiAgentNuscenesIntersectionBicycleKinematicsDiscreteEnvironment(
    MultiAgentNuscenesIntersectionBicycleKinematicsEnvironment
):
    def configure_action_space(self):
        self.max_accln = 1.5
        self.max_steering = 0.1
        actions = list(
            product(
                torch.arange(
                    -self.max_steering, self.max_steering + 0.01, 0.05
                ),
                torch.arange(-self.max_accln, self.max_accln + 0.05, 0.5),
            )
        )
        self.action_list = torch.as_tensor(actions)

    def get_action_space(self):
        self.normalization_factor = torch.as_tensor(
            [self.max_steering, self.max_accln]
        )
        return Discrete(self.action_list.size(0))

    def discrete_to_continuous_actions(self, action: torch.Tensor):
        return self.action_list[action]
