from os import cpu_count
import torch.multiprocessing as mp
import numpy as np

from src.rubiks.utils.logger import NullLogger, Logger
from src.rubiks.utils.ticktock import TickTock

from src.rubiks.cube.cube import Cube
from src.rubiks.solving.agents import Agent

# Multiprocessing is silly, so all functions have to be top-level
# This also means all info has to be parsed in with a single argument
# https://stackoverflow.com/questions/3288595/multiprocessing-how-to-use-pool-map-on-a-function-defined-in-a-class
def _eval_game(cfg: (Agent, int, int)):
	agent, max_time, depth = cfg
	unfinished = -1
	turns_to_complete = unfinished  # -1 for unfinished
	state, _, _ = Cube.scramble(depth)
	if agent.is_jit():  # Evaluation of tree agents
		solution_found = agent.generate_action_queue(state)
		if solution_found:
			return len(agent.searcher.action_queue)
		else:
			return unfinished
	else:  # Evaluations of non-tree agents
		tt = TickTock()
		tt.tick()
		while tt.tock() < max_time:
			turns_to_complete += 1
			action = agent.act(state)
			state = Cube.rotate(state, *action)
			if Cube.is_solved(state):
				return turns_to_complete
		return unfinished


class Evaluator:
	def __init__(self,
				 n_games			= 420,  # Nice
				 max_time			= 600,
				 scrambling_depths	= range(1, 10),
				 logger: Logger		= NullLogger()
		):

		self.n_games = n_games
		self.max_time = max_time

		self.tt = TickTock()
		self.log = logger
		self.scrambling_depths = np.array(scrambling_depths)

		self.log("\n".join([
			"Creating evaluator",
			f"Games per scrambling depth: {self.n_games}",
			f"Scrambling depths: {scrambling_depths}",
		]))

	def eval(self, agent: Agent, max_threads=1):
		"""
		Evaluates an agent
		"""
		max_time = self.max_time
		if hasattr(agent, 'time_limit'):
			assert agent.time_limit == max_time
			max_time *= 2 #To be sure that the agent class is allowed to control the running time.

		self.log(f"Evaluating {self.n_games*len(self.scrambling_depths)} games with agent {agent} with max time {self.max_time}. Expected time <~ {self.max_time*self.n_games*len(self.scrambling_depths)/60} minutes ")

		# Builds configurations for runs
		cfgs = []
		# TODO: Pass a logger along to log progress
		for i, d in enumerate(self.scrambling_depths):
			for _ in range(self.n_games):
				cfgs.append((agent, max_time, d))

		self.tt.section(f"Evaluation of {agent}")
		if agent.with_mt:
			with mp.Pool(min(max_threads, cpu_count())) as p:
				res = p.map(_eval_game, cfgs)
		else:
			res = []
			for i, cfg in enumerate(cfgs):
				self.log(f"Performing evaluation {i+1} / {len(cfgs)}. Depth: {cfg[2]}")
				res.append(_eval_game(cfg))
		self.tt.end_section(f"Evaluation of {agent}")
		res = np.reshape(res, (len(self.scrambling_depths), self.n_games))

		self.log(f"Evaluation results")
		for i, d in enumerate(self.scrambling_depths):
			self.log(f"Scrambling depth {d}", with_timestamp=False)
			self.log(f"\tShare completed: {np.count_nonzero(res[i]!=-1)*100/len(res[i]):.2f} %", with_timestamp=False)
			if res.any():
				self.log(f"\tMean turns to complete (ex. unfinished): {res[i][res[i]!=-1].mean():.2f}", with_timestamp=False)
				self.log(f"\tMedian turns to complete (ex. unfinished): {np.median(res[i][res[i]!=-1]):.2f}", with_timestamp=False)
		self.log.verbose(f"Evaluation runtime\n{self.tt}")

		return res

	def eval_hists(self, eval_results: dict):
		"""
		{agent: results from self.eval}
		"""
		raise NotImplementedError


if __name__ == "__main__":
	from src.rubiks.solving.agents import RandomAgent, PolicyCube, DeepCube
	e = Evaluator(n_games = 100,
				  max_time = 1,
				  logger = Logger("local_evaluation/mcts.log", "Testing MCTS", True),
				  scrambling_depths = range(1, 8)
	)
	# agent = DeepCube.from_saved("local_train", 1)
	agent = PolicyCube.from_saved("local_train")
	results = e.eval(agent, 1)
	# results = e.eval(PolicyCube.from_saved("local_train"))
	# TODO: Boxplot with completion turns for each scrambling depth


