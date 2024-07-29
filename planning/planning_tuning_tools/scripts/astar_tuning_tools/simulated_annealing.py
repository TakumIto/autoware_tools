import argparse
import math
import os
import pickle
import random

import autoware_freespace_planning_algorithms.astar_search as fp
from geometry_msgs.msg import Pose
from nav_msgs.msg import OccupancyGrid
import numpy as np
from pyquaternion import Quaternion
from tqdm import tqdm
import yaml

costmap = OccupancyGrid()


class TestData:
    def __init__(self, costmap, goal_pose):
        self.costmap = costmap
        self.goal_pose = goal_pose


class OptimizationVariable:
    def __init__(self, param_obj, config_dict):
        self.param_obj = param_obj

        # processing
        use_attrs = []
        mins = []
        maxs = []
        for key in config_dict.keys():
            param_config = config_dict[key]
            setattr(self.param_obj, key, param_config["initial"])
            if param_config["use_optimization"]:
                use_attrs.append(key)
                mins.append(param_config["min"])
                maxs.append(param_config["max"])

        self.use_attrs = use_attrs
        self.mins = np.array(mins)
        self.maxs = np.array(maxs)
        self.N = len(mins)

    def increment(self, diff, update):
        diff_normalized = self.normalize(diff)

        param_obj_tmp = self.param_obj
        for i, attr in enumerate(self.use_attrs):
            curr_value = getattr(param_obj_tmp, attr)
            updated_value = np.clip(curr_value + diff_normalized[i], self.mins[i], self.maxs[i])
            setattr(param_obj_tmp, attr, updated_value)
        if update:
            self.param_obj = param_obj_tmp

        return param_obj_tmp

    def normalize(self, diff):
        param_range = self.maxs - self.mins
        return diff * param_range / 20

    def set_param(self, param_obj):
        self.param_obj = param_obj

    def get_param(self):
        return self.param_obj


class SimulatedAnnealing:
    def __init__(self, config_path, val_data_set):
        with open(config_path) as file:
            all_config = yaml.safe_load(file)

        self.planner_var = OptimizationVariable(
            fp.PlannerCommonParam(), all_config["planner_param"]
        )
        self.astar_var = OptimizationVariable(fp.AstarParam(), all_config["astar_param"])
        self.vehicle_var = OptimizationVariable(fp.VehicleShape(), all_config["vehicle_shape"])

        self.best_param = None
        self.best_energy = None

        self.val_data_set = val_data_set

    def objective_function(self, params):
        planner_param = params[0]
        astar_param = params[1]
        vehicle_shape = params[2]

        astar = fp.AstarSearch(planner_param, vehicle_shape, astar_param)
        start_pose = Pose()

        total_result = 0
        total_length_rate = 0
        total_direction_change = 0

        for test_data in self.val_data_set:
            astar.setMap(test_data.costmap)
            goal_pose = test_data.goal_pose

            try:
                find = astar.makePlan(start_pose, goal_pose)
            except RuntimeError:
                find = False
            else:
                find = False

            waypoints = fp.PlannerWaypoints()
            if find:
                total_result += 1
                waypoints = astar.getWaypoints()
                L2_dist = math.hypot(
                    goal_pose.position.x - start_pose.position.x,
                    goal_pose.position.y - start_pose.position.y,
                )
                if L2_dist != 0:
                    total_length_rate += waypoints.compute_length() / L2_dist
                total_direction_change += self.count_forward_backward_change(waypoints)

        N = len(self.val_data_set)
        if total_result != 0:
            unsuccess_rate = 1 - total_result / N
            average_length_rate = total_length_rate / total_result
            average_forward_backward_change = total_direction_change / total_result
            return 10 * unsuccess_rate + average_length_rate + 10 * average_forward_backward_change
        else:
            return 1000

    # Define the cooling schedule function
    def cooling_schedule(self, t, initial_temperature):
        return initial_temperature / (1 + t)

    # Simulated Annealing algorithm
    def simulated_annealing(self, initial_temperature, iterations):
        current_params = [
            self.planner_var.get_param(),
            self.astar_var.get_param(),
            self.vehicle_var.get_param(),
        ]
        current_energy = self.objective_function(current_params)
        print("Initial objective value:", current_energy)

        self.best_param = current_params
        self.best_energy = current_energy

        for t in tqdm(range(iterations)):
            temperature = self.cooling_schedule(t, initial_temperature)

            # Small random change
            planner_diff = np.random.uniform(-1, 1, self.planner_var.N)
            astar_diff = np.random.uniform(-1, 1, self.astar_var.N)
            vehicle_diff = np.random.uniform(-1, 1, self.vehicle_var.N)
            neighbor_params = [
                self.planner_var.increment(planner_diff, update=False),
                self.astar_var.increment(astar_diff, update=False),
                self.vehicle_var.increment(vehicle_diff, update=False),
            ]
            neighbor_energy = self.objective_function(neighbor_params)

            if neighbor_energy < current_energy or random.random() < math.exp(
                (current_energy - neighbor_energy) / temperature
            ):
                current_params = [
                    self.planner_var.increment(planner_diff, update=True),
                    self.astar_var.increment(astar_diff, update=True),
                    self.vehicle_var.increment(vehicle_diff, update=True),
                ]
                current_energy = neighbor_energy

                if neighbor_energy < self.best_energy:
                    self.best_param = neighbor_params
                    self.best_energy = neighbor_energy

        return self.best_param, self.best_energy

    def get_result(self):
        return self.best_param, self.best_energy

    def count_forward_backward_change(self, waypoints):
        count = 0
        if len(waypoints.waypoints):
            pre_is_back = waypoints.waypoints[0].is_back
            for waypoint in waypoints.waypoints:
                is_back = waypoint.is_back
                if is_back != pre_is_back:
                    count += 1
                pre_is_back = is_back

        return count


def save_param_as_yaml(params, save_name):
    param_dict = {}
    param_dict["planner_param"] = vars(params[0])
    param_dict["astar_param"] = vars(params[1])
    param_dict["vehicle_shape"] = vars(params[2])
    with open(save_name, "w") as f:
        yaml.dump(param_dict, f)
        print("optimized parameters are saved!!")


# Example usage
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="place of save result")
    parser.add_argument(
        "--costmap",
        default="costmap_default",
        type=str,
        help="file name of costmap without extension",
    )
    parser.add_argument(
        "--save_name",
        default="optimal_param_default",
        type=str,
        help="file name without extension to save",
    )
    args = parser.parse_args()

    config_path = os.path.dirname(__file__) + "/config/optimization_config.yaml"

    with open(os.path.dirname(__file__) + "/costmap/" + args.costmap + ".txt", "rb") as f:
        costmap = pickle.load(f)

    costmap_height_half = costmap.info.resolution * costmap.info.height / 2
    costmap_width_half = costmap.info.resolution * costmap.info.width / 2

    val_data_set = []
    for i in range(20):
        goal_pose = Pose()
        x = np.random.uniform(-(costmap_height_half - 3), (costmap_height_half - 3))
        y = np.random.uniform(-(costmap_width_half - 3), (costmap_width_half - 3))
        yaw = np.random.uniform(-np.pi, np.pi)

        goal_pose.position.x = float(x)
        goal_pose.position.y = float(y)

        quaternion = Quaternion(axis=[0, 0, 1], angle=yaw)

        goal_pose.orientation.w = quaternion.w
        goal_pose.orientation.x = quaternion.x
        goal_pose.orientation.y = quaternion.y
        goal_pose.orientation.z = quaternion.z

        val_data_set.append(TestData(costmap, goal_pose))

    initial_temperature = 200.0
    iterations = 300

    simulated_annealing = SimulatedAnnealing(config_path, val_data_set)
    best_param, best_energy = simulated_annealing.simulated_annealing(
        initial_temperature, iterations
    )

    if not os.path.exists("opt_param"):
        os.makedirs("opt_param")

    # TODO: how to save optimal parameter
    file_name_yaml = os.path.dirname(__file__) + "/opt_param/" + args.save_name + ".yaml"
    save_param_as_yaml(best_param, file_name_yaml)
